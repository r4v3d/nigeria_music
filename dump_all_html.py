import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
PROFILE_DIR_MAIN = SCRIPT_DIR / "perfiles" / "principal"

def dump_html():
    print("Iniciando volcado completo del HTML...")
    with sync_playwright() as p:
        launch_args = ["--disable-blink-features=AutomationControlled"]
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR_MAIN),
            channel="chrome",
            headless=False,
            args=launch_args,
            viewport={"width": 1280, "height": 800},
            locale="es-ES"
        )
        
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.tunemymusic.com/es/transfer", wait_until="networkidle")
        time.sleep(5.0)
        
        # Guardar el HTML del body
        html_content = page.locator("body").evaluate("el => el.innerHTML")
        with open("tmm_body.html", "w", encoding="utf-8") as f:
            f.write(html_content)
            
        print("HTML guardado en 'tmm_body.html'.")
        context.close()

if __name__ == "__main__":
    dump_html()
