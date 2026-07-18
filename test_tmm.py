import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

# Importamos las funciones necesarias del script principal
from migrar_cuentas_tidal import (
    autorizar_tidal_en_tmm, aceptar_cookies_con_espera, 
    hacer_click_por_textos, esperar_visibilidad,
    esperar_locator_en_frames, rellenar_campo_humanizado,
    manejar_bloqueos_e_intervencion, navegar_con_bypass_referencia,
    encontrar_locator_en_frames
)
import re

SCRIPT_DIR = Path(__file__).resolve().parent
PROFILE_DIR_MAIN = SCRIPT_DIR / "perfiles" / "principal"
DOWNLOADS_DIR = SCRIPT_DIR / "descargas"


def login_tidal_cliente(context, client_email, client_pwd):
    """Cierra sesion de TIDAL si hay una activa y logea con las credenciales del cliente."""
    print("\n--- Cambiando cuenta TIDAL al cliente ---")
    page = context.pages[0] if context.pages else context.new_page()
    
    # 1. Cerrar sesion actual de TIDAL
    print("  [Login] Cerrando sesion anterior de TIDAL...")
    try:
        page.goto("https://account.tidal.com/logout", wait_until="domcontentloaded", timeout=15000)
        time.sleep(2.0)
    except Exception:
        pass
    
    # 2. Navegar al login
    print("  [Login] Navegando al login de TIDAL...")
    navegar_con_bypass_referencia(page, "https://login.tidal.com/")
    time.sleep(2.0)
    aceptar_cookies_con_espera(page)
    manejar_bloqueos_e_intervencion(page, "Login Tidal (Correo)")
    
    # 3. Rellenar correo
    print(f"  [Login] Ingresando correo: {client_email}")
    email_input = esperar_locator_en_frames(
        page, ['input[type="email"]', 'input[name="email"]', '#email'],
        label_regex=re.compile(r"correo|email", re.I), timeout_s=15.0
    )
    if not email_input:
        raise RuntimeError("No se encontro el campo de correo en la pagina de login.")
    rellenar_campo_humanizado(email_input, client_email)
    time.sleep(0.5)
    
    # 4. Pulsar Continuar
    btn_continue = esperar_locator_en_frames(
        page, ["button:has-text('Continuar')", "button:has-text('Continue')", "button[type='submit']"],
        text_regex=re.compile(r"continuar|continue", re.I), timeout_s=5.0
    )
    if btn_continue:
        btn_continue.click()
    time.sleep(3.0)
    
    manejar_bloqueos_e_intervencion(page, "Login Tidal (Contrasena)")
    
    # 5. Detectar si TIDAL pide codigo en vez de contrasena
    #    Si aparece la pantalla de "Revisa tu correo electronico" con inputs de codigo,
    #    hay que pulsar "Inicia sesion con contrasena" para cambiar al modo password.
    pwd_input = esperar_locator_en_frames(page, ['input[type="password"]', 'input[name="password"]'], timeout_s=4.0)
    if not pwd_input:
        print("  [Login] No se encontro campo de contrasena. Verificando si pide codigo...")
        # Buscar el enlace "Inicia sesion con contrasena" / "Sign in with password"
        btn_pwd_mode = esperar_locator_en_frames(
            page,
            ["a:has-text('contraseña')", "button:has-text('contraseña')",
             "a:has-text('password')", "button:has-text('password')",
             "text='Inicia sesión con contraseña'", "text='Sign in with password'"],
            text_regex=re.compile(r"con contrase|with password|iniciar.*contrase|sign.*password", re.I),
            timeout_s=5.0
        )
        if btn_pwd_mode:
            print("  [Login] Pantalla de codigo detectada. Pulsando 'Inicia sesion con contrasena'...")
            btn_pwd_mode.click()
            time.sleep(3.0)
            # Ahora deberia aparecer el campo de contrasena
            pwd_input = esperar_locator_en_frames(page, ['input[type="password"]', 'input[name="password"]'], timeout_s=10.0)
        
    if not pwd_input:
        raise RuntimeError("No se encontro el campo de contrasena.")
    rellenar_campo_humanizado(pwd_input, client_pwd)
    time.sleep(0.5)
    
    # 6. Iniciar sesion
    btn_login = esperar_locator_en_frames(
        page,
        ["button[type='submit']", "button:has-text('Iniciar')", "button:has-text('Log in')"],
        timeout_s=8.0
    )
    if btn_login:
        btn_login.click()
    time.sleep(4.0)
    
    # 7. Aceptar consentimiento si aparece
    btn_consent = encontrar_locator_en_frames(
        page, ['button', '[role="button"]'],
        text_regex=re.compile(r"si.*continuar|yes.*continue|continuar|continue", re.I)
    )
    if btn_consent:
        btn_consent.click()
        time.sleep(3.0)
    
    manejar_bloqueos_e_intervencion(page, "Login Tidal (Final)")
    print(f"  [Login] Sesion iniciada como: {client_email}")


def seleccionar_csv_nativo(page):
    """Selecciona el radio button de CSV usando eventos nativos del DOM que React captura.
    
    Hallazgo del debug: los radios tienen id='csv' e id='txt', value='on', name='format'.
    React usa event delegation y solo captura PointerEvent/MouseEvent nativos, no .click() simple.
    """
    result = page.evaluate("""
        () => {
            const csvInput = document.getElementById('csv');
            if (!csvInput) return { success: false, error: 'input#csv not found' };
            
            // Usar el setter nativo de HTMLInputElement para forzar checked=true
            // Esto evita que React intercepte el setter
            const nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'checked'
            );
            if (nativeSetter && nativeSetter.set) {
                nativeSetter.set.call(csvInput, true);
            } else {
                csvInput.checked = true;
            }
            
            // Disparar la secuencia COMPLETA de eventos de mouse que React captura
            // via su event delegation en el document root
            const opts = { bubbles: true, cancelable: true, view: window };
            csvInput.dispatchEvent(new PointerEvent('pointerdown', opts));
            csvInput.dispatchEvent(new MouseEvent('mousedown', opts));
            csvInput.dispatchEvent(new PointerEvent('pointerup', opts));
            csvInput.dispatchEvent(new MouseEvent('mouseup', opts));
            csvInput.dispatchEvent(new MouseEvent('click', opts));
            csvInput.dispatchEvent(new Event('input', { bubbles: true }));
            csvInput.dispatchEvent(new Event('change', { bubbles: true }));
            
            // Tambien clickear el label padre por si React escucha ahi
            const label = csvInput.closest('label') || document.querySelector('label[for="csv"]');
            if (label) {
                label.dispatchEvent(new MouseEvent('click', opts));
            }
            
            return { success: csvInput.checked, inputId: csvInput.id };
        }
    """)
    return result


def pulsar_exportar_nativo(page):
    """Pulsa el boton Exportar usando form.requestSubmit() que es la forma correcta
    de enviar un formulario programaticamente en el navegador.
    
    Hallazgo del debug: hay 2 botones 'Exportar' en la pagina:
      [0] 'Exportar archivo' - boton de servicio, NO es el correcto (no tiene form)
      [1] 'Exportar' - boton del modal dentro de un <form>, SI es el correcto
    Solo el [1] tiene form asociado, asi que filtramos por btn.form !== null.
    """
    result = page.evaluate("""
        () => {
            // Buscar todos los botones submit que esten dentro de un form
            const buttons = document.querySelectorAll('form button[type="submit"]');
            for (const btn of buttons) {
                // Solo el boton visible con texto exacto "Exportar"
                if (btn.offsetParent !== null) {
                    const text = (btn.textContent || '').trim();
                    if (text === 'Exportar' || text === 'Export') {
                        const form = btn.closest('form');
                        if (form) {
                            try {
                                form.requestSubmit(btn);
                                return { success: true, method: 'requestSubmit', text: text };
                            } catch(e) {
                                // Fallback: disparar eventos nativos completos en el boton
                                const opts = { bubbles: true, cancelable: true, view: window };
                                btn.dispatchEvent(new PointerEvent('pointerdown', opts));
                                btn.dispatchEvent(new MouseEvent('mousedown', opts));
                                btn.dispatchEvent(new PointerEvent('pointerup', opts));
                                btn.dispatchEvent(new MouseEvent('mouseup', opts));
                                btn.dispatchEvent(new MouseEvent('click', opts));
                                return { success: true, method: 'nativeEvents', text: text, error: e.message };
                            }
                        }
                    }
                }
            }
            return { success: false, error: 'No visible Exportar button inside a form found' };
        }
    """)
    return result


def test_exportar_csv():
    print("="*60)
    print("  INICIANDO TEST DE EXPORTACION EN TUNEMYMUSIC")
    print("  (Usa eventos nativos del DOM para React)")
    print("="*60)
    
    # Pedir correo y contrasena del cliente
    client_email = input("\n  Introduce el correo del cliente: ").strip()
    if not client_email:
        client_email = "test_backup"
        print(f"  [INFO] No se introdujo correo. Se usara '{client_email}' como nombre.")
    
    client_pwd = input("  Introduce la contrasena del cliente: ").strip()
    
    # Sanitizar el correo para usarlo como nombre de archivo
    csv_filename = client_email.replace("@", "_at_").replace(".", "_") + ".csv"
    print(f"  [INFO] El CSV se guardara como: {csv_filename}")
    
    with sync_playwright() as p:
        launch_args = ["--disable-blink-features=AutomationControlled"]
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR_MAIN),
            channel="chrome",
            headless=False,
            args=launch_args,
            viewport={"width": 1280, "height": 900},
            locale="es-ES"
        )
        
        # Cerrar otras pestanas
        while len(context.pages) > 1:
            try:
                context.pages[-1].close()
            except Exception:
                pass
        
        try:
            # Paso 0: Iniciar sesion en TIDAL con la cuenta del cliente
            if client_pwd:
                login_tidal_cliente(context, client_email, client_pwd)
                # Cerrar pestanas de TIDAL y abrir una nueva para TuneMyMusic
                while len(context.pages) > 1:
                    try: context.pages[-1].close()
                    except: pass
            else:
                print("\n  [INFO] No se introdujo contrasena. Se usara la cuenta TIDAL ya logeada.")
            
            tmm_page = context.pages[0] if context.pages else context.new_page()
            tmm_page.bring_to_front()
            
            print("\n1. Navegando a TuneMyMusic...")
            tmm_page.goto("https://www.tunemymusic.com/es/transfer", wait_until="domcontentloaded")
            time.sleep(2.0)
            
            aceptar_cookies_con_espera(tmm_page)
            
            # Seleccionar TIDAL como fuente
            print("2. Seleccionando TIDAL...")
            btn_tidal = tmm_page.locator("button[name='Tidal']").or_(tmm_page.locator("button[aria-label='TIDAL']")).first
            if esperar_visibilidad(btn_tidal, 5000):
                btn_tidal.click(force=True)
            else:
                if not hacer_click_por_textos(tmm_page, ["TIDAL", "Tidal"]):
                    raise RuntimeError("No se pudo hacer click en el boton de TIDAL.")
            time.sleep(3.0)
            
            # Manejar flujo de autorizacion
            print("3. Ejecutando flujo de autorizacion de TIDAL...")
            autorizar_tidal_en_tmm(tmm_page)
            
            # Cargar desde la cuenta TIDAL
            print("4. Cargando canciones...")
            if not hacer_click_por_textos(tmm_page, ["CARGAR DESDE CUENTA TIDAL", "Cargar desde cuenta TIDAL", "Cargar desde cuenta", "CARGAR DESDE CUENTA"]):
                btn_cargar = tmm_page.locator("button:has-text('Cargar desde cuenta')").or_(tmm_page.locator("button:has-text('Load from account')")).first
                if esperar_visibilidad(btn_cargar, 5000):
                    btn_cargar.click(force=True)
            time.sleep(5.0)
            
            # Pulsar elegir destino
            print("5. Pulsando 'Elegir destino'...")
            if not hacer_click_por_textos(tmm_page, ["ELEGIR DESTINO", "Elegir destino", "Elige destino", "Elige Destino", "SELECT DESTINATION", "Select destination", "Choose destination"]):
                btn_elegir_dest = tmm_page.locator("button:has-text('Elige destino')").or_(tmm_page.locator("button:has-text('Elegir destino')")).or_(tmm_page.locator("button:has-text('Select destination')")).first
                btn_elegir_dest.wait_for(state="visible", timeout=15000)
                btn_elegir_dest.click(force=True)
            time.sleep(3.0)
            
            # Seleccionar "EXPORTAR ARCHIVO"
            print("6. Seleccionando 'Exportar archivo'...")
            btn_archivo = tmm_page.locator("button[name='ToFile']").or_(tmm_page.locator("button[aria-label='Exportar archivo']")).first
            if esperar_visibilidad(btn_archivo, 4000):
                btn_archivo.click(force=True)
            else:
                if not hacer_click_por_textos(tmm_page, ["EXPORTAR ARCHIVO", "Exportar archivo", "Archivo", "File"]):
                    raise RuntimeError("No se pudo hacer click en la opcion de Exportar archivo.")
            time.sleep(3.0)
            
            # ============================================================
            #  PASO 7: Seleccionar CSV con eventos nativos del DOM
            # ============================================================
            print("7. Seleccionando formato CSV (eventos nativos)...")
            csv_result = seleccionar_csv_nativo(tmm_page)
            if csv_result.get("success"):
                print(f"  [OK] CSV seleccionado correctamente (input#{csv_result.get('inputId')})")
            else:
                print(f"  [WARN] No se pudo seleccionar CSV via JS nativo: {csv_result.get('error')}")
                # Fallback: intentar click de Playwright en el label
                lbl_csv = tmm_page.locator("label[for='csv']").first
                if esperar_visibilidad(lbl_csv, 3000):
                    lbl_csv.click(force=True)
                    print("  [Fallback] Click en label[for='csv'] via Playwright")
            time.sleep(2.0)
            
            # ============================================================
            #  PASO 8: Pulsar Exportar con form.requestSubmit()
            # ============================================================
            print("8. Pulsando boton 'Exportar' (form.requestSubmit)...")
            export_result = pulsar_exportar_nativo(tmm_page)
            if export_result.get("success"):
                print(f"  [OK] Formulario enviado via {export_result.get('method')} (boton: '{export_result.get('text')}')")
            else:
                print(f"  [WARN] form.requestSubmit fallo: {export_result.get('error')}")
                # Fallback: intentar Playwright click
                btn_exportar = tmm_page.locator("form button[type='submit']").last
                if esperar_visibilidad(btn_exportar, 3000):
                    btn_exportar.scroll_into_view_if_needed()
                    btn_exportar.click(force=True, timeout=4000)
                    print("  [Fallback] Click en form button[type='submit'] via Playwright")
            
            # Esperar a que el modal se cierre y aparezca el boton de Comenzar Transferencia
            print("  Esperando a que se procese la exportacion...")
            btn_start_check = tmm_page.locator("button:has-text('Comenzar transferencia')").or_(tmm_page.locator("button:has-text('Comenzar a mover')")).or_(tmm_page.locator("button:has-text('Start Transfer')")).or_(tmm_page.locator("button:has-text('Start moving')")).first
            if not esperar_visibilidad(btn_start_check, 20000):
                print("  [WARN] El boton 'Comenzar transferencia' no aparecio en 20s. Verificando estado...")
                tmm_page.screenshot(path="tmm_after_export_wait.png")
                print("  [Debug] Screenshot guardado: tmm_after_export_wait.png")
            time.sleep(1.5)
            
            # Pulsar "COMENZAR TRANSFERENCIA"
            # IMPORTANTE: La exportación a CSV descarga el archivo AUTOMÁTICAMENTE
            # al pulsar este botón. No hay un botón separado de "Descargar".
            # Usamos expect_download() para capturar la descarga que se inicia.
            print("9. Iniciando transferencia y capturando descarga automatica...")
            csv_path = DOWNLOADS_DIR / csv_filename
            
            try:
                with tmm_page.expect_download(timeout=180000) as download_info:
                    if not hacer_click_por_textos(tmm_page, ["COMENZAR TRANSFERENCIA", "Comenzar transferencia", "Comenzar a mover mi musica", "START TRANSFER", "Start Transfer", "Start moving my music"]):
                        btn_start = tmm_page.locator("button:has-text('Comenzar a mover')").or_(tmm_page.locator("button:has-text('Comenzar transferencia')")).or_(tmm_page.locator("button:has-text('Start')")).first
                        btn_start.click(force=True)
                    print("  -> Transferencia iniciada. Esperando descarga automatica del CSV...")
                
                download = download_info.value
                download.save_as(str(csv_path))
                print(f"\n** EXITO! Archivo CSV descargado y guardado en:\n   {csv_path}")
                
            except Exception as download_err:
                # Si expect_download falla, puede que el archivo se haya descargado
                # a la carpeta de descargas predeterminada del navegador.
                # Verificar si la transferencia se completo de todos modos.
                print(f"  [WARN] expect_download no capturo descarga: {str(download_err)[:100]}")
                
                # Verificar si aparecio "Transferencia completada"
                completada = tmm_page.locator("text='Transferencia completada'").or_(tmm_page.locator("text='Transfer completed'")).first
                if esperar_visibilidad(completada, 10000):
                    print("  [OK] La transferencia se completo exitosamente.")
                    print("  [INFO] El archivo CSV se descargo automaticamente a la carpeta de descargas del navegador.")
                    print(f"  [INFO] Busca un archivo .csv reciente en tu carpeta de descargas.")
                else:
                    print("  [ERROR] La transferencia no parece haberse completado.")
            
        except Exception as e:
            err_msg = str(e).encode('ascii', errors='replace').decode('ascii')
            print(f"\n** Error durante el test: {err_msg}")
            try:
                tmm_page.screenshot(path="tmm_test_error.png")
                print("   [Debug] Captura del error guardada como 'tmm_test_error.png'")
            except Exception:
                pass
        finally:
            context.close()

if __name__ == "__main__":
    test_exportar_csv()
