import base64
import os

fonts = [
    ('Blaka', 'My fonts/Blaka-Regular.ttf'),
    ('Changa', 'My fonts/Changa-VariableFont_wght.ttf'),
    ('ElMessiri', 'My fonts/ElMessiri-VariableFont_wght.ttf'),
    ('Janna', 'My fonts/Janna LT Regular.ttf'),
    ('Zain', 'Zain/Zain-Regular.ttf')
]

print("FONTS_BASE64 = {")
for name, path in fonts:
    if os.path.exists(path):
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode('utf-8')
            print(f"    '{name}': '{b64}',")
print("}")
