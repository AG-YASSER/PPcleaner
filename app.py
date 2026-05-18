"""
PPCleaning — Webtoon Translation Tool
Sends webtoon images directly to Google Gemini API for vision-based translation.
Outputs a single clean TXT file.
"""
import os
import sys
import threading
import json
import webview
import base64
import logging
import socket
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item

# ─── Logging Setup ───
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("ppcleaning.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ─── Processing State ───
class AppState:
    def __init__(self):
        self.is_running = False
        self.logs = []
        self.window = None
        self.tray_icon = None
        self.is_quitting = False

    def add_log(self, msg):
        self.logs.append(msg)

    def reset(self):
        self.is_running = False
        self.logs = []

state = AppState()

# ─── Tray Icon Logic ───
def create_default_icon():
    # Create a simple icon if none exists
    width = 64
    height = 64
    color1 = (124, 58, 237) # Purple
    image = Image.new('RGB', (width, height), color1)
    dc = ImageDraw.Draw(image)
    dc.text((20, 10), "P", fill=(255, 255, 255))
    return image

def on_tray_show(icon, item):
    if state.window:
        state.window.show()

def on_tray_quit(icon, item):
    state.is_quitting = True
    icon.stop()
    if state.window:
        state.window.destroy()
    os._exit(0)

def setup_tray():
    try:
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        if os.path.exists(icon_path):
            image = Image.open(icon_path)
        else:
            image = create_default_icon()
            
        menu = (
            item('Show Window', on_tray_show),
            item('Quit', on_tray_quit)
        )
        state.tray_icon = pystray.Icon("ppcleaning", image, "PPCleaning", menu)
        state.tray_icon.run()
    except Exception as e:
        logger.error(f"Tray Icon Error: {e}")

def on_closing():
    if state.is_quitting:
        return True
    if state.window:
        state.window.hide()
    return False # Prevent closing

# ─── Single Instance Check ───
SINGLE_INSTANCE_PORT = 51234

def single_instance_checker():
    """Ensures only one instance runs. If another starts, it triggers 'show' on the first."""
    try:
        # Try to bind to the port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', SINGLE_INSTANCE_PORT))
        sock.listen(5)
        
        def listen_for_show():
            while True:
                try:
                    conn, addr = sock.accept()
                    data = conn.recv(1024).decode('utf-8')
                    if data == "SHOW":
                        if state.window:
                            state.window.show()
                    conn.close()
                except:
                    break
        
        threading.Thread(target=listen_for_show, daemon=True).start()
        return True
    except socket.error:
        # Port already in use, send "SHOW" to the existing instance
        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.connect(('127.0.0.1', SINGLE_INSTANCE_PORT))
            client.sendall(b"SHOW")
            client.close()
        except:
            pass
        return False

# ─── API for JS ↔ Python ───
class Api:
    def select_folder(self):
        try:
            result = state.window.create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=False)
            if not result: return ""
            return result[0] if isinstance(result, (list, tuple)) else str(result)
        except Exception:
            return ""

    def select_file(self):
        try:
            result = state.window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False)
            if not result: return ""
            return result[0] if isinstance(result, (list, tuple)) else str(result)
        except Exception:
            return ""

    def get_image_base64(self, path):
        try:
            with open(path, "rb") as f:
                return "data:image/png;base64," + base64.b64encode(f.read()).decode('utf-8')
        except: return ""

    def get_font_base64(self, rel_path):
        try:
            app_dir = os.path.dirname(os.path.abspath(__file__))
            full_path = os.path.join(app_dir, rel_path)
            with open(full_path, "rb") as f:
                return base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to load font {rel_path}: {e}")
            return ""

    def start_extraction(self, input_dir, clean_dir, target_lang, slice_height, smart_stitch=False, watermark_path=""):
        try:
            if state.is_running: return json.dumps({"error": "العملية قيد التشغيل بالفعل."})
            if not input_dir or not os.path.isdir(input_dir): return json.dumps({"error": "مجلد الصور غير صالح."})
            
            slice_h = int(slice_height) if slice_height else 5000
            state.reset()
            state.is_running = True
            state.watermark_path = watermark_path
            logger.info(f"Starting extraction: input={input_dir}, clean={clean_dir}, watermark={watermark_path}")
            
            def run():
                try:
                    def cb(msg): state.add_log(msg)
                    from engine import extract_chapter
                    data = extract_chapter(input_dir, clean_dir, target_lang, slice_h, smart_stitch, callback=cb)
                    if data:
                        data["watermark_path"] = watermark_path
                        state.project_data = data
                        state.add_log("__EXTRACTION_DONE__")
                    else:
                        state.add_log("❌ فشل الاستخراج.")
                except Exception as e:
                    state.add_log(f"❌ خطأ فادح: {str(e)}")
                    logger.exception("Extraction Error")
                finally:
                    state.is_running = False
            
            threading.Thread(target=run, daemon=True).start()
            return json.dumps({"status": "started"})
        except Exception as e:
            logger.exception("start_extraction API Error")
            return json.dumps({"error": f"Internal Error: {str(e)}"})

    def get_project_data(self):
        if hasattr(state, 'project_data') and state.project_data:
            return json.dumps(state.project_data)
        return json.dumps({"error": "No data available."})

    def render_project(self, project_data_json, output_dir, target_lang, watermark_path="", watermark_size=40, watermark_count=8):
        try:
            if state.is_running: return json.dumps({"error": "العملية قيد التشغيل بالفعل."})
            if not output_dir: return json.dumps({"error": "حدد مجلد الإخراج."})
            
            data = json.loads(project_data_json)
            state.reset()
            state.is_running = True
            logger.info(f"Starting render: output={output_dir}")
            
            def run():
                try:
                    def cb(msg): state.add_log(msg)
                    from engine import render_chapter
                    final_path = render_chapter(data, output_dir, target_lang, watermark_path, int(watermark_size) if watermark_size else 40, int(watermark_count) if watermark_count else 8, callback=cb)
                    state.add_log(f"__RENDER_DONE__::{final_path}")
                except Exception as e:
                    state.add_log(f"❌ خطأ فادح: {str(e)}")
                    logger.exception("Rendering Error")
                finally:
                    state.is_running = False
            
            threading.Thread(target=run, daemon=True).start()
            return json.dumps({"status": "started"})
        except Exception as e:
            logger.exception("render_project API Error")
            return json.dumps({"error": f"Internal Error: {str(e)}"})

    def poll_logs(self):
        try:
            logs = list(state.logs)
            state.logs.clear()
            return json.dumps({"logs": logs, "running": state.is_running})
        except: return json.dumps({"logs": [], "running": False})

    def cancel(self):
        state.is_running = False
        return json.dumps({"status": "cancelled"})

    def open_folder(self, path):
        if os.path.isdir(path): os.startfile(path)

# ─── HTML UI ───
HTML = r'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PPCleaning — محرر الويبتون الذكي</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=Zain:wght@300;400;700;900&display=swap" rel="stylesheet">
<style>
/* DYNAMIC_FONTS_CSS */

:root {
  --bg: #030305; --panel: rgba(15, 15, 26, 0.7);
  --accent: #7c3aed; --accent-glow: rgba(124, 58, 237, 0.4);
  --text: #f8fafc; --text-dim: #94a3b8;
  --border: rgba(255, 255, 255, 0.08);
  --success: #10b981; --error: #ef4444;
  --gradient: linear-gradient(135deg, #7c3aed 0%, #4f46e5 100%);
}
* { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Outfit', 'Zain', sans-serif; }
body { background: var(--bg); color: var(--text); overflow: hidden; height: 100vh; display: flex; align-items: center; justify-content: center; }

/* Views */
#settingsView, #editorView { width: 100%; height: 100%; display: flex; transition: opacity 0.3s; position: absolute; top:0; left:0; }
#editorView { display: none; background: #0a0a0f; }

/* Settings View UI */
.bg-blobs { position: absolute; top: 0; left: 0; width: 100%; height: 100%; z-index: -1; filter: blur(80px); opacity: 0.4; pointer-events: none; }
.blob { position: absolute; width: 400px; height: 400px; background: var(--accent); border-radius: 50%; animation: move 20s infinite alternate; }
.blob-2 { background: #3b82f6; left: 60%; top: 40%; animation-delay: -5s; }
@keyframes move { from { transform: translate(-10%, -10%); } to { transform: translate(20%, 20%); } }

.main-container { width: 900px; height: 700px; background: var(--panel); backdrop-filter: blur(20px); border: 1px solid var(--border); border-radius: 24px; display: flex; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5); position: relative; overflow: hidden; margin: auto; }
.sidebar { width: 320px; border-left: 1px solid var(--border); padding: 32px; display: flex; flex-direction: column; background: rgba(0,0,0,0.2); overflow-y: auto; }
.logo { display: flex; align-items: center; gap: 12px; margin-bottom: 30px; }
.logo-icon { width: 40px; height: 40px; background: var(--gradient); border-radius: 12px; display: flex; align-items: center; justify-content: center; font-size: 20px; }
.logo-text h1 { font-size: 20px; font-weight: 800; }
.logo-text p { font-size: 10px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px; }
.input-group { margin-bottom: 20px; }
.input-group label { display: block; font-size: 13px; margin-bottom: 8px; color: var(--text-dim); }
.input-wrapper { display: flex; gap: 8px; }
.input-wrapper input { flex: 1; background: rgba(255,255,255,0.05); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; color: white; font-size: 13px; outline: none; transition: all 0.3s; }
.input-wrapper input:focus { border-color: var(--accent); box-shadow: 0 0 0 4px var(--accent-glow); }
.browse-btn { background: var(--border); border: none; border-radius: 10px; padding: 0 12px; color: white; cursor: pointer; font-size: 12px; transition: 0.2s; }
.browse-btn:hover { background: rgba(255,255,255,0.15); }
.content { flex: 1; padding: 32px; display: flex; flex-direction: column; }
.console-container { flex: 1; background: #000; border: 1px solid var(--border); border-radius: 16px; overflow: hidden; display: flex; flex-direction: column; margin-bottom: 20px; }
.console-header { padding: 10px 16px; background: #0a0a0f; border-bottom: 1px solid var(--border); font-size: 12px; font-weight: 700; display: flex; justify-content: space-between; align-items: center; }
.copy-btn { background: rgba(255,255,255,0.05); border: 1px solid var(--border); border-radius: 6px; color: var(--text-dim); padding: 4px 8px; font-size: 11px; cursor: pointer; transition: 0.2s; }
.copy-btn:hover { background: rgba(255,255,255,0.1); color: white; }
.console-body { flex: 1; padding: 16px; font-family: 'Consolas', monospace; font-size: 12px; overflow-y: auto; line-height: 1.6; }
.log-line { margin-bottom: 4px; padding-right: 8px; }
.log-line.info { color: var(--text-dim); }
.log-line.success { color: var(--success); }
.log-line.error { color: var(--error); }
.btn-primary { background: var(--gradient); color: white; border: none; border-radius: 14px; padding: 16px; font-weight: 700; cursor: pointer; font-size: 15px; width: 100%; transition: 0.3s; box-shadow: 0 10px 20px -10px var(--accent-glow); }
.btn-primary:hover { transform: translateY(-2px); box-shadow: 0 15px 30px -10px var(--accent-glow); }
.btn-primary:disabled { opacity: 0.5; pointer-events: none; }

/* Editor View UI */
.editor-sidebar { width: 320px; min-width: 280px; background: #11111a; border-left: 1px solid var(--border); display: flex; flex-direction: column; padding: 0; overflow: hidden; }
.editor-sidebar::-webkit-scrollbar { width: 6px; }
.editor-sidebar::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.sidebar-header { padding: 16px 20px; border-bottom: 1px solid var(--border); background: rgba(0,0,0,0.3); }
.sidebar-body { flex: 1; overflow-y: auto; padding: 16px 20px; }
.sidebar-body::-webkit-scrollbar { width: 5px; }
.sidebar-body::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
.sidebar-footer { padding: 12px 20px; border-top: 1px solid var(--border); background: rgba(0,0,0,0.3); }
.editor-main { flex: 1; position: relative; background: #050508; overflow: auto; display: flex; justify-content: center; padding: 40px; min-width: 0; }
.canvas-wrapper { position: relative; transform-origin: top center; box-shadow: 0 0 40px rgba(0,0,0,0.8); direction: ltr; }
.canvas-img { display: block; max-width: none; user-select: none; -webkit-user-drag: none; pointer-events: none; }
#boxesLayer { position: absolute; top: 0; left: 0; width: 100%; height: 100%; direction: ltr; }
.bubble-box { position: absolute; border: 1px dashed rgba(255,255,255,0.4); background: rgba(0, 0, 0, 0.1); cursor: grab; display: flex; align-items: center; justify-content: center; box-sizing: border-box; direction: ltr; user-select: none; transition: border-color 0.15s, background 0.15s; transform-origin: center center; }
.bubble-box:hover { border-color: rgba(255,255,255,0.7); }
.bubble-box:active { cursor: grabbing; }
.bubble-box.selected { border: 2px solid var(--accent); background: rgba(124, 58, 237, 0.2); box-shadow: 0 0 15px var(--accent-glow); z-index: 10; }
.bubble-box.gradient { border-color: #3b82f6; background: rgba(59, 130, 246, 0.2); }
.bubble-text { 
  width: 100%; 
  height: 100%; 
  display: flex; 
  align-items: center; 
  justify-content: center; 
  text-align: center; 
  direction: rtl; 
  pointer-events: none; 
  user-select: none; 
  font-family: 'Zain', 'Outfit', sans-serif;
  font-weight: 700;
  white-space: pre-wrap; 
  overflow: hidden; 
  paint-order: stroke fill;
}
.logo-box-img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  pointer-events: none;
  user-select: none;
}
.resize-handle { position: absolute; width: 24px; height: 24px; z-index: 20; display: flex; align-items: center; justify-content: center; }
.resize-handle::after { content: ""; width: 10px; height: 10px; background: white; border-radius: 50%; border: 2px solid var(--accent); box-shadow: 0 0 5px rgba(0,0,0,0.5); }
.handle-tl { left: -12px; top: -12px; cursor: nw-resize; }
.handle-tr { left: calc(100% - 12px); top: -12px; cursor: ne-resize; }
.handle-bl { left: -12px; top: calc(100% - 12px); cursor: sw-resize; }
.handle-br { left: calc(100% - 12px); top: calc(100% - 12px); cursor: se-resize; }

/* Zoom Toolbar */
.zoom-toolbar { position: absolute; bottom: 20px; left: 50%; transform: translateX(-50%); display: flex; gap: 4px; background: rgba(15, 15, 26, 0.9); backdrop-filter: blur(16px); border: 1px solid var(--border); border-radius: 12px; padding: 6px; z-index: 50; box-shadow: 0 8px 32px rgba(0,0,0,0.5); }
.zoom-btn { width: 36px; height: 36px; background: none; border: none; color: var(--text-dim); font-size: 16px; cursor: pointer; border-radius: 8px; display: flex; align-items: center; justify-content: center; transition: all 0.15s; }
.zoom-btn:hover { background: rgba(124, 58, 237, 0.2); color: white; }
.zoom-btn.active { background: var(--accent); color: white; }
.zoom-label { min-width: 48px; text-align: center; font-size: 12px; font-weight: 600; color: var(--text-dim); line-height: 36px; user-select: none; }
.zoom-divider { width: 1px; background: var(--border); margin: 4px 2px; }

/* Page Navigation Bar */
.page-nav-bar { display: flex; align-items: center; gap: 0; background: #0a0a0f; border-bottom: 1px solid var(--border); padding: 0; min-height: 50px; }
.page-nav-btn { width: 40px; min-width: 40px; height: 50px; background: none; border: none; color: var(--text-dim); font-size: 18px; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: all 0.2s; flex-shrink: 0; }
.page-nav-btn:hover { color: white; background: rgba(124, 58, 237, 0.15); }
.page-nav-btn:disabled { opacity: 0.2; cursor: default; }
.page-nav-btn:disabled:hover { background: none; color: var(--text-dim); }
.page-counter { min-width: 70px; text-align: center; font-size: 12px; font-weight: 600; color: var(--text-dim); flex-shrink: 0; padding: 0 6px; white-space: nowrap; }
.page-list { display: flex; gap: 6px; overflow-x: auto; padding: 6px 4px; scroll-behavior: smooth; flex: 1; min-width: 0; align-items: center; }
.page-list::-webkit-scrollbar { height: 4px; }
.page-list::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.12); border-radius: 3px; }
.page-thumb { height: 36px; padding: 0 14px; background: #1a1a24; border-radius: 8px; cursor: pointer; border: 2px solid transparent; opacity: 0.6; transition: all 0.2s; flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 12px; white-space: nowrap; user-select: none; }
.page-thumb:hover { opacity: 0.85; background: #222230; }
.page-thumb.active { border-color: var(--accent); opacity: 1; background: rgba(124, 58, 237, 0.15); color: white; box-shadow: 0 0 10px var(--accent-glow); }

/* Section Labels */
.section-label { font-size: 11px; font-weight: 700; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; margin-top: 16px; display: flex; align-items: center; gap: 6px; }
.section-label:first-child { margin-top: 0; }
.section-label .badge { background: var(--accent); color: white; font-size: 10px; padding: 1px 6px; border-radius: 10px; font-weight: 700; }

/* Block List */
.block-list { display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; max-height: 180px; overflow-y: auto; }
.block-list::-webkit-scrollbar { width: 4px; }
.block-list::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
.block-item { display: flex; align-items: center; gap: 8px; padding: 8px 10px; background: rgba(255,255,255,0.03); border: 1px solid transparent; border-radius: 8px; cursor: pointer; transition: all 0.15s; font-size: 12px; direction: rtl; }
.block-item:hover { background: rgba(255,255,255,0.06); border-color: var(--border); }
.block-item.active { background: rgba(124, 58, 237, 0.15); border-color: var(--accent); }
.block-item-num { width: 22px; height: 22px; border-radius: 6px; background: rgba(255,255,255,0.08); display: flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 700; color: var(--text-dim); flex-shrink: 0; }
.block-item.active .block-item-num { background: var(--accent); color: white; }
.block-item-text { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-dim); }
.block-item.active .block-item-text { color: white; }

/* Properties Panel */
.prop-panel { background: rgba(0,0,0,0.25); border-radius: 10px; padding: 14px; display: none; flex-direction: column; gap: 4px; }
.prop-panel.active { display: flex; }
#textOnlyProps, #textStyleProps { display: flex; flex-direction: column; gap: 4px; }
.prop-panel textarea { width: 100%; height: 90px; background: rgba(0,0,0,0.5); border: 1px solid var(--border); color: white; padding: 10px; border-radius: 8px; resize: vertical; outline: none; margin-bottom: 10px; font-family: 'Zain', 'Outfit', sans-serif; font-size: 14px; direction: rtl; transition: border-color 0.2s; }
.prop-panel textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }

/* Toggle Switch */
.toggle-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; font-size: 12px; color: var(--text-dim); }
.switch { position: relative; width: 38px; height: 20px; flex-shrink: 0; }
.switch input { opacity: 0; width: 0; height: 0; }
.switch .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background: rgba(255,255,255,0.1); border-radius: 20px; transition: 0.3s; }
.switch .slider:before { content: ""; position: absolute; height: 14px; width: 14px; left: 3px; bottom: 3px; background: white; border-radius: 50%; transition: 0.3s; }
.switch input:checked + .slider { background: var(--accent); }
.switch input:checked + .slider:before { transform: translateX(18px); }

.color-row { display: flex; gap: 8px; margin-bottom: 12px; align-items: center; }
.color-row input[type="color"] { border: none; width: 28px; height: 28px; border-radius: 6px; cursor: pointer; background: none; }
.color-row span { font-size: 11px; color: var(--text-dim); }

/* Action Buttons */
.action-btn { width: 100%; padding: 10px; border: none; border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer; transition: all 0.2s; display: flex; align-items: center; justify-content: center; gap: 6px; margin-bottom: 6px; }
.action-btn:hover { transform: translateY(-1px); }
.action-btn.add { background: rgba(255,255,255,0.06); color: var(--text-dim); border: 1px dashed var(--border); }
.action-btn.add:hover { background: rgba(255,255,255,0.1); color: white; border-color: var(--accent); }
.action-btn.delete { background: rgba(239, 68, 68, 0.1); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.2); }
.action-btn.delete:hover { background: rgba(239, 68, 68, 0.2); }

.controls { display: flex; gap: 8px; }
.ctrl-btn { flex: 1; padding: 12px; border: none; border-radius: 10px; font-weight: 700; cursor: pointer; font-size: 13px; transition: all 0.2s; }
.ctrl-btn:hover { transform: translateY(-1px); }
.ctrl-btn.save { background: var(--gradient); color: white; box-shadow: 0 6px 16px -6px var(--accent-glow); flex: 2; }
.ctrl-btn.cancel { background: rgba(255,255,255,0.06); color: var(--text-dim); }
</style>
</head>
<body>

<!-- Settings View -->
<div id="settingsView">
  <div class="bg-blobs"><div class="blob"></div><div class="blob blob-2"></div></div>
  <div class="main-container">
    <div class="sidebar">
      <div class="logo"><div class="logo-icon">✦</div><div class="logo-text"><h1>PPCleaning</h1><p>Pro Webtoon Localizer</p></div></div>
      <div class="input-group"><label>مجلد الصور</label><div class="input-wrapper"><input type="text" id="inputDir" readonly placeholder="اختر المجلد..."><button class="browse-btn" onclick="browse('inputDir')">فتح</button></div></div>
      <div class="input-group"><label>مجلد التنظيف (اختياري)</label><div class="input-wrapper"><input type="text" id="cleanDir" readonly placeholder="صور بدون نص..."><button class="browse-btn" onclick="browse('cleanDir')">فتح</button></div></div>
      <div class="input-group"><label>مجلد الإخراج النهائي</label><div class="input-wrapper"><input type="text" id="outputDir" placeholder="مجلد الحفظ النهائي"><button class="browse-btn" onclick="browse('outputDir')">فتح</button></div></div>
      <div class="input-group"><label>العلامة المائية (اختياري)</label><div class="input-wrapper"><input type="text" id="wmPath" readonly placeholder="صورة العلامة..."><button class="browse-btn" onclick="browse('wmPath', true)">فتح</button></div></div>
      <div class="toggle-row"><label>الدمج الذكي (تحسين 5000px للتسريع)</label><input type="checkbox" id="smartStitch"></div>
    </div>
    <div class="content">
      <div class="console-container">
        <div class="console-header">
          <span>سجل العمليات</span>
          <button class="copy-btn" onclick="copyConsole(this)">نسخ السجل</button>
        </div>
        <div class="console-body" id="console">
          <div class="log-line info">✦ مرحباً بك في وضع التحرير الجديد!</div>
          <div class="log-line info">✦ سيقوم البوت باستخراج الترجمة أولاً، ثم سيعرضها لك لتتمكن من تعديل المربعات وتلوين الأسماء المهمة قبل الحفظ النهائي.</div>
        </div>
      </div>
      <button class="btn-primary" id="startBtn" onclick="startExtraction()">استخراج الترجمات ⚡</button>
    </div>
  </div>
</div>

<!-- Editor View -->
<div id="editorView">
  <div style="display: flex; flex-direction: column; flex: 1; min-width: 0;">
    <div class="page-nav-bar">
      <button class="page-nav-btn" id="prevPageBtn" onclick="navigatePage(-1)" title="الصفحة السابقة">◀</button>
      <div class="page-list" id="pageList"></div>
      <button class="page-nav-btn" id="nextPageBtn" onclick="navigatePage(1)" title="الصفحة التالية">▶</button>
      <div class="page-counter" id="pageCounter">0 / 0</div>
    </div>
    <div class="editor-main" id="editorMain" style="position: relative;">
      <div class="canvas-wrapper" id="canvasWrapper">
        <img id="canvasImg" class="canvas-img" src="">
        <div id="boxesLayer"></div>
      </div>
      <!-- Zoom Toolbar -->
      <div class="zoom-toolbar">
        <button class="zoom-btn" onclick="zoomChange(-0.1)" title="تصغير (-)">−</button>
        <div class="zoom-label" id="zoomLabel">100%</div>
        <button class="zoom-btn" onclick="zoomChange(0.1)" title="تكبير (+)">+</button>
        <div class="zoom-divider"></div>
        <button class="zoom-btn" onclick="zoomFit()" title="ملائمة العرض">⊞</button>
        <button class="zoom-btn" onclick="zoomReset()" title="الحجم الأصلي">1:1</button>
      </div>
    </div>
  </div>
  <div class="editor-sidebar">
    <div class="sidebar-header">
      <div class="logo"><div class="logo-icon">✎</div><div class="logo-text"><h1>المحرر المرئي</h1></div></div>
    </div>
    <div class="sidebar-body">
      <!-- Block List -->
      <div class="section-label">فقاعات النص <span class="badge" id="blockCount">0</span></div>
      <div class="block-list" id="blockList"></div>
      
      <div style="display:flex; gap:6px;">
        <button class="action-btn add" onclick="addBoxCenter()" style="flex:1">+ إضافة نص</button>
        <button class="action-btn add" onclick="addLogoCenter()" style="flex:1">🖼️ إضافة شعار</button>
      </div>

      <!-- Properties Panel -->
      <div class="prop-panel" id="propPanel">
        <div class="section-label" style="margin-top:0;" id="propTitle">تعديل الفقاعة المحددة</div>
        <div id="textOnlyProps">
          <textarea id="propText" oninput="updateSelectedBox()" placeholder="اكتب النص المترجم هنا..."></textarea>
          
          <div class="toggle-row">
            <span>حجم الخط (Size)</span>
            <input type="number" id="propFontSize" style="width:60px; background:rgba(0,0,0,0.5); border:1px solid var(--border); color:white; border-radius:6px; text-align:center;" value="28" oninput="updateSelectedBox()">
          </div>
        </div>
        
        <div class="toggle-row">
          <span>دوران (Rotation)</span>
          <div style="display:flex; gap:4px; align-items:center;">
            <input type="range" id="propRotation" min="-180" max="180" value="0" style="width:80px" oninput="document.getElementById('propRotInput').value = this.value; updateSelectedBox()">
            <input type="number" id="propRotInput" style="width:50px; background:rgba(0,0,0,0.5); border:1px solid var(--border); color:white; border-radius:6px; text-align:center;" value="0" oninput="document.getElementById('propRotation').value = this.value; updateSelectedBox()">
            <span style="color:var(--accent)">°</span>
          </div>
        </div>

        <div class="toggle-row" id="opacityProp">
          <span>الشفافية (Opacity)</span>
          <div style="display:flex; gap:4px; align-items:center;">
            <input type="range" id="propOpacity" min="0" max="1" step="0.05" value="1" style="width:80px" oninput="document.getElementById('propOpacityVal').innerText = Math.round(this.value*100) + '%'; updateSelectedBox()">
            <span id="propOpacityVal" style="font-size:11px; color:var(--accent); min-width:30px;">100%</span>
          </div>
        </div>

        <div id="textStyleProps">
          <div class="toggle-row">
            <span>المحاذاة (Align)</span>
            <select id="propAlign" style="background:rgba(0,0,0,0.5); border:1px solid var(--border); color:white; border-radius:6px; padding:2px; width:120px;" onchange="updateSelectedBox()">
              <option value="center">وسط (Center)</option>
              <option value="right">يمين (Right)</option>
              <option value="left">يسار (Left)</option>
            </select>
          </div>

          <div class="toggle-row">
            <span>نوع الخط (Font)</span>
            <select id="propFont" style="background:rgba(0,0,0,0.5); border:1px solid var(--border); color:white; border-radius:6px; padding:2px; width:120px;" onchange="updateSelectedBox()">
              <!-- Dynamic font options populated by JS -->
            </select>
          </div>
          
          <div class="toggle-row">
            <span>تباعد الأسطر (Line H)</span>
            <input type="number" id="propLineHeight" style="width:60px; background:rgba(0,0,0,0.5); border:1px solid var(--border); color:white; border-radius:6px; text-align:center;" step="0.1" value="1.2" oninput="updateSelectedBox()">
          </div>
          
          <div class="color-row" style="justify-content: space-between; margin-bottom: 8px;">
            <div style="display:flex; align-items:center; gap:6px;">
              <input type="color" id="propColor" value="#000000" oninput="updateSelectedBox()">
              <span>لون النص</span>
            </div>
            <div style="display:flex; align-items:center; gap:6px;">
              <input type="color" id="propOutline" value="#ffffff" oninput="updateSelectedBox()">
              <span>الإطار</span>
            </div>
          </div>
          
          <div class="toggle-row">
            <span>سمك الإطار (Stroke)</span>
            <div style="display:flex; gap:6px; align-items:center;">
              <input type="checkbox" id="propNoOutline" oninput="updateSelectedBox()" title="بدون إطار">
              <span style="font-size:10px">بدون</span>
              <input type="number" id="propOutlineWidth" style="width:50px; background:rgba(0,0,0,0.5); border:1px solid var(--border); color:white; border-radius:6px; text-align:center;" value="4" oninput="updateSelectedBox()">
            </div>
          </div>
          
          <div class="toggle-row">
            <span style="color: #3b82f6; font-weight: 600;">تدرج لوني للنص</span>
            <label class="switch"><input type="checkbox" id="propGrad" oninput="updateSelectedBox()"><span class="slider"></span></label>
          </div>
          <div class="color-row" id="colorRow" style="display:none; flex-direction:column; align-items:flex-start; margin-bottom:12px;">
            <div style="display:flex; justify-content: space-between; width:100%;">
              <span>ألوان التدرج:</span>
              <div style="display:flex; gap:4px;">
                <input type="color" id="gradC1" value="#8b5cf6" oninput="updateSelectedBox()">
                <input type="color" id="gradC2" value="#3b82f6" oninput="updateSelectedBox()">
              </div>
            </div>
            <div style="display:flex; justify-content:space-between; width:100%; align-items:center;">
               <span style="font-size:11px; color:var(--text-dim);">مقياس التدرج (النسبة)</span>
               <div style="display:flex; gap:4px; align-items:center;">
                 <input type="range" id="gradScale" min="10" max="200" value="100" style="width:80px" oninput="document.getElementById('gradScaleVal').innerText = this.value + '%'; updateSelectedBox()">
                 <span id="gradScaleVal" style="font-size:11px; color:var(--accent); min-width:30px;">100%</span>
               </div>
            </div>
          </div>
        </div>
        
        <button class="action-btn delete" onclick="deleteSelectedBox()">🗑 حذف المربع</button>
      </div>
    </div>
    <div class="sidebar-footer">
      <div class="controls">
        <button class="ctrl-btn save" onclick="startRendering()">حفظ وتصدير ✅</button>
        <button class="ctrl-btn cancel" onclick="cancelEditor()">إلغاء</button>
      </div>
    </div>
  </div>
</div>

<script>
let polling = null;
let projectData = null;
let currentPageIdx = 0;
let selectedBoxId = null;
let currentZoom = 1.0;

const ALL_FONTS = /* DYNAMIC_FONTS_JSON */;

// Find Hayah or default to the first scanned font
let defaultFont = 'My fonts/Hayah.otf';
if (ALL_FONTS.length > 0) {
  const hasHayah = ALL_FONTS.some(f => f.rel_path === defaultFont);
  if (!hasHayah) {
    defaultFont = ALL_FONTS[0].rel_path;
  }
}

let globalStyle = {
  font_size: 28, rotation: 0, color: '#000000', outline_color: '#ffffff', outline_width: 4, line_height: 1.2, font: defaultFont, align: 'center', gradient_scale: 100, is_gradient: false, opacity: 1.0
};

// Async font loading to bypass WebView2 string limits by injecting @font-face rules dynamically
async function loadAllFonts() {
  const styleEl = document.createElement('style');
  styleEl.id = 'dynamic-fonts-style';
  document.head.appendChild(styleEl);
  
  let cssContent = "";
  for (const f of ALL_FONTS) {
    try {
      const b64 = await pywebview.api.get_font_base64(f.rel_path);
      if (b64) {
        const ext = f.rel_path.toLowerCase().endsWith('.ttf') ? 'ttf' : 'otf';
        cssContent += `@font-face {
  font-family: '${f.css_name}';
  src: url('data:font/${ext};base64,${b64}');
}\n`;
      }
    } catch (e) {
      console.error("Failed to load font: " + f.display_name, e);
    }
  }
  styleEl.textContent = cssContent;
  
  // If editor is already active, force re-rendering of visible text boxes to apply the new fonts
  if (typeof projectData !== 'undefined' && projectData && typeof renderBoxes === 'function') {
    renderBoxes();
  }
}

// Wait until pywebview has fully loaded and injected its API
window.addEventListener('pywebviewready', () => {
  loadAllFonts();
});

function initFontSelect() {
  const select = document.getElementById('propFont');
  if (!select) return;
  select.innerHTML = "";
  ALL_FONTS.forEach(f => {
    const opt = document.createElement('option');
    opt.value = f.rel_path;
    opt.textContent = f.display_name;
    select.appendChild(opt);
  });
}
initFontSelect();

let logoBase64 = null;

// Zoom Controls
function applyZoom() {
  const wrapper = document.getElementById('canvasWrapper');
  if (!wrapper) return;
  wrapper.style.transform = `scale(${currentZoom})`;
  document.getElementById('zoomLabel').textContent = Math.round(currentZoom * 100) + '%';
}
function zoomChange(delta) {
  currentZoom = Math.max(0.2, Math.min(3.0, currentZoom + delta));
  applyZoom();
}
function zoomFit() {
  const main = document.getElementById('editorMain');
  const img = document.getElementById('canvasImg');
  if (!main || !img || !img.naturalWidth) return;
  const fitW = (main.clientWidth - 80) / img.naturalWidth;
  const fitH = (main.clientHeight - 80) / img.naturalHeight;
  currentZoom = Math.min(fitW, fitH, 1.0);
  applyZoom();
}
function zoomReset() {
  currentZoom = 1.0;
  applyZoom();
}

// Extractor Logic
function addLog(msg) {
  const container = document.getElementById('console');
  if(!container) return;
  const div = document.createElement('div');
  div.className = 'log-line';
  if(msg.includes('✅')||msg.includes('نجاح')) div.className += ' success';
  else if(msg.includes('❌')||msg.includes('خطأ')||msg.includes('⚠️')) div.className += ' error';
  else div.className += ' info';
  div.textContent = msg;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function copyConsole(btn) {
  const container = document.getElementById('console');
  if(!container) return;
  const text = Array.from(container.querySelectorAll('.log-line')).map(div => div.textContent).join('\n');
  
  const showSuccess = () => {
    const originalText = btn.textContent;
    btn.textContent = 'تم النسخ!';
    btn.style.color = 'var(--success)';
    setTimeout(() => {
      btn.textContent = originalText;
      btn.style.color = '';
    }, 2000);
  };

  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(showSuccess).catch(err => console.error(err));
  } else {
    // Fallback for non-secure contexts or webviews
    const textArea = document.createElement("textarea");
    textArea.value = text;
    textArea.style.position = "fixed";
    textArea.style.left = "-999999px";
    textArea.style.top = "-999999px";
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();
    try {
      document.execCommand('copy');
      showSuccess();
    } catch (err) {
      console.error('Fallback: Oops, unable to copy', err);
    }
    textArea.remove();
  }
}

async function browse(id, isFile = false) {
  try {
    const path = isFile ? await pywebview.api.select_file() : await pywebview.api.select_folder();
    if(path) document.getElementById(id).value = path;
  } catch(e) {}
}

async function startExtraction() {
  const inDir = document.getElementById('inputDir').value;
  const clDir = document.getElementById('cleanDir').value;
  const wmPath = document.getElementById('wmPath').value;
  const smart = document.getElementById('smartStitch').checked;
  if(!inDir) { addLog("❌ يرجى اختيار مجلد الصور أولاً!"); return; }

  document.getElementById('startBtn').disabled = true;
  document.getElementById('console').innerHTML = "";
  addLog("🚀 بدء عملية استخراج النصوص والترجمة...");

  const response = await pywebview.api.start_extraction(inDir, clDir, "Arabic", 5000, smart, wmPath);
  const result = JSON.parse(response);
  if(result.error) { addLog("❌ " + result.error); document.getElementById('startBtn').disabled = false; return; }

  polling = setInterval(async () => {
    try {
      const logsData = JSON.parse(await pywebview.api.poll_logs());
      for (const log of logsData.logs) {
        if(log === "__EXTRACTION_DONE__") {
          clearInterval(polling);
          await loadProjectData();
          return;
        }
        addLog(log);
      }
    } catch(e) {}
  }, 500);
}

// Editor Logic
async function loadProjectData() {
  addLog("📦 جاري تحميل المحرر المرئي...");
  const resp = await pywebview.api.get_project_data();
  projectData = JSON.parse(resp);
  if(!projectData || projectData.error) {
    addLog("❌ فشل تحميل بيانات المشروع.");
    document.getElementById('startBtn').disabled = false;
    return;
  }
  
  document.getElementById('settingsView').style.display = 'none';
  document.getElementById('editorView').style.display = 'flex';
  
  if (projectData.watermark_path) {
    logoBase64 = await pywebview.api.get_image_base64(projectData.watermark_path);
  }
  
  // Render thumbnails
  const list = document.getElementById('pageList');
  list.innerHTML = "";
  
  // Enable horizontal scrolling with mouse wheel
  list.onwheel = (evt) => {
    evt.preventDefault();
    list.scrollLeft += evt.deltaY;
  };

  for(let i=0; i<projectData.pages.length; i++) {
    const thumb = document.createElement('div');
    thumb.className = 'page-thumb';
    thumb.textContent = `${i+1}`;
    thumb.onclick = () => loadPage(i);
    list.appendChild(thumb);
  }
  loadPage(0);
}

function updateNavButtons() {
  if (!projectData) return;
  const total = projectData.pages.length;
  document.getElementById('prevPageBtn').disabled = (currentPageIdx <= 0);
  document.getElementById('nextPageBtn').disabled = (currentPageIdx >= total - 1);
  document.getElementById('pageCounter').textContent = `${currentPageIdx + 1} / ${total}`;
}

function navigatePage(delta) {
  if (!projectData) return;
  const newIdx = currentPageIdx + delta;
  if (newIdx >= 0 && newIdx < projectData.pages.length) {
    loadPage(newIdx);
  }
}

// Keyboard navigation for pages
document.addEventListener('keydown', (e) => {
  if (!projectData || document.activeElement.tagName === 'TEXTAREA' || document.activeElement.tagName === 'INPUT') return;
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
    // In RTL layout: ArrowRight = previous, ArrowLeft = next
    const delta = e.key === 'ArrowLeft' ? 1 : -1;
    navigatePage(delta);
    e.preventDefault();
  }
  if (e.key === 'Delete' && selectedBoxId) {
    deleteSelectedBox();
    e.preventDefault();
  }
});

async function loadPage(idx) {
  currentPageIdx = idx;
  selectedBoxId = null;
  document.getElementById('propPanel').classList.remove('active');
  
  const thumbs = document.querySelectorAll('.page-thumb');
  thumbs.forEach(t => t.classList.remove('active'));
  if (thumbs[idx]) {
    thumbs[idx].classList.add('active');
    thumbs[idx].scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
  }
  
  updateNavButtons();

  const page = projectData.pages[idx];
  const imgPath = page.clean_path ? page.clean_path : page.raw_path;
  
  // To avoid CORS/Local restrictions, request Base64 from Python
  const b64 = await pywebview.api.get_image_base64(imgPath);
  const canvasImg = document.getElementById('canvasImg');
  canvasImg.src = b64;
  
  // Scroll editor to top when changing pages
  document.getElementById('editorMain').scrollTop = 0;
  
  canvasImg.onload = () => {
    renderBoxes();
  };
}

let _isResizing = false;

function applyBoxStyles(b, boxElem) {
  if (!boxElem) return;
  
  boxElem.style.left = b.x + 'px';
  boxElem.style.top = b.y + 'px';
  boxElem.style.width = b.w + 'px';
  boxElem.style.height = b.h + 'px';
  boxElem.style.transform = `rotate(${b.rotation}deg)`;
  boxElem.style.opacity = b.opacity !== undefined ? b.opacity : 1.0;

  if (b.type === 'logo') {
    const img = boxElem.querySelector('.logo-box-img');
    if (img && logoBase64) img.src = logoBase64;
    return;
  }

  const text = boxElem.querySelector('.bubble-text');
  if (!text) return;

  boxElem.classList.toggle('gradient', !!b.is_gradient);

  text.textContent = b.text;
  const fontCfg = ALL_FONTS.find(f => {
    if (!b.font) return false;
    const bName = b.font.split('/').pop().split('\\').pop().toLowerCase();
    const fName = f.rel_path.split('/').pop().split('\\').pop().toLowerCase();
    return f.rel_path === b.font || bName === fName;
  });
  if (fontCfg) {
    text.style.fontFamily = `'${fontCfg.css_name}'`;
  } else {
    const fallbackCfg = ALL_FONTS.find(f => f.rel_path === defaultFont);
    text.style.fontFamily = fallbackCfg ? `'${fallbackCfg.css_name}'` : "sans-serif";
  }
  text.style.fontSize = b.font_size + 'px';
  text.style.lineHeight = b.line_height;
  text.style.textAlign = b.align || 'center';

  if(b.is_gradient && b.gradient_colors && b.gradient_colors.length === 2) {
    const scale = b.gradient_scale !== undefined ? b.gradient_scale : 100;
    text.style.backgroundImage = `linear-gradient(to bottom, ${b.gradient_colors[0]} 0%, ${b.gradient_colors[1]} ${scale}%)`;
    text.style.webkitBackgroundClip = 'text';
    text.style.webkitTextFillColor = 'transparent';
    text.style.color = 'transparent';
  } else {
    text.style.backgroundImage = 'none';
    text.style.webkitBackgroundClip = 'unset';
    text.style.webkitTextFillColor = b.color;
    text.style.color = b.color;
  }

  if (b.outline_width > 0) {
    text.style.webkitTextStrokeWidth = b.outline_width + 'px';
    text.style.webkitTextStrokeColor = b.outline_color;
  } else {
    text.style.webkitTextStrokeWidth = '0px';
    text.style.webkitTextStrokeColor = 'transparent';
  }
}

function renderBoxes() {
  const layer = document.getElementById('boxesLayer');
  layer.innerHTML = "";
  const list = document.getElementById('blockList');
  if (list) list.innerHTML = "";
  
  const page = projectData.pages[currentPageIdx];
  if (page) {
    document.getElementById('blockCount').textContent = page.blocks.length;
  }
  
  page.blocks.forEach((b, idx) => {
    // defaults if missing
    if(b.font_size === undefined) {
      b.font_size = globalStyle.font_size;
      b.rotation = globalStyle.rotation;
      b.color = b.is_dark ? '#ffffff' : globalStyle.color; 
      b.outline_color = b.is_dark ? '#000000' : globalStyle.outline_color;
      b.outline_width = globalStyle.outline_width;
      b.line_height = globalStyle.line_height;
      b.font = globalStyle.font;
      b.align = globalStyle.align;
      b.gradient_scale = globalStyle.gradient_scale;
      b.is_gradient = globalStyle.is_gradient;
      b.is_custom = false;
    }

    const box = document.createElement('div');
    box.className = 'bubble-box';
    box.id = `box_el_${b.id}`;
    if(b.id === selectedBoxId) box.classList.add('selected');
    
    if (b.type === 'logo') {
      const img = document.createElement('img');
      img.className = 'logo-box-img';
      box.appendChild(img);
    } else {
      const text = document.createElement('div');
      text.className = 'bubble-text';
      box.appendChild(text);
    }
    
    // Use the helper to apply all styles
    applyBoxStyles(b, box);
    
    box.dataset.id = b.id;
    
    // Resize handles
    ['tl', 'tr', 'bl', 'br'].forEach(pos => {
      const h = document.createElement('div');
      h.className = `resize-handle handle-${pos}`;
      h.addEventListener('mousedown', (e) => {
        e.preventDefault();
        e.stopPropagation();
        startResize(e, b, pos, box);
      });
      box.appendChild(h);
    });
    
    // Drag on box body
    box.addEventListener('mousedown', (e) => {
      if (_isResizing) return;
      e.preventDefault();
      startDrag(e, b, box);
    });
    
    layer.appendChild(box);
    
    // Sidebar List Item
    if (list) {
      const item = document.createElement('div');
      item.className = 'block-item';
      item.id = `list_item_${b.id}`;
      if(b.id === selectedBoxId) item.classList.add('active');
      item.dataset.id = b.id;
      item.onclick = () => {
        selectBox(b);
        document.getElementById('editorMain').scrollTo({
          top: (b.y * currentZoom) - 100,
          left: (b.x * currentZoom) - 100,
          behavior: 'smooth'
        });
      };
      
      const num = document.createElement('div');
      num.className = 'block-item-num';
      num.textContent = idx + 1;
      
      const lbl = document.createElement('div');
      lbl.className = 'block-item-text';
      lbl.textContent = b.type === 'logo' ? "🖼️ شعار" : (b.text || "مربع فارغ");
      
      item.appendChild(num);
      item.appendChild(lbl);
      list.appendChild(item);
    }
  });
}

function selectBox(b) {
  selectedBoxId = b.id;
  
  // "Remember Changes" Logic: If this box hasn't been customized, 
  // inherit the current global style (last used settings)
  if (!b.is_custom) {
    b.font_size = globalStyle.font_size;
    b.rotation = globalStyle.rotation;
    b.color = globalStyle.color; 
    b.outline_color = globalStyle.outline_color;
    b.outline_width = globalStyle.outline_width;
    b.line_height = globalStyle.line_height;
    b.font = globalStyle.font;
    b.align = globalStyle.align;
    b.is_gradient = globalStyle.is_gradient;
    b.gradient_scale = globalStyle.gradient_scale;
  }

  document.querySelectorAll('.bubble-box').forEach(el => {
    el.classList.toggle('selected', el.dataset.id === b.id);
  });
  document.querySelectorAll('.block-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === b.id);
  });
  
  // Visually apply styles to the box (Inherited or Custom)
  applyBoxStyles(b, document.getElementById(`box_el_${b.id}`));
  
  const isLogo = b.type === 'logo';
  document.getElementById('propTitle').innerText = isLogo ? "تعديل الشعار المحدد" : "تعديل الفقاعة المحددة";
  document.getElementById('textOnlyProps').style.display = isLogo ? 'none' : 'block';
  document.getElementById('textStyleProps').style.display = isLogo ? 'none' : 'block';
  
  const panel = document.getElementById('propPanel');
  panel.classList.add('active');
  
  if (!isLogo) {
    document.getElementById('propText').value = b.text;
    document.getElementById('propFontSize').value = b.font_size;
    document.getElementById('propLineHeight').value = b.line_height;
    document.getElementById('propColor').value = b.color;
    document.getElementById('propOutline').value = b.outline_color;
    document.getElementById('propOutlineWidth').value = b.outline_width;
    document.getElementById('propOutlineWidth').disabled = (b.outline_width === 0);
    document.getElementById('propNoOutline').checked = (b.outline_width === 0);
    document.getElementById('propFont').value = b.font || globalStyle.font;
    document.getElementById('propAlign').value = b.align || 'center';
    document.getElementById('propGrad').checked = b.is_gradient;
    document.getElementById('colorRow').style.display = b.is_gradient ? 'flex' : 'none';
    if(b.gradient_colors && b.gradient_colors.length === 2) {
      document.getElementById('gradC1').value = b.gradient_colors[0];
      document.getElementById('gradC2').value = b.gradient_colors[1];
    }
    const scale = b.gradient_scale !== undefined ? b.gradient_scale : 100;
    document.getElementById('gradScale').value = scale;
    document.getElementById('gradScaleVal').innerText = scale + '%';
  }
  document.getElementById('propRotation').value = b.rotation;
  document.getElementById('propRotInput').value = b.rotation;
  
  const opacity = b.opacity !== undefined ? b.opacity : 1.0;
  document.getElementById('propOpacity').value = opacity;
  document.getElementById('propOpacityVal').innerText = Math.round(opacity * 100) + '%';
}

function updateSelectedBox() {
  if(!selectedBoxId) return;
  const page = projectData.pages[currentPageIdx];
  const b = page.blocks.find(x => x.id === selectedBoxId);
  if(!b) return;
  
  b.is_custom = true;
  
  if (b.type !== 'logo') {
    b.text = document.getElementById('propText').value;
    b.font_size = parseFloat(document.getElementById('propFontSize').value) || 28;
    b.line_height = parseFloat(document.getElementById('propLineHeight').value) || 1.2;
    b.color = document.getElementById('propColor').value;
    b.outline_color = document.getElementById('propOutline').value;
    
    let noOutline = document.getElementById('propNoOutline').checked;
    b.outline_width = noOutline ? 0 : (parseFloat(document.getElementById('propOutlineWidth').value) || 4);
    document.getElementById('propOutlineWidth').disabled = noOutline;
    
    b.font = document.getElementById('propFont').value;
    b.align = document.getElementById('propAlign').value;

    b.is_gradient = document.getElementById('propGrad').checked;
    document.getElementById('colorRow').style.display = b.is_gradient ? 'flex' : 'none';
    
    if(b.is_gradient) {
      document.getElementById('gradC1').value = b.color;
    }
    
    b.gradient_colors = [document.getElementById('gradC1').value, document.getElementById('gradC2').value];
    b.gradient_scale = parseFloat(document.getElementById('gradScale').value) || 100;
  }

  b.rotation = parseFloat(document.getElementById('propRotation').value) || 0;
  document.getElementById('propRotInput').value = b.rotation;
  b.opacity = parseFloat(document.getElementById('propOpacity').value) || 1.0;
  
  const boxEl = document.getElementById(`box_el_${b.id}`);
  applyBoxStyles(b, boxEl);
  
  const listItem = document.getElementById(`list_item_${b.id}`);
  if(listItem) {
    const lbl = listItem.querySelector('.block-item-text');
    if(lbl) lbl.textContent = b.type === 'logo' ? "🖼️ شعار" : (b.text || "مربع فارغ");
  }
  
  if (b.type !== 'logo') {
    globalStyle = {
      font_size: b.font_size, rotation: b.rotation, color: b.color, 
      outline_color: b.outline_color, outline_width: b.outline_width, 
      line_height: b.line_height, font: b.font, align: b.align,
      gradient_scale: b.gradient_scale, is_gradient: b.is_gradient, opacity: b.opacity
    };
  } else {
    globalStyle.rotation = b.rotation;
    globalStyle.opacity = b.opacity;
  }
}

function addLogoCenter() {
  const wmPath = document.getElementById('wmPath').value;
  if (!wmPath && !projectData.watermark_path) {
    alert("يرجى اختيار مسار الشعار (العلامة المائية) في الإعدادات أولاً.");
    return;
  }
  
  // Update watermark path if it was changed in settings but not reflected in projectData
  if (wmPath && wmPath !== projectData.watermark_path) {
    projectData.watermark_path = wmPath;
    logoBase64 = null; // Force reload
  }

  (async () => {
    if (!logoBase64 && projectData.watermark_path) {
      logoBase64 = await pywebview.api.get_image_base64(projectData.watermark_path);
    }
    
    if (!logoBase64) {
      alert("فشل تحميل صورة الشعار. تأكد من صحة المسار.");
      return;
    }

    const page = projectData.pages[currentPageIdx];
  const wrapper = document.getElementById('canvasWrapper');
  const main = document.getElementById('editorMain');
  
  const imgRect = wrapper.getBoundingClientRect();
  const mainRect = main.getBoundingClientRect();
  
  const viewportCenterX = mainRect.left + mainRect.width / 2;
  const viewportCenterY = mainRect.top + mainRect.height / 2;
  
  let targetX = (viewportCenterX - imgRect.left) / currentZoom;
  let targetY = (viewportCenterY - imgRect.top) / currentZoom;
  
  if (targetY < 0) targetY = 200;
  if (targetX < 0) targetX = 200;

  const newBox = {
    id: "logo_" + Date.now(),
    type: "logo",
    x: targetX - 75, y: targetY - 75,
    w: 150, h: 150,
    cx: targetX, cy: targetY,
    rotation: globalStyle.rotation,
    opacity: globalStyle.opacity !== undefined ? globalStyle.opacity : 1.0
  };
  page.blocks.push(newBox);
  renderBoxes();
  selectBox(newBox);
  })();
}

function addBoxCenter() {
  const page = projectData.pages[currentPageIdx];
  const wrapper = document.getElementById('canvasWrapper');
  const main = document.getElementById('editorMain');
  
  const imgRect = wrapper.getBoundingClientRect();
  const mainRect = main.getBoundingClientRect();
  
  const viewportCenterX = mainRect.left + mainRect.width / 2;
  const viewportCenterY = mainRect.top + mainRect.height / 2;
  
  let targetX = (viewportCenterX - imgRect.left) / currentZoom;
  let targetY = (viewportCenterY - imgRect.top) / currentZoom;
  
  if (targetY < 0) targetY = 200;
  if (targetX < 0) targetX = 200;

  const newBox = {
    id: "custom_" + Date.now(), text: "نص جديد هنا",
    x: targetX - 100, y: targetY - 50,
    w: 200, h: 100,
    cx: targetX, cy: targetY,
    font_size: globalStyle.font_size, rotation: globalStyle.rotation, 
    color: globalStyle.color, outline_color: globalStyle.outline_color, 
    outline_width: globalStyle.outline_width, line_height: globalStyle.line_height,
    font: globalStyle.font, align: globalStyle.align,
    is_gradient: false, gradient_colors: ["#8b5cf6", "#3b82f6"], gradient_scale: globalStyle.gradient_scale
  };
  page.blocks.push(newBox);
  renderBoxes();
  selectBox(newBox);
}

function deleteSelectedBox() {
  if(!selectedBoxId) return;
  const page = projectData.pages[currentPageIdx];
  page.blocks = page.blocks.filter(x => x.id !== selectedBoxId);
  selectedBoxId = null;
  document.getElementById('propPanel').classList.remove('active');
  renderBoxes();
}

// ── Drag Logic ──
function startDrag(e, b, boxElem) {
  selectBox(b);
  const startX = e.clientX, startY = e.clientY;
  const origX = b.x, origY = b.y;

  function onMove(ev) {
    const dx = (ev.clientX - startX) / currentZoom;
    const dy = (ev.clientY - startY) / currentZoom;
    b.x = origX + dx;
    b.y = origY + dy;
    boxElem.style.left = b.x + 'px';
    boxElem.style.top = b.y + 'px';
  }
  function onUp() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    b.cx = b.x + b.w / 2;
    b.cy = b.y + b.h / 2;
  }
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

// ── Resize Logic ──
function startResize(e, b, handle, boxElem) {
  _isResizing = true;
  selectBox(b);
  const startX = e.clientX, startY = e.clientY;
  const origX = b.x, origY = b.y, origW = b.w, origH = b.h;

  function onMove(ev) {
    const dx = (ev.clientX - startX) / currentZoom;
    const dy = (ev.clientY - startY) / currentZoom;

    if (handle === 'br') {
      b.w = Math.max(30, origW + dx);
      b.h = Math.max(20, origH + dy);
    } else if (handle === 'bl') {
      b.w = Math.max(30, origW - dx);
      b.x = origX + origW - b.w;
      b.h = Math.max(20, origH + dy);
    } else if (handle === 'tr') {
      b.w = Math.max(30, origW + dx);
      b.h = Math.max(20, origH - dy);
      b.y = origY + origH - b.h;
    } else if (handle === 'tl') {
      b.w = Math.max(30, origW - dx);
      b.x = origX + origW - b.w;
      b.h = Math.max(20, origH - dy);
      b.y = origY + origH - b.h;
    }

    boxElem.style.left = b.x + 'px';
    boxElem.style.top = b.y + 'px';
    boxElem.style.width = b.w + 'px';
    boxElem.style.height = b.h + 'px';
  }
  function onUp() {
    _isResizing = false;
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    document.body.style.cursor = '';
    b.cx = b.x + b.w / 2;
    b.cy = b.y + b.h / 2;
  }
  const cursorMap = { tl: 'nw-resize', tr: 'ne-resize', bl: 'sw-resize', br: 'se-resize' };
  document.body.style.cursor = cursorMap[handle];
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

async function startRendering() {
  const outDir = document.getElementById('outputDir').value;
  const wmPath = document.getElementById('wmPath').value;
  if(!outDir) { alert("يرجى العودة واختيار مجلد الإخراج أولاً."); return; }
  
  document.getElementById('editorView').style.display = 'none';
  document.getElementById('settingsView').style.display = 'flex';
  addLog("🚀 بدء تطبيق التعديلات والرسم النهائي...");
  
  const resp = await pywebview.api.render_project(JSON.stringify(projectData), outDir, "Arabic", wmPath, 40, 8);
  const result = JSON.parse(resp);
  if(result.error) { addLog("❌ " + result.error); document.getElementById('startBtn').disabled = false; return; }

  polling = setInterval(async () => {
    try {
      const logsData = JSON.parse(await pywebview.api.poll_logs());
      for (const log of logsData.logs) {
        if(log.startsWith("__RENDER_DONE__")) {
          const parts = log.split("::");
          const finalPath = parts.length > 1 ? parts[1] : outDir;
          clearInterval(polling);
          addLog("🎉 تمت العملية بالكامل! يمكنك فتح مجلد الإخراج الآن.");
          document.getElementById('startBtn').disabled = false;
          pywebview.api.open_folder(finalPath);
          return;
        }
        addLog(log);
      }
    } catch(e) {}
  }, 500);
}

function cancelEditor() {
  document.getElementById('editorView').style.display = 'none';
  document.getElementById('settingsView').style.display = 'flex';
  document.getElementById('startBtn').disabled = false;
  addLog("🛑 تم إلغاء العملية.");
}
</script>
</body>
</html>'''

def main():
    api = Api()
    
    # Resolve absolute path for font loading
    app_dir = os.path.dirname(os.path.abspath(__file__))
    
    def get_font_b64(rel_path):
        try:
            full_path = os.path.join(app_dir, rel_path)
            with open(full_path, "rb") as f:
                return base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to load font {rel_path}: {e}")
            return ""

    # Dynamic font scanning
    found_fonts = []
    for root, dirs, files in os.walk(app_dir):
        # Ignore hidden/unwanted directories
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'chrome_profile', 'scratch')]
        for file in files:
            if file.lower().endswith(('.ttf', '.otf')):
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, app_dir)
                rel_path = rel_path.replace('\\', '/')
                found_fonts.append(rel_path)
    
    # Sort fonts by filename naturally
    found_fonts.sort(key=lambda s: os.path.basename(s).lower())
    
    fonts_metadata = []
    for rel_path in found_fonts:
        basename = os.path.basename(rel_path)
        display_name, _ = os.path.splitext(basename)
        display_name = display_name.replace('-', ' ').replace('_', ' ')
        css_name = rel_path.replace('/', '_').replace('\\', '_').replace('.', '_').replace(' ', '_').replace('-', '_')
        
        fonts_metadata.append({
            'rel_path': rel_path,
            'display_name': display_name,
            'css_name': css_name
        })

    final_html = HTML
    final_html = final_html.replace("/* DYNAMIC_FONTS_CSS */", "") # Dynamic loading handled by CSS Font Loading API in JS
    final_html = final_html.replace("/* DYNAMIC_FONTS_JSON */", json.dumps(fonts_metadata, ensure_ascii=False))

    window = webview.create_window(
        'PPCleaning — Webtoon Pro',
        html=final_html,
        js_api=api,
        width=940,
        height=760,
        min_size=(800, 600),
        background_color='#030305',
    )
    state.window = window
    window.events.closing += on_closing
    
    # Start tray in a separate thread
    threading.Thread(target=setup_tray, daemon=True).start()
    
    webview.start(debug=False, private_mode=False)

if __name__ == '__main__':
    if not single_instance_checker():
        sys.exit(0)
    main()
