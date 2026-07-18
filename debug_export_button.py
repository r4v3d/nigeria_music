"""
Script de depuración enfocado SOLO en el modal de EXPORTAR ARCHIVO.
Captura el HTML del modal y usa eventos nativos del DOM para React.
"""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
PROFILE_DIR_MAIN = SCRIPT_DIR / "perfiles" / "principal"

def debug_exportar():
    print("="*60)
    print("  DEBUG: Modal de EXPORTAR ARCHIVO en TuneMyMusic")
    print("="*60)
    
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR_MAIN),
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            locale="es-ES"
        )
        
        while len(context.pages) > 1:
            try: context.pages[-1].close()
            except: pass
            
        page = context.pages[0] if context.pages else context.new_page()
        page.bring_to_front()
        
        try:
            # --- Navegar ---
            print("\n1. Navegando a TuneMyMusic...")
            page.goto("https://www.tunemymusic.com/es/transfer", wait_until="domcontentloaded")
            time.sleep(3.0)
            
            # Cookies
            for sel in ["button:has-text('Aceptar')", "#onetrust-accept-btn-handler"]:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(): btn.click(force=True); break
                except: pass
            
            # --- TIDAL ---
            print("2. Seleccionando TIDAL...")
            page.locator("button[name='Tidal']").or_(page.locator("button[aria-label='TIDAL']")).first.click(force=True, timeout=8000)
            time.sleep(3.0)
            
            # --- Auth flow ---
            print("3. Autorizando TIDAL...")
            from migrar_cuentas_tidal import autorizar_tidal_en_tmm, esperar_visibilidad, hacer_click_por_textos
            autorizar_tidal_en_tmm(page)
            
            # --- Cargar ---
            print("4. Cargando canciones...")
            hacer_click_por_textos(page, ["CARGAR DESDE CUENTA TIDAL", "Cargar desde cuenta TIDAL", "Cargar desde cuenta"])
            time.sleep(5.0)
            
            # --- Elegir destino ---
            print("5. Pulsando 'Elegir destino'...")
            hacer_click_por_textos(page, ["ELEGIR DESTINO", "Elegir destino", "Elige destino", "SELECT DESTINATION"])
            time.sleep(3.0)
            
            # --- Exportar archivo ---
            print("6. Seleccionando 'Exportar archivo'...")
            btn_archivo = page.locator("button[name='ToFile']").or_(page.locator("button[aria-label='Exportar archivo']")).first
            if esperar_visibilidad(btn_archivo, 4000):
                btn_archivo.click(force=True)
            else:
                hacer_click_por_textos(page, ["EXPORTAR ARCHIVO", "Exportar archivo"])
            time.sleep(3.0)
            
            # ============================================================
            #  PASO CRÍTICO: Capturar el HTML del modal para debug
            # ============================================================
            print("\n" + "="*60)
            print("  CAPTURANDO HTML DEL MODAL...")
            print("="*60)
            
            # Capturar todo el HTML del diálogo/modal
            modal_html = page.evaluate("""
                () => {
                    // Buscar el dialog/modal visible
                    const dialog = document.querySelector('[role="dialog"]')
                        || document.querySelector('[class*="Modal"][class*="open"]')
                        || document.querySelector('[class*="dialog"]')
                        || document.querySelector('[data-slot="dialog-content"]');
                    
                    if (dialog) return { found: 'dialog', html: dialog.outerHTML.substring(0, 5000) };
                    
                    // Buscar formulario que contenga radio buttons
                    const forms = document.querySelectorAll('form');
                    for (const form of forms) {
                        if (form.querySelector('input[type="radio"]')) {
                            return { found: 'form', html: form.outerHTML.substring(0, 5000) };
                        }
                    }
                    
                    // Buscar cualquier contenedor con "Exportar" y radio buttons
                    const allDivs = document.querySelectorAll('div');
                    for (const div of allDivs) {
                        const text = div.textContent || '';
                        if (text.includes('Seleccionar formato') || text.includes('Select format')) {
                            return { found: 'div-format', html: div.outerHTML.substring(0, 5000) };
                        }
                    }
                    
                    return { found: 'none', html: document.body.innerHTML.substring(0, 3000) };
                }
            """)
            
            print(f"  Encontrado: {modal_html['found']}")
            print(f"  HTML:\n{modal_html['html'][:3000]}")
            
            # Capturar info específica de los radio buttons
            radio_info = page.evaluate("""
                () => {
                    const radios = document.querySelectorAll('input[type="radio"]');
                    const results = [];
                    radios.forEach(r => {
                        results.push({
                            id: r.id,
                            name: r.name,
                            value: r.value,
                            checked: r.checked,
                            visible: r.offsetParent !== null,
                            parentTag: r.parentElement ? r.parentElement.tagName : 'none',
                            parentClass: r.parentElement ? r.parentElement.className.substring(0, 100) : 'none',
                            rect: r.getBoundingClientRect()
                        });
                    });
                    return results;
                }
            """)
            
            print(f"\n  Radio buttons encontrados: {len(radio_info)}")
            for i, r in enumerate(radio_info):
                print(f"  [{i}] id='{r['id']}' name='{r['name']}' value='{r['value']}' checked={r['checked']} visible={r['visible']}")
                print(f"      parent={r['parentTag']}.{r['parentClass'][:60]}")
                print(f"      rect: top={r['rect']['top']:.0f} left={r['rect']['left']:.0f} w={r['rect']['width']:.0f} h={r['rect']['height']:.0f}")
            
            # Capturar info del botón Exportar
            btn_info = page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll('button');
                    const results = [];
                    buttons.forEach(b => {
                        const text = (b.textContent || '').trim();
                        if (text.toLowerCase().includes('exportar') || text.toLowerCase().includes('export')) {
                            results.push({
                                text: text,
                                type: b.type,
                                disabled: b.disabled,
                                visible: b.offsetParent !== null,
                                className: b.className.substring(0, 150),
                                rect: b.getBoundingClientRect(),
                                formTag: b.form ? 'yes' : 'no',
                                formAction: b.form ? b.form.action : 'none'
                            });
                        }
                    });
                    return results;
                }
            """)
            
            print(f"\n  Botones 'Exportar' encontrados: {len(btn_info)}")
            for i, b in enumerate(btn_info):
                print(f"  [{i}] text='{b['text']}' type='{b['type']}' disabled={b['disabled']} visible={b['visible']}")
                print(f"      class={b['className'][:80]}")
                print(f"      rect: top={b['rect']['top']:.0f} left={b['rect']['left']:.0f} w={b['rect']['width']:.0f} h={b['rect']['height']:.0f}")
                print(f"      form={b['formTag']} formAction={b['formAction']}")
            
            # ============================================================
            #  INTENTO DE CLIC CON EVENTOS NATIVOS PARA REACT
            # ============================================================
            print("\n" + "="*60)
            print("  INTENTANDO SELECCIONAR CSV CON EVENTOS NATIVOS...")
            print("="*60)
            
            csv_result = page.evaluate("""
                () => {
                    // Buscar el input de CSV
                    let csvInput = document.getElementById('csv')
                        || document.querySelector('input[value="csv"]')
                        || document.querySelector('input[id*="csv" i]');
                    
                    if (!csvInput) {
                        // Buscar por label
                        const labels = document.querySelectorAll('label');
                        for (const label of labels) {
                            const forAttr = label.getAttribute('for');
                            const text = (label.textContent || '').trim();
                            if (forAttr === 'csv' || text === 'CSV') {
                                if (forAttr) {
                                    csvInput = document.getElementById(forAttr);
                                }
                                if (!csvInput) {
                                    csvInput = label.querySelector('input[type="radio"]');
                                }
                                break;
                            }
                        }
                    }
                    
                    if (!csvInput) {
                        return { success: false, error: 'CSV input not found' };
                    }
                    
                    // Intentar usar el setter nativo de React para forzar el cambio
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'checked'
                    );
                    if (nativeSetter && nativeSetter.set) {
                        nativeSetter.set.call(csvInput, true);
                    } else {
                        csvInput.checked = true;
                    }
                    
                    // Disparar secuencia completa de eventos del mouse que React captura
                    const eventOpts = { bubbles: true, cancelable: true, view: window };
                    csvInput.dispatchEvent(new PointerEvent('pointerdown', eventOpts));
                    csvInput.dispatchEvent(new MouseEvent('mousedown', eventOpts));
                    csvInput.dispatchEvent(new PointerEvent('pointerup', eventOpts));
                    csvInput.dispatchEvent(new MouseEvent('mouseup', eventOpts));
                    csvInput.dispatchEvent(new MouseEvent('click', eventOpts));
                    csvInput.dispatchEvent(new Event('input', { bubbles: true }));
                    csvInput.dispatchEvent(new Event('change', { bubbles: true }));
                    
                    // También intentar click en el label padre
                    const label = csvInput.closest('label') || document.querySelector('label[for="csv"]');
                    if (label) {
                        label.dispatchEvent(new MouseEvent('click', eventOpts));
                    }
                    
                    return { 
                        success: true, 
                        inputId: csvInput.id,
                        inputChecked: csvInput.checked,
                        inputValue: csvInput.value
                    };
                }
            """)
            print(f"  Resultado CSV: {csv_result}")
            time.sleep(2.0)
            
            # Verificar si el cambio se reflejó
            verify = page.evaluate("""
                () => {
                    const radios = document.querySelectorAll('input[type="radio"]');
                    const status = [];
                    radios.forEach(r => {
                        status.push({ id: r.id, value: r.value, checked: r.checked });
                    });
                    return status;
                }
            """)
            print(f"  Estado radios después del intento: {verify}")
            
            # Tomar screenshot después de intentar CSV
            page.screenshot(path="debug_after_csv_click.png")
            print("  Screenshot guardado: debug_after_csv_click.png")
            
            # ============================================================
            #  INTENTO DE CLIC EN EXPORTAR CON FORM SUBMIT
            # ============================================================
            print("\n" + "="*60)
            print("  INTENTANDO PULSAR EXPORTAR VÍA FORM SUBMIT...")
            print("="*60)
            
            export_result = page.evaluate("""
                () => {
                    // Buscar el botón de Exportar visible
                    const buttons = document.querySelectorAll('button[type="submit"]');
                    let targetBtn = null;
                    let targetForm = null;
                    
                    for (const btn of buttons) {
                        const text = (btn.textContent || '').trim().toLowerCase();
                        if ((text.includes('exportar') || text.includes('export')) && btn.offsetParent !== null) {
                            targetBtn = btn;
                            targetForm = btn.form || btn.closest('form');
                            break;
                        }
                    }
                    
                    if (!targetBtn) {
                        // Fallback: cualquier botón visible con "Exportar"
                        const allBtns = document.querySelectorAll('button');
                        for (const btn of allBtns) {
                            const text = (btn.textContent || '').trim().toLowerCase();
                            if ((text.includes('exportar') || text.includes('export')) && btn.offsetParent !== null) {
                                targetBtn = btn;
                                targetForm = btn.form || btn.closest('form');
                                break;
                            }
                        }
                    }
                    
                    if (!targetBtn) {
                        return { success: false, error: 'Exportar button not found' };
                    }
                    
                    const info = {
                        text: targetBtn.textContent.trim(),
                        type: targetBtn.type,
                        disabled: targetBtn.disabled,
                        hasForm: !!targetForm,
                    };
                    
                    // Método 1: requestSubmit (triggers submit event properly)
                    if (targetForm) {
                        try {
                            targetForm.requestSubmit(targetBtn);
                            info.method = 'requestSubmit';
                            info.success = true;
                            return info;
                        } catch(e) {
                            info.requestSubmitError = e.message;
                        }
                    }
                    
                    // Método 2: Eventos nativos completos del mouse
                    const eventOpts = { bubbles: true, cancelable: true, view: window };
                    targetBtn.dispatchEvent(new PointerEvent('pointerdown', eventOpts));
                    targetBtn.dispatchEvent(new MouseEvent('mousedown', eventOpts));
                    targetBtn.dispatchEvent(new PointerEvent('pointerup', eventOpts));
                    targetBtn.dispatchEvent(new MouseEvent('mouseup', eventOpts));
                    targetBtn.dispatchEvent(new MouseEvent('click', eventOpts));
                    
                    info.method = 'nativeEvents';
                    info.success = true;
                    return info;
                }
            """)
            print(f"  Resultado Exportar: {export_result}")
            
            time.sleep(3.0)
            page.screenshot(path="debug_after_exportar_click.png")
            print("  Screenshot guardado: debug_after_exportar_click.png")
            
            # Esperar un poco y ver si el modal se cerró
            print("\n  Esperando 5 segundos para ver si el modal se cierra...")
            time.sleep(5.0)
            page.screenshot(path="debug_final_state.png")
            print("  Screenshot final guardado: debug_final_state.png")
            
            # Verificar estado final
            final_state = page.evaluate("""
                () => {
                    const dialog = document.querySelector('[role="dialog"]');
                    const hasStartBtn = !!(
                        document.querySelector("button:has-text('Comenzar')") 
                        || Array.from(document.querySelectorAll('button')).find(b => 
                            (b.textContent || '').toLowerCase().includes('comenzar') 
                            || (b.textContent || '').toLowerCase().includes('start')
                        )
                    );
                    return {
                        dialogVisible: dialog ? dialog.offsetParent !== null : false,
                        hasStartButton: hasStartBtn,
                        currentUrl: window.location.href
                    };
                }
            """)
            print(f"  Estado final: {final_state}")
            
            print("\n\n>>> Script de debug terminado. Revisa los screenshots y la información de arriba.")
            input(">>> Presiona Enter para cerrar el navegador <<<")
            
        except Exception as e:
            print(f"\n❌ Error: {e}")
            import traceback
            traceback.print_exc()
            try:
                page.screenshot(path="debug_error.png")
            except: pass
        finally:
            context.close()

if __name__ == "__main__":
    debug_exportar()
