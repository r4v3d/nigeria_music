import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
PROFILE_DIR_MAIN = SCRIPT_DIR / "perfiles" / "principal"

def dump_elements():
    print("Iniciando volcado de elementos en TuneMyMusic...")
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
        
        print(f"\nURL actual del navegador: {page.url}")
        print(f"Título actual del navegador: {page.title()}")
        print(f"Número total de frames: {len(page.frames)}")
        
        # 1. Mostrar texto del body principal
        try:
            body_text = page.locator("body").inner_text()
            print("\n=== TEXTO COMPLETO DEL BODY PRINCIPAL (Primeros 500 caracteres) ===")
            print(body_text[:500])
            print("==================================================================")
        except Exception as e:
            print(f"Error al leer body principal: {e}")
            
        # 2. Buscar en todos los frames
        for f_idx, frame in enumerate(page.frames):
            print(f"\n--- Analizando Frame [{f_idx}] (Name: '{frame.name}', URL: '{frame.url}') ---")
            try:
                # Buscar por texto "TIDAL"
                elements_by_text = frame.locator("text=TIDAL").all()
                print(f"  Encontrados {len(elements_by_text)} elementos con texto 'TIDAL'")
                for idx, el in enumerate(elements_by_text):
                    tag = el.evaluate("el => el.tagName")
                    html = el.evaluate("el => el.outerHTML")
                    text = el.evaluate("el => el.innerText")
                    print(f"    [{idx}] Tag: {tag} | Text: '{text}' | OuterHTML: {html[:150]}")
                    
                # Buscar cualquier elemento con data-source
                data_sources = frame.locator("[data-source]").all()
                print(f"  Encontrados {len(data_sources)} elementos con [data-source]")
                for idx, el in enumerate(data_sources):
                    ds = el.get_attribute("data-source")
                    tag = el.evaluate("el => el.tagName")
                    print(f"    [{idx}] data-source: '{ds}' | Tag: {tag}")
            except Exception as e:
                print(f"  Error al analizar frame {f_idx}: {e}")
                
        context.close()

if __name__ == "__main__":
    dump_elements()
