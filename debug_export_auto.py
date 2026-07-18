import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
PROFILE_DIR_MAIN = SCRIPT_DIR / "perfiles" / "principal"
DOWNLOADS_DIR = SCRIPT_DIR / "descargas"

# Importamos las funciones necesarias del script principal
from migrar_cuentas_tidal import autorizar_tidal_en_tmm, aceptar_cookies_con_espera, hacer_click_por_textos, esperar_visibilidad

def test_exportar_csv():
    print("Iniciando depuración automática del botón Exportar...")
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
        
        while len(context.pages) > 1:
            try:
                context.pages[-1].close()
            except Exception:
                pass
            
        tmm_page = context.pages[0] if context.pages else context.new_page()
        tmm_page.bring_to_front()
        
        try:
            tmm_page.goto("https://www.tunemymusic.com/es/transfer", wait_until="domcontentloaded")
            time.sleep(2.0)
            aceptar_cookies_con_espera(tmm_page)
            
            btn_tidal = tmm_page.locator("button[name='Tidal']").or_(tmm_page.locator("button[aria-label='TIDAL']")).first
            esperar_visibilidad(btn_tidal, 5000)
            btn_tidal.click()
            time.sleep(2.0)
            
            autorizar_tidal_en_tmm(tmm_page)
            
            hacer_click_por_textos(tmm_page, ["CARGAR DESDE CUENTA TIDAL", "Cargar desde cuenta TIDAL", "Cargar desde cuenta", "CARGAR DESDE CUENTA"])
            time.sleep(4.0)
            
            hacer_click_por_textos(tmm_page, ["ELEGIR DESTINO", "Elegir destino", "Elige destino", "Elige Destino", "SELECT DESTINATION", "Select destination", "Choose destination"])
            time.sleep(3.0)
            
            btn_archivo = tmm_page.locator("button[name='ToFile']").or_(tmm_page.locator("button[aria-label='Exportar archivo']")).first
            esperar_visibilidad(btn_archivo, 4000)
            btn_archivo.click()
            time.sleep(2.0)
            
            opt_csv = tmm_page.locator("input[value='csv']").or_(tmm_page.locator("label:has-text('CSV')")).or_(tmm_page.locator("text=CSV")).first
            opt_csv.click()
            time.sleep(2.0)
            
            # Aquí es donde se abre la ventana flotante. Vamos a inspeccionar todos los botones que estén visibles!
            print("\n=== ELEMENTOS EN EL MODAL FLOTANTE ===")
            buttons = tmm_page.locator("button, [role='button'], input[type='button'], input[type='submit']").all()
            for idx, btn in enumerate(buttons):
                try:
                    text = btn.evaluate("el => el.innerText")
                    html = btn.evaluate("el => el.outerHTML")
                    vis = btn.is_visible()
                    print(f"Botón [{idx}]: Visible={vis} | Text='{text.strip()}'")
                    print(f"  OuterHTML: {html[:250]}")
                except Exception as e:
                    print(f"Error en botón [{idx}]: {e}")
                    
            # También vamos a buscar texto que contenga "Exportar"
            exports = tmm_page.locator("text=Exportar").all()
            print(f"\nElementos con texto 'Exportar' ({len(exports)}):")
            for idx, exp in enumerate(exports):
                try:
                    html = exp.evaluate("el => el.outerHTML")
                    print(f"Exportar [{idx}]: {html[:250]}")
                except Exception as e:
                    pass
            
        finally:
            context.close()

if __name__ == "__main__":
    test_exportar_csv()
