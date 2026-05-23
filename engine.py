"""
Webtoon Translation Engine (Chrome + Gemini Web UI)
─────────────────────────────────────────
- Smart-slices tall webtoon images
- Uses Playwright to drive Google Chrome to gemini.google.com
- Uploads images and asks for translation
- Outputs a single clean TXT file
"""

import os
import time
import re
import tempfile
import random
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright
from pathlib import Path
from PIL import Image
import win32clipboard
import io
import numpy as np
import cv2
try:
    from arabic_reshaper import reshape
    from bidi.algorithm import get_display
except ImportError:
    reshape = None
    get_display = None
from PIL import ImageFont, ImageDraw, features
HAS_RAQM = features.check("raqm")

def load_image_rgb(path_or_img):
    if isinstance(path_or_img, (str, Path)):
        img = Image.open(path_or_img)
    else:
        img = path_or_img
        
    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
        alpha = img.convert('RGBA').split()[-1]
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=alpha)
        return bg
    return img.convert('RGB')

def get_arabic_text(text):
    from arabic_reshaper import reshape
    from bidi.algorithm import get_display
    return get_display(reshape(text))

def refine_bubble_center(clean_img_pil, cx_global, cy_global, gemini_box=None):
    """
    Uses Flood Fill on a LOCAL CROP around Gemini's predicted center to find the 
    exact bubble boundaries. Returns global coordinates.
    
    Args:
        clean_img_pil: The full clean PIL image.
        cx_global, cy_global: Center of Gemini's predicted text box (global coords).
        gemini_box: Optional (x1, y1, x2, y2) of Gemini's raw prediction for sanity checking.
    """
    import cv2
    import numpy as np
    
    img = np.array(clean_img_pil)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    full_h, full_w = gray.shape
    cx, cy = int(cx_global), int(cy_global)
    
    if cx < 0 or cy < 0 or cx >= full_w or cy >= full_h:
        return None
    
    # --- STEP 1: Crop a local search region around Gemini's center ---
    # This prevents the flood fill from leaking across the entire image.
    # Use Gemini's box to determine the search radius, or default to a reasonable area.
    if gemini_box:
        gx1, gy1, gx2, gy2 = gemini_box
        g_w, g_h = gx2 - gx1, gy2 - gy1
        # Search area = 3x Gemini's box, clamped to image bounds
        margin_w = max(g_w * 1.5, 150)
        margin_h = max(g_h * 1.5, 150)
    else:
        margin_w, margin_h = 250, 250
    
    crop_x1 = max(0, int(cx - margin_w))
    crop_y1 = max(0, int(cy - margin_h))
    crop_x2 = min(full_w, int(cx + margin_w))
    crop_y2 = min(full_h, int(cy + margin_h))
    
    local_gray = gray[crop_y1:crop_y2, crop_x1:crop_x2].copy()
    local_h, local_w = local_gray.shape
    
    # Local coordinates for the seed point
    local_cx = cx - crop_x1
    local_cy = cy - crop_y1
    
    if local_cx < 0 or local_cy < 0 or local_cx >= local_w or local_cy >= local_h:
        return None
    
    # --- STEP 2: Adaptive flood fill tolerance ---
    # Use the actual pixel value at the seed to pick a good tolerance.
    # Bright bubbles (white) need tight tolerance; dark bubbles need tight too.
    seed_val = int(local_gray[local_cy, local_cx])
    if seed_val > 200:  # White/near-white bubble
        lo_diff, up_diff = 12, 12
    elif seed_val < 80:  # Dark bubble
        lo_diff, up_diff = 15, 15
    else:  # Colored/gray bubble
        lo_diff, up_diff = 20, 20
    
    mask = np.zeros((local_h + 2, local_w + 2), np.uint8)
    cv2.floodFill(local_gray, mask, (local_cx, local_cy), 255, 
                  loDiff=lo_diff, upDiff=up_diff, flags=cv2.FLOODFILL_MASK_ONLY)
    mask = mask[1:-1, 1:-1]
    
    # Find bounding box of the flooded area
    x, y, bw, bh = cv2.boundingRect(mask)
    
    # --- STEP 3: Sanity checks ---
    # If flood fill filled most of the LOCAL crop, it escaped
    if bw > local_w * 0.90 or bh > local_h * 0.90:
        return None
    
    # If flood fill area is too small (less than 20x20), it's noise
    if bw < 20 or bh < 20:
        return None
    
    # If we have Gemini's box, check that the flood-filled area isn't absurdly 
    # larger than what Gemini predicted (max 4x area = 2x each dimension)
    if gemini_box:
        gx1, gy1, gx2, gy2 = gemini_box
        g_w, g_h = max(20, gx2 - gx1), max(20, gy2 - gy1)
        if bw > g_w * 3.0 or bh > g_h * 3.0:
            return None  # Flood fill leaked way beyond the bubble
    
    # --- STEP 4: Find visual center using Distance Transform ---
    dist_map = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    _, max_val, _, max_loc = cv2.minMaxLoc(dist_map)
    v_cx_local, v_cy_local = max_loc
    
    # Geometric center
    geom_cx_local = x + bw / 2.0
    geom_cy_local = y + bh / 2.0
    
    # Blend: 50/50 visual + geometric for best results
    final_cx_local = (v_cx_local * 0.5) + (geom_cx_local * 0.5)
    final_cy_local = (v_cy_local * 0.5) + (geom_cy_local * 0.5)
    
    # --- STEP 5: Convert back to global coordinates ---
    global_x1 = crop_x1 + x
    global_y1 = crop_y1 + y
    global_x2 = crop_x1 + x + bw
    global_y2 = crop_y1 + y + bh
    global_cx = crop_x1 + final_cx_local
    global_cy = crop_y1 + final_cy_local
    
    # Determine if it's a dark bubble
    roi = local_gray[y:y+bh, x:x+bw]
    is_dark = np.mean(roi) < 120
    
    return global_x1, global_y1, global_x2, global_y2, global_cx, global_cy, is_dark

def _resize_for_gemini(img, max_w=2000, max_h=5000):
    """Downscale image so Gemini receives it quickly. Caps width at 2000px and height at 5000px."""
    w, h = img.size
    ratio = 1.0
    if w > max_w:
        ratio = min(ratio, max_w / w)
    if h > max_h:
        ratio = min(ratio, max_h / h)
    if ratio >= 1.0:
        return img
    new_w, new_h = int(w * ratio), int(h * ratio)
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)

def send_image_to_clipboard(image_path):
    img = load_image_rgb(image_path)
    img = _resize_for_gemini(img)          # ← downscale before clipboard
    output = io.BytesIO()
    img.save(output, 'BMP')
    data = output.getvalue()[14:]
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
    win32clipboard.CloseClipboard()

# apply_watermark removed in favor of manual logo placement

# detect_text_blocks removed (using Gemini now)



def wrap_balanced(text, font, max_width):
    """Professional balanced word wrapping to fit ovals better."""
    words = text.split()
    if not words: return []
    
    # Simple greedy wrap first to get baseline
    lines = []
    curr = ""
    for w in words:
        test = (curr + " " + w).strip()
        tw = font.getbbox(get_display(test))[2]
        if tw <= max_width or not curr:
            curr = test
        else:
            lines.append(curr)
            curr = w
    if curr: lines.append(curr)
    
    if len(lines) <= 1: return lines
    
    # Try to balance: redistribute words to make line widths more equal
    total_len = sum(len(w) for w in words)
    target_len = total_len / len(lines)
    
    # This is a simplified balanced wrap
    balanced_lines = []
    curr_line = []
    curr_c = 0
    for w in words:
        if curr_c + len(w) > target_len * 1.2 and curr_line:
            balanced_lines.append(" ".join(curr_line))
            curr_line = [w]
            curr_c = len(w)
        else:
            curr_line.append(w)
            curr_c += len(w)
    if curr_line: balanced_lines.append(" ".join(curr_line))
    
    # Final safety check for width
    final_lines = []
    for l in balanced_lines:
        tw = font.getbbox(get_display(l))[2]
        if tw > max_width: # If balancing failed width, split it
            sub_curr = ""
            for w in l.split():
                if font.getbbox(get_display(sub_curr + " " + w))[2] <= max_width:
                    sub_curr = (sub_curr + " " + w).strip()
                else:
                    final_lines.append(sub_curr)
                    sub_curr = w
            if sub_curr: final_lines.append(sub_curr)
        else:
            final_lines.append(l)
            
    return final_lines

def typeset_arabic(img, text, font_path, block):
    """Professional Arabic Typesetting that exactly matches the HTML live preview."""
    x, y, w, h = block['x'], block['y'], block['w'], block['h']
    cx, cy = x + w/2, y + h/2
    
    font_size = int(block.get('font_size', 28))
    rotation = float(block.get('rotation', 0))
    color = block.get('color', '#000000')
    outline_color = block.get('outline_color', '#ffffff')
    outline_width = int(block.get('outline_width', 4))
    line_height_mult = float(block.get('line_height', 1.2))
    is_gradient = block.get('is_gradient', False)
    gradient_colors = block.get('gradient_colors', ['#8b5cf6', '#3b82f6'])
    gradient_scale = float(block.get('gradient_scale', 100))
    align = block.get('align', 'center')
    
    custom_font = block.get('font', 'My fonts/Hayah.otf')
    # Resolve relative font path to absolute based on script directory
    if custom_font and not os.path.isabs(custom_font):
        font_path = os.path.join(os.path.dirname(__file__), custom_font)
    else:
        font_path = custom_font
        
    try: font = ImageFont.truetype(font_path, font_size)
    except: font = ImageFont.load_default()
    
    # Text wrapping matching HTML pre-wrap
    lines = []
    for user_line in text.split('\n'):
        if not user_line.strip():
            lines.append("")
            continue
            
        words = user_line.split()
        curr_line = ""
        for word in words:
            test_line = (curr_line + " " + word).strip()
            if HAS_RAQM:
                tw = font.getlength(test_line, direction='rtl')
            elif get_display and reshape:
                tw = font.getlength(get_display(reshape(test_line)))
            else:
                tw = font.getlength(test_line)
                
            if tw <= w * 0.95 or not curr_line: # 5% padding
                curr_line = test_line
            else:
                lines.append(curr_line)
                curr_line = word
        if curr_line:
            lines.append(curr_line)
            
    if not lines: return img
    
    line_h = int(font_size * line_height_mult)
    total_h = len(lines) * line_h
    
    # Calculate dimensions for text layer
    def get_line_w(l):
        if not l: return 0
        if HAS_RAQM: return font.getlength(l, direction='rtl')
        if get_display and reshape: return font.getlength(get_display(reshape(l)))
        return font.getlength(l)

    max_line_w = max(get_line_w(l) for l in lines)
    text_img_w = int(max(w, max_line_w) + outline_width * 4)
    text_img_h = int(max(h, total_h) + outline_width * 4)
    
    text_layer = Image.new('RGBA', (text_img_w, text_img_h), (0,0,0,0))
    text_draw = ImageDraw.Draw(text_layer)
    
    y_start = (text_img_h - total_h) / 2
    
    for line in lines:
        if not line:
            y_start += line_h
            continue
            
        if HAS_RAQM:
            display_text = line
            render_dir = 'rtl'
        elif get_display and reshape:
            display_text = get_display(reshape(line))
            render_dir = None
        else:
            display_text = line
            render_dir = None
        
        if align == 'right':
            x_draw = text_img_w - outline_width*2 - 4
            anch = "rm"
        elif align == 'left':
            x_draw = outline_width*2 + 4
            anch = "lm"
        else:
            x_draw = text_img_w / 2
            anch = "mm"
            
        y_pos = y_start + (line_h / 2)
        
        # Draw outline using stroke (modern Pillow)
        text_draw.text((x_draw, y_pos), display_text, font=font, fill=color, anchor=anch, 
                       direction=render_dir, stroke_width=outline_width, stroke_fill=outline_color)
        y_start += line_h
        
    if is_gradient and len(gradient_colors) == 2:
        mask_layer = Image.new('L', (text_img_w, text_img_h), 0)
        mask_draw = ImageDraw.Draw(mask_layer)
        y_st = (text_img_h - total_h) / 2
        for line in lines:
            if not line:
                y_st += line_h
                continue
            
            if HAS_RAQM:
                display_text = line
                render_dir = 'rtl'
            else:
                from arabic_reshaper import reshape
                from bidi.algorithm import get_display
                display_text = get_display(reshape(line))
                render_dir = None
            
            if align == 'right':
                x_dw = text_img_w - outline_width*2 - 4
                anch = "rm"
            elif align == 'left':
                x_dw = outline_width*2 + 4
                anch = "lm"
            else:
                x_dw = text_img_w / 2
                anch = "mm"
                
            y_p = y_st + (line_h / 2)
            mask_draw.text((x_dw, y_p), display_text, font=font, fill=255, anchor=anch, direction=render_dir)
            y_st += line_h
            
        grad_roi = Image.new('RGBA', (text_img_w, text_img_h))
        c1 = tuple(int(gradient_colors[0].lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
        c2 = tuple(int(gradient_colors[1].lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
        
        scale_ratio = gradient_scale / 100.0 if gradient_scale > 0 else 1.0
        effective_h = text_img_h * scale_ratio
        
        for i in range(text_img_h):
            ratio = min(1.0, i / effective_h) if effective_h > 0 else 1.0
            r = int(c1[0] + (c2[0] - c1[0]) * ratio)
            g = int(c1[1] + (c2[1] - c1[1]) * ratio)
            b = int(c1[2] + (c2[2] - c1[2]) * ratio)
            ImageDraw.Draw(grad_roi).line([(0, i), (text_img_w, i)], fill=(r, g, b, 255))
            
        text_layer.paste(grad_roi, (0,0), mask=mask_layer)
        
        # Redraw outline over gradient since gradient replaced everything
        text_draw_grad = ImageDraw.Draw(text_layer)
        y_st2 = (text_img_h - total_h) / 2
        for line in lines:
            if not line:
                y_st2 += line_h
                continue
            
            if HAS_RAQM:
                display_text = line
                render_dir = 'rtl'
            else:
                from arabic_reshaper import reshape
                from bidi.algorithm import get_display
                display_text = get_display(reshape(line))
                render_dir = None
                
            x_dw = text_img_w / 2
            y_p = y_st2 + (line_h / 2)
            # stroke handled by original draw if mask is just body
            y_st2 += line_h

    # Re-apply outline correctly for gradient:
    # Actually, if we just paste the gradient over the text mask, the gradient replaces BOTH the text and the stroke if we masked it with the stroke!
    # Wait, the mask above did NOT use `stroke_width`! So `mask_layer` is strictly the text body.
    # Therefore, pasting the gradient only affected the text body! The outline drawn previously remains intact underneath/around it!
    # PERFECT.

    if rotation != 0:
        text_layer = text_layer.rotate(-rotation, resample=Image.Resampling.BICUBIC, expand=True)
        
    final_w, final_h = text_layer.size
    paste_x = int(cx - final_w / 2)
    paste_y = int(cy - final_h / 2)
    
    img.paste(text_layer, (paste_x, paste_y), mask=text_layer)
    
    return img

def draw_logo(img, block, logo_path):
    """Draws a logo image with specific transform (resize, rotate, opacity)."""
    if not logo_path or not os.path.exists(logo_path):
        return img
        
    try:
        logo = Image.open(logo_path).convert("RGBA")
        x, y, w, h = block['x'], block['y'], block['w'], block['h']
        cx, cy = x + w/2, y + h/2
        
        rotation = float(block.get('rotation', 0))
        opacity = float(block.get('opacity', 1.0))
        
        # Resize logo to fit the block size
        logo = logo.resize((int(w), int(h)), Image.Resampling.LANCZOS)
        
        # Apply opacity
        if opacity < 1.0:
            r, g, b, a = logo.split()
            a = a.point(lambda p: p * opacity)
            logo = Image.merge('RGBA', (r, g, b, a))
            
        # Rotate
        if rotation != 0:
            logo = logo.rotate(-rotation, resample=Image.Resampling.BICUBIC, expand=True)
            
        # Paste centered
        final_w, final_h = logo.size
        paste_x = int(cx - final_w / 2)
        paste_y = int(cy - final_h / 2)
        
        img.paste(logo, (paste_x, paste_y), mask=logo)
    except Exception as e:
        print(f"Error drawing logo: {e}")
        
    return img

def render_chapter(project_data, output_dir, target_lang, watermark_path="", watermark_size=40, watermark_count=8, callback=None):
    folder_name = project_data.get("input_folder_name", "Chapter")
    final_output_dir = os.path.join(output_dir, folder_name)
    os.makedirs(final_output_dir, exist_ok=True)
    
    # Use Hayah as default if not specified
    font_path = os.path.join(os.path.dirname(__file__), "My fonts", "Hayah.otf")
    if not os.path.exists(font_path): font_path = "arial.ttf" # Fallback

    full_text_script = []

    for idx, page in enumerate(project_data["pages"]):
        if callback: callback(f"🎨 جاري رسم الصفحة {idx + 1}/{len(project_data['pages'])}...")
        
        img_path = page["clean_path"] if page.get("clean_path") else page["raw_path"]
        img = load_image_rgb(img_path)
        

        page_texts = []
        for block in page["blocks"]:
            if block.get("type") == "logo":
                img = draw_logo(img, block, watermark_path)
            else:
                img = typeset_arabic(
                    img, 
                    block["text"], 
                    font_path, 
                    block
                )
            page_texts.append(block.get("text", "[Logo]"))
            
        # Preserve the EXACT original filename and extension
        out_name = page.get("original_name")
        if not out_name:
            out_name = Path(page["raw_path"]).name
            if out_name.startswith("raw_") and out_name.endswith(".jpg"):
                # Fallback if original_name is missing (shouldn't happen with new logic)
                out_name = out_name.replace("raw_", "page_")
        
        # Industry standard quality: 80 for JPEG, optimize for all
        save_path = os.path.join(final_output_dir, out_name)
        if out_name.lower().endswith(('.jpg', '.jpeg')):
            img.save(save_path, "JPEG", quality=80, optimize=True)
        elif out_name.lower().endswith('.png'):
            img.save(save_path, "PNG", optimize=True)
        else:
            # Fallback for other formats
            img.save(save_path, optimize=True)
        
        full_text_script.append(f"--- صفحة {idx + 1} ({out_name}) ---")
        full_text_script.extend(page_texts)
        full_text_script.append("")
        
    txt_path = os.path.join(final_output_dir, f"{folder_name}_script.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(full_text_script))
        
    if callback: callback(f"✅ تم الانتهاء من التصدير في المجلد: {final_output_dir}")
    return final_output_dir

def get_slice_boxes(image_path, max_slice_height=5000):
    """Smartly split an image into slices avoiding text cuts if it exceeds max_slice_height."""
    img = load_image_rgb(image_path)
    w, h = img.width, img.height
    
    if h <= max_slice_height:
        return [(0, 0, w, h)]
        
    arr = np.array(img)
    gray = np.mean(arr, axis=2) if len(arr.shape) == 3 else arr.astype(float)
    row_var = np.var(gray, axis=1)
    smoothed = np.convolve(row_var, np.ones(10) / 10, mode='same')
    
    slices = []
    current_y = 0
    while current_y < h:
        if h - current_y <= max_slice_height:
            cut_y = h
        else:
            # Expand search window to find the absolute quietest point (seam)
            search_start = current_y + int(max_slice_height * 0.3)
            search_end = min(current_y + max_slice_height, h)
            window = smoothed[search_start:search_end]
            # Find the absolute quietest point in the window (min variance)
            cut_y = search_start + int(np.argmin(window)) if len(window) > 0 else current_y + max_slice_height
        
        slices.append((0, current_y, w, cut_y))
        current_y = cut_y
        
    return slices

def _process_parsed_json(data_list):
    """Normalizes keys and ensures every object has valid coordinates."""
    if not isinstance(data_list, list): return None
    n = len(data_list)
    valid_results = []
    
    for i, obj in enumerate(data_list):
        if not isinstance(obj, dict) or 'text' not in obj: continue
        
        # Filter out the example translation from the prompt
        if obj['text'].strip().lower().rstrip('.') == "this is a translation":
            continue
            
        # Normalize various key names to 'box_2d'
        for key in ['box_2d', 'box', 'coordinates', 'position', 'box_20', 'box_2']:
            if key in obj:
                obj['box_2d'] = obj.pop(key)
                break
        
        coords = obj.get('box_2d')
        # If missing, zeroed, or invalid, generate a vertical distribution fallback
        if not coords or not isinstance(coords, list) or len(coords) < 4 or all(v == 0 for v in coords):
            # Spread bubbles more logically if AI fails (center-aligned column)
            y_start = int((i / n) * 900) + 50
            y_end = y_start + 60
            obj['box_2d'] = [y_start, 300, y_end, 700]
            
        valid_results.append(obj)
    return valid_results if valid_results else None

TRANSLATION_PROMPT = """Role: You are an Expert Manhwa Localization Engine and Master Typesetter.

Context: You are translating a Manhwa (Korean Webtoon). You will receive images (either full pages or sequential slices). 
CRITICAL: ONLY translate the text in the CURRENT image provided in this turn. DO NOT repeat translations from previous images in the chat history.

Input: This is an image containing English or Korean text.

Objective: Extract and translate ALL text located strictly INSIDE speech bubbles, thought bubbles, and narrative boxes into {lang}.

### STRICT PROCESSING DIRECTIVES:

**Target Elements**: You must ONLY process text located strictly INSIDE speech bubbles, thought bubbles, and narrative boxes.

**Excluded Elements**: You must STRICTLY IGNORE:
- All Sound Effects (SFX) outside of bubbles
- Chapter titles and watermark credits
- Background signs and environmental text
- Any text that is NOT inside a clearly drawn bubble or box

### TRANSLATION QUALITY:
1. **MANHWA STYLE**: The translation must fit the context of a Manhwa/Webtoon. It should be natural, engaging, and flow perfectly like a professional human localization.
2. **CONTEXTUAL ARABIC (CRITICAL)**: Analyze the visual context (who is speaking, who they are talking to) and use the strictly correct Arabic masculine, feminine, or plural forms. DO NOT add diacritics (التشكيل/harakat) to the Arabic translation at all (e.g., no fatha, damma, kasra, shadda, sukun, tanween). All Arabic text must be completely plain/raw text without any diacritics.
3. **PROFESSIONAL GRADE**: No clunky, robotic, or literal-sounding output. Adapt idioms appropriately for Arabic readers.

### THE SINGLE-OBJECT RULE:
1. **ONE BUBBLE = ONE BOX**: Treat every speech bubble or narrative box as a single object.
2. **NEVER SPLIT**: Never split a single bubble into multiple coordinate boxes. Even if the text is long, return ONE box and ONE complete text entry.

### COORDINATE PRECISION:
1. **MANDATORY**: Provide precise bounding coordinates [ymin, xmin, ymax, xmax] for every bubble. Coordinates MUST be tightly bounded to the bubble edges.
2. **ACCURACY**: Coordinates must perfectly frame the bubble — not too loose, not too tight.

### OUTPUT FORMAT (MANDATORY — NO EXCEPTIONS):
Return ONLY lines in this exact format, one per bubble:
ymin, xmin, ymax, xmax | Translated text

**IMPORTANT**: Coordinates MUST be integers on a scale of 0 to 1000 (where 0 is top/left and 1000 is bottom/right of the image).
Example: 120, 250, 180, 450 | This is a translation.

Do NOT return JSON, markdown, explanations, or any other format. ONLY the pipe-separated lines above.
"""


def sanitize_gemini_json(raw_text):
    """
    Robust JSON extraction from Gemini responses.
    Returns:
      - list of dicts   → success (translation blocks)
      - "__NO_TEXT__"    → Gemini says no text in image (skip this image)
      - "__NO_IMAGE__"  → Gemini says no image was received (resend)
      - None            → unparseable response (retry)
    """
    import json as pyjson

    if not raw_text or not raw_text.strip():
        return None

    lower = raw_text.lower()
    stripped = raw_text.strip()

    # Quick check for explicit keywords we told Gemini to use
    if stripped == "NO_IMAGE" or stripped == "no_image":
        return "__NO_IMAGE__"

    # --- Detect "no image received" ---
    no_img_phrases = [
        "no_image", "NO_IMAGE",
        "no image", "i don't see", "i cannot see", "don't see an image",
        "pas d'image", "aucune image", "je ne vois pas", "fournir une image",
        "provide an image", "upload an image", "attach an image",
        "لم أجد صورة", "لا توجد صورة", "أرسل صورة", "لم يتم إرفاق",
        "no file", "no attachment", "can't find any image",
        "i do not see", "there is no image", "image is missing",
    ]
    if any(phrase in lower for phrase in no_img_phrases):
        return "__NO_IMAGE__"

    # --- Detect "no text in image" ---
    no_text_phrases = [
        "no text", "aucun texte", "لا يوجد نص", "لا نص", "لم أجد نص",
        "no dialogue", "no speech", "empty image", "pas de texte",
        "i can't find any text", "there is no text", "no words",
        "doesn't contain any text", "does not contain any text",
        "no readable text", "cannot find any text", "i don't see any text",
        "no speech bubbles", "does not contain any speech bubbles",
        "[]",  # empty JSON array = no text
    ]
    if stripped == "[]":
        return "__NO_TEXT__"
    if any(phrase in lower for phrase in no_text_phrases) and "[" not in stripped:
        return "__NO_TEXT__"

    # Strategy 1: Clean-Pipe Format (ymin, xmin, ymax, xmax | text)
    clean_pipe_results = []
    # Match: 120, 250, 180, 450 | some text (handles integers and decimals)
    pipe_matches = re.findall(r'([\d\.]+)\s*,\s*([\d\.]+)\s*,\s*([\d\.]+)\s*,\s*([\d\.]+)\s*\|\s*(.*)', raw_text)
    if pipe_matches:
        for m in pipe_matches:
            try:
                coords = [float(m[0]), float(m[1]), float(m[2]), float(m[3])]
                text_val = m[4].strip()
                if text_val and text_val.lower().rstrip('.') != "this is a translation":
                    clean_pipe_results.append({"box_2d": coords, "text": text_val})
            except: pass
        if clean_pipe_results: return clean_pipe_results

    # Strategy 2: Bracketed Pipe Format ([y, x, y, x] | text)
    bracket_pipe_matches = re.findall(r'\[\s*([\d\.]+)(?:[\s,]+)([\d\.]+)(?:[\s,]+)([\d\.]+)(?:[\s,]+)([\d\.]+)\s*\]\s*\|\s*(.*)', raw_text)
    if bracket_pipe_matches:
        pipe_results = []
        for m in bracket_pipe_matches:
            try:
                coords = [float(m[0]), float(m[1]), float(m[2]), float(m[3])]
                text_val = m[4].strip()
                if text_val and text_val.lower().rstrip('.') != "this is a translation":
                    pipe_results.append({"box_2d": coords, "text": text_val})
            except: pass
        if pipe_results: return pipe_results

    # Strategy 3: Standard JSON Array with aggressive repair
    fixed = raw_text
    for key in ['box_2d', 'box', 'coordinates', 'position', 'box_20', 'box_2', 'p']:
        fixed = re.sub(r'"' + key + r'"\s*:\s*(?=[,}])', f'"{key}": [0,0,0,0]', fixed)
        fixed = re.sub(r'"' + key + r'"\s*:\s*\[\s*\]', f'"{key}": [0,0,0,0]', fixed)
        fixed = re.sub(r'"' + key + r'"\s*:\s*,', f'"{key}": [0,0,0,0],', fixed)
    
    json_match = re.search(r'\[\s*\{.*\}\s*\]', fixed, re.DOTALL)
    if json_match:
        try:
            result = pyjson.loads(json_match.group(0))
            if isinstance(result, list) and len(result) > 0:
                return _process_parsed_json(result)
        except: pass

    # Strategy 4: Last Resort — Extract any text and use vertical fallback
    texts = re.findall(r'"text"\s*:\s*"([^"]+)"', raw_text)
    if not texts:
        texts = re.findall(r'\|\s*(.+)', raw_text)
    
    if texts:
        result = []
        valid_texts = [t.strip() for t in texts if t.strip().lower().rstrip('.') != "this is a translation"]
        n = len(valid_texts)
        for i, t in enumerate(valid_texts):
            # Better distribution for fallback (centered and spread)
            y_start = int((i / n) * 900) + 50
            y_end = y_start + 60
            result.append({"box_2d": [y_start, 300, y_end, 700], "text": t})
        return result if result else None

    return None

def start_new_gemini_chat(page, callback=None):
    """Starts a new fresh chat session in Gemini to prevent context leakage and hallucinations."""
    if callback: callback("   🧹 جاري بدء محادثة جديدة لتنظيف الذاكرة...")
    
    # Try different selectors to click the New Chat button
    selectors = [
        'button[aria-label="New chat"]',
        'a[aria-label="New chat"]',
        '[aria-label="New chat"]',
        'button[aria-label="Start new chat"]',
        'a[aria-label="Start new chat"]',
        '[aria-label="Start new chat"]',
        'a[href="/app"]',
        '.new-chat-button',
        'text="New chat"'
    ]
    
    clicked = False
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                time.sleep(2)
                clicked = True
                break
        except:
            pass
            
    if not clicked:
        # Try to open the navigation menu first if the sidebar is collapsed
        menu_selectors = [
            'button[aria-label="Main menu"]',
            '[aria-label="Main menu"]',
            'button[aria-label="Navigation menu"]',
            '.menu-button'
        ]
        for m_sel in menu_selectors:
            try:
                menu_btn = page.locator(m_sel).first
                if menu_btn.is_visible(timeout=1000):
                    menu_btn.click()
                    time.sleep(1)
                    # Try selectors again
                    for sel in selectors:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=2000):
                            btn.click()
                            time.sleep(2)
                            clicked = True
                            break
                if clicked:
                    break
            except:
                pass
                
    # If clicking didn't work, we can fallback to navigating to the base URL and waiting
    if not clicked:
        try:
            page.goto("https://gemini.google.com/app?hl=en", timeout=30000)
            time.sleep(1)
        except:
            pass
            
    # Wait for the editor to be ready and empty
    try:
        page.wait_for_selector('.ql-editor[contenteditable="true"]', timeout=30000)
        # Force-clear the editor just in case
        page.evaluate("""
        () => {
            const editors = document.querySelectorAll('.ql-editor[contenteditable="true"]');
            const editor = editors[editors.length - 1];
            if (editor) {
                editor.focus();
                document.execCommand('selectAll', false, null);
                document.execCommand('delete', false, null);
            }
        }
        """)
        time.sleep(0.5)
    except Exception as e:
        if callback: callback(f"   ⚠️ تنبيه: لم نتمكن من التأكد من جهوزية حقل الكتابة: {e}")

def extract_chapter(input_dir, clean_dir, target_lang='Arabic', max_slice_height=5000, smart_stitch=False, callback=None):
    import tempfile, shutil
    input_path = Path(input_dir)
    exts = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff'}
    
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s.name)]
        
    images = sorted([f for f in input_path.iterdir() if f.suffix.lower() in exts], key=natural_sort_key)
    clean_images = []
    if clean_dir and os.path.exists(clean_dir):
        clean_path_obj = Path(clean_dir)
        matched_by_name = 0
        for img_path in images:
            c_path = clean_path_obj / img_path.name
            if c_path.exists():
                clean_images.append(c_path)
                matched_by_name += 1
            else:
                clean_images.append(None)
        if matched_by_name == 0:
            clean_sorted = sorted([f for f in clean_path_obj.iterdir() if f.suffix.lower() in exts], key=natural_sort_key)
            clean_images = [clean_sorted[i] if i < len(clean_sorted) else None for i in range(len(images))]
    else:
        clean_images = [None] * len(images)

    if not images:
        if callback: callback("❌ لم يتم العثور على صور!")
        return None

    if callback: callback(f"📂 تم العثور على {len(images)} صور")

    # Keep track of original filenames to preserve extensions during export
    original_filenames = [img.name for img in images]
    
    # Use a temporary directory for the workspace instead of a persistent folder next to input
    temp_workspace = tempfile.mkdtemp(prefix="ppcleaning_")
    
    smart_images, smart_clean = [], []
    if smart_stitch:
        if callback: callback("🧵 جاري الدمج الذكي للصور...")
        Image.MAX_IMAGE_PIXELS = None
        try:
            imgs_objs = [load_image_rgb(p) for p in images]
            # Use the max width of all images to preserve maximum quality
            target_w = max([i.width for i in imgs_objs]) if imgs_objs else 1000
            h = sum(int(i.height * (target_w / i.width)) for i in imgs_objs)
            
            stitched = Image.new('RGB', (target_w, h))
            y = 0
            for i in imgs_objs:
                # Resize image to match target width while maintaining aspect ratio
                if i.width != target_w:
                    new_h = int(i.height * (target_w / i.width))
                    i = i.resize((target_w, new_h), Image.Resampling.LANCZOS)
                stitched.paste(i, (0, y))
                y += i.height
                
            has_clean = any(clean_images)
            if has_clean:
                stitched_clean = Image.new('RGB', (target_w, h))
                y = 0
                for c_path, r_img_orig in zip(clean_images, imgs_objs):
                    # Use original r_img width/height for ratio consistency
                    r_w, r_h = r_img_orig.width, r_img_orig.height
                    target_h = int(r_h * (target_w / r_w))
                    
                    if c_path:
                        c_img = load_image_rgb(c_path)
                        # Resize clean image to match the resized raw image
                        c_img = c_img.resize((target_w, target_h), Image.Resampling.LANCZOS)
                        stitched_clean.paste(c_img, (0, y))
                    else:
                        # If no clean image, resize the raw one
                        r_img_resized = r_img_orig.resize((target_w, target_h), Image.Resampling.LANCZOS)
                        stitched_clean.paste(r_img_resized, (0, y))
                    y += target_h
            
            # Slice the giant stitched image into manageable pieces for the editor
            # This avoids having one extremely tall image that is hard to view or edit.
            import tempfile
            
            # Temporary save to a real file to use get_slice_boxes logic
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name
            stitched.save(tmp_path, "JPEG", quality=90)
            
            slice_boxes = get_slice_boxes(tmp_path, max_slice_height)
            os.remove(tmp_path) # Clean up temp file
            
            for i, box in enumerate(slice_boxes):
                slice_img = stitched.crop(box)
                p = os.path.join(temp_workspace, f"smart_raw_{i:03d}.jpg")
                slice_img.save(p, "JPEG", quality=90)
                smart_images.append(Path(p))
                
                if has_clean:
                    slice_clean = stitched_clean.crop(box)
                    c_p = os.path.join(temp_workspace, f"smart_clean_{i:03d}.jpg")
                    slice_clean.save(c_p, "JPEG", quality=90)
                    smart_clean.append(Path(c_p))
                else:
                    smart_clean.append(None)
            
            images = smart_images
            if has_clean: clean_images = smart_clean
            if callback: callback(f"✅ تم دمج وتجزئة الصور إلى {len(images)} صفحات للمحرر.")
        except Exception as e:
            if callback: callback(f"⚠️ فشل الدمج الذكي: {e}")
            # Fallback to non-stitched if it fails
            smart_stitch = False
            
    # If not stitched, just copy original files to temp workspace for UI
    if not smart_stitch:
        for i, img_path in enumerate(images):
            # Save as JPG for editor preview to save disk space and RAM
            p = os.path.join(temp_workspace, f"raw_{i:03d}.jpg")
            load_image_rgb(img_path).save(p, "JPEG", quality=90)
            smart_images.append(Path(p))
            
            if clean_images[i]:
                c_p = os.path.join(temp_workspace, f"clean_{i:03d}.jpg")
                load_image_rgb(clean_images[i]).save(c_p, "JPEG", quality=90)
                smart_clean.append(Path(c_p))
            else:
                smart_clean.append(None)
        images = smart_images
        clean_images = smart_clean

    project_data = {"workspace": temp_workspace, "input_folder_name": input_path.name, "pages": []}

    try:
        if callback: callback("🚀 جاري بدء المتصفح لاستخراج النصوص...")
        user_data_dir = os.path.join(os.path.dirname(__file__), 'chrome_profile')
        os.makedirs(user_data_dir, exist_ok=True)
        
        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                user_data_dir, 
                channel="chrome", 
                headless=False, 
                args=['--disable-blink-features=AutomationControlled'],
                permissions=['clipboard-read', 'clipboard-write']
            )
            browser.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto("https://gemini.google.com/app?hl=en", timeout=60000)
            page.wait_for_selector('div[contenteditable="true"], rich-textarea', timeout=60000)
            
            # Start a fresh chat for the very first page to clear old history
            start_new_gemini_chat(page, callback)
            
            for idx, img_path in enumerate(images):
                if callback: callback(f"\n━━━ الصفحة {idx + 1}/{len(images)} ━━━")
                
                # Start a fresh chat session for each new page to keep context within the same page
                if idx > 0:
                    start_new_gemini_chat(page, callback)
                
                boxes = get_slice_boxes(str(img_path), max_slice_height)
                
                # Use original filename for the final export
                if smart_stitch:
                    # For stitched images, use the first original name but add a part suffix
                    stem = Path(original_filenames[0]).stem
                    ext = Path(original_filenames[0]).suffix
                    orig_name = f"{stem}_part{idx+1:02d}{ext}"
                else:
                    orig_name = original_filenames[idx]
                
                page_data = {
                    "id": idx,
                    "raw_path": str(img_path),
                    "clean_path": str(clean_images[idx]) if clean_images[idx] else None,
                    "original_name": orig_name,
                    "width": Image.open(img_path).width,
                    "height": Image.open(img_path).height,
                    "blocks": []
                }
                
                raw_img = load_image_rgb(img_path)
                clean_img = load_image_rgb(clean_images[idx]) if clean_images[idx] else raw_img.copy()

                for si, box in enumerate(boxes):

                    fd, slice_temp = tempfile.mkstemp(suffix=".jpg")
                    os.close(fd)
                    raw_piece = raw_img.crop(box)
                    raw_piece.save(slice_temp, 'JPEG', quality=85)
                    pw, ph = raw_piece.size
                    box_x_off, box_y_off = box[0], box[1]
                    
                    try:
                        # Prepare image data once (outside retry loop)
                        img_to_send = load_image_rgb(slice_temp)
                        img_to_send = _resize_for_gemini(img_to_send, max_w=2000, max_h=5000)
                        
                        import io as _io, base64 as _b64
                        output_b64 = _io.BytesIO()
                        img_to_send.save(output_b64, 'JPEG', quality=85)
                        b64_data = _b64.b64encode(output_b64.getvalue()).decode('utf-8')
                        prompt_text = TRANSLATION_PROMPT.format(lang=target_lang)
                        
                        # === RETRY LOOP: up to 3 attempts per slice ===
                        blocks_data = None
                        last_text = ""
                        skip_slice = False
                        for attempt in range(3):
                            try:
                                if attempt > 0 and callback:
                                    callback(f"   🔄 إعادة المحاولة ({attempt + 1}/3)...")
                                
                                # STEP 1: Ensure we are on the Gemini chat page
                                if page.is_closed():
                                    page = browser.new_page()
                                page.bring_to_front()
                                
                                # Only navigate to a new chat if we are at the very beginning or if the page is stuck
                                if page.url == "about:blank" or (attempt > 1):
                                    page.goto("https://gemini.google.com/app?hl=en", timeout=60000)
                                    page.wait_for_selector('.ql-editor[contenteditable="true"]', timeout=60000)
                                    time.sleep(1)
                                
                                # STEP 2: Force-clear the input
                                page.evaluate("""
                                () => {
                                    const editors = document.querySelectorAll('.ql-editor[contenteditable="true"]');
                                    const editor = editors[editors.length - 1];
                                    if (editor) {
                                        editor.focus();
                                        document.execCommand('selectAll', false, null);
                                        document.execCommand('delete', false, null);
                                    }
                                }
                                """)
                                time.sleep(0.5)
                                
                                # STEP 2.5: Count initial previews before pasting
                                previews_selector = 'img[src*="blob:"], img[src*="data:"], .image-preview, .attachment-preview, .inline-image, [data-image-upload], .ql-image, img.ql-image, .media-upload-chip, .upload-chip, file-upload-chip, rich-textarea img, .ql-editor img'
                                try:
                                    initial_count = len(page.locator(previews_selector).all())
                                except Exception:
                                    initial_count = 0
                                
                                # STEP 3: Inject image via ClipboardEvent
                                page.evaluate(f"""
                                async () => {{
                                    const res = await fetch("data:image/jpeg;base64,{b64_data}");
                                    const blob = await res.blob();
                                    const file = new File([blob], "image.jpg", {{ type: "image/jpeg" }});
                                    const dataTransfer = new DataTransfer();
                                    dataTransfer.items.add(file);
                                    
                                    const editors = document.querySelectorAll('.ql-editor[contenteditable="true"]');
                                    let target = editors[editors.length - 1] || document.activeElement;
                                    target.focus();
                                    
                                    const event = new ClipboardEvent('paste', {{
                                        clipboardData: dataTransfer,
                                        bubbles: true,
                                        cancelable: true
                                    }});
                                    target.dispatchEvent(event);
                                }}
                                """)

                                # STEP 4: Wait for image attachment to appear using previews count difference
                                image_ready = False
                                for _wait in range(15):
                                    time.sleep(1)
                                    try:
                                        current_count = len(page.locator(previews_selector).all())
                                        if current_count > initial_count:
                                            image_ready = True
                                            time.sleep(1.5)  # Upload stability buffer
                                            break
                                    except Exception:
                                        pass
                                
                                # Fallback check using explicit selectors
                                if not image_ready:
                                    try:
                                        previews = page.locator('rich-textarea img, rich-textarea file-upload-chip, rich-textarea .upload-chip, rich-textarea [data-image-upload], rich-textarea .media-upload-chip').all()
                                        if len(previews) == 0:
                                            previews = page.locator('.ql-editor img, .ql-image img, img.ql-image').all()
                                        if len(previews) > 0:
                                            image_ready = True
                                    except Exception:
                                        pass
                                        
                                if not image_ready:
                                    if callback: callback(f"   ⚠️ الصورة لم تظهر، إعادة المحاولة...")
                                    continue
                                
                                # STEP 5: Re-focus and type prompt
                                page.evaluate("""
                                () => {
                                    const editors = document.querySelectorAll('.ql-editor[contenteditable="true"]');
                                    const editor = editors[editors.length - 1];
                                    if (editor) editor.focus();
                                }
                                """)
                                time.sleep(0.3)
                                page.keyboard.insert_text(prompt_text)
                                time.sleep(0.5)
                                
                                # STEP 6: Verify input is not empty before sending
                                input_text = page.evaluate("""
                                () => {
                                    const editors = document.querySelectorAll('.ql-editor[contenteditable="true"]');
                                    const editor = editors[editors.length - 1];
                                    return editor ? editor.innerText.trim() : '';
                                }
                                """)
                                if len(input_text) < 20:
                                    if callback: callback(f"   ⚠️ النص فُقد، إعادة الكتابة...")
                                    page.evaluate("""
                                    () => {
                                        const editors = document.querySelectorAll('.ql-editor[contenteditable="true"]');
                                        const editor = editors[editors.length - 1];
                                        if (editor) {
                                            editor.focus();
                                            document.execCommand('selectAll', false, null);
                                            document.execCommand('delete', false, null);
                                        }
                                    }
                                    """)
                                    time.sleep(0.3)
                                    page.keyboard.insert_text(prompt_text)
                                    time.sleep(0.5)
                                
                                # STEP 7: Send
                                page.evaluate("""
                                () => {
                                    const editors = document.querySelectorAll('.ql-editor[contenteditable="true"]');
                                    const editor = editors[editors.length - 1];
                                    if (editor) editor.focus();
                                }
                                """)
                                time.sleep(0.3)
                                page.keyboard.press('Enter')
                                
                                # STEP 8: Wait for Gemini response (robust multi-selector)
                                time.sleep(4)
                                last_text = ""
                                stable_count = 0
                                blocks_data = None
                                skip_slice = False
                                
                                # JavaScript function to extract response text from Gemini UI
                                # CRITICAL: Use textContent on code/pre blocks — innerText strips [arrays]
                                extract_response_js = """
                                () => {
                                    // Strategy 1: Look for the new "Pipe" format (y, x, y, x | text) inside model responses
                                    const allContent = document.querySelectorAll('model-response message-content, .model-response-text, [data-message-author-role="model"] message-content, [data-message-author-role="model"]');
                                    for (let i = allContent.length - 1; i >= 0; i--) {
                                        const t = allContent[i].innerText || allContent[i].textContent || '';
                                        // Detect 4 numbers followed by a pipe |
                                        if (t.includes('|') && /\d+,\s*\d+,\s*\d+,\s*\d+/.test(t)) return t;
                                        // Fallback for JSON
                                        if ((t.includes('box_2d') || t.includes('position')) && t.includes('text')) return t;
                                    }
                                    
                                    // Strategy 2: Code blocks inside model responses
                                    const codeBlocks = document.querySelectorAll('model-response code, model-response pre, [data-message-author-role="model"] code, [data-message-author-role="model"] pre');
                                    for (let i = codeBlocks.length - 1; i >= 0; i--) {
                                        const t = codeBlocks[i].innerText || '';
                                        if (t.includes('|') || t.includes('box_2d')) return t;
                                    }
                                    // Strategy 3: Extract text from model response containers
                                    const selectors = [
                                        'model-response message-content',
                                        '.model-response-text',
                                        '[data-message-author-role="model"] message-content',
                                        '[data-message-author-role="model"]',
                                        '.markdown-main-panel'
                                    ];
                                    for (const sel of selectors) {
                                        const els = document.querySelectorAll(sel);
                                        if (els.length > 0) {
                                            const last = els[els.length - 1];
                                            // Handle visual detection elements (Gemini UI sometimes hides numbers in tooltips)
                                            // Extract all text nodes, including those inside spans or interactive elements
                                            const txt = Array.from(last.querySelectorAll('*')).map(e => e.textContent).join(' ') + ' ' + last.textContent;
                                            if ((txt.includes('box_2d') || txt.includes('box') || txt.includes('|')) && txt.length > 20) return txt;
                                        }
                                    }
                                    // Strategy 4: Aggressive DOM scan for coordinate strings inside model responses
                                    const all = document.querySelectorAll('model-response div, model-response p, model-response span, [data-message-author-role="model"] div, [data-message-author-role="model"] p, [data-message-author-role="model"] span');
                                    for (let i = all.length - 1; i >= 0; i--) {
                                        const t = all[i].textContent || '';
                                        if (t.includes('|') && /\d+,\s*\d+/.test(t) && t.length < 2000) return t;
                                    }
                                    return '';
                                }
                                """
                                
                                for wait_tick in range(40):
                                    try:
                                        current_text = page.evaluate(extract_response_js)
                                        if current_text and current_text == last_text and len(current_text) > 10:
                                            stable_count += 1
                                            if stable_count >= 2:
                                                break
                                        elif current_text != last_text:
                                            stable_count = 0
                                            last_text = current_text
                                    except:
                                        pass
                                    if wait_tick % 5 == 0 and wait_tick > 0:
                                        if callback: callback(f"     ⏳ جاري الانتظار... ({wait_tick*2}ث)")
                                    time.sleep(2)
                                
                                # STEP 9: Parse response using smart sanitizer
                                parsed = sanitize_gemini_json(last_text)
                                
                                if parsed == "__NO_IMAGE__":
                                    if callback: callback(f"   ⚠️ جيميني لم يستلم الصورة، إعادة الإرسال...")
                                    continue
                                
                                if parsed == "__NO_TEXT__":
                                    if callback: callback(f"   ℹ️ لا يوجد نص في هذه القصاصة، تخطي...")
                                    skip_slice = True
                                    break
                                
                                if isinstance(parsed, list) and len(parsed) > 0:
                                    blocks_data = parsed
                                    break  # Success!
                                
                                if not last_text.strip():
                                    if callback: callback(f"   ⚠️ لم يتم تلقي أي رد، إعادة المحاولة...")
                                    continue
                                
                                # Debug: show what Gemini actually returned
                                preview = (last_text[:200] + '...') if len(last_text) > 200 else last_text
                                if callback: callback(f"   ⚠️ استجابة غير صالحة. رد جيميني: {preview}")
                                break # Stop retrying if Gemini returned a response but it wasn't valid text/JSON
                                    
                            except Exception as retry_err:
                                err_msg = str(retry_err)
                                if "closed" in err_msg.lower() or "target page" in err_msg.lower():
                                    if callback: callback("   ❌ المتصفح أُغلق! يتم إيقاف العملية.")
                                    return None
                                if callback: callback(f"   ⚠️ خطأ: {err_msg[:100]}, إعادة المحاولة...")
                                time.sleep(2)
                        
                        # Process results — use Gemini coordinates directly
                        if blocks_data and isinstance(blocks_data, list):
                            for block in blocks_data:
                                # Prioritize 'box_2d' as the native Gemini detection key
                                coords = block.get('box_2d') or block.get('box') or block.get('coordinates') or block.get('position')
                                if coords and 'text' in block:
                                    # Auto-detect normalized 0-1 vs 0-1000 scale
                                    if all(0 <= v <= 1.1 for v in coords):
                                        coords = [v * 1000 for v in coords]
                                    elif all(v <= 1.5 for v in coords if v > 0):
                                        coords = [v * 1000 for v in coords]
                                    
                                    ymin, xmin, ymax, xmax = coords
                                    # Precise mapping from 0-1000 scale to slice pixels, then to global pixels
                                    x1 = box_x_off + (xmin * pw) / 1000
                                    y1 = box_y_off + (ymin * ph) / 1000
                                    x2 = box_x_off + (xmax * pw) / 1000
                                    y2 = box_y_off + (ymax * ph) / 1000
                                    
                                    # Ensure coordinates are within image bounds and integers
                                    x1, y1 = max(0, int(x1)), max(0, int(y1))
                                    x2, y2 = min(raw_img.width, int(x2)), min(raw_img.height, int(y2))
                                    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                                    try:
                                        crop_box = (max(0, int(x1)), max(0, int(y1)), min(raw_img.width, int(x2)), min(raw_img.height, int(y2)))
                                        is_dark = np.mean(np.array(raw_img.crop(crop_box).convert('L'))) < 127
                                    except:
                                        is_dark = False
                                    
                                    page_data["blocks"].append({
                                        "id": f"b_{idx}_{si}_{len(page_data['blocks'])}",
                                        "text": block['text'],
                                        "x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1,
                                        "cx": cx, "cy": cy,
                                        "is_dark": bool(is_dark),
                                        "is_gradient": False,
                                        "gradient_colors": ["#8b5cf6", "#3b82f6"]
                                    })
                            if callback: callback(f"   ✅ تم استخراج {len(blocks_data)} فقاعة نصية")
                        elif not skip_slice:
                            if callback: callback(f"   ⚠️ فشل استخراج القصاصة {si+1} بعد 3 محاولات")
                    except Exception as e:
                        if callback: callback(f"   ⚠️ Slice {si + 1} error: {str(e)}")
                    finally:
                        try:
                            os.remove(slice_temp)
                        except: pass
                
                project_data["pages"].append(page_data)

            browser.close()
            return project_data

    except Exception as e:
        err_msg = str(e)
        if callback: callback(f"❌ Error: {err_msg[:200]}")
        return None
        err_msg = str(e)
        if callback: callback(f"❌ Error: {err_msg[:200]}")
        return None
