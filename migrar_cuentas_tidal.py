import os
import re
import sys
import time
import random
import argparse
import threading
import shutil
import concurrent.futures
from pathlib import Path

# Configurar salida estándar para UTF-8 y reemplazar caracteres no mapeables en Windows
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from playwright.sync_api import sync_playwright, Error as PWErr, TimeoutError as PWTimeout

# Locks y variables globales para la ejecución concurrente
class SystemLock:
    """Bloqueo exclusivo a nivel de sistema operativo para sincronizar hilos y procesos paralelos."""
    def __init__(self, lock_path):
        self.lock_path = Path(lock_path)
    def acquire(self):
        while True:
            try:
                self.lock_path.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                time.sleep(1.0)
    def release(self):
        try:
            if self.lock_path.exists():
                shutil.rmtree(self.lock_path, ignore_errors=True)
        except Exception:
            pass

tmm_lock = SystemLock(Path.home() / ".tidal_migrator" / "tmm_system_lock")
parent_lock = threading.Lock()
reset_lock = threading.Lock()
stdin_lock = threading.Lock()
progreso_lock = threading.Lock()
family_invite_queue = []
BATCH_MODE = False
BATCH_MODE_VPN = False
NORMAL_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
valid_pe_list = []
valid_ng_list = []
barrier_step1 = None
barrier_step2 = None
barrier_step3_4 = None
barrier_step5 = None
barrier_step6 = None
barrier_step7 = None
barrier_step8 = None
barrier_step9 = None
barrier_step10 = None

def input_concurrente(prompt, identificador=None):
    with stdin_lock:
        print("\n" + "!" * 80)
        if identificador:
            print(f"  [AVISO] PAUSA MANUAL REQUERIDA PARA LA CUENTA: {identificador}")
        else:
            print("  [AVISO] PAUSA MANUAL REQUERIDA")
        print("!" * 80)
        res = input(prompt)
        print("!" * 80 + "\n")
        return res

def guardar_progreso_migracion(email: str, step: int):
    import json
    from pathlib import Path
    
    file_path = Path("progreso_migraciones.json")
    with progreso_lock:
        data = {}
        if file_path.exists():
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        data[email.lower().strip()] = step
        try:
            file_path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
            print(f"  [Progreso] Guardado paso {step} para {email} en progreso_migraciones.json")
        except Exception as e:
            print(f"  [Progreso] [WARN] No se pudo guardar progreso en JSON: {e}")

def preparar_perfil_temporal(origen_path, email_sanitized):
    # Generar un nombre de carpeta único usando un timestamp y un número aleatorio para evitar colisiones de bloqueos en Windows
    unique_id = f"{int(time.time())}_{random.randint(1000, 9999)}"
    temp_dir = origen_path.parent / f"temp_profile_{email_sanitized}_{unique_id}"
    if temp_dir.exists():
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass
            
    # Filtro para omitir archivos de bloqueo y de Singleton de Chrome durante la copia
    def ignorar_locks(directory, files):
        locks = {"SingletonLock", "SingletonCookie", "SingletonSocket", "lock", "Lock"}
        return [f for f in files if f in locks]
        
    try:
        shutil.copytree(origen_path, temp_dir, ignore=ignorar_locks)
        print(f"  [Perfil] Perfil principal copiado a temporal (sin archivos de bloqueo): {temp_dir}")
        
        # Limpiar cookies de Tidal en la base de datos SQLite de Chrome para asegurar
        # que inicie sesión completamente desconectado de Tidal
        import sqlite3
        cookies_paths = [
            temp_dir / "Default" / "Network" / "Cookies",
            temp_dir / "Default" / "Cookies"
        ]
        for cp in cookies_paths:
            if cp.exists():
                try:
                    conn = sqlite3.connect(str(cp))
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM cookies WHERE host_key LIKE '%tidal.com%'")
                    conn.commit()
                    conn.close()
                    print(f"  [Perfil] Limpiadas cookies de Tidal de base de datos: {cp.name}")
                except Exception as ex:
                    print(f"  [Perfil] [WARN] No se pudieron limpiar cookies de Tidal en {cp.name}: {ex}")
                            
        return temp_dir
    except Exception as e:
        print(f"  [Perfil] [WARN] No se pudo copiar el perfil ({e}). Usando vacío.")
        temp_dir.mkdir(parents=True, exist_ok=True)
        return temp_dir

def limpiar_perfil_temporal(temp_dir):
    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
        print(f"  [Perfil] Limpiado perfil temporal: {temp_dir}")
    except Exception as e:
        print(f"  [Perfil] [WARN] No se pudo eliminar perfil temporal {temp_dir}: {e}")


# Importar helper de human_slider si está disponible
try:
    from human_slider import generate_human_track
except ImportError:
    # Fallback si no está el archivo, usamos una simulación simple de track
    def generate_human_track(distance):
        track = []
        pasos = 20
        for i in range(pasos):
            dx = distance // pasos
            dy = random.choice([-1, 0, 1])
            track.append((dx, dy, 0.01))
        resto = distance % pasos
        if resto:
            track.append((resto, 0, 0.02))
        return track

SCRIPT_DIR = Path(__file__).resolve().parent
USER_HOME = Path.home()
PROFILE_DIR_MAIN = USER_HOME / ".tidal_migrator" / "perfiles" / "principal"
PROFILE_DIR_PARENT = USER_HOME / ".tidal_migrator" / "perfiles" / "familiar_titular"
DOWNLOADS_DIR = SCRIPT_DIR / "descargas"

# Asegurar directorios
PROFILE_DIR_MAIN.mkdir(parents=True, exist_ok=True)
PROFILE_DIR_PARENT.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

STEALTH_SCRIPT = """
    // Ocultar la propiedad navigator.webdriver de forma limpia
    try {
        const newProto = Object.getPrototypeOf(navigator);
        delete newProto.webdriver;
    } catch (e) {}
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true
    });

    // Simular el objeto window.chrome estándar de Google Chrome
    if (!window.chrome) {
        window.chrome = {
            app: {
                isInstalled: false,
                InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                RunningState: { CANNOT_RUN: 'cannot_run', RUNNING: 'running', READY_TO_RUN: 'ready_to_run' }
            },
            runtime: {
                OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
                OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
                PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
                RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' }
            }
        };
    }

    // Parchear navigator.permissions.query
    try {
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters)
        );
    } catch (e) {}

    // Asegurar navigator.plugins y navigator.mimeTypes estándar
    try {
        if (!navigator.plugins || navigator.plugins.length === 0) {
            Object.defineProperty(navigator, 'plugins', {
                get: () => [
                    { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                    { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }
                ],
                configurable: true
            });
        }
    } catch (e) {}

    // Parchear navigator.languages para evitar discrepancias
    try {
        Object.defineProperty(navigator, 'languages', {
            get: () => ['es-ES', 'es', 'en'],
            configurable: true
        });
    } catch (e) {}

    // Spoofear WebGL Renderer para ocultar SwiftShader (Headless GPU)
    try {
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            // UNMASKED_VENDOR_WEBGL
            if (parameter === 37445) {
                return 'Google Inc. (NVIDIA)';
            }
            // UNMASKED_RENDERER_WEBGL
            if (parameter === 37446) {
                return 'ANGLE (NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)';
            }
            return getParameter.apply(this, arguments);
        };
    } catch (e) {}
"""

# Coordenadas y selectores para captcha de arrastre (DataDome)
def resolver_slider_captcha_playwright(page) -> bool:
    """Intenta resolver el slider captcha en Playwright usando simulación humana."""
    try:
        page.bring_to_front()
        time.sleep(0.5)
    except Exception:
        pass

    js_finder = """
    () => {
        const handle = document.querySelector(".slider") || document.querySelector(".slider-button") || document.querySelector(".captcha_verify_slide_button") || document.querySelector("[class*='thumb' i]") || document.querySelector("[class*='handle' i]");
        if (!handle) return null;
        
        const track = document.querySelector(".sliderContainer") || document.querySelector(".sliderbg") || document.querySelector(".sliderText") || handle.parentElement;
        if (!track) return null;
        
        const rHandle = handle.getBoundingClientRect();
        const rTrack = track.getBoundingClientRect();
        
        if (rHandle.width >= 20 && rTrack.width >= 150) {
            return {
                handleX: rHandle.left,
                handleY: rHandle.top,
                handleW: rHandle.width,
                handleH: rHandle.height,
                trackW: rTrack.width
            };
        }
        return null;
    }
    """
    
    # Buscar captcha en todos los frames
    target_frame = None
    captcha_data = None
    for frame in page.frames:
        try:
            data = frame.evaluate(js_finder)
            if data:
                target_frame = frame
                captcha_data = data
                break
        except Exception:
            continue
            
    if not captcha_data or not target_frame:
        return False

    try:
        iframe_handle = target_frame.frame_element()
        if iframe_handle:
            iframe_handle.scroll_into_view_if_needed()
            
        # Re-obtener las coordenadas del captcha
        captcha_data = target_frame.evaluate(js_finder)
        iframe_box = iframe_handle.bounding_box() if iframe_handle else {"x": 0, "y": 0}
        if not iframe_box:
            return False
            
        start_x = iframe_box["x"] + captcha_data["handleX"] + captcha_data["handleW"] / 2
        start_y = iframe_box["y"] + captcha_data["handleY"] + captcha_data["handleH"] / 2
        distance = captcha_data["trackW"] - captcha_data["handleW"]
        if distance <= 0:
            distance = 240
            
        print(f"    [Anti-bot] Captcha de arrastre detectado. Arrastrando {distance}px...")
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        time.sleep(random.uniform(0.15, 0.3))
        
        track = generate_human_track(distance)
        curr_x, curr_y = start_x, start_y
        for dx, dy, t_sleep in track:
            curr_x += dx
            curr_y += dy
            page.mouse.move(curr_x, curr_y)
            time.sleep(t_sleep)
            
        time.sleep(random.uniform(0.2, 0.35))
        page.mouse.up()
        return True
    except Exception as e:
        print(f"    [Anti-bot] Error al arrastrar captcha: {e}")
        try:
            page.mouse.up()
        except Exception:
            pass
        return False

def detectar_pantalla_antirobot(page) -> bool:
    """Verifica si hay pantallas de captcha, CloudFront 403 o bloqueos."""
    try:
        if page.is_closed():
            return False
        
        # PRIMERO: remover banner de cookies del DOM para eliminar falsos positivos
        try:
            page.evaluate("""
                () => {
                    const selectors = [
                        '#onetrust-banner-sdk', '#onetrust-consent-sdk',
                        '[class*="onetrust"]', '[class*="cookie-banner"]', 
                        '[class*="cookie-consent"]', '[class*="CookieConsent"]',
                        '[id*="cookie"]', '[id*="consent"]',
                        '[class*="gdpr"]', '[class*="privacy-banner"]'
                    ];
                    for (const sel of selectors) {
                        document.querySelectorAll(sel).forEach(el => el.remove());
                    }
                    document.querySelectorAll('[class*="onetrust-pc-dark-filter"], .onetrust-pc-dark-filter').forEach(el => el.remove());
                }
            """)
        except Exception:
            pass
        
        # Título de CloudFront 403 o Bloqueos Generales
        titulo = page.title()
        if re.search(r"403|request could not be satisfied|access denied|attention required|cloudflare|just a moment|security|blocked|datadome", titulo, re.I):
            print(f"  [Anti-bot DEBUG] Detectado por TITULO: '{titulo}'")
            return True
            
        # Textos específicos de captcha/antibot
        patrones = [
            "nos aseguramos de que", "no a un robot", "desliza hacia la derecha",
            "making sure you are not a robot", "not a robot", "slide to right",
            "algo salió mal", "something went wrong",
            "verify you are human", "verifying you are human", 
            "confirmar que eres humano", "site connection is secure",
            "checking your browser", "comprobando si la conexión", "comprobando que la conexión",
            "access denied", "error code 1020", "ray id", "secure connection", "unusual activity",
            "bot detection", "please turn on javascript", "enable cookies",
            "please enable JS", "enable JS", "disable any ad blocker", "ad blocker", "enable javascript",
            "acceso está restringido", "acceso restringido", "restringido temporalmente",
            "se encuentra en la misma red", "comportamiento del navegador nos ha intrigado"
        ]
        
        for frame in page.frames:
            url_lower = frame.url.lower()
            
            # Ignorar frames de rastreo invisibles o de tamaño irrelevante (0x0, 1x1, etc.)
            if frame != page.main_frame:
                try:
                    el = frame.frame_element()
                    if not el or not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if not box or box['width'] < 50 or box['height'] < 50:
                        continue
                except Exception:
                    continue
                    
            for domain_kw in ["captcha", "datadome", "turnstile", "captcha-delivery"]:
                if domain_kw in url_lower:
                    print(f"  [Anti-bot DEBUG] Detectado por URL de frame visible: '{frame.url}' (keyword: '{domain_kw}')")
                    return True
            for pat in patrones:
                try:
                    count = frame.get_by_text(re.compile(re.escape(pat), re.I)).count()
                    if count > 0:
                        print(f"  [Anti-bot DEBUG] Detectado por PATRON en frame: '{pat}' (frame URL: {frame.url[:80]})")
                        return True
                except Exception:
                    continue
                    
        # Comprobar texto del body para CloudFront o Cloudflare
        try:
            body_text = page.locator("body").inner_text(timeout=500)
            if re.search(r"403\s*ERROR|generated by cloudfront|request blocked|access denied|error code 1020|ray id|please turn on javascript|enable cookies|please enable JS|disable any ad blocker|ad blocker|enable javascript", body_text, re.I):
                print(f"  [Anti-bot DEBUG] Detectado por BODY TEXT (CloudFront/blocked)")
                return True
        except Exception:
            pass
    except Exception:
        pass
    return False

def manejar_bloqueos_e_intervencion(page, subtitulo: str = "") -> None:
    """Detecta captchas y bloqueos. Si los encuentra, intenta auto-resolver y si falla pausa para intervención manual."""
    time.sleep(1.0)
    
    intentos_bucle = 0
    rotaciones_realizadas = 0
    while detectar_pantalla_antirobot(page):
        intentos_bucle += 1
        print(f"\n[BLOQUEO DETECTADO] -> {subtitulo}")
        
        manager = getattr(page, "manager", None)
        if manager:
            # 1. Si la página tiene un manager asociado y está en modo headless, forzar transición a visual
            if getattr(manager, "headless", False):
                manager.forzar_modo_visual()
                
            # 2. Si se usan proxies y no hemos excedido el límite de rotaciones en este bloqueo
            if getattr(manager, "use_proxy", False) and rotaciones_realizadas < 3:
                rotaciones_realizadas += 1
                print(f"  [Auto-Proxy] [BLOQUEO] Rotación de proxy automática {rotaciones_realizadas}/3...")
                manager.ejecutar_rotacion_proxy_y_recargar()
                time.sleep(2.0)
                continue
        
        # Intentar auto-resolver slider en el primer intento
        if intentos_bucle == 1:
            if resolver_slider_captcha_playwright(page):
                time.sleep(2.0)
                if not detectar_pantalla_antirobot(page):
                    print("  [Anti-bot] ¡Slider captcha resuelto automáticamente!")
                    return
        
        # Si ya hemos pausado una vez y el bloqueo persiste (típico error 403 o captcha colgado), recargar
        if intentos_bucle > 1:
            try:
                print("  [Anti-bot] Detectado bloqueo persistente o página colgada. Recargando la página...")
                page.reload(timeout=10000)
                time.sleep(2.5)
                if not detectar_pantalla_antirobot(page):
                    print("  [Anti-bot] ¡Página recargada y bloqueo superado!")
                    return
            except Exception:
                pass
        
        # Pausa manual
        print("\n" + "=" * 60)
        print("  PAUSA MANUAL DE SEGURIDAD")
        print("  Detectamos un bloqueo o verificación en el navegador.")
        print("  1. Ve a la ventana abierta del navegador y completa la verificación/captcha manualmente o vuelve a cargar la página.")
        print("  2. Si es necesario, rota de IP en tu VPN / Proxy.")
        print("  3. Una vez superado el bloqueo y veas el campo de correo o de destino, regresa a esta consola y presiona Enter.")
        print("=" * 60 + "\n")
        
        input_concurrente(">>> Presiona Enter para continuar una vez resuelto el bloqueo <<<", getattr(page, "client_email", None))
        time.sleep(3.0)

def rellenar_campo_humanizado(loc, valor: str) -> bool:
    """Escribe un valor en un localizador simulando pulsaciones de teclado humanas."""
    try:
        loc.click(timeout=4000)
        time.sleep(0.2)
        # Limpiar campo por si tiene contenido
        loc.press("Control+A")
        time.sleep(0.1)
        loc.press("Backspace")
        time.sleep(0.15)
        
        for char in valor:
            loc.type(char, delay=random.randint(45, 120))
        time.sleep(0.2)
        
        # Validar y corregir si hubo pérdida de caracteres por lag
        try:
            val_actual = loc.input_value()
            if val_actual != valor:
                print(f"    [Human Input] [WARN] Se detectó discrepancia al escribir (escrito: '{val_actual}', esperado: '{valor}'). Corrigiendo...")
                loc.fill(valor)
                time.sleep(0.2)
        except Exception:
            pass
            
        return True
    except Exception as e:
        print(f"    [Human Input] Error al rellenar campo: {e}")
        return False

def aceptar_cookies_con_espera(page) -> bool:
    """Busca y acepta banners de cookies habituales con reintentos y fallback destructivo."""
    cookie_selectors = [
        "button:has-text('Aceptar todas')", "button:has-text('Aceptar todo')", "button:has-text('Aceptar')", 
        "button:has-text('Accept all')", "button:has-text('Accept')", "button:has-text('OK')", "button:has-text('Entendido')",
        "button:has-text('Got it')", "#onetrust-accept-btn-handler", "[id*='cookie' i] button",
        "button[class*='accept' i]", "div[class*='accept' i] button", "button[class*='ok' i]"
    ]
    # Esperar hasta 3 segundos con reintentos periódicos
    for intento in range(6):
        try:
            if page.is_closed():
                return False
        except Exception:
            return False
            
        for frame in page.frames:
            # 1. Intentar por selectores CSS
            for sel in cookie_selectors:
                try:
                    loc = frame.locator(sel)
                    cnt = loc.count()
                    for idx in range(cnt):
                        btn = loc.nth(idx)
                        if btn.is_visible():
                            btn.click(force=True)
                            print(f"  [Cookies] Banner de cookies aceptado (selector: '{sel}').")
                            time.sleep(0.5)
                            return True
                except Exception:
                    continue
            
            # 2. Intentar por rol/texto exacto o aproximado
            for text in ["Aceptar todas", "Aceptar todo", "Aceptar", "Accept all", "Accept", "OK", "Entendido", "Got it"]:
                try:
                    loc = frame.get_by_role("button", name=re.compile(rf"^\s*{text}\s*$", re.I))
                    cnt = loc.count()
                    for idx in range(cnt):
                        btn = loc.nth(idx)
                        if btn.is_visible():
                            btn.click(force=True)
                            print(f"  [Cookies] Banner de cookies aceptado (rol/texto: '{text}').")
                            time.sleep(0.5)
                            return True
                except Exception:
                    continue
        time.sleep(0.5)
        
    # Si no se pudo hacer clic, remover proactivamente del DOM cualquier banner de cookies flotante o fijo
    try:
        page.evaluate("""() => {
            const ids = ['onetrust-consent-sdk', 'onetrust-banner-sdk', 'onetrust-style', 'cookie-consent', 'cookiebanner'];
            ids.forEach(id => {
                const el = document.getElementById(id);
                if (el) el.remove();
            });
            document.querySelectorAll('.onetrust-pc-dark-filter, [class*="onetrust" i], [class*="cookie" i], [id*="cookie" i], [class*="banner" i]').forEach(el => {
                try {
                    const style = window.getComputedStyle(el);
                    if (style.position === 'fixed' || style.position === 'absolute' || el.style.position === 'fixed' || el.style.position === 'absolute') {
                        el.remove();
                    }
                } catch(e) {}
            });
            // Remover también overlays/backdrops que bloqueen clics en el fondo
            document.querySelectorAll('[class*="backdrop" i], [class*="overlay" i], [class*="modal-bg" i]').forEach(el => {
                try {
                    const style = window.getComputedStyle(el);
                    if (style.position === 'fixed' || style.position === 'absolute') {
                        el.remove();
                    }
                } catch(e) {}
            });
        }""")
        print("  [Cookies] Fallback JS ejecutado: removiendo banners y overlays del DOM.")
    except Exception as e:
        print(f"  [Cookies] Error al remover banners del DOM por JS: {e}")
    return False

def esperar_visibilidad(loc, timeout_ms=15000) -> bool:
    """Espera a que un localizador sea visible y devuelve True si lo es, o False si da timeout."""
    try:
        loc.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False

def hacer_click_por_textos(page, textos: list) -> bool:
    """Busca un botón o elemento que coincida con alguno de los textos proporcionados y hace click con force=True."""
    for texto in textos:
        try:
            # Intentar clickear elemento con texto exacto
            loc = page.get_by_text(texto, exact=True).first
            if esperar_visibilidad(loc, 4000):
                loc.click(force=True)
                print(f"  [Click Auto] Click exitoso en texto exacto: '{texto}'")
                return True
        except Exception:
            pass
        try:
            # Intentar clickear elemento con texto parcial (evitar emparejar 'EXPORTAR ARCHIVO' para el botón 'Exportar')
            if texto.upper() == "EXPORTAR":
                loc = page.locator("button:has-text('Exportar')").first
            else:
                loc = page.get_by_text(texto, exact=False).first
            if esperar_visibilidad(loc, 1000):
                loc.click(force=True)
                print(f"  [Click Auto] Click exitoso en texto parcial: '{texto}'")
                return True
        except Exception:
            pass
        try:
            # Intentar buscar un selector de tipo botón con ese texto
            loc = page.locator(f"button:has-text('{texto}'), a:has-text('{texto}')").first
            if esperar_visibilidad(loc, 1000):
                loc.click(force=True)
                print(f"  [Click Auto] Click exitoso en selector de botón: '{texto}'")
                return True
        except Exception:
            pass
    return False

def escribir_codigo_verificacion_inteligente(page, codigo: str) -> bool:
    """Ingresa un código de verificación de forma inteligente.
    Detecta si la página usa múltiples cajas (un dígito por input) o un solo input,
    y rellena los valores simulando pulsaciones físicas de teclado con Playwright.
    """
    for frame in page.frames:
        try:
            # Esperar dinámicamente hasta 4 segundos a que se rendericen al menos 6 inputs de código
            code_inputs = []
            for poll in range(8):
                inputs = frame.locator('input').all()
                code_inputs = []
                for ip in inputs:
                    try:
                        if ip.is_visible():
                            type_attr = (ip.get_attribute("type") or "").lower()
                            mode = (ip.get_attribute("inputmode") or "").lower()
                            name = (ip.get_attribute("name") or "").lower()
                            placeholder = (ip.get_attribute("placeholder") or "").lower()
                            
                            # Aceptar tipos habituales y campos con 'code'
                            if (type_attr in ["text", "number", "tel", "password"] or 
                                mode == "numeric" or 
                                "code" in name or 
                                "code" in placeholder or 
                                "código" in placeholder or 
                                "codigo" in placeholder):
                                code_inputs.append(ip)
                    except Exception:
                        pass
                
                # Si ya detectamos al menos 6 inputs (o la longitud esperada del código), paramos de esperar
                if len(code_inputs) >= len(codigo):
                    break
                time.sleep(0.5)
            
            if not code_inputs:
                continue
                
            # Si hay múltiples inputs
            if len(code_inputs) >= 4:
                print(f"  [Codigo] Detectadas {len(code_inputs)} cajas. Rellenando con teclado simulado...")
                code_inputs[0].click()
                time.sleep(0.5)
                # Borrar cualquier residuo anterior enviando retrocesos
                for _ in range(10):
                    page.keyboard.press("Backspace")
                    time.sleep(0.05)
                # Escribir cada dígito de forma secuencial y humana para permitir el auto-focus nativo de Tidal
                for digit in codigo:
                    page.keyboard.type(digit, delay=150)
                    time.sleep(0.15)
                print("  [Codigo] Código ingresado exitosamente en múltiples cajas.")
                return True
            else:
                # Un solo input de codigo completo
                print("  [Codigo] Detectado 1 campo de codigo completo. Rellenando...")
                target = code_inputs[0]
                target.focus()
                target.fill("")
                target.type(codigo, delay=150)
                print("  [Codigo] Codigo ingresado exitosamente en campo unico.")
                return True
        except Exception:
            continue
            
    print("  [Codigo] [WARN] No se pudo rellenar el codigo via Playwright nativo.")
    return False

# Estructuras globales seguras para hilos para rastrear correos temporales únicos
used_temp_emails = set()
used_temp_emails_lock = threading.Lock()

def generar_correo_con_puntos(base_email: str = "cakeseller1234@gmail.com") -> str:
    """Genera variaciones aleatorias de puntos en un correo de Gmail, garantizando unicidad global."""
    if "@" not in base_email:
        return base_email
    username, domain = base_email.split("@", 1)
    if len(username) <= 1:
        return base_email

    global used_temp_emails, used_temp_emails_lock
    
    for _ in range(100):  # Intentar hasta 100 veces generar uno único
        chars = list(username)
        posiciones = list(range(1, len(username)))
        num_puntos = random.randint(1, min(6, len(posiciones)))
        
        posiciones_elegidas = []
        random.shuffle(posiciones)
        for pos in posiciones:
            if len(posiciones_elegidas) >= num_puntos:
                break
            if not any(abs(pos - p) <= 1 for p in posiciones_elegidas):
                posiciones_elegidas.append(pos)
                
        posiciones_elegidas.sort(reverse=True)
        for pos in posiciones_elegidas:
            chars.insert(pos, ".")
            
        email_generado = "".join(chars) + "@" + domain
        
        with used_temp_emails_lock:
            if email_generado not in used_temp_emails:
                used_temp_emails.add(email_generado)
                return email_generado
                
    # Fallback si por alguna razón no encuentra uno único aleatorio
    return f"{username}+{int(time.time())}_{random.randint(100,999)}@{domain}"

def obtener_credenciales_imap_reales(gmail_user_solicitado: str) -> tuple[str | None, str | None]:
    """Busca en passwords.txt el usuario real de IMAP y su App Password.
    Soporta variaciones de puntos (alias) de forma totalmente genérica.
    """
    pwd_file = SCRIPT_DIR / "passwords.txt"
    if not pwd_file.exists():
        return None, None
        
    lines = pwd_file.read_text(encoding="utf-8").splitlines()
    
    # 1. Limpiar el correo solicitado (remover puntos del username de Gmail)
    gmail_user_solicitado = gmail_user_solicitado.lower().strip()
    if "@gmail.com" in gmail_user_solicitado:
        username, domain = gmail_user_solicitado.split("@", 1)
        solicitado_no_dots = username.replace(".", "") + "@" + domain
    else:
        solicitado_no_dots = gmail_user_solicitado

    user_clean_key = solicitado_no_dots.replace("@", "_at_").replace(".", "_")
    
    # 2. Buscar si hay contraseña específica para el correo solicitado (exacto o ignorando puntos)
    for line in lines:
        if "=" in line:
            key, val = line.split("=", 1)
            key_name = key.strip().lower()
            if key_name.startswith("gmail_app_password_") or key_name.startswith("imap_password_"):
                email_part = key_name[19:].strip() if key_name.startswith("gmail_app_password_") else key_name[14:].strip()
                if "@" in email_part:
                    # Limpiar el email de la clave (remover puntos)
                    usr, dom = email_part.split("@", 1)
                    email_part_no_dots = usr.replace(".", "") + "@" + dom
                    
                    # Si coincide (es el mismo correo con o sin puntos), retornamos
                    if email_part_no_dots == solicitado_no_dots:
                        return email_part, val.strip().strip('"').strip("'")
            
            # Formato clásico de llave limpia
            key_clean = key.strip().lower().replace("@", "_at_").replace(".", "_")
            if (key_clean == f"gmail_app_password_{user_clean_key}" or 
                key_clean == f"gmail_app_password_{solicitado_no_dots}" or
                key_clean == f"imap_password_{user_clean_key}" or
                key_clean == f"imap_password_{solicitado_no_dots}"):
                return solicitado_no_dots, val.strip().strip('"').strip("'")

    # 3. Fallback general: buscar gmail_app_password= o imap_password=
    for line in lines:
        if "=" in line:
            key, val = line.split("=", 1)
            key_stripped = key.strip().lower()
            if key_stripped in ("gmail_app_password", "imap_password"):
                # Si el solicitado contiene cakeseller1234, asumimos cakeseller1234@gmail.com
                if "cakeseller1234" in solicitado_no_dots:
                    return "cakeseller1234@gmail.com", val.strip().strip('"').strip("'")
                # Si no, retornamos el solicitado limpio
                return solicitado_no_dots, val.strip().strip('"').strip("'")

    # 4. Si no coincide con el solicitado, pero hay OTRAS cuentas específicas en passwords.txt,
    # y el solicitado NO es una de ellas, retornamos la primera cuenta específica como fallback
    # (ya que todos los correos de Tidal se reciben ahí por reenvío)
    for line in lines:
        if "=" in line and line.strip().lower().startswith("gmail_app_password_"):
            key, val = line.split("=", 1)
            key_name = key.strip().lower()
            email_part = key_name[19:].strip()
            if "@" in email_part:
                return email_part, val.strip().strip('"').strip("'")
                
    return None, None


def obtener_max_email_id(gmail_user="cakeseller1234@gmail.com", query_from="tidal") -> int:
    """Obtiene el ID numérico más alto (más reciente) de los correos de Tidal en el buzón.
    Esto sirve como marca de tiempo para saber qué correos existían antes de disparar una acción.
    """
    import imaplib
    user_real, app_pwd = obtener_credenciales_imap_reales(gmail_user)
    if not user_real or not app_pwd:
        return 0
    try:
        # Determinar servidor IMAP según el dominio del correo
        domain = user_real.split("@")[-1].lower() if "@" in user_real else "gmail.com"
        if "outlook" in domain or "hotmail" in domain or "live.com" in domain:
            imap_server = "outlook.office365.com"
        elif "yahoo" in domain:
            imap_server = "imap.mail.yahoo.com"
        elif "icloud" in domain:
            imap_server = "imap.mail.me.com"
        else:
            imap_server = "imap.gmail.com"

        mail = imaplib.IMAP4_SSL(imap_server)
        mail.login(user_real, app_pwd)
        mail.select("INBOX")
        status, messages = mail.search(None, f'(FROM "{query_from}")')
        if status == "OK" and messages[0]:
            ids = [int(x) for x in messages[0].split() if x.isdigit()]
            if ids:
                mail.logout()
                return max(ids)
        mail.logout()
    except Exception as e:
        print(f"    [IMAP] [WARN] Error en obtener_max_email_id para {gmail_user} (conectado a {user_real}): {e}")
    return 0


def vpn_surfshark_conectar(ubicacion="nigeria"):
    import subprocess
    import sys
    print(f"  [Surfshark] Intentando conectar VPN a la ubicación '{ubicacion}'...")
    try:
        script_path = os.path.join(os.path.dirname(__file__), "surfshark_reconectar.py")
        res = subprocess.run([sys.executable, script_path, "--ubicacion", ubicacion], capture_output=True, text=True)
        if res.returncode == 0:
            print(f"  [Surfshark] [OK] VPN conectada exitosamente a '{ubicacion}'.")
            return True
        else:
            print(f"  [Surfshark] [WARN] No se pudo conectar automáticamente: {res.stderr or res.stdout}")
    except Exception as e:
        print(f"  [Surfshark] [WARN] Error al ejecutar el script de reconexión: {e}")
    return False


def vpn_surfshark_desconectar():
    import subprocess
    import sys
    print("  [Surfshark] Intentando desconectar VPN...")
    try:
        script_path = os.path.join(os.path.dirname(__file__), "surfshark_reconectar.py")
        res = subprocess.run([sys.executable, script_path, "--solo-desconectar"], capture_output=True, text=True)
        if res.returncode == 0:
            print("  [Surfshark] [OK] VPN desconectada exitosamente.")
            return True
        else:
            print(f"  [Surfshark] [WARN] No se pudo desconectar automáticamente: {res.stderr or res.stdout}")
    except Exception as e:
        print(f"  [Surfshark] [WARN] Error al ejecutar el script de desconexión: {e}")
    return False


def obtener_codigo_via_imap(gmail_user="cakeseller1234@gmail.com", gmail_app_password=None, 
                             query_from="tidal", required_keywords=None, query_exclude=None, 
                             max_age_minutes=15, after_email_id=0, solo_link=False) -> str | None:
    """Lee correos de Gmail via IMAP sin necesidad de abrir el navegador.
    Requiere una 'App Password' de Google.
    Busca dinámicamente en passwords.txt la contraseña específica del correo."""
    import imaplib
    import email
    from email.header import decode_header
    from datetime import datetime, timedelta
    
    user_real, app_pwd = obtener_credenciales_imap_reales(gmail_user)
    if not user_real or not app_pwd:
        print(f"    [IMAP] No se encontraron credenciales de IMAP para {gmail_user}.")
        return None
    
    try:
        # Determinar servidor IMAP según el dominio del correo
        domain = user_real.split("@")[-1].lower() if "@" in user_real else "gmail.com"
        if "outlook" in domain or "hotmail" in domain or "live.com" in domain:
            imap_server = "outlook.office365.com"
        elif "yahoo" in domain:
            imap_server = "imap.mail.yahoo.com"
        elif "icloud" in domain:
            imap_server = "imap.mail.me.com"
        else:
            imap_server = "imap.gmail.com"

        print(f"    [IMAP] Conectando a {imap_server} via IMAP ({user_real})...")
        mail = imaplib.IMAP4_SSL(imap_server)
        mail.login(user_real, app_pwd)
        mail.select("INBOX")
        
        # Buscar correos recientes del remitente
        search_criteria = f'(FROM "{query_from}")'
        status, messages = mail.search(None, search_criteria)
        
        if status != "OK" or not messages[0]:
            print("    [IMAP] No se encontraron correos.")
            mail.logout()
            return None
        
        # Tomar los ultimos 5 correos (mas recientes primero)
        msg_ids = messages[0].split()[-5:]
        msg_ids.reverse()
        
        for msg_id in msg_ids:
            msg_id_int = 0
            try:
                msg_id_int = int(msg_id)
            except ValueError:
                pass
                
            status, msg_data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue
                
            msg = email.message_from_bytes(msg_data[0][1])
            
            # Aceptar si el correo es más reciente por ID, o si tiene una antigüedad menor o igual a 3 minutos (180 segs)
            is_newer_id = (after_email_id == 0 or msg_id_int > after_email_id)
            is_recent_age = False
            try:
                from email.utils import parsedate_to_datetime
                from datetime import datetime, timezone
                date_str = msg.get("Date")
                if date_str:
                    msg_date = parsedate_to_datetime(date_str)
                    now_tz = datetime.now(timezone.utc)
                    age_seconds = (now_tz - msg_date.astimezone(timezone.utc)).total_seconds()
                    if age_seconds <= 180:  # 3 minutos
                        is_recent_age = True
            except Exception:
                pass
                
            if not (is_newer_id or is_recent_age):
                # Ignorar correos antiguos
                continue
            
            # [Reconocer destinatario para evitar mezclar códigos de cuentas paralelas]
            to_header = (msg.get("To") or "").lower()
            delivered_to = (msg.get("Delivered-To") or "").lower()
            envelope_to = (msg.get("Envelope-To") or "").lower()
            destinatario_limpio = gmail_user.lower().strip()
            
            # Para evitar mezclar códigos o enlaces de cuentas paralelas que comparten la misma
            # bandeja de entrada (por usar alias o variaciones de puntos de Gmail),
            # exigimos que la dirección de correo con sus puntos exactos esté presente
            # en los campos de destinatario del correo (To, Delivered-To, Envelope-To).
            recipients = f"{to_header} {delivered_to} {envelope_to}"
            if destinatario_limpio not in recipients:
                # El correo es para otra variación con puntos o alias running en otro hilo
                continue
            
            # Extraer el cuerpo del correo
            body_text = ""
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    # Si encontramos texto plano lo usamos como prioritario
                    if content_type == "text/plain":
                        try:
                            plain_payload = part.get_payload(decode=True).decode("utf-8", errors="replace")
                            # Limpiar citas de Gmail en el texto plano
                            body_text = re.split(r'(?i)-+\s*Original Message\s*-+|^On.*wrote:|^El.*escribió:', plain_payload)[0]
                            break
                        except Exception:
                            pass
                # Si no encontramos texto plano, buscamos en el HTML
                if not body_text:
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        if content_type == "text/html":
                            try:
                                html = part.get_payload(decode=True).decode("utf-8", errors="replace")
                                # Eliminar historial de conversación de Gmail (hilos agrupados)
                                html_clean = re.sub(r'(?i)<div[^>]+class=["\']gmail_quote["\'][\s\S]*', '', html)
                                html_clean = re.sub(r'(?i)<blockquote[\s\S]*', '', html_clean)
                                # Eliminar bloques <style>...</style> y <script>...</script> con su contenido
                                html_clean = re.sub(r'<style[^>]*>[\s\S]*?</style>', ' ', html_clean, flags=re.I)
                                html_clean = re.sub(r'<script[^>]*>[\s\S]*?</script>', ' ', html_clean, flags=re.I)
                                # Extraer texto limpio removiendo etiquetas HTML
                                body_text = re.sub(r'<[^>]+>', ' ', html_clean)
                                break
                            except Exception:
                                pass
            else:
                try:
                    raw_payload = msg.get_payload(decode=True).decode("utf-8", errors="replace")
                    body_text = re.split(r'(?i)-+\s*Original Message\s*-+|^On.*wrote:|^El.*escribió:', raw_payload)[0]
                except Exception:
                    pass
            
            if not body_text:
                continue
            
            # Verificar keywords requeridas
            if required_keywords:
                cumple = any(kw.lower() in body_text.lower() for kw in required_keywords)
                if not cumple:
                    continue
            
            # Verificar exclusion
            if query_exclude and query_exclude.lower() in body_text.lower():
                continue
            
            # Buscar codigo de 5 o 6 digitos (Tidal envia codigos de 5 digitos para eliminacion de cuenta) - solo si no se solicita buscar un enlace exclusivamente
            if not solo_link:
                codigos = re.findall(r"\b\d{5,6}\b", body_text)
                if codigos:
                    print(f"    [IMAP] Codigo extraido: {codigos[0]}")
                    mail.logout()
                    return codigos[0]
            
            # Buscar enlaces de confirmacion en el texto plano
            enlaces = re.findall(r'https?://[^\s<>"\']+', body_text)
            
            # Prioridad 0 (máxima): Buscar la URL directa como TEXTO VISIBLE dentro de <a> tags
            # Tidal muestra el enlace directo (login.tidal.com/resetpass/...) como texto del anchor,
            # pero el href apunta a un tracking link. Extraemos el texto visible de cada <a> tag.
            enlace_directo_anchor = None
            try:
                for part in (msg.walk() if msg.is_multipart() else [msg]):
                    if part.get_content_type() == "text/html":
                        html_content = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        html_content_clean = re.sub(r'(?i)<div[^>]+class=["\']gmail_quote["\'][\s\S]*', '', html_content)
                        html_content_clean = re.sub(r'(?i)<blockquote[\s\S]*', '', html_content_clean)
                        a_tags = re.findall(r'<a[^>]+href=["\'][^"\']+["\'][^>]*>([\s\S]*?)</a>', html_content_clean, re.I)
                        for inner_html in a_tags:
                            inner_text = re.sub(r'<[^>]+>', '', inner_html).strip()
                            inner_lower = inner_text.lower()
                            if inner_text.startswith("http") and ("login.tidal.com/resetpass/" in inner_lower or "login.tidal.com/family/" in inner_lower):
                                enlace_directo_anchor = inner_text
                                break
                        if enlace_directo_anchor:
                            break
            except Exception:
                pass
            
            if enlace_directo_anchor:
                print(f"    [IMAP] Enlace directo extraido del texto visible del anchor: {enlace_directo_anchor}")
                mail.logout()
                return enlace_directo_anchor
            
            # Prioridad 0.5: Buscar el href del anchor cuyo texto visible sea "Join Family" u otra variante CTA de invitación
            # El email de Tidal tiene múltiples anchors (logo, botón, soporte, términos).
            # El botón negro "Join Family" es el segundo anchor — su texto visible identifica inequívocamente el enlace correcto.
            JOIN_TEXTS = ["join family", "unirse a la familia", "join the family", "únete", "unirme", "join now", "accept invitation", "aceptar invitación"]
            enlace_join_btn = None
            try:
                for part in (msg.walk() if msg.is_multipart() else [msg]):
                    if part.get_content_type() == "text/html":
                        html_content = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        html_content_clean = re.sub(r'(?i)<div[^>]+class=["\']gmail_quote["\'][\s\S]*', '', html_content)
                        html_content_clean = re.sub(r'(?i)<blockquote[\s\S]*', '', html_content_clean)
                        a_tags_full = re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', html_content_clean, re.I)
                        for href, inner_html in a_tags_full:
                            inner_text = re.sub(r'<[^>]+>', '', inner_html).strip().lower()
                            if any(jt in inner_text for jt in JOIN_TEXTS):
                                enlace_join_btn = href
                                break
                        if enlace_join_btn:
                            break
            except Exception:
                pass
            
            if enlace_join_btn:
                print(f"    [IMAP] Enlace del botón 'Join Family' extraido por texto del anchor: {enlace_join_btn}")
                mail.logout()
                return enlace_join_btn
            
            # Buscar también de forma robusta enlaces dentro de etiquetas href en las partes HTML
            # Esto evita que se pierdan los enlaces reales (como el de 'Join Family' o 'resetpass') al eliminar las etiquetas HTML.
            try:
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/html":
                            html_content = part.get_payload(decode=True).decode("utf-8", errors="replace")
                            # Limpiar cita de Gmail del HTML antes de extraer href
                            html_content_clean = re.sub(r'(?i)<div[^>]+class=["\']gmail_quote["\'][\s\S]*', '', html_content)
                            html_content_clean = re.sub(r'(?i)<blockquote[\s\S]*', '', html_content_clean)
                            html_links = re.findall(r'href=["\'](https?://[^"\']+)["\']', html_content_clean)
                            enlaces.extend(html_links)
                else:
                    if msg.get_content_type() == "text/html":
                        html_content = msg.get_payload(decode=True).decode("utf-8", errors="replace")
                        html_content_clean = re.sub(r'(?i)<div[^>]+class=["\']gmail_quote["\'][\s\S]*', '', html_content)
                        html_content_clean = re.sub(r'(?i)<blockquote[\s\S]*', '', html_content_clean)
                        html_links = re.findall(r'href=["\'](https?://[^"\']+)["\']', html_content_clean)
                        enlaces.extend(html_links)
            except Exception:
                pass
            
            # Prioridad 1: Buscar enlaces directos de Tidal en href (no tracking)
            for link in enlaces:
                link_lower = link.lower()
                if any(x in link_lower for x in ["/privacy", "/terms", "/legal", "support.tidal.com", "tidal.com/es", "tidal.com/en", "tidal.com/us"]):
                    continue
                if "login.tidal.com/resetpass/" in link_lower or "login.tidal.com/family/" in link_lower or "/accept/" in link_lower or "/join/" in link_lower:
                    print(f"    [IMAP] Enlace directo de Tidal extraido: {link}")
                    mail.logout()
                    return link
            
            # Prioridad 2: Fallback a cualquier otro enlace dinámico de Tidal (incluyendo tracking click/ablink)
            for link in enlaces:
                link_lower = link.lower()
                if any(x in link_lower for x in ["/privacy", "/terms", "/legal", "support.tidal.com", "tidal.com/es", "tidal.com/en", "tidal.com/us"]):
                    continue
                if "tidal.com" in link_lower:
                    print(f"    [IMAP] Enlace de confirmacion (tracking) extraido: {link}")
                    mail.logout()
                    return link
        
        print("    [IMAP] No se encontro codigo ni enlace en los correos recientes.")
        mail.logout()
        return None
        
    except imaplib.IMAP4.error as e:
        err_msg = str(e).encode('ascii', errors='replace').decode('ascii')
        print(f"    [IMAP] Error de autenticacion IMAP para {gmail_user}: {err_msg}")
        return None
    except Exception as e:
        err_msg = str(e).encode('ascii', errors='replace').decode('ascii')
        print(f"    [IMAP] Error IMAP: {err_msg}")
        return None


def obtener_codigo_de_gmail(page, email_destinatario, query="from:tidal", required_keywords=None, query_exclude=None, after_email_id=0, solo_link=False) -> str | None:
    """Obtiene codigo de Gmail. Intenta IMAP primero (sin navegador), luego fallback al navegador."""
    
    # 1. Intentar via IMAP (no requiere navegador, no hay captcha)
    resultado_imap = obtener_codigo_via_imap(
        gmail_user=email_destinatario,
        query_from=query.replace("from:", "").strip(),
        required_keywords=required_keywords,
        query_exclude=query_exclude,
        after_email_id=after_email_id,
        solo_link=solo_link
    )
    if resultado_imap:
        return resultado_imap
    
    # Si IMAP falló o no está configurado, retornar None para pedir el código manualmente en consola
    return None

# Helpers para búsqueda recursiva en todos los frames (soluciona iframes del login de TIDAL)
def encontrar_locator_en_frames(page, selectors: list, label_regex=None, text_regex=None):
    """Busca en todos los frames de la página y devuelve el primer localizador que sea visible."""
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel)
                cnt = loc.count()
                for idx in range(cnt):
                    btn = loc.nth(idx)
                    if btn.is_visible():
                        return btn
            except Exception:
                continue
        if label_regex:
            try:
                loc = frame.get_by_label(label_regex)
                cnt = loc.count()
                for idx in range(cnt):
                    btn = loc.nth(idx)
                    if btn.is_visible():
                        return btn
            except Exception:
                pass
        if text_regex:
            try:
                loc = frame.get_by_text(text_regex)
                cnt = loc.count()
                for idx in range(cnt):
                    btn = loc.nth(idx)
                    if btn.is_visible():
                        return btn
            except Exception:
                pass
    return None

def esperar_locator_en_frames(page, selectors: list, label_regex=None, text_regex=None, timeout_s=15.0):
    """Espera a que un selector aparezca en alguno de los frames y lo devuelve."""
    # Aumentar la resiliencia ante latencia de proxies residenciales
    # Si el timeout especificado es menor a 15s, lo subimos a 15s. Si es mayor, aseguramos al menos 25s.
    timeout_s = max(timeout_s, 15.0) if timeout_s < 15.0 else max(timeout_s, 25.0)
    
    start_time = time.time()
    while time.time() - start_time < timeout_s:
        if detectar_pantalla_antirobot(page):
            manejar_bloqueos_e_intervencion(page, "Bloqueo detectado durante la espera de elementos")
            # Resetear el tiempo inicial para que el usuario tenga todo el timeout original tras superar el captcha
            start_time = time.time()
            continue
            
        loc = encontrar_locator_en_frames(page, selectors, label_regex, text_regex)
        if loc:
            return loc
        time.sleep(0.5)
    return None

def navegar_con_bypass_referencia(page, url):
    """Navega a TIDAL acumulando reputación para evitar bloqueos CloudFront iniciales, con reintentos para proxies."""
    for intento in range(1, 4):
        try:
            print(f"  [Bypass] Cargando tidal.com/pricing primero para acumular reputación (intento {intento}/3)...")
            page.goto("https://tidal.com/pricing", wait_until="domcontentloaded", timeout=30000)
            manejar_bloqueos_e_intervencion(page, "Bypass Precios")
            time.sleep(random.uniform(2.0, 3.5))
            aceptar_cookies_con_espera(page)
            time.sleep(random.uniform(0.5, 1.0))
            
            # En vez de intentar cliquear botones que cambian de diseño constantemente,
            # navegamos directo al destino pasando el referer para simular la redirección de precios.
            print("  [Bypass] Redirigiendo al destino vía referer...")
            page.goto(url, wait_until="domcontentloaded", timeout=30000, referer="https://tidal.com/pricing")
            manejar_bloqueos_e_intervencion(page, "Bypass Destino")
            return
        except Exception as e:
            print(f"  [Bypass] [WARN] Intento {intento}/3 falló al navegar a {url}: {e}")
            if intento == 3:
                # Si fallaron los 3 intentos, probar una navegación directa limpia como último recurso
                try:
                    print("  [Bypass] Intentando navegación directa limpia como último recurso...")
                    page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    manejar_bloqueos_e_intervencion(page, "Navegación Directa")
                    return
                except Exception as final_err:
                    raise final_err
            time.sleep(3.0)


def autorizar_tidal_en_tmm(tmm_page, client_email=None) -> None:
    """Maneja el flujo de autorización de TIDAL en TuneMyMusic, incluyendo popups de login y modales."""
    # 1. Comprobar si aparece el modal de seleccionar cuenta o agregar cuenta
    btn_agregar = tmm_page.locator("button:has-text('Agregar una cuenta nueva')").or_(tmm_page.locator("text=Agregar una cuenta nueva")).first
    popup = None
    
    if esperar_visibilidad(btn_agregar, 3500):
        print("  [TMM Auth] Modal de selección de cuenta detectado. Pulsando 'Agregar una cuenta nueva'...")
        try:
            with tmm_page.context.expect_page(timeout=10000) as popup_info:
                btn_agregar.click()
            popup = popup_info.value
        except Exception as e:
            print(f"  [TMM Auth] Error al abrir popup tras pulsar 'Agregar una cuenta nueva': {e}")
    else:
        # Si no hay modal de selección, al hacer clic en TIDAL puede abrirse el popup directamente
        # Capturar popup en el contexto en caso de que esté cargando
        time.sleep(2.0)
        if len(tmm_page.context.pages) > 1:
            popup = tmm_page.context.pages[-1]
            print("  [TMM Auth] Popup de TIDAL detectado en segundo plano.")

    # 2. Si hay popup, interactuar con él
    if popup:
        try:
            popup.bring_to_front()
            popup.wait_for_load_state("domcontentloaded", timeout=10000)
            print("  [TMM Auth] Procesando popup de TIDAL...")
            
            # Esperar botón "Sí, continuar" (pantalla de confirmación de cuenta — solo aparece a veces)
            btn_si = popup.locator("button:has-text('Sí, continuar')").or_(popup.locator("button:has-text('Si, continuar')")).or_(popup.locator("button:has-text('Yes, continue')")).first
            if esperar_visibilidad(btn_si, 5000):
                for intento_si in range(1, 3):
                    try:
                        print(f"  [TMM Auth] Pulsando 'Sí, continuar' en popup (intento {intento_si}/2)...")
                        btn_si.evaluate("el => el.click()")
                    except Exception:
                        try:
                            btn_si.click(force=True, timeout=2000)
                        except Exception:
                            pass
                    time.sleep(2.0)
                    try:
                        if not btn_si.is_visible():
                            break
                    except Exception:
                        break
            
            # Esperar botón "Continuar" (pantalla de permisos/conectar aplicación)
            # El botón puede estar debajo del fold — hay que hacer scroll antes de clicarlo
            btn_cont = popup.locator("button:has-text('Continuar')").or_(popup.locator("button:has-text('Continue')")).first
            if esperar_visibilidad(btn_cont, 10000):
                try:
                    btn_cont.scroll_into_view_if_needed(timeout=5000)
                    time.sleep(0.5)
                except Exception:
                    pass
                
                # Bucle de clics con espera para contrarrestar listeners tardíos (React/Vue)
                clicado_exito = False
                for intento_click in range(1, 4):
                    try:
                        print(f"  [TMM Auth] Pulsando 'Continuar' en popup (intento {intento_click}/3)...")
                        btn_cont.evaluate("el => el.click()")
                    except Exception:
                        try:
                            btn_cont.click(force=True, timeout=3000)
                        except Exception:
                            pass
                    
                    time.sleep(3.0)
                    try:
                        if popup.is_closed() or "login.tidal.com" not in popup.url:
                            clicado_exito = True
                            break
                    except Exception:
                        clicado_exito = True
                        break
                
            # Esperar a que el popup se cierre (aumentado a 25 segundos para tolerar lag del proxy)
            try:
                popup.wait_for_event("close", timeout=25000)
                print("  [TMM Auth] Popup de autorización de TIDAL cerrado de forma automática.")
            except Exception:
                # Si no se cerró solo tras 25 segundos, verificar si está en la página de callback de TMM y esperar un poco más
                try:
                    if popup.is_closed():
                        pass
                    elif "tunemymusic.com" in popup.url:
                        print("  [TMM Auth] Popup en tunemymusic.com. Esperando 5 segundos adicionales a que se auto-cierre...")
                        time.sleep(5.0)
                        if not popup.is_closed():
                            popup.close()
                    elif "login.tidal.com" not in popup.url:
                        print("  [TMM Auth] El popup navegó fuera de Tidal y no está en tunemymusic.com. Cerrándolo...")
                        popup.close()
                    else:
                        print("  [TMM Auth] [WARN] El popup sigue abierto en Tidal. Reintentando clic y forzando cierre...")
                        btn_cont.evaluate("el => el.click()")
                        time.sleep(2.0)
                        popup.close()
                except Exception:
                    try:
                        popup.close()
                    except Exception:
                        pass
        except Exception as e:
            print(f"  [TMM Auth] Nota/Aviso en popup de TIDAL: {e}. Intentando continuar...")
            try:
                popup.close()
            except Exception:
                pass
                
    time.sleep(3.0)
    
    # 2.5 Comprobar primero si aparece el modal "Name your TIDAL Accounts" (Nombra tus cuentas de TIDAL) y cerrarlo
    print("  [TMM Auth] Comprobando si aparece el modal 'Name your TIDAL Accounts'...")
    modal_title = tmm_page.locator("text='Name your TIDAL Accounts'").or_(tmm_page.locator("text='Nombra tus cuentas de TIDAL'")).first
    if esperar_visibilidad(modal_title, 5000):
        print("  [TMM Auth] Modal 'Name your TIDAL Accounts' detectado. Cerrando con la X...")
        cerrado = False
        
        # Intentar múltiples selectores del botón X en orden de fiabilidad
        x_selectors = [
            "button[aria-label='Close']",
            "button[aria-label='Cerrar']",
            "button[aria-label='close']",
            # Variantes del carácter × (times, multiplication sign, etc.)
            "button:has-text('×')",
            "button:has-text('✕')",
            "button:has-text('✖')",
            "button:has-text('x')",
            "button:has-text('X')",
            # Selector por clase típica de botón de cierre
            "button[class*='close' i]",
            "button[class*='Close' i]",
            "button[class*='dismiss' i]",
        ]
        
        for selector in x_selectors:
            try:
                btn = tmm_page.locator(selector).first
                if btn.count() > 0 and btn.is_visible():
                    btn.scroll_into_view_if_needed(timeout=2000)
                    btn.click(force=True)
                    time.sleep(1.5)
                    if not modal_title.is_visible():
                        print(f"  [TMM Auth] Modal cerrado con selector: {selector}")
                        cerrado = True
                        break
            except Exception:
                continue
        
        if not cerrado:
            # Fallback JS: hacer clic en el primer botón visible del modal
            print("  [TMM Auth] Cerrando via JS nativo...")
            closed = tmm_page.evaluate("""
                () => {
                    const modal = Array.from(document.querySelectorAll('[role="dialog"], .modal, [class*="modal" i]'))
                        .find(e => e.offsetParent !== null);
                    if (modal) {
                        const closeBtn = modal.querySelector('button[aria-label="Close"]')
                            || modal.querySelector('button[aria-label="Cerrar"]')
                            || modal.querySelector('button[class*="close" i]')
                            || Array.from(modal.querySelectorAll('button')).find(b => 
                                ['×','✕','✖','x','X'].includes(b.textContent.trim())
                            )
                            || modal.querySelector('button');
                        if (closeBtn) {
                            closeBtn.click();
                            return 'closed_via_button';
                        }
                    }
                    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
                    return 'escape_dispatched';
                }
            """)
            print(f"  [TMM Auth] Resultado JS: {closed}")
            time.sleep(1.5)
        
        # Último recurso: Escape de teclado
        if modal_title.is_visible():
            print("  [TMM Auth] Modal aún visible. Usando Escape de teclado...")
            tmm_page.keyboard.press("Escape")
            time.sleep(1.0)
            
    # 2.6 Si el modal "Seleccionar cuenta" está abierto, seleccionar la cuenta correcta y hacer clic en Continuar
    modal_select_title = tmm_page.locator("text='Seleccionar cuenta'").or_(tmm_page.locator("text='Select account'")).or_(tmm_page.locator("text='Selecciona la cuenta con la que'")).first
    btn_continuar_tmm = tmm_page.locator("button:has-text('Continuar')").or_(tmm_page.locator("button:has-text('Continue')")).first
    
    if esperar_visibilidad(modal_select_title, 5000):
        print("  [TMM Auth] El modal 'Seleccionar cuenta' está abierto. Buscando la cuenta recién agregada...")
        cuenta_seleccionada = False
        
        # Esperar hasta 25 segundos a que la lista se actualice y aparezca la cuenta
        start_wait = time.time()
        while time.time() - start_wait < 25.0:
            # Forzar scroll hacia abajo del modal para revelar ítems ocultos y forzar recargas del DOM en TMM
            try:
                tmm_page.evaluate("""
                    () => {
                        const containers = document.querySelectorAll('[class*="scrollbar" i], [class*="list" i], [class*="accounts" i], [role="dialog"] .modal-body, .accounts-list');
                        for (const c of containers) {
                            c.scrollTop = c.scrollHeight;
                        }
                    }
                """)
            except Exception:
                pass
                
            if client_email:
                email_clean = client_email.lower().strip()
                variantes = [email_clean, email_clean.replace(".", "")]
                if "@" in email_clean:
                    user_part = email_clean.split("@")[0]
                    variantes.append(user_part)
                    variantes.append(user_part.replace(".", ""))
                
                for var in variantes:
                    try:
                        loc = tmm_page.locator(f"text='{var}'").first
                        # Comprobar presencia en el DOM (count > 0) sin requerir visibilidad inicial, 
                        # ya que scroll_into_view_if_needed la hará visible antes de clicar
                        if loc.count() > 0:
                            loc.scroll_into_view_if_needed(timeout=3000)
                            time.sleep(0.5)
                            loc.click(force=True)
                            print(f"  [TMM Auth] Seleccionada cuenta en la lista mediante texto: '{var}'")
                            cuenta_seleccionada = True
                            time.sleep(1.0)
                            break
                    except Exception:
                        continue
            
            if cuenta_seleccionada:
                break
                
            time.sleep(1.5)
            
        # Si el modal está abierto y NO pudimos seleccionar la cuenta correcta, abortamos para evitar transferir al titular equivocado
        if not cuenta_seleccionada:
            raise RuntimeError(f"El modal 'Seleccionar cuenta' de TuneMyMusic está abierto, pero la cuenta '{client_email}' no apareció en la lista tras esperar 20s. Se aborta para evitar transferir playlists a una cuenta incorrecta.")
            
        # Hacer clic en Continuar
        try:
            print("  [TMM Auth] Pulsando 'Continuar' en el modal de selección de cuenta...")
            btn_continuar_tmm.click(force=True)
            time.sleep(3.0)
        except Exception as e:
            print(f"  [TMM Auth] [WARN] No se pudo pulsar 'Continuar': {e}")


class TidalMigrationManager:
    def __init__(self, main_profile, parent_profile, client_email, client_pwd, target_pwd,
                 use_proxy=False, proxy_pe_server=None, proxy_pe_user=None, proxy_pe_pass=None,
                 proxy_ng_server=None, proxy_ng_user=None, proxy_ng_pass=None, batch_mode=False,
                 start_step=1, reset_password_first=False, headless=False):
        self.main_profile = main_profile
        self.parent_profile = parent_profile
        self.client_email = client_email
        self.client_pwd = client_pwd
        self.target_pwd = target_pwd
        self.use_proxy = use_proxy
        self.proxy_pe_server = proxy_pe_server
        self.proxy_pe_user = proxy_pe_user
        self.proxy_pe_pass = proxy_pe_pass
        self.proxy_ng_server = proxy_ng_server
        self.proxy_ng_user = proxy_ng_user
        self.proxy_ng_pass = proxy_ng_pass
        self.batch_mode = batch_mode
        self.start_step = start_step
        self.reset_password_first = reset_password_first
        self.headless = headless
        self.new_email_temp = None
        self.cuenta_abortada = False
        self.abort_reason = None
        self.csv_path = None
        self.skip_playlists = False
        self.reset_baseline_id = 0
        self.invite_baseline_id = 0
        self.total_bytes_transferred = 0
        self.user_agent = None

    def forzar_modo_visual(self):
        """Si el navegador está en modo headless, lo cierra y lo vuelve a abrir en modo headed (visual) para permitir la intervención del usuario."""
        if not getattr(self, "headless", False):
            return  # Ya es visual, no hace falta hacer nada
            
        print(f"\n  [Modo Headless] Intervención requerida para {self.client_email}. Transicionando a modo visual (Headed)...")
        
        # Guardar URL actual
        current_url = "https://account.tidal.com/"
        try:
            if self.page and not self.page.is_closed():
                current_url = self.page.url
        except Exception:
            pass
            
        # Cambiar el flag a headed
        self.headless = False
        
        # Cerrar el contexto actual de forma segura
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        self.context = None
        self.page = None
        
        # Esperar a que el proceso de Chrome termine y limpiar archivos de lock del perfil
        # El exitCode=2147483651 ocurre cuando Chrome intenta abrir un perfil aún bloqueado
        import subprocess as _sp
        try:
            # Matar cualquier proceso de Chrome/Chromium huérfano que siga usando el perfil
            profile_str = str(self.main_profile)
            _sp.run(
                ["taskkill", "/F", "/IM", "chrome.exe"],
                capture_output=True, timeout=5
            )
        except Exception:
            pass
        time.sleep(2.0)
        
        # Limpiar los archivos Singleton que bloquean el perfil
        import glob
        profile_path = Path(self.main_profile)
        for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            lock_path = profile_path / lock_file
            try:
                if lock_path.exists():
                    lock_path.unlink()
                    print(f"  [Modo Headless] Eliminado archivo de bloqueo: {lock_file}")
            except Exception:
                pass
        # También limpiar el lockfile en subdirectorios comunes
        for pattern in [str(profile_path / "**" / "SingletonLock"), str(profile_path / "**" / "lockfile")]:
            for f in glob.glob(pattern, recursive=True):
                try:
                    Path(f).unlink()
                except Exception:
                    pass
        
        time.sleep(1.0)
        
        # Re-levantar el navegador principal en modo visual con reintentos
        print("  [Modo Headless] Levantando navegador headed...")
        last_error = None
        for intento in range(1, 4):
            try:
                self.asegurar_navegador_abierto()
                if self.page and not self.page.is_closed():
                    self.page.client_email = self.client_email
                    self.page.manager = self
                    if current_url and current_url.startswith("http"):
                        try:
                            self.page.goto(current_url, wait_until="domcontentloaded", timeout=25000)
                            time.sleep(2.0)
                        except Exception:
                            pass  # No es crítico si la navegación falla, lo importante es tener el browser abierto
                    print(f"  [Modo Headless] Navegador headed abierto correctamente (intento {intento}/3).")
                    return
            except Exception as e:
                last_error = e
                print(f"  [Modo Headless] [WARN] Intento {intento}/3 de levantar navegador headed falló: {e}")
                # Limpiar antes de reintentar
                try:
                    if self.context:
                        self.context.close()
                except Exception:
                    pass
                self.context = None
                self.page = None
                time.sleep(3.0)
        
        # Si llegamos aquí, ningún intento funcionó
        print(f"  [Modo Headless] [ERROR] No se pudo abrir el navegador headed tras 3 intentos. Último error: {last_error}")
        raise RuntimeError(f"No se pudo transicionar a modo visual: {last_error}")

    def input_concurrente(self, prompt):
        self.forzar_modo_visual()
        return input_concurrente(prompt, self.client_email)

    def ejecutar_rotacion_proxy_y_recargar(self):
        """Rotará la IP del proxy y volverá a cargar la página actual con la nueva IP."""
        # Determinar si estamos usando el proxy de Nigeria o Perú en esta instancia
        tipo = "PE"
        if getattr(self, "proxy_ng_server", None) and self.start_step == 6:
            tipo = "NG"
            
        print(f"\n  [Auto-Proxy] Bloqueo detectado en {self.client_email}. Rotando proxy de tipo {tipo}...")
        
        # Guardar URL actual
        current_url = "https://account.tidal.com/"
        try:
            if self.page:
                current_url = self.page.url
        except Exception:
            pass
            
        # Rotar el proxy (esto cierra el contexto y llama a asegurar_navegador_abierto)
        self.rotar_proxy_contexto(tipo=tipo)
        
        # Limpiar cookies de Tidal para evitar propagar el bloqueo de la IP/sesión anterior
        try:
            if self.context:
                print("  [Auto-Proxy] Limpiando cookies antiguas de Tidal...")
                self.context.clear_cookies(domain="tidal.com")
                self.context.clear_cookies(domain="login.tidal.com")
                self.context.clear_cookies(domain="account.tidal.com")
        except Exception:
            pass
            
        # Re-navegar a la URL original con el nuevo proxy
        if current_url and current_url.startswith("http"):
            print(f"  [Auto-Proxy] Recargando página con el nuevo proxy en: {current_url}")
            try:
                self.page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2.0)
            except Exception as e:
                print(f"  [Auto-Proxy] [WARN] Error al recargar con la nueva IP: {e}")

    def registrar_contador_datos(self, context):
        """Registra un listener en el contexto para sumar los bytes transferidos de forma exacta."""
        def on_request_finished(request):
            try:
                sizes = request.sizes
                # Sumar tamaño de cabeceras y cuerpos (tanto subida como bajada)
                download = sizes.get("responseBodySize", 0) + sizes.get("responseHeadersSize", 0)
                upload = sizes.get("requestBodySize", 0) + sizes.get("requestHeadersSize", 0)
                self.total_bytes_transferred += (download + upload)
            except Exception:
                pass
        context.on("requestfinished", on_request_finished)



    def abort_all_barriers(self):
        global barrier_step1, barrier_step2, barrier_step3_4, barrier_step5, barrier_step6, barrier_step7, barrier_step8, barrier_step9, barrier_step10
        for b in [barrier_step1, barrier_step2, barrier_step3_4, barrier_step5, barrier_step6, barrier_step7, barrier_step8, barrier_step9, barrier_step10]:
            if b is not None:
                try:
                    b.abort()
                except Exception:
                    pass

    def registrar_error_y_abortar(self, exception, paso_nombre):
        """Registra un error en el hilo actual, marca la cuenta como abortada y cierra el navegador."""
        if not self.cuenta_abortada:
            print(f"  [ERROR] {paso_nombre} falló para {self.client_email}: {exception}")
            self.cuenta_abortada = True
            self.abort_reason = f"Error en {paso_nombre}: {exception}"
            self.abort_all_barriers()
            try:
                self.context.close()
            except Exception:
                pass

    def esperar_sincronizacion(self, barrier, nombre_paso):
        if barrier is not None:
            print(f"  [Lote] Cuenta {self.client_email} completó {nombre_paso}. Esperando a que las demás cuentas terminen este paso...")
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                raise RuntimeError(f"La sincronización de {nombre_paso} falló porque otra cuenta del lote tuvo un error.")

    def run_pipeline(self):
        with sync_playwright() as p:
            self.playwright = p
            # Determinar canal y User-Agent dinámico limpio para esta sesión
            channel = None if (self.use_proxy and (self.proxy_pe_server or self.proxy_ng_server)) else "chrome"
            self.user_agent = obtener_user_agent_limpio(p, channel=channel)
            print(f"  [User-Agent] UA dinámico detectado y limpio: {self.user_agent}")
            # 1. Lanzar el navegador principal con el perfil persistente
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--credentials-enable-service=false",
                "--password-store=basic",
                "--disable-autofill",
                "--disable-save-password-bubble",
                "--disable-gpu",
                "--disable-software-rasterizer"
            ]
            
            proxy_dict = None
            if self.use_proxy and self.proxy_pe_server:
                proxy_dict = {"server": self.proxy_pe_server}
                if self.proxy_pe_user:
                    proxy_dict["username"] = self.proxy_pe_user
                if self.proxy_pe_pass:
                    proxy_dict["password"] = self.proxy_pe_pass
                print(f"  [Proxy] Usando proxy de PERÚ para navegador principal: {self.proxy_pe_server}")

            # Si estamos en modo lote, usar una copia temporal del perfil principal para evitar bloqueos
            temp_profile_dir = None
            if self.batch_mode:
                email_sanitizado = self.client_email.replace("@", "_at_").replace(".", "_")
                temp_profile_dir = preparar_perfil_temporal(self.main_profile, email_sanitizado)
                # Reemplazar self.main_profile para que todos los relanzamientos en step6 y step7 usen este mismo directorio temporal
                self.main_profile = temp_profile_dir
            actual_profile = self.main_profile

            print(f"\n>>> Iniciando navegador principal con perfil: {actual_profile} (batch_mode={self.batch_mode})...")

            launch_kwargs = {
                "user_data_dir": str(actual_profile),
                "headless": self.headless,
                "args": launch_args,
                "ignore_default_args": ["--enable-automation"],
                "viewport": {"width": 1280, "height": 800},
                "locale": "es-ES",
                "proxy": proxy_dict
            }
            if not proxy_dict:
                launch_kwargs["channel"] = "chrome"
            if self.headless:
                launch_kwargs["user_agent"] = self.user_agent
            self.context = p.chromium.launch_persistent_context(**launch_kwargs)
            self.context.set_default_navigation_timeout(45000)
            self.context.set_default_timeout(35000)
            
            # Registrar script de stealth completo y contador de datos
            self.registrar_contador_datos(self.context)
            self.context.add_init_script(STEALTH_SCRIPT)
            
            # Cerrar pestañas adicionales si las hay para evitar operar en segundo plano
            while len(self.context.pages) > 1:
                try:
                    self.context.pages[-1].close()
                except Exception:
                    pass
            
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.client_email = self.client_email
            self.page.manager = self
            self.page.bring_to_front()
            
            try:
                # Paso 1 & Asegurar Login
                for intento in range(1, 4):
                    try:
                        if self.cuenta_abortada:
                            break
                        if self.start_step > 1 and self.start_step <= 5:
                            self.asegurar_login_cuenta_cliente()
                        if self.start_step <= 1:
                            self.step1_login_tidal()
                        break
                    except Exception as e:
                        if "contraseña o usuario incorrecto" in str(e).lower() or self.cuenta_abortada:
                            self.registrar_error_y_abortar(e, "Paso 1 (Login)")
                            break
                        if intento == 3:
                            self.registrar_error_y_abortar(e, "Paso 1 (Login)")
                        else:
                            print(f"  [WARN] Paso 1 (Login) falló (intento {intento}/3): {e}. Rotando proxy PE...")
                            try:
                                self.rotar_proxy_contexto(tipo="PE")
                            except Exception:
                                pass
                            time.sleep(3.0)
                if not self.cuenta_abortada and self.start_step <= 1:
                    guardar_progreso_migracion(self.client_email, 1)
                self.esperar_sincronizacion(barrier_step1, "Paso 1 (Login)")
                
                # Paso 2
                for intento in range(1, 4):
                    try:
                        if self.cuenta_abortada:
                            break
                        if self.start_step <= 2:
                            self.step2_change_email()
                        break
                    except Exception as e:
                        if intento == 3:
                            self.registrar_error_y_abortar(e, "Paso 2 (Cambio de email)")
                        else:
                            print(f"  [WARN] Paso 2 (Cambio de email) falló (intento {intento}/3): {e}. Rotando proxy PE...")
                            try:
                                self.rotar_proxy_contexto(tipo="PE")
                            except Exception:
                                pass
                            time.sleep(3.0)
                if not self.cuenta_abortada and self.start_step <= 2:
                    guardar_progreso_migracion(self.client_email, 2)
                self.esperar_sincronizacion(barrier_step2, "Paso 2 (Cambio de email)")
                
                # Paso 3 & 4
                for intento in range(1, 4):
                    try:
                        if self.cuenta_abortada:
                            break
                        if self.start_step <= 4:
                            self.step3_4_export_to_csv()
                        break
                    except Exception as e:
                        if intento == 3:
                            self.registrar_error_y_abortar(e, "Paso 3 & 4 (Exportación)")
                        else:
                            print(f"  [WARN] Paso 3 & 4 (Exportación) falló (intento {intento}/3): {e}. Rotando proxy PE...")
                            try:
                                self.rotar_proxy_contexto(tipo="PE")
                            except Exception:
                                pass
                            time.sleep(3.0)
                if not self.cuenta_abortada and self.start_step <= 4:
                    guardar_progreso_migracion(self.client_email, 4)
                self.esperar_sincronizacion(barrier_step3_4, "Paso 3 & 4 (Exportación)")
                
                # Paso 5
                for intento in range(1, 4):
                    try:
                        if self.cuenta_abortada:
                            break
                        if self.start_step <= 5:
                            self.step5_delete_account()
                        break
                    except Exception as e:
                        if intento == 3:
                            self.registrar_error_y_abortar(e, "Paso 5 (Eliminar cuenta)")
                        else:
                            print(f"  [WARN] Paso 5 (Eliminar cuenta) falló (intento {intento}/3): {e}. Rotando proxy PE...")
                            try:
                                self.rotar_proxy_contexto(tipo="PE")
                            except Exception:
                                pass
                            time.sleep(3.0)
                
                if not self.cuenta_abortada and self.start_step <= 5:
                    guardar_progreso_migracion(self.client_email, 5)
                # Sincronizar antes de activar la VPN global para el Paso 6
                self.esperar_sincronizacion(barrier_step5, "Paso 5 (Eliminar cuenta)")

                # Paso 6
                for intento in range(1, 4):
                    try:
                        if self.cuenta_abortada:
                            break
                        if self.start_step <= 6:
                            self.step6_create_account()
                        break
                    except Exception as e:
                        if intento == 3:
                            self.registrar_error_y_abortar(e, "Paso 6 (Crear nueva cuenta)")
                        else:
                            print(f"  [WARN] Paso 6 (Crear nueva cuenta) falló (intento {intento}/3): {e}. Rotando proxy NG...")
                            try:
                                self.rotar_proxy_contexto(tipo="NG")
                            except Exception:
                                pass
                            time.sleep(3.0)
                
                if not self.cuenta_abortada and self.start_step <= 6:
                    guardar_progreso_migracion(self.client_email, 6)
                # Sincronizar antes de desactivar la VPN global para volver a IP local
                self.esperar_sincronizacion(barrier_step6, "Paso 6 (Crear nueva cuenta)")

                # Restablecimiento Previo de Contraseña si iniciamos en Paso 7
                for intento in range(1, 4):
                    try:
                        if self.cuenta_abortada:
                            break
                        if self.start_step == 7 and getattr(self, "reset_password_first", False):
                            print("\n--- [Paso 7 - Restablecimiento Previo] Iniciando restablecimiento de contraseña solicitado... ---")
                            self.step8_request_password_reset()
                            if not self.cuenta_abortada:
                                guardar_progreso_migracion(self.client_email, 8)
                            self.esperar_sincronizacion(barrier_step8, "Paso 8 Previo (Solicitar reset)")
                            self.step10_complete_password_reset()
                            if not self.cuenta_abortada:
                                guardar_progreso_migracion(self.client_email, 10)
                            self.esperar_sincronizacion(barrier_step10, "Paso 10 Previo (Completar reset)")
                            print("\n--- [Paso 7 - Restablecimiento Previo] Contraseña restablecida con éxito. Iniciando transferencia... ---")
                        break
                    except Exception as e:
                        if intento == 3:
                            self.registrar_error_y_abortar(e, "Paso 7 - Restablecimiento Previo")
                        else:
                            print(f"  [WARN] Paso 7 - Restablecimiento Previo falló (intento {intento}/3): {e}. Rotando proxy PE...")
                            try:
                                self.rotar_proxy_contexto(tipo="PE")
                            except Exception:
                                pass
                            time.sleep(3.0)

                # Paso 7
                for intento in range(1, 4):
                    try:
                        if self.cuenta_abortada:
                            break
                        if self.start_step <= 7:
                            self.step7_copy_csv_to_new_account()
                        break
                    except Exception as e:
                        if intento == 3:
                            self.registrar_error_y_abortar(e, "Paso 7 (Importación)")
                        else:
                            print(f"  [WARN] Paso 7 (Importación) falló (intento {intento}/3): {e}. Rotando proxy PE...")
                            try:
                                self.rotar_proxy_contexto(tipo="PE")
                            except Exception:
                                pass
                            time.sleep(3.0)
                if not self.cuenta_abortada and self.start_step <= 7:
                    guardar_progreso_migracion(self.client_email, 7)
                self.esperar_sincronizacion(barrier_step7, "Paso 7 (Importación)")
                
                # Paso 8
                for intento in range(1, 4):
                    try:
                        if self.cuenta_abortada:
                            break
                        if self.start_step <= 8:
                            if not getattr(self, "reset_password_first", False):
                                self.step8_request_password_reset()
                        break
                    except Exception as e:
                        if intento == 3:
                            self.registrar_error_y_abortar(e, "Paso 8 (Solicitar reset de clave)")
                        else:
                            print(f"  [WARN] Paso 8 (Solicitar reset de clave) falló (intento {intento}/3): {e}. Rotando proxy PE...")
                            try:
                                self.rotar_proxy_contexto(tipo="PE")
                            except Exception:
                                pass
                            time.sleep(3.0)
                if not self.cuenta_abortada and self.start_step <= 8 and not getattr(self, "reset_password_first", False):
                    guardar_progreso_migracion(self.client_email, 8)
                if not getattr(self, "reset_password_first", False) or self.cuenta_abortada:
                    self.esperar_sincronizacion(barrier_step8, "Paso 8 (Solicitar reset de clave)")
                
                # Paso 9
                for intento in range(1, 4):
                    try:
                        if self.cuenta_abortada:
                            break
                        if self.start_step <= 9:
                            self.step9_invite_to_family_plan(p)
                        break
                    except Exception as e:
                        if intento == 3:
                            self.registrar_error_y_abortar(e, "Paso 9 (Invitar a plan familiar)")
                        else:
                            print(f"  [WARN] Paso 9 (Invitar a plan familiar) falló (intento {intento}/3): {e}. Rotando proxy PE...")
                            try:
                                self.rotar_proxy_contexto(tipo="PE")
                            except Exception:
                                pass
                            time.sleep(3.0)
                if not self.cuenta_abortada and self.start_step <= 9:
                    guardar_progreso_migracion(self.client_email, 9)
                self.esperar_sincronizacion(barrier_step9, "Paso 9 (Invitar a plan familiar)")
                
                # Paso 10
                for intento in range(1, 4):
                    try:
                        if self.cuenta_abortada:
                            break
                        if self.start_step <= 10:
                            if not getattr(self, "reset_password_first", False):
                                self.step10_complete_password_reset()
                        break
                    except Exception as e:
                        if intento == 3:
                            self.registrar_error_y_abortar(e, "Paso 10 (Completar reset de clave)")
                        else:
                            print(f"  [WARN] Paso 10 (Completar reset de clave) falló (intento {intento}/3): {e}. Rotando proxy PE...")
                            try:
                                self.rotar_proxy_contexto(tipo="PE")
                            except Exception:
                                pass
                            time.sleep(3.0)
                if not self.cuenta_abortada and self.start_step <= 10 and not getattr(self, "reset_password_first", False):
                    guardar_progreso_migracion(self.client_email, 10)
                if not getattr(self, "reset_password_first", False) or self.cuenta_abortada:
                    self.esperar_sincronizacion(barrier_step10, "Paso 10 (Completar reset de clave)")
                
                # Paso 11
                for intento in range(1, 4):
                    try:
                        if self.cuenta_abortada:
                            break
                        if self.start_step <= 11:
                            self.step11_accept_family_invite()
                        break
                    except Exception as e:
                        if intento == 3:
                            self.registrar_error_y_abortar(e, "Paso 11 (Aceptar invitación)")
                        else:
                            print(f"  [WARN] Paso 11 (Aceptar invitación) falló (intento {intento}/3): {e}. Rotando proxy PE...")
                            try:
                                self.rotar_proxy_contexto(tipo="PE")
                            except Exception:
                                pass
                            time.sleep(3.0)
                
                if not self.cuenta_abortada and self.start_step <= 11:
                    guardar_progreso_migracion(self.client_email, 11)
                
                if not self.cuenta_abortada:
                    print("\n[EXITO] ¡PROCESO DE MIGRACIÓN COMPLETADO CON ÉXITO! [EXITO]")
                else:
                    print(f"\n[INFO] Hilo finalizado para {self.client_email} (Estado: Abortado debido a errores).")
                # Sumar un 10% de overhead de red (TCP/TLS/IP packet overhead) para alinearse con lo facturado por el proxy
                consumo_facturable = (self.total_bytes_transferred * 1.1) / (1024 * 1024)
                print(f"  [Consumo] Datos de red transferidos: {self.total_bytes_transferred / (1024 * 1024):.2f} MB")
                print(f"  [Consumo] Estimación facturable por Proxy (con 10% de overhead): {consumo_facturable:.2f} MB")
                
            finally:
                consumo_facturable = (self.total_bytes_transferred * 1.1) / (1024 * 1024)
                print(f"\n[Consumo Final] Datos de red transferidos: {self.total_bytes_transferred / (1024 * 1024):.2f} MB")
                print(f"[Consumo Final] Estimación facturable por Proxy (+10% overhead): {consumo_facturable:.2f} MB")
                print("Cerrando contextos de Playwright...")
                try:
                    self.context.close()
                except Exception:
                    pass
                if temp_profile_dir:
                    limpiar_perfil_temporal(temp_profile_dir)

    # --- PASO 1: Iniciar sesión en TIDAL ---
    def asegurar_login_cuenta_cliente(self):
        """Asegura que el navegador principal esté logueado en la cuenta del cliente (antes de proceder a pasos 2-5)."""
        print("  [Sesión] Verificando si la sesión de la cuenta del cliente está activa...")
        try:
            self.page.goto("https://account.tidal.com/profile", wait_until="domcontentloaded", timeout=15000)
            time.sleep(2.0)
            aceptar_cookies_con_espera(self.page)
            
            if "login.tidal.com" not in self.page.url:
                # Extraer el email logueado en pantalla para ver si coincide
                email_activo = None
                for frame in self.page.frames:
                    try:
                        text = frame.evaluate("() => document.body.innerText")
                        emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
                        if emails:
                            email_activo = emails[0].strip()
                            break
                    except Exception:
                        pass
                
                # Comprobar si coincide con el cliente (con o sin puntos) o con el alias de cakeseller1234
                if email_activo:
                    if (self.emails_coinciden_sin_puntos(email_activo, self.client_email) or 
                        self.emails_coinciden_sin_puntos(email_activo, "cakeseller1234@gmail.com")):
                        print(f"  [Sesión] [OK] Sesión activa confirmada para {email_activo}.")
                        return
                        
                print("  [Sesión] La sesión activa no corresponde al cliente o no se pudo verificar. Forzando login...")
            
            # Si no está logueado o es la cuenta incorrecta, ejecutar paso 1 para loguearse
            self.step1_login_tidal()
        except Exception as e:
            print(f"  [Sesión] [WARN] Error al verificar sesión. Intentando login por si acaso: {e}")
            self.step1_login_tidal()

    def step1_login_tidal(self):
        print("\n--- PASO 1: Iniciando sesión en la cuenta Tidal del cliente ---")
        # Comprobar e identificar si hay una cuenta previamente logueada para cerrarla interactivamente
        print("  [Paso 1] Comprobando si hay una sesión previa activa de TIDAL...")
        try:
            self.page.goto("https://account.tidal.com/profile", wait_until="domcontentloaded", timeout=15000)
            time.sleep(2.0)
            aceptar_cookies_con_espera(self.page)
            
            # Si no fuimos redirigidos a login.tidal.com, es que hay una cuenta logueada
            if "login.tidal.com" not in self.page.url:
                print("  [Paso 1] Cuenta previa detectada en perfil. Cerrando sesión de forma interactiva...")
                # 1. Pasar el mouse por encima de "Mi cuenta" (Hover) para desplegar las opciones
                print("  [Paso 1] Pasando el mouse sobre 'Mi cuenta' para desplegar el menu...")
                
                # Intentar hover con Playwright nativo (más realista)
                menu_hovered = False
                try:
                    btn_menu = self.page.locator("text='Mi cuenta'").or_(self.page.locator("text='My account'")).first
                    if btn_menu.count() > 0:
                        btn_menu.hover()
                        menu_hovered = True
                except Exception:
                    pass
                
                # Respaldo: despachar eventos de mouseover/mouseenter vía JS nativo
                self.page.evaluate("""
                    () => {
                        const keywords = ['mi cuenta', 'my account'];
                        const elms = document.querySelectorAll('button, a, [role="button"], div, span');
                        const isVisible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
                        for (const el of elms) {
                            const text = (el.textContent || '').trim().toLowerCase();
                            if (isVisible(el) && keywords.some(kw => text === kw || text.includes(kw))) {
                                const opts = { bubbles: true, cancelable: true, view: window };
                                el.dispatchEvent(new PointerEvent('pointerover', opts));
                                el.dispatchEvent(new PointerEvent('pointerenter', opts));
                                el.dispatchEvent(new MouseEvent('mouseover', opts));
                                el.dispatchEvent(new MouseEvent('mouseenter', opts));
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                
                time.sleep(2.0)
                
                # 2. Hacer clic en "Cerrar sesión" en el menú desplegado
                print("  [Paso 1] Pulsando en 'Cerrar sesión'...")
                logout_clicked = False
                try:
                    btn_logout = self.page.locator("text='Cerrar sesión'").or_(self.page.locator("text='Log out'")).or_(self.page.locator("text='Cerrar'")).first
                    if esperar_visibilidad(btn_logout, 5000):
                        btn_logout.click(force=True)
                        logout_clicked = True
                except Exception:
                    pass
                
                if not logout_clicked:
                    logout_clicked = self.page.evaluate("""
                        () => {
                            const keywords = ['cerrar', 'sesi', 'log out', 'logout', 'sign out'];
                            const elms = document.querySelectorAll('button, a, [role="button"], div, span');
                            const isVisible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
                            for (const el of elms) {
                                if (isVisible(el) && keywords.some(kw => (el.textContent || '').trim().toLowerCase().includes(kw))) {
                                    el.click();
                                    return true;
                                }
                            }
                            return false;
                        }
                    """)
                
                print("  [Paso 1] Clic en cerrar sesión completado. Esperando redirección...")
                time.sleep(3.0)
            else:
                print("  [Paso 1] No se detectó sesión activa previa de TIDAL. Todo limpio.")
        except Exception as e:
            print(f"  [Paso 1] [WARN] Ocurrió un error comprobando la sesión previa: {e}")
            
        # Si ya estamos en login.tidal.com con el input de email/login visible, no hace falta re-navegar ni borrar cookies
        email_selectors = ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]', '#email']
        is_already_on_login = False
        try:
            if "login.tidal.com" in self.page.url:
                for frame in self.page.frames:
                    for sel in email_selectors:
                        if frame.locator(sel).first.is_visible():
                            is_already_on_login = True
                            break
                    if is_already_on_login:
                        break
        except Exception:
            pass

        if not is_already_on_login:
            try:
                # Borrar cookies de dominios de Tidal para destruir cualquier residuo en el contexto
                self.context.clear_cookies(domain="tidal.com")
                self.context.clear_cookies(domain="login.tidal.com")
                self.context.clear_cookies(domain="account.tidal.com")
            except Exception:
                pass
                
            navegar_con_bypass_referencia(self.page, "https://account.tidal.com/")
            time.sleep(2.0)
            aceptar_cookies_con_espera(self.page)
            manejar_bloqueos_e_intervencion(self.page, "Login Tidal (Correo)")
        
        # Rellenar correo buscando recursivamente en frames
        email_selectors = ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]', '#email']
        email_input = esperar_locator_en_frames(self.page, email_selectors, label_regex=re.compile(r"correo|email", re.I), timeout_s=15.0)
        if not email_input:
            raise RuntimeError("No se localizó el campo de correo para iniciar sesión.")
            
        rellenar_campo_humanizado(email_input, self.client_email)
        time.sleep(0.5)
        
        # Buscar botón continuar en frames
        btn_continue = esperar_locator_en_frames(
            self.page, 
            ["button:has-text('Continuar')", "button:has-text('Continue')", "button[type='submit']"],
            text_regex=re.compile(r"continuar|continue", re.I),
            timeout_s=5.0
        )
        if not btn_continue:
            raise RuntimeError("No se encontró el botón 'Continuar'.")
        btn_continue.click()
        time.sleep(3.0)
        
        manejar_bloqueos_e_intervencion(self.page, "Login Tidal (Contraseña)")
        
        # Detectar si TIDAL pide codigo en vez de contrasena
        # Si aparece la pantalla de "Revisa tu correo electronico" con inputs de codigo,
        # hay que pulsar "Inicia sesion con contrasena" para cambiar al modo password.
        pwd_input = esperar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]'], timeout_s=4.0)
        if not pwd_input:
            print("  [Paso 1] No se encontro campo de contrasena. Verificando si pide codigo...")
            btn_pwd_mode = esperar_locator_en_frames(
                self.page,
                ["a:has-text('contraseña')", "button:has-text('contraseña')",
                 "a:has-text('password')", "button:has-text('password')",
                 "text='Inicia sesión con contraseña'", "text='Sign in with password'"],
                text_regex=re.compile(r"con contrase|with password|iniciar.*contrase|sign.*password", re.I),
                timeout_s=5.0
            )
            if btn_pwd_mode:
                print("  [Paso 1] Pantalla de codigo detectada. Pulsando 'Inicia sesion con contrasena'...")
                btn_pwd_mode.click()
                time.sleep(3.0)
                pwd_input = esperar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]'], timeout_s=10.0)
        
        if not pwd_input:
            raise RuntimeError("No se localizó el campo de contraseña.")
            
        rellenar_campo_humanizado(pwd_input, self.client_pwd)
        time.sleep(0.5)
        
        # Iniciar sesión (Prioriza button[type='submit'] exacto para evitar enlaces de "iniciar sesión con código")
        btn_login = esperar_locator_en_frames(
            self.page,
            [
                "button[type='submit']",
                "button:has-text(/^Iniciar sesión$/)", "button:has-text(/^Log in$/)", 
                "button:has-text(/^Continuar$/)", "button:has-text(/^Continue$/)", 
                "button:has-text(/^Siguiente$/)", "button:has-text(/^Next$/)",
                "button:has-text('Iniciar sesión')", "button:has-text('Log in')"
            ],
            timeout_s=8.0
        )
        if not btn_login:
            raise RuntimeError("No se encontró el botón de inicio de sesión.")
        btn_login.click()
        time.sleep(3.5)
        # Comprobar si aparece el banner de contraseña o usuario incorrecto
        for frame in self.page.frames:
            try:
                body_text = frame.evaluate("() => document.body.innerText")
                if "incorrecto" in body_text or "incorrect" in body_text:
                    if "contraseña" in body_text or "password" in body_text or "usuario" in body_text or "username" in body_text:
                        print(f"  [Paso 1] [ERROR] Contraseña o usuario incorrecto detectado en Tidal para {self.client_email}. Cancelando cuenta...")
                        self.cuenta_abortada = True
                        self.abort_reason = "Contraseña o usuario incorrecto de Tidal"
                        try:
                            self.context.close()
                        except Exception:
                            pass
                        return
            except Exception:
                pass
        time.sleep(0.5)
        
        # Consentimiento
        btn_consent = encontrar_locator_en_frames(
            self.page,
            ['button', '[role="button"]'],
            text_regex=re.compile(r"sí,\s*continuar|si,\s*continuar|yes,\s*continue|continuar|continue", re.I)
        )
        if btn_consent:
            btn_consent.click()
            time.sleep(3.0)
            
        manejar_bloqueos_e_intervencion(self.page, "Login Tidal (Final)")
        print("  [Paso 1] [OK] Sesión iniciada con éxito.")

    # --- PASO 2: Cambiar correo a cakeseller1234@gmail.com con puntos ---
    def step2_change_email(self):
        if self.cuenta_abortada:
            return
        print("\n--- PASO 2: Cambiando correo a cakeseller1234 con puntos aleatorios ---")
        self.new_email_temp = generar_correo_con_puntos("cakeseller1234@gmail.com")
        print(f"  [Paso 2] Correo generado: {self.new_email_temp}")
        
        # Evitar ERR_ABORTED si el navegador ya se está redirigiendo al perfil tras el login
        print("  [Paso 2] Esperando y cargando página de perfil...")
        try:
            if "/profile" in self.page.url:
                self.page.wait_for_load_state("domcontentloaded", timeout=5000)
            else:
                self.page.goto("https://account.tidal.com/profile", wait_until="domcontentloaded", timeout=15000)
        except Exception as err:
            err_msg = str(err).encode('ascii', errors='replace').decode('ascii')
            print(f"  [Paso 2] [WARN] Goto inicial falló ({err_msg[:60]}). Reintentando navegación...")
            time.sleep(2.0)
            try:
                self.page.goto("https://account.tidal.com/profile", wait_until="domcontentloaded", timeout=15000)
            except Exception:
                # Si sigue fallando por redirecciones, esperar que cargue el DOM actual
                pass
                
        time.sleep(3.0)
        aceptar_cookies_con_espera(self.page)
        
        # Verificar que estamos en la página de perfil y no fuimos redirigidos a login
        if "login.tidal.com" in self.page.url:
            print("  [Paso 2] [WARN] Fuimos redirigidos a login. La sesión pudo haber expirado. Reintentando navegación...")
            time.sleep(2.0)
            self.page.goto("https://account.tidal.com/profile", wait_until="domcontentloaded", timeout=15000)
            time.sleep(3.0)
            aceptar_cookies_con_espera(self.page)
        
        # Buscar botón Editar con reintentos
        btn_editar = None
        for intento_editar in range(1, 4):
            btn_editar = esperar_locator_en_frames(
                self.page,
                [
                    "button:has-text('Editar información')", "a:has-text('Editar información')", "[role='button']:has-text('Editar información')",
                    "button:has-text('Edit information')", "a:has-text('Edit information')", "[role='button']:has-text('Edit information')",
                    "button:has-text('Editar')", "a:has-text('Editar')", "[role='button']:has-text('Editar')",
                    "button:has-text('Edit')", "a:has-text('Edit')", "[role='button']:has-text('Edit')"
                ],
                text_regex=re.compile(r"editar\s+informaci|edit\s+informati|^editar$|^edit$", re.I),
                timeout_s=10.0
            )
            if btn_editar:
                break
            print(f"  [Paso 2] [WARN] Botón 'Editar' no encontrado (intento {intento_editar}/3). Recargando página...")
            try:
                self.page.goto("https://account.tidal.com/profile", wait_until="domcontentloaded", timeout=15000)
                time.sleep(3.0)
                aceptar_cookies_con_espera(self.page)
            except Exception:
                time.sleep(3.0)
        
        if not btn_editar:
            raise RuntimeError("No se encontró el botón 'Editar información' en el perfil.")
        btn_editar.click()
        time.sleep(1.5)
        
        email_input = esperar_locator_en_frames(self.page, ['input[type="email"]', 'input[name="email"]'], timeout_s=5.0)
        rellenar_campo_humanizado(email_input, self.new_email_temp)
        time.sleep(0.5)
        
        btn_guardar = esperar_locator_en_frames(
            self.page,
            [
                "button[type='submit']",
                "button:has-text('Guardar')", "a:has-text('Guardar')", "[role='button']:has-text('Guardar')",
                "button:has-text('Save')", "a:has-text('Save')", "[role='button']:has-text('Save')",
                "button:has-text('Continuar')", "a:has-text('Continuar')", "[role='button']:has-text('Continuar')",
                "button:has-text('Continue')", "a:has-text('Continue')", "[role='button']:has-text('Continue')"
            ],
            timeout_s=8.0
        )
        btn_guardar.click()
        
        # Esperar a ver si aparece el campo de confirmación de contraseña (hasta 10 segundos)
        pwd_confirm = esperar_locator_en_frames(self.page, ['input[type="password"]'], timeout_s=10.0)
        if pwd_confirm:
            print("  [Paso 2] Confirmación de contraseña requerida por TIDAL.")
            rellenar_campo_humanizado(pwd_confirm, self.client_pwd)
            time.sleep(0.5)
            btn_guardar_confirm = esperar_locator_en_frames(
                self.page,
                ["button[type='submit']", "button:has-text('Guardar')", "button:has-text('Confirmar')", "button:has-text('Save')", "button:has-text('Continuar')"],
                timeout_s=5.0
            )
            if btn_guardar_confirm:
                btn_guardar_confirm.click()
                print("  [Paso 2] Enviada confirmación de contraseña.")
            time.sleep(4.0)
            
        # VERIFICACIÓN: Comprobar si el cambio de correo se guardó con éxito.
        # Si fue exitoso, la página debe volver al perfil (o no mostrar el botón Guardar del formulario edit).
        verificado = False
        start_verify = time.time()
        while time.time() - start_verify < 15.0:
            if "/profile/edit" not in self.page.url:
                verificado = True
                break
            if not email_input.is_visible():
                verificado = True
                break
            time.sleep(1.0)
            
        if not verificado:
            raise RuntimeError("El correo no se guardó correctamente en TIDAL. La pantalla de edición sigue activa.")
            
        print("  [Paso 2] [OK] Correo cambiado con éxito en TIDAL.")

    # --- PASO 3 y 4: Extraer playlists a CSV en TuneMyMusic ---
    def step3_4_export_to_csv(self):
        if self.cuenta_abortada:
            return
        print("\n--- PASOS 3 & 4: Exportando playlists a CSV vía TuneMyMusic ---")
        print("  [TuneMyMusic] Esperando bloqueo global para TuneMyMusic (Exportación)...")
        tmm_lock.acquire()
        print("  [TuneMyMusic] Bloqueo global adquirido. Iniciando...")
        tmm_page = self.page
        try:
            # No borramos las cookies de tunemymusic.com para mantener la sesión de usuario iniciada
                
            tmm_page.goto("https://www.tunemymusic.com/es/transfer", wait_until="domcontentloaded")
            time.sleep(2.0)
            
            try:
                tmm_page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
                tmm_page.reload(wait_until="domcontentloaded")
                time.sleep(2.0)
            except Exception:
                pass
                
            aceptar_cookies_con_espera(tmm_page)
            
            # Seleccionar TIDAL como fuente
            btn_tidal = tmm_page.locator("button[name='Tidal']").or_(tmm_page.locator("button[aria-label='TIDAL']")).first
            if esperar_visibilidad(btn_tidal, 5000):
                btn_tidal.click(force=True)
            else:
                if not hacer_click_por_textos(tmm_page, ["TIDAL", "Tidal"]):
                    raise RuntimeError("No se pudo hacer click en el botón de TIDAL.")
            time.sleep(3.0)
            
            # Manejar flujo de autorización
            email_a_buscar = self.new_email_temp if self.new_email_temp else self.client_email
            autorizar_tidal_en_tmm(tmm_page, email_a_buscar)
            
            # Cargar desde la cuenta TIDAL
            if not hacer_click_por_textos(tmm_page, ["CARGAR DESDE CUENTA TIDAL", "Cargar desde cuenta TIDAL", "Cargar desde cuenta", "CARGAR DESDE CUENTA"]):
                btn_cargar = tmm_page.locator("button:has-text('Cargar desde cuenta')").or_(tmm_page.locator("button:has-text('Load from account')")).first
                if esperar_visibilidad(btn_cargar, 5000):
                    btn_cargar.click(force=True)
            # Esperar dinámicamente a que las canciones se carguen en pantalla antes de proceder
            print("  [Paso 3] Esperando a que termine la carga de las canciones de Tidal en TuneMyMusic...")
            lbl_canciones = tmm_page.locator(
                "text='Canciones favoritas'"
            ).or_(tmm_page.locator("text='Favorite Tracks'")
            ).or_(tmm_page.locator("text='Álbumes'")
            ).or_(tmm_page.locator("text='Albums'")
            ).or_(tmm_page.locator("text='Toda la biblioteca'")
            ).or_(tmm_page.locator("text='My Library'")
            ).or_(tmm_page.locator("text='Elige destino'")
            ).or_(tmm_page.locator("text='Elegir destino'")
            ).or_(tmm_page.locator("text='Select destination'")
            ).or_(tmm_page.locator("text='Choose destination'")
            ).or_(tmm_page.locator("text='Selecciona las listas'")).first
            
            songs_loaded = False
            for poll in range(60):  # Esperar hasta 30 segundos en total
                # Comprobar si aparece el banner de "No se encontraron listas de reproducción"
                no_playlists_banner = tmm_page.locator("text='No se encontraron listas de reproducción'").or_(
                    tmm_page.locator("text='No playlists found'")
                ).or_(
                    tmm_page.locator("text='No se encontraron playlists'")
                ).first
                if no_playlists_banner.is_visible():
                    print("  [Paso 3] [INFO] TuneMyMusic detectó que esta cuenta Tidal no tiene listas de reproducción. Se omitirá la transferencia.")
                    self.skip_playlists = True
                    break

                if lbl_canciones.is_visible():
                    songs_loaded = True
                    break
                
                # Cerrar modal 'Name your TIDAL Accounts' si aparece de forma tardía
                modal_title = tmm_page.locator("text='Name your TIDAL Accounts'").or_(tmm_page.locator("text='Nombra tus cuentas de TIDAL'")).first
                if modal_title.is_visible():
                    print("  [Paso 3] [Bloqueo] Detectado modal 'Name your TIDAL Accounts' durante la espera. Cerrándolo...")
                    for selector in ["button[aria-label='Close']", "button[aria-label='Cerrar']", "button:has-text('×')", "button:has-text('X')", "button[class*='close' i]"]:
                        try:
                            btn = tmm_page.locator(selector).first
                            if btn.is_visible():
                                btn.click(force=True)
                                print(f"  [Paso 3] Modal cerrado con selector: {selector}")
                                break
                        except Exception:
                            pass
                    time.sleep(1.0)
                    if modal_title.is_visible():
                        tmm_page.keyboard.press("Escape")
                        time.sleep(1.0)
                
                time.sleep(0.5)
                
            if getattr(self, "skip_playlists", False):
                return

            if songs_loaded:
                print("  [Paso 3] Canciones cargadas en la interfaz de TuneMyMusic.")
            else:
                print("  [Paso 3] [WARN] El indicador de canciones tardó en aparecer. Continuando de todos modos...")
            time.sleep(2.5)
                
            # Pulsar 'Elige destino' con reintentos dinámicos en caso de que React ignore el primer clic
            print("  [Paso 3] Pulsando 'Elige destino'...")
            btn_archivo = None
            for intento_click in range(4):
                # Intentar pulsar por textos o selectores directos
                hacer_click_por_textos(tmm_page, ["ELEGIR DESTINO", "Elegir destino", "Elige destino", "Elige Destino", "SELECT DESTINATION", "Select destination", "Choose destination"])
                btn_elegir_dest = tmm_page.locator("button:has-text('Elige destino')").or_(tmm_page.locator("button:has-text('Elegir destino')")).or_(tmm_page.locator("button:has-text('Select destination')")).first
                if esperar_visibilidad(btn_elegir_dest, 1500):
                    try:
                        btn_elegir_dest.click(force=True)
                    except Exception:
                        pass
                
                time.sleep(2.5)
                
                # Verificar si ya avanzamos a la siguiente pantalla (donde el botón 'ToFile' está visible)
                btn_archivo = tmm_page.locator("button[name='ToFile']").or_(tmm_page.locator("button[aria-label='Exportar archivo']")).first
                if esperar_visibilidad(btn_archivo, 2000):
                    print("  [Paso 3] Transicion exitosa a la pantalla de seleccion de destino.")
                    break
                else:
                    print(f"  [Paso 3] Reintento {intento_click + 1}/4 de click en 'Elige destino'...")
            
            if not btn_archivo or not esperar_visibilidad(btn_archivo, 3000):
                # Intentar buscar por texto si los localizadores fallan
                if not hacer_click_por_textos(tmm_page, ["EXPORTAR ARCHIVO", "Exportar archivo", "Archivo", "File", "CSV"]):
                    raise RuntimeError("No se pudo hacer click en la opción de Exportar archivo.")
            else:
                btn_archivo.click(force=True)
            time.sleep(2.0)
            
            # Seleccionar CSV via eventos nativos del DOM que React captura
            # Debug mostro: input#csv (id='csv', name='format', value='on')
            # React usa event delegation y necesita PointerEvent + MouseEvent nativos
            print("  [Paso 3] Seleccionando formato CSV (eventos nativos)...")
            csv_result = tmm_page.evaluate("""
                () => {
                    const csvInput = document.getElementById('csv');
                    if (!csvInput) return { success: false, error: 'input#csv not found' };
                    
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'checked'
                    );
                    if (nativeSetter && nativeSetter.set) {
                        nativeSetter.set.call(csvInput, true);
                    } else {
                        csvInput.checked = true;
                    }
                    
                    const opts = { bubbles: true, cancelable: true, view: window };
                    csvInput.dispatchEvent(new PointerEvent('pointerdown', opts));
                    csvInput.dispatchEvent(new MouseEvent('mousedown', opts));
                    csvInput.dispatchEvent(new PointerEvent('pointerup', opts));
                    csvInput.dispatchEvent(new MouseEvent('mouseup', opts));
                    csvInput.dispatchEvent(new MouseEvent('click', opts));
                    csvInput.dispatchEvent(new Event('input', { bubbles: true }));
                    csvInput.dispatchEvent(new Event('change', { bubbles: true }));
                    
                    const label = csvInput.closest('label') || document.querySelector('label[for="csv"]');
                    if (label) label.dispatchEvent(new MouseEvent('click', opts));
                    
                    return { success: csvInput.checked, inputId: csvInput.id };
                }
            """)
            if csv_result.get('success'):
                print(f"  [OK] CSV seleccionado (input#{csv_result.get('inputId')})")
            else:
                print(f"  [WARN] CSV nativo fallo: {csv_result.get('error')}. Fallback Playwright...")
                tmm_page.locator("label[for='csv']").first.click(force=True)
            time.sleep(2.0)
            
            # Pulsar Exportar via form.requestSubmit()
            # Debug mostro: 2 botones 'Exportar'. Solo el [1] tiene form asociado.
            # Filtrar: solo botones dentro de un <form> con texto exacto 'Exportar'
            print("  [Paso 3] Pulsando boton 'Exportar' (form.requestSubmit)...")
            export_result = tmm_page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll('form button[type="submit"]');
                    const isVisible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
                    for (const btn of buttons) {
                        if (isVisible(btn)) {
                            const text = (btn.textContent || '').trim();
                            if (text === 'Exportar' || text === 'Export') {
                                const form = btn.closest('form');
                                if (form) {
                                    try {
                                        form.requestSubmit(btn);
                                        return { success: true, method: 'requestSubmit', text: text };
                                    } catch(e) {
                                        const opts = { bubbles: true, cancelable: true, view: window };
                                        btn.dispatchEvent(new PointerEvent('pointerdown', opts));
                                        btn.dispatchEvent(new MouseEvent('mousedown', opts));
                                        btn.dispatchEvent(new PointerEvent('pointerup', opts));
                                        btn.dispatchEvent(new MouseEvent('mouseup', opts));
                                        btn.dispatchEvent(new MouseEvent('click', opts));
                                        return { success: true, method: 'nativeEvents', text: text };
                                    }
                                }
                            }
                        }
                    }
                    return { success: false, error: 'No Exportar button in form found' };
                }
            """)
            if export_result.get('success'):
                print(f"  [OK] Formulario enviado via {export_result.get('method')}")
            else:
                print(f"  [WARN] form.requestSubmit fallo. Fallback Playwright...")
                tmm_page.locator("form button[type='submit']").last.click(force=True, timeout=4000)
            
            # Esperar a que el modal de exportación se procese y aparezca el botón de Comenzar Transferencia
            print("  [Paso 3] Procesando exportación. Esperando al siguiente paso...")
            btn_start_check = tmm_page.locator("button:has-text('Comenzar transferencia')").or_(tmm_page.locator("button:has-text('Comenzar a mover')")).or_(tmm_page.locator("button:has-text('Start Transfer')")).first
            esperar_visibilidad(btn_start_check, 15000)
            time.sleep(1.0)
            
            # Pulsar "COMENZAR TRANSFERENCIA"
            # IMPORTANTE: La exportación a CSV descarga el archivo AUTOMÁTICAMENTE
            # al pulsar este botón. No hay un botón separado de "Descargar".
            self.csv_path = DOWNLOADS_DIR / f"{self.client_email}.csv"
            print(f"  [Paso 3] Iniciando transferencia y capturando descarga automatica...")
            
            try:
                # Aumentado a 10 minutos (600,000 ms) para soportar cuentas masivas (ej. 30k+ canciones)
                with tmm_page.expect_download(timeout=600000) as download_info:
                    if not hacer_click_por_textos(tmm_page, ["COMENZAR TRANSFERENCIA", "Comenzar transferencia", "Comenzar a mover mi música", "START TRANSFER", "Start Transfer"]):
                        btn_start = tmm_page.locator("button:has-text('Comenzar a mover mi música')").or_(tmm_page.locator("button:has-text('Start Transfer')")).first
                        btn_start.click(force=True)
                    print("  [Paso 3] Transferencia iniciada. Esperando descarga del CSV...")
                
                download = download_info.value
                download.save_as(str(self.csv_path))
                print(f"  [Paso 4] CSV guardado como: {self.csv_path.name}")
                
            except Exception as download_err:
                # Verificar si la transferencia se completo de todos modos
                err_msg = str(download_err).encode('ascii', errors='replace').decode('ascii')
                print(f"  [WARN] expect_download no capturo descarga: {err_msg[:100]}")
                
                completada = tmm_page.locator("text=Transferencia completada").or_(tmm_page.locator("text=Transfer completed")).first
                if esperar_visibilidad(completada, 15000):
                    print("  [OK] Transferencia completada. El CSV se descargo a la carpeta de descargas del navegador.")
                    print("  [INFO] Busca el archivo .csv mas reciente en tu carpeta de descargas y copialo a: " + str(DOWNLOADS_DIR))
                    self.input_concurrente(">>> Copia el CSV descargado a la carpeta indicada y presiona Enter <<<")
                else:
                    raise RuntimeError("La transferencia a CSV no se completo correctamente.")
            
        except Exception as e:
            try:
                tmm_page.screenshot(path="tmm_error_screenshot_step3.png")
                print("  [Error Debug] Captura de pantalla del error guardada en la carpeta del script como 'tmm_error_screenshot_step3.png'.")
            except Exception:
                pass
            raise e
        finally:
            try:
                if tmm_page != self.page:
                    tmm_page.close()
            except Exception:
                pass
            tmm_lock.release()
            print("  [TuneMyMusic] Bloqueo global de TuneMyMusic liberado.")
            
        print("  [Paso 4] [OK] Playlists exportadas y CSV renombrado con éxito.")

    # --- PASO 5: Eliminando la cuenta Tidal del cliente ---
    def step5_delete_account(self):
        if self.cuenta_abortada:
            return
        print("\n--- PASO 5: Eliminando la cuenta Tidal del cliente ---")
        
        # Si el correo temporal no está configurado o es el fallback sin puntos, intentar obtenerlo del perfil primero
        if not self.new_email_temp or self.new_email_temp == "cakeseller1234@gmail.com":
            print("  [Paso 5] Detectando alias de cakeseller1234@gmail.com en la página de perfil...")
            try:
                self.page.goto("https://account.tidal.com/profile", wait_until="domcontentloaded", timeout=20000)
                time.sleep(2.5)
                aceptar_cookies_con_espera(self.page)
                for frame in self.page.frames:
                    try:
                        text = frame.evaluate("() => document.body.innerText")
                        emails_encontrados = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
                        for email_candidato in emails_encontrados:
                            if self.emails_coinciden_sin_puntos(email_candidato, "cakeseller1234@gmail.com"):
                                self.new_email_temp = email_candidato.strip()
                                print(f"  [Paso 5] [OK] Correo temporal (alias) detectado automáticamente: {self.new_email_temp}")
                                break
                    except Exception:
                        pass
                    if self.new_email_temp and self.new_email_temp != "cakeseller1234@gmail.com":
                        break
            except Exception as e:
                print(f"  [Paso 5] [WARN] No se pudo cargar el perfil para buscar el alias: {e}")
                
        if not self.new_email_temp:
            print("  [Paso 5] [WARN] No se pudo detectar el alias. Usando cakeseller1234@gmail.com como fallback.")
            self.new_email_temp = "cakeseller1234@gmail.com"

        max_intentos_borrado = 3
        for intento_borrado in range(1, max_intentos_borrado + 1):
            try:
                # Si no es el primer intento, rotar proxy PE para recuperar conexión muerta
                if intento_borrado > 1:
                    print(f"  [Paso 5] [Reintento {intento_borrado}/3] Rotando proxy PE para restablecer conexión congelada...")
                    self.rotar_proxy_contexto(tipo="PE")
                
                # Navegar directamente a la pagina de eliminacion
                print("  [Paso 5] Navegando a la pagina de eliminacion de cuenta...")
                self.page.goto("https://account.tidal.com/account-deletion", wait_until="domcontentloaded", timeout=30000)
                time.sleep(3.0)
                aceptar_cookies_con_espera(self.page)
                manejar_bloqueos_e_intervencion(self.page, "Carga de Eliminación de Cuenta")
                
                # Si redirige al perfil, buscar el enlace de eliminacion
                if "/profile" in self.page.url and "/account-deletion" not in self.page.url:
                    print("  [Paso 5] Redirigido al perfil. Buscando opcion de eliminar cuenta...")
                    btn_eliminar = encontrar_locator_en_frames(
                        self.page,
                        ["a:has-text('Eliminar cuenta')", "button:has-text('Eliminar cuenta')", 
                         "a:has-text('Delete account')", "button:has-text('Delete account')"]
                    )
                    if btn_eliminar:
                        btn_eliminar.click()
                        time.sleep(3.0)
                        manejar_bloqueos_e_intervencion(self.page, "Click Eliminación de Cuenta")
                    else:
                        self.page.goto("https://account.tidal.com/account-deletion", wait_until="domcontentloaded")
                        time.sleep(3.0)
                        manejar_bloqueos_e_intervencion(self.page, "Reintento Carga de Eliminación de Cuenta")
                
                # Pasar por los pasos de confirmacion (Paso 1 de 3, Paso 2 de 3, etc.)
                # Obtener el ID de correo mas reciente antes de gatillar el envio del codigo
                max_id_previo = obtener_max_email_id(self.new_email_temp)
                print(f"  [Paso 5] ID de correo de Tidal mas reciente antes de gatillar: {max_id_previo}")
                
                # Pasar por los pasos de confirmacion (Paso 1 de 3, Paso 2 de 3, etc.)
                # En cada paso hay que pulsar "Continuar" hasta llegar al paso del codigo
                for paso_num in range(1, 6):  # Hasta 5 pasos por seguridad
                    print(f"  [Paso 5] Verificando paso de confirmacion {paso_num}...")
                    
                    # Verificar si ya llegamos al campo de codigo (paso final)
                    code_input = encontrar_locator_en_frames(
                        self.page,
                        ['input[name="code"]', 'input[placeholder*="codigo" i]', 'input[placeholder*="code" i]',
                         'input[type="text"]', 'input[inputmode="numeric"]']
                    )
                    if code_input:
                        print(f"  [Paso 5] Campo de codigo encontrado en paso {paso_num}. Listo para ingresar codigo.")
                        break
                    
                    # Detectar y marcar checkbox de confirmación ("He leído lo anterior...") si existe
                    print(f"  [Paso 5] Comprobando si hay checkbox de confirmación en paso {paso_num}...")
                    
                    # Ejecutar bucle de reintentos con recarga de página por si el checkbox o el botón se bloquean en Vue
                    max_intentos_checkbox = 3
                    for intento_cb in range(1, max_intentos_checkbox + 1):
                        checkbox_clicked = self.page.evaluate(r"""
                            () => {
                                const regex = /he\s+le[íi]do\s+lo\s+anterior\s+y\s+lo\s+comprendo/i;
                                const findAndClickCheckbox = (root) => {
                                    const elms = Array.from(root.querySelectorAll('label, span, input, p, div'));
                                    const matches = elms.filter(el => {
                                        const text = (el.textContent || '').trim();
                                        const isVisible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                                        return regex.test(text) && isVisible;
                                    });
                                    
                                    if (matches.length === 0) {
                                        const frames = root.querySelectorAll('iframe, frame');
                                        for (const frame of frames) {
                                            try {
                                                const doc = frame.contentDocument || frame.contentWindow.document;
                                                if (doc && findAndClickCheckbox(doc)) return true;
                                            } catch(e) {}
                                        }
                                        return false;
                                    }
                                    
                                    matches.sort((a, b) => a.textContent.length - b.textContent.length);
                                    const bestEl = matches[0];
                                    
                                    const wrapper = bestEl.closest('.form-checkbox-wrapper') || bestEl.closest('label')?.parentElement || bestEl.parentElement;
                                    if (wrapper) {
                                        const cbEl = wrapper.querySelector('input[type="checkbox"], [role="checkbox"], .form-checkbox, .form-checked-icon');
                                        if (cbEl) {
                                            cbEl.click();
                                        } else {
                                            bestEl.click();
                                        }
                                    } else {
                                        bestEl.click();
                                    }
                                    return true;
                                };
                                return findAndClickCheckbox(document);
                            }
                        """)
                        
                        if (checkbox_clicked):
                            print(f"  [Paso 5] Se pulsó el checkbox 'He leído lo anterior'. Verificando si habilitó el botón...")
                            time.sleep(2.0)
                            
                            # Verificar si el botón de continuar se habilitó tras pulsar el checkbox
                            boton_habilitado = self.page.evaluate(r"""
                                () => {
                                    const findEnabledButton = (root) => {
                                        const elms = Array.from(root.querySelectorAll('button, a, [role="button"]'));
                                        const targetTexts = ['Continuar', 'Continue', 'Siguiente', 'Next', 'Confirmar', 'Confirm', 'Eliminar', 'Delete'];
                                        for (const el of elms) {
                                            const text = (el.textContent || '').trim();
                                            const isVisible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                                            if (targetTexts.includes(text) && isVisible) {
                                                return !el.disabled && !el.hasAttribute('disabled');
                                            }
                                        }
                                        const frames = root.querySelectorAll('iframe, frame');
                                        for (const frame of frames) {
                                            try {
                                                const doc = frame.contentDocument || frame.contentWindow.document;
                                                if (doc) {
                                                    const res = findEnabledButton(doc);
                                                    if (res) return res;
                                                }
                                            } catch(e) {}
                                        }
                                        return false;
                                    };
                                    return findEnabledButton(document);
                                }
                            """)
                            
                            if boton_habilitado:
                                print(f"  [Paso 5] [OK] El botón 'Continuar' está HABILITADO. Procediendo...")
                                break
                            else:
                                print(f"  [Paso 5] [WARN] El botón 'Continuar' sigue DESHABILITADO (intento {intento_cb}/{max_intentos_checkbox}).")
                                if intento_cb < max_intentos_checkbox:
                                    print(f"  [Paso 5] Recargando la página para intentar destrabar el checkbox...")
                                    self.page.reload(wait_until="domcontentloaded")
                                    time.sleep(4.0)
                                    aceptar_cookies_con_espera(self.page)
                        else:
                            # No hay checkbox en este paso
                            break
                    
                    # Buscar boton "Continuar" para avanzar al siguiente paso
                    print(f"  [Paso 5] Pulsando 'Continuar' o 'Siguiente' en paso {paso_num} (JS nativo)...")
                    btn_clicked = self.page.evaluate("""
                        () => {
                            const texts = ['Continuar', 'Continue', 'Siguiente', 'Next', 'Confirmar', 'Confirm', 'Eliminar', 'Delete'];
                            const findAndClick = (root) => {
                                const elms = root.querySelectorAll('button, a, [role="button"], div, span');
                                const isVisible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
                                for (const el of elms) {
                                    const text = (el.textContent || '').trim();
                                    if (texts.includes(text) && isVisible(el)) {
                                        el.click();
                                        return true;
                                    }
                                }
                                const frames = root.querySelectorAll('iframe, frame');
                                for (const frame of frames) {
                                    try {
                                        const doc = frame.contentDocument || frame.contentWindow.document;
                                        if (doc && findAndClick(doc)) return true;
                                    } catch(e) {}
                                }
                                return false;
                            };
                            return findAndClick(document);
                        }
                    """)
                    
                    if btn_clicked:
                        print(f"  [Paso 5] [OK] Click realizado con éxito en paso {paso_num}.")
                        time.sleep(3.5)
                    else:
                        print(f"  [Paso 5] [WARN] No se pudo hacer click via JS en el paso {paso_num}.")
                        btn_fallback = esperar_locator_en_frames(
                            self.page,
                            ["button:has-text('Continuar')", "a:has-text('Continuar')", "text='Continuar'"],
                            timeout_s=3.0
                        )
                        if btn_fallback:
                            btn_fallback.click(force=True)
                            time.sleep(3.5)
                        else:
                            print(f"  [Paso 5] Deteniendo wizard en paso {paso_num}.")
                            break
                
                # Ahora buscar el codigo de eliminacion en Gmail con reintentos
                print("  [Paso 5] Esperando código de eliminación en cakeseller1234@gmail.com (hasta 1.5 minutos)...")
                codigo = None
                for intento in range(1, 9):  # Incrementar a 8 intentos (~1.5 minutos)
                    # En el primer intento o a la mitad, si el botón de reenvío está activo (sin cuenta atrás), forzar envío
                    if intento == 1 or intento == 4:
                        try:
                            btn_resend = encontrar_locator_en_frames(
                                self.page,
                                ["button:has-text('Reenviar')", "a:has-text('Reenviar')", 
                                 "button:has-text('Resend')", "a:has-text('Resend')",
                                 "span:has-text('Reenviar')", "span:has-text('Resend')"]
                            )
                            if btn_resend and btn_resend.is_visible():
                                texto_boton = btn_resend.inner_text()
                                # Si el botón no muestra números/cuenta atrás, es que Tidal no envió nada o se congeló
                                if not any(char.isdigit() for char in texto_boton):
                                    print(f"  [Paso 5] Botón de reenvío activo y libre detectado ('{texto_boton}'). Forzando envío del código...")
                                    btn_resend.click()
                                    time.sleep(4.0)
                                    # Regenerar base de ID de correo tras forzar el envío
                                    max_id_previo = obtener_max_email_id(self.new_email_temp)
                        except Exception as e:
                            print(f"  [Paso 5] [WARN] Error al verificar/forzar reenvío de código: {e}")

                    print(f"  [Paso 5] Intento {intento}/8: Buscando correo de eliminación...")
                    codigo = obtener_codigo_de_gmail(
                        self.page, 
                        email_destinatario=self.new_email_temp, 
                        query="from:tidal", 
                        required_keywords=["elimin", "desactiv", "delete", "code", "codigo"], 
                        after_email_id=max_id_previo
                    )
                    if codigo:
                        break
                        
                    # Reenviar de respaldo a la mitad de la espera si no ha llegado
                    if intento == 4:
                        print("  [Paso 5] El correo está tardando. Intentando hacer clic en 'Reenviar el código'...")
                        try:
                            btn_resend = encontrar_locator_en_frames(
                                self.page,
                                ["button:has-text('Reenviar')", "a:has-text('Reenviar')", 
                                 "button:has-text('Resend')", "a:has-text('Resend')",
                                 "span:has-text('Reenviar')", "span:has-text('Resend')"]
                            )
                            if btn_resend:
                                btn_resend.click()
                                print("  [Paso 5] Clic en 'Reenviar el código' realizado.")
                        except Exception as e:
                            print(f"  [Paso 5] [WARN] No se pudo hacer clic en reenviar: {e}")
                            
                    if intento < 8:
                        print("  [Paso 5] Correo no encontrado aún. Esperando 12 segundos...")
                        time.sleep(12.0)
                
                if not codigo:
                    raise RuntimeError("No se pudo obtener el código de eliminación vía IMAP automáticamente.")
                
                if codigo and not codigo.startswith("http"):
                    # Buscar el campo de codigo en la pagina actual
                    code_input = esperar_locator_en_frames(
                        self.page,
                        ['input[name="code"]', 'input[placeholder*="codigo" i]', 'input[placeholder*="code" i]',
                         'input[type="text"]', 'input[inputmode="numeric"]'],
                        timeout_s=5.0
                    )
                    if code_input:
                        escribir_codigo_verificacion_inteligente(self.page, codigo)
                        time.sleep(1.0)
                        
                        # Hacer clic en el botón de confirmación de eliminación usando JS nativo en todos los frames
                        print("  [Paso 5] Pulsando el boton de confirmacion de eliminacion...")
                        btn_clicked = False
                        for frame in self.page.frames:
                            try:
                                btn_clicked = frame.evaluate(r"""
                                    () => {
                                        const deleteKeywords = ['eliminar cuenta', 'eliminar', 'delete account', 'delete', 'confirmar', 'confirm'];
                                        const isVisible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
                                        const interactive = document.querySelectorAll('button, a, [role="button"]');
                                        for (const el of interactive) {
                                            const text = (el.textContent || '').trim().toLowerCase();
                                            if (isVisible(el) && deleteKeywords.some(kw => text === kw || text.includes(kw))) {
                                                el.click();
                                                return true;
                                            }
                                        }
                                        const divs = document.querySelectorAll('div, span');
                                        for (const el of divs) {
                                            const text = (el.textContent || '').trim().toLowerCase();
                                            if (text.length < 30 && isVisible(el) && deleteKeywords.some(kw => text === kw || text.includes(kw))) {
                                                el.click();
                                                return true;
                                            }
                                        }
                                        return false;
                                    }
                                """)
                                if btn_clicked:
                                    print("  [Paso 5] Boton pulsado con éxito en un frame.")
                                    break
                            except Exception:
                                pass
                                
                        if not btn_clicked:
                            btn_confirm_del = esperar_locator_en_frames(
                                self.page,
                                ["button[type='submit']", "button:has-text('Eliminar')", 
                                 "button:has-text('Confirmar')", "button:has-text('Delete')"],
                                timeout_s=5.0
                            )
                            if btn_confirm_del:
                                btn_confirm_del.click(force=True)
                                btn_clicked = True
                                
                        time.sleep(4.0)
                        
                # Esperar unos segundos para asegurar que se procesa la solicitud de eliminación en el servidor
                print("  [Paso 5] Procesando solicitud de eliminación en el servidor...")
                
                # Esperar dinámicamente hasta 10 segundos a que la URL cambie, aparezca texto de éxito o desaparezca el input
                deletions_succeeded = False
                for _ in range(20):  # 10 segundos max (20 * 0.5s)
                    current_url = self.page.url.lower()
                    if "/login" in current_url or "login.tidal.com" in current_url or "success" in current_url or "/account-deletion" not in current_url:
                        deletions_succeeded = True
                        break
                        
                    found_success_text = False
                    for frame in self.page.frames:
                        try:
                            content = frame.evaluate("() => document.body.innerText").lower()
                            if any(kw in content for kw in ["eliminada", "eliminado", "deleted", "correctamente", "exitosamente", "success", "confirmada", "su cuenta ha sido"]):
                                has_code = False
                                for selector in ['input[name="code"]', 'input[placeholder*="code" i]', 'input[placeholder*="código" i]']:
                                    try:
                                        if frame.locator(selector).first.is_visible():
                                            has_code = True
                                            break
                                    except Exception:
                                        pass
                                if not has_code:
                                    found_success_text = True
                                    break
                        except Exception:
                            pass
                    if found_success_text:
                        deletions_succeeded = True
                        break
                        
                    found_error = False
                    for frame in self.page.frames:
                        try:
                            error_loc = frame.locator("text=incorrecto").or_(frame.locator("text=inválido")).or_(frame.locator("text=error")).or_(frame.locator("text=invalid")).first
                            if error_loc and error_loc.is_visible():
                                found_error = True
                                break
                        except Exception:
                            pass
                    if found_error:
                        break
                        
                    time.sleep(0.5)
                
                if not deletions_succeeded:
                    try:
                        print("  [Paso 5] Verificando estado de la cuenta navegando a account.tidal.com...")
                        self.page.goto("https://account.tidal.com/", wait_until="domcontentloaded", timeout=10000)
                        time.sleep(3.0)
                        if "login" in self.page.url or "login.tidal.com" in self.page.url:
                            print("  [Paso 5] Redirigido a la página de inicio de sesión. Confirmado: cuenta eliminada.")
                            deletions_succeeded = True
                    except Exception as e:
                        print(f"  [Paso 5] [WARN] No se pudo verificar la sesión: {e}")
                
                if not deletions_succeeded:
                    raise RuntimeError("No se pudo confirmar la eliminación automática de la cuenta.")
                
                print("  [Paso 5] Cuenta Tidal eliminada con éxito.")
                return  # Salida exitosa
                
            except Exception as e:
                print(f"  [Paso 5] [WARN] Intento {intento_borrado}/3 de eliminación falló o se congeló: {e}")
                if intento_borrado == 3:
                    print("\n" + "=" * 60)
                    print("  PAUSA MANUAL DE ELIMINACIÓN DE CUENTA (ÚLTIMO RECURSO)")
                    print("=" * 60)
                    self.input_concurrente(">>> Completa la eliminación de forma manual y pulsa Enter <<<")

    # --- PASO 6: Crear cuenta Tidal con VPN Nigeria ---
    def step6_create_account(self):
        if self.cuenta_abortada:
            return
        print("\n" + "=" * 70)
        print("  [Paso 6] Cambiando navegador a IP de Nigeria...")
        self.context.close()
        
        # Conectar VPN si no estamos usando proxies
        if not (self.use_proxy and self.proxy_ng_server):
            if BATCH_MODE_VPN:
                print("  [Surfshark] Modo VPN global activo. Se asume que la VPN ya está conectada para todo el lote.")
            else:
                if not vpn_surfshark_conectar("nigeria"):
                    print("  [Surfshark] [WARN] No se pudo conectar de forma automática. Por favor, activa tu VPN a NIGERIA manualmente.")
                    self.input_concurrente(">>> Presiona Enter cuando la VPN a Nigeria esté ACTIVA <<<")
                else:
                    print("  [Surfshark] VPN activada.")
                # Limpiar caché DNS del sistema para evitar ERR_NAME_NOT_RESOLVED tras el cambio de red
                import subprocess as _sp
                print("  [Surfshark] Limpiando caché DNS y esperando estabilización de la red...")
                _sp.run(["ipconfig", "/flushdns"], capture_output=True)
                time.sleep(8.0)
        else:
            print("  [Proxy] Usando proxy residencial para registro. Se omite conexión de Surfshark.")
            
        # Re-lanzar navegador principal con el proxy de Nigeria
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--dns-prefetch-disable",
            "--credentials-enable-service=false",
            "--password-store=basic",
            "--disable-autofill",
            "--disable-save-password-bubble",
            "--disable-gpu",
            "--disable-software-rasterizer"
        ]
        proxy_dict = None
        if self.use_proxy and self.proxy_ng_server:
            proxy_dict = {"server": self.proxy_ng_server}
            if self.proxy_ng_user:
                proxy_dict["username"] = self.proxy_ng_user
            if self.proxy_ng_pass:
                proxy_dict["password"] = self.proxy_ng_pass
            print(f"  [Proxy] Usando proxy de NIGERIA: {self.proxy_ng_server}")
            
        launch_kwargs = {
            "user_data_dir": str(self.main_profile),
            "headless": self.headless,
            "args": launch_args,
            "ignore_default_args": ["--enable-automation"],
            "viewport": {"width": 1280, "height": 800},
            "locale": "es-ES",
            "proxy": proxy_dict
        }
        if not proxy_dict:
            launch_kwargs["channel"] = "chrome"
        if self.headless:
            launch_kwargs["user_agent"] = self.user_agent
        self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        self.context.set_default_navigation_timeout(45000)
        self.context.set_default_timeout(35000)
        self.registrar_contador_datos(self.context)
        self.context.add_init_script(STEALTH_SCRIPT)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.client_email = self.client_email
        self.page.manager = self
        self.page.bring_to_front()
        print("=" * 70 + "\n")
        
        print("\n--- PASO 6: Creando nueva cuenta Tidal del cliente ---")
        # Forzar el cierre de sesión limpiando cookies y almacenamiento local/sesión de Tidal
        self.context.clear_cookies(domain="tidal.com")
        self.context.clear_cookies(domain="login.tidal.com")
        self.context.clear_cookies(domain="account.tidal.com")
        try:
            self.page.goto("https://account.tidal.com/", wait_until="domcontentloaded", timeout=15000)
            self.page.evaluate("() => { window.localStorage.clear(); window.sessionStorage.clear(); }")
            time.sleep(1.0)
        except Exception:
            pass
        self.context.clear_cookies(domain="tidal.com")
        self.context.clear_cookies(domain="login.tidal.com")
        self.context.clear_cookies(domain="account.tidal.com")
        
        # Navegar con reintentos (la VPN puede tardar en estabilizar la resolución DNS)
        _max_intentos_nav = 3
        for _intento_nav in range(1, _max_intentos_nav + 1):
            navegar_con_bypass_referencia(self.page, "https://account.tidal.com/")
            time.sleep(2.0)
            # Verificar si la página cargó correctamente (no error de DNS/red)
            try:
                page_content = self.page.content()
                if "ERR_NAME_NOT_RESOLVED" in page_content or "ERR_CONNECTION" in page_content:
                    raise Exception("Error de DNS/red detectado en la página")
                break  # Página cargó correctamente
            except Exception as _nav_err:
                if _intento_nav < _max_intentos_nav:
                    print(f"  [Paso 6] [WARN] Intento {_intento_nav}/{_max_intentos_nav} falló: {_nav_err}. Limpiando DNS y reintentando en 5s...")
                    import subprocess as _sp
                    _sp.run(["ipconfig", "/flushdns"], capture_output=True)
                    time.sleep(5.0)
                else:
                    print(f"  [Paso 6] [WARN] No se pudo cargar account.tidal.com tras {_max_intentos_nav} intentos.")
        aceptar_cookies_con_espera(self.page)
        
        manejar_bloqueos_e_intervencion(self.page, "Registro Tidal (Email)")
        
        email_input = esperar_locator_en_frames(self.page, ['input[type="email"]', 'input[name="email"]'], timeout_s=15.0)
        if not email_input:
            try:
                self.page.screenshot(path="tmm_error_screenshot_step6.png")
                print("  [Error Debug] Captura de pantalla de diagnóstico guardada como 'tmm_error_screenshot_step6.png'.")
            except Exception:
                pass
            print("\n  [Paso 6] [WARN] No se localizó el campo de correo para el registro.")
            print("  [Paso 6] Esto puede deberse a un retraso de carga, bloqueo de CloudFront o página en blanco.")
            self.input_concurrente(">>> Por favor, ve al navegador, asegúrate de estar en la pantalla de ingreso de correo de Tidal, y luego presiona Enter aquí <<<")
            email_input = esperar_locator_en_frames(self.page, ['input[type="email"]', 'input[name="email"]'], timeout_s=10.0)
            if not email_input:
                raise RuntimeError("No se encontró el campo de correo para registro de forma definitiva.")
                
        rellenar_campo_humanizado(email_input, self.client_email)
        time.sleep(0.5)
        
        # Obtener el ID de correo mas reciente antes de gatillar el envio del codigo de registro
        max_id_previo = obtener_max_email_id(self.client_email)
        print(f"  [Paso 6] ID de correo de Tidal mas reciente antes de gatillar: {max_id_previo}")
        
        btn_continue = esperar_locator_en_frames(
            self.page,
            ["button:has-text('Continuar')", "button:has-text('Continue')", "button[type='submit']"],
            timeout_s=5.0
        )
        if btn_continue:
            btn_continue.click()
        time.sleep(3.5)
        
        # 1. Rellenar fecha de nacimiento y marcar términos (Pantalla "Crea tu cuenta" que aparece antes del código)
        print("  [Paso 6] Rellenando fecha de nacimiento por defecto (15/08/1995)...")
        time.sleep(1.0)
        
        # Seleccionar Día
        self.page.evaluate("""
            () => {
                const selects = document.querySelectorAll('select');
                if (selects.length >= 3) {
                    const daySelect = document.querySelector('select[name*="day" i]') || selects[0];
                    if (daySelect) {
                        daySelect.value = "15";
                        daySelect.dispatchEvent(new Event('input', { bubbles: true }));
                        daySelect.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                } else {
                    const dayInput = document.querySelector('input[name*="day" i]');
                    if (dayInput) {
                        dayInput.value = "15";
                        dayInput.dispatchEvent(new Event('input', { bubbles: true }));
                        dayInput.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }
            }
        """)
        time.sleep(0.6)
        
        # Seleccionar Mes
        self.page.evaluate("""
            () => {
                const selects = document.querySelectorAll('select');
                if (selects.length >= 3) {
                    const monthSelect = document.querySelector('select[name*="month" i]') || selects[1];
                    if (monthSelect) {
                        const opts = Array.from(monthSelect.options);
                        const targets = ["8", "08", "aug", "ago", "august", "agosto"];
                        let matched = false;
                        for (const opt of opts) {
                            const val = (opt.value || '').trim().toLowerCase();
                            const txt = (opt.textContent || '').trim().toLowerCase();
                            if (targets.some(t => val === t || txt === t || txt.includes(t))) {
                                monthSelect.value = opt.value;
                                monthSelect.dispatchEvent(new Event('input', { bubbles: true }));
                                monthSelect.dispatchEvent(new Event('change', { bubbles: true }));
                                matched = true;
                                break;
                            }
                        }
                        if (!matched && opts.length > 8) {
                            monthSelect.selectedIndex = opts.length === 13 ? 8 : 7;
                            monthSelect.dispatchEvent(new Event('input', { bubbles: true }));
                            monthSelect.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }
                } else {
                    const monthInput = document.querySelector('input[name*="month" i]');
                    if (monthInput) {
                        monthInput.value = "08";
                        monthInput.dispatchEvent(new Event('input', { bubbles: true }));
                        monthInput.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }
            }
        """)
        time.sleep(0.6)
        
        # Seleccionar Año
        self.page.evaluate("""
            () => {
                const selects = document.querySelectorAll('select');
                if (selects.length >= 3) {
                    const yearSelect = document.querySelector('select[name*="year" i]') || selects[2];
                    if (yearSelect) {
                        yearSelect.value = "1995";
                        yearSelect.dispatchEvent(new Event('input', { bubbles: true }));
                        yearSelect.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                } else {
                    const yearInput = document.querySelector('input[name*="year" i]');
                    if (yearInput) {
                        yearInput.value = "1995";
                        yearInput.dispatchEvent(new Event('input', { bubbles: true }));
                        yearInput.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }
            }
        """)
        time.sleep(0.6)
        
        # Marcar checkbox de términos y condiciones (usando click nativo para React)
        print("  [Paso 6] Marcando checkbox de términos y privacidad...")
        self.page.evaluate("""
            () => {
                const checkboxes = document.querySelectorAll('input[type="checkbox"]');
                checkboxes.forEach(cb => {
                    const parentText = cb.parentElement ? cb.parentElement.textContent || '' : '';
                    if (parentText.includes('Términos') || parentText.includes('Terms') || 
                        parentText.includes('Privacidad') || parentText.includes('Privacy')) {
                        if (!cb.checked) {
                            cb.click();
                            // Si sigue sin marcarse por diseño interceptor, clickear el elemento padre
                            if (!cb.checked && cb.parentElement) {
                                cb.parentElement.click();
                            }
                        }
                    }
                });
            }
        """)
        time.sleep(1.0)
        
        # Obtener el ID de correo mas reciente justo ANTES de gatillar el envio del código al presionar Suscribete
        max_id_previo = obtener_max_email_id(self.client_email)
        print(f"  [Paso 6] ID de correo de Tidal mas reciente antes de gatillar: {max_id_previo}")
        
        # Presionar el botón "Suscríbete" para disparar el código de verificación
        print("  [Paso 6] Pulsando boton 'Suscríbete' para gatillar el envio del codigo...")
        btn_subscribe_clicked = self.page.evaluate("""
            () => {
                const btn = document.querySelector('button[type="submit"]') || 
                            Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Suscríbete') || b.textContent.includes('Subscribe'));
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            }
        """)
        
        if not btn_subscribe_clicked:
            btn_sub = esperar_locator_en_frames(
                self.page,
                ["button:has-text('Suscríbete')", "button:has-text('Subscribe')", "button[type='submit']"],
                timeout_s=5.0
            )
            if btn_sub:
                btn_sub.click(force=True)
                
        time.sleep(4.0)
        
        # 2. Buscar y colocar el código de verificación
        print("  [Paso 6] Buscando código de registro enviado al correo del cliente (hasta 1 minuto)...")
        codigo = None
        for intento in range(1, 6):
            print(f"  [Paso 6] Intento {intento}/5: Buscando correo de registro...")
            codigo = obtener_codigo_de_gmail(self.page, email_destinatario=self.client_email, query="from:tidal", required_keywords=["registr", "bienven", "código", "code", "verific"], after_email_id=max_id_previo)
            if codigo:
                break
            if intento < 5:
                print("  [Paso 6] Correo no encontrado aun. Esperando 12 segundos...")
                time.sleep(12.0)
        
        if not codigo:
            print("\n  [Gmail Auto] [WARN] No se pudo extraer el código de registro automáticamente.")
            codigo = self.input_concurrente(">>> Introduce el código de registro enviado al correo del cliente: ").strip()
            
        if codigo:
            # Esperar a que el input del código aparezca en pantalla
            code_input = esperar_locator_en_frames(
                self.page,
                ['input[name="code"]', 'input[placeholder*="código" i]', 'input[placeholder*="code" i]',
                 'input[type="text"]', 'input[inputmode="numeric"]'],
                timeout_s=5.0
            )
            if code_input:
                escribir_codigo_verificacion_inteligente(self.page, codigo)
                time.sleep(3.5)
                
                # Hacer clic en verificar código (envolver en try por si la página ya navegó automáticamente)
                try:
                    self.page.evaluate("""
                        () => {
                            const texts = ['Continuar', 'Continue', 'Confirmar', 'Confirm', 'Verify', 'Verificar'];
                            const elms = document.querySelectorAll('button, a, [role="button"]');
                            const isVisible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
                            for (const el of elms) {
                                const text = (el.textContent || '').trim();
                                if (texts.includes(text) && isVisible(el)) {
                                    el.click();
                                    return true;
                                }
                            }
                            return false;
                        }
                    """)
                except Exception as e:
                    print(f"  [Paso 6] [Info] Se omitio el click manual de continuar (el formulario se envio de forma automatica): {e}")
                # Aumentado de 4.0 a 12.0 segundos para dar tiempo a la red lenta del VPN en Nigeria
                print("  [Paso 6] Enviando codigo de registro. Esperando procesamiento de red (12 segundos)...")
                time.sleep(12.0)
            else:
                if codigo.startswith("http"):
                    reg_page = self.context.new_page()
                    reg_page.goto(codigo)
                    time.sleep(4.0)
                    reg_page.close()
                    


        print("  [Paso 6] Asegurando que la cuenta de Nigeria está creada...")
        time.sleep(2.0)
        print("  [Paso 6] [OK] Registro completado.")
        
        # Cerrar el contexto de Nigeria
        print("  [Paso 6] Cerrando navegador de Nigeria...")
        self.context.close()
        
        # Desconectar VPN si no estamos usando proxies
        if not (self.use_proxy and self.proxy_pe_server):
            if BATCH_MODE_VPN:
                print("  [Surfshark] Modo VPN global activo. Manteniendo VPN conectada para otros hilos.")
            else:
                if not vpn_surfshark_desconectar():
                    print("  [Surfshark] [WARN] No se pudo desactivar de forma automática. Por favor, desconecta tu VPN manualmente.")
                    input(">>> Presiona Enter cuando la VPN esté DESACTIVADA <<<")
                else:
                    print("  [Surfshark] VPN desactivada. Esperando 3 segundos...")
                    time.sleep(3.0)
        else:
            print("  [Proxy] Usando proxy residencial de Perú. Se omite desconexión de Surfshark.")

    def asegurar_navegador_abierto(self):
        """Asegura que el navegador principal con el perfil local esté abierto."""
        try:
            if self.context and self.page and not self.page.is_closed():
                return
        except Exception:
            pass
            
        print("  [Navegador] Lanzando navegador principal con perfil local...")
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--credentials-enable-service=false",
            "--password-store=basic",
            "--disable-autofill",
            "--disable-save-password-bubble",
            "--disable-gpu",
            "--disable-software-rasterizer"
        ]
        proxy_dict = None
        if self.use_proxy and self.proxy_pe_server:
            proxy_dict = {"server": self.proxy_pe_server}
            if self.proxy_pe_user:
                proxy_dict["username"] = self.proxy_pe_user
            if self.proxy_pe_pass:
                proxy_dict["password"] = self.proxy_pe_pass
            print(f"  [Proxy] Usando proxy de PERÚ para el navegador: {self.proxy_pe_server}")
            
        launch_kwargs = {
            "user_data_dir": str(self.main_profile),
            "headless": self.headless,
            "args": launch_args,
            "ignore_default_args": ["--enable-automation"],
            "viewport": {"width": 1280, "height": 800},
            "locale": "es-ES",
            "proxy": proxy_dict
        }
        if not proxy_dict:
            launch_kwargs["channel"] = "chrome"
        if self.headless:
            launch_kwargs["user_agent"] = self.user_agent
        self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        self.context.set_default_navigation_timeout(45000)
        self.context.set_default_timeout(35000)
        self.registrar_contador_datos(self.context)
        self.context.add_init_script(STEALTH_SCRIPT)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.client_email = self.client_email
        self.page.manager = self
        self.page.bring_to_front()

    def rotar_proxy_contexto(self, tipo="PE"):
        """Cierra el navegador actual, selecciona otro proxy de la lista y vuelve a abrirlo con el mismo perfil."""
        global valid_pe_list, valid_ng_list
        print(f"\n  [Proxy Rotation] Rotando proxy de tipo {tipo} por uno nuevo debido a bloqueos...")
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        self.context = None
        self.page = None
        
        # Limpiar procesos Chrome residuales y archivos de bloqueo del perfil
        import subprocess as _sp
        try:
            _sp.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True, timeout=5)
        except Exception:
            pass
        time.sleep(2.0)
        
        import glob
        profile_path = Path(self.main_profile)
        for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            lock_path = profile_path / lock_file
            try:
                if lock_path.exists():
                    lock_path.unlink()
            except Exception:
                pass
        for pattern in [str(profile_path / "**" / "SingletonLock"), str(profile_path / "**" / "lockfile")]:
            for f in glob.glob(pattern, recursive=True):
                try:
                    Path(f).unlink()
                except Exception:
                    pass
        
        if tipo == "PE":
            if valid_pe_list:
                import random
                p_pe = random.choice(valid_pe_list)
                self.proxy_pe_server = p_pe["server"]
                self.proxy_pe_user = p_pe["username"]
                self.proxy_pe_pass = p_pe["password"]
                print(f"  [Proxy Rotation] Nuevo proxy PE configurado: {self.proxy_pe_server}")
            else:
                print("  [Proxy Rotation] No hay más proxies PE disponibles.")
        else:
            if valid_ng_list:
                import random
                p_ng = random.choice(valid_ng_list)
                self.proxy_ng_server = p_ng["server"]
                self.proxy_ng_user = p_ng["username"]
                self.proxy_ng_pass = p_ng["password"]
                print(f"  [Proxy Rotation] Nuevo proxy NG configurado: {self.proxy_ng_server}")
            else:
                print("  [Proxy Rotation] No hay más proxies NG disponibles.")
                
        # Reabrir el navegador
        self.asegurar_navegador_abierto()

    def asegurar_login_cuenta_nueva(self):
        """Asegura que el navegador principal esté logueado en la nueva cuenta de Tidal (con target_pwd)."""
        print("  [Paso 7] Comprobando si la sesión en la nueva cuenta de TIDAL está activa...")
        
        # Intentaremos loguearnos hasta 3 veces rotando de proxy si Tidal nos bloquea o pide verificación
        for intento_login in range(1, 4):
            try:
                # 1. Comprobar si la sesión ya está activa
                try:
                    self.page.goto("https://account.tidal.com/profile", wait_until="domcontentloaded", timeout=15000)
                    time.sleep(2.0)
                    aceptar_cookies_con_espera(self.page)
                    
                    if "login.tidal.com" not in self.page.url:
                        print("  [Paso 7] Sesión activa de TIDAL detectada. Continuando...")
                        return
                except Exception:
                    pass
                
                print(f"  [Paso 7] No se detectó sesión activa de TIDAL (Intento {intento_login}/3). Iniciando sesión...")
                try:
                    self.context.clear_cookies(domain="tidal.com")
                    self.context.clear_cookies(domain="login.tidal.com")
                    self.context.clear_cookies(domain="account.tidal.com")
                except Exception:
                    pass
                
                # 2. Cargar página de Tidal
                navegar_con_bypass_referencia(self.page, "https://account.tidal.com/")
                time.sleep(2.0)
                aceptar_cookies_con_espera(self.page)
                
                # 3. Rellenar correo
                email_selectors = ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]', '#email']
                email_input = esperar_locator_en_frames(self.page, email_selectors, label_regex=re.compile(r"correo|email", re.I), timeout_s=15.0)
                if not email_input:
                    raise RuntimeError("No se localizó el campo de correo para iniciar sesión en Tidal.")
                    
                rellenar_campo_humanizado(email_input, self.client_email)
                time.sleep(0.5)
                
                btn_continue = esperar_locator_en_frames(
                    self.page, 
                    ["button:has-text('Continuar')", "button:has-text('Continue')", "button[type='submit']"],
                    text_regex=re.compile(r"continuar|continue", re.I),
                    timeout_s=5.0
                )
                if not btn_continue:
                    raise RuntimeError("No se encontró el botón 'Continuar' en Tidal.")
                btn_continue.click()
                time.sleep(3.0)
                
                # 4. Rellenar contraseña (detectar si pide contraseña o código primero)
                pwd_input = esperar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]'], timeout_s=4.0)
                if not pwd_input:
                    btn_pwd_mode = esperar_locator_en_frames(
                        self.page,
                        ["a:has-text('contraseña')", "button:has-text('contraseña')",
                         "a:has-text('código')", "button:has-text('código')",
                         "a:has-text('code')", "button:has-text('code')",
                         "a:has-text('password')", "button:has-text('password')",
                         "text='Inicia sesión con contraseña'", "text='Sign in with password'"],
                        text_regex=re.compile(r"con contrase|with password|iniciar.*contrase|sign.*password", re.I),
                        timeout_s=5.0
                    )
                    if btn_pwd_mode:
                        btn_pwd_mode.click()
                        time.sleep(3.0)
                        pwd_input = esperar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]'], timeout_s=10.0)
                
                if not pwd_input:
                    raise RuntimeError("No se localizó el campo de contraseña en Tidal.")
                    
                rellenar_campo_humanizado(pwd_input, self.target_pwd)
                time.sleep(0.5)
                
                # 5. Clicar Login
                btn_login = esperar_locator_en_frames(
                    self.page,
                    [
                        "button[type='submit']",
                        "button:has-text(/^Iniciar sesión$/)", "button:has-text(/^Log in$/)", 
                        "button:has-text(/^Continuar$/)", "button:has-text(/^Continue$/)", 
                        "button:has-text(/^Siguiente$/)", "button:has-text(/^Next$/)",
                        "button:has-text('Iniciar sesión')", "button:has-text('Log in')"
                    ],
                    timeout_s=8.0
                )
                if not btn_login:
                    raise RuntimeError("No se localizó el botón para iniciar sesión en Tidal.")
                btn_login.click()
                
                # Esperar dinámicamente hasta 15 segundos a que la URL cambie a account.tidal.com o profile
                try:
                    self.page.wait_for_url(re.compile(r"https://account\.tidal\.com"), timeout=15000)
                except Exception:
                    pass
                    
                url_actual = self.page.url.lower()
                if "login.tidal.com" in url_actual:
                    raise RuntimeError("No se pudo iniciar sesión en Tidal de forma automática (redirección fallida).")
                
                print("  [Paso 7] Sesión en nueva cuenta iniciada con éxito.")
                return
                
            except Exception as e:
                print(f"  [Paso 7] [WARN] Intento {intento_login}/3 de inicio de sesión falló o fue bloqueado: {e}")
                if intento_login == 3:
                    # En el último intento, si todo falla, levantamos la pausa manual por seguridad
                    print("\n" + "=" * 60)
                    print("  PAUSA MANUAL DE INICIO DE SESIÓN (ÚLTIMO RECURSO)")
                    print("  No se pudo iniciar sesión tras rotar de proxy 3 veces.")
                    print("  1. Ve a la ventana abierta del navegador.")
                    print("  2. Inicia sesión manualmente en la nueva cuenta de Tidal.")
                    print("  3. Una vez veas tu perfil de Tidal activo, regresa aquí y presiona Enter.")
                    print("=" * 60 + "\n")
                    self.input_concurrente(">>> Presiona Enter una vez hayas iniciado sesión manualmente <<<")
                    return
                
                # Rotar de proxy PE y volver a intentar con el mismo perfil
                if self.use_proxy:
                    self.rotar_proxy_contexto(tipo="PE")
                else:
                    time.sleep(5.0)

    # --- PASO 7: Copiar CSV a la nueva cuenta TIDAL en TuneMyMusic ---
    def step7_copy_csv_to_new_account(self):
        if self.cuenta_abortada:
            return
        if getattr(self, "skip_playlists", False):
            print("\n--- PASO 7: Omitiendo transferencia de playlists ya que la cuenta origen no tenía ninguna ---")
            return
            
        # Asegurar la ruta del CSV si iniciamos desde este paso
        if not self.csv_path:
            self.csv_path = DOWNLOADS_DIR / f"{self.client_email}.csv"
            
        if not self.csv_path.exists():
            print(f"  [Paso 7] [WARN] No se encontró el archivo CSV en '{self.csv_path}'. Omitiendo Paso 7 por falta de archivo.")
            self.skip_playlists = True
            return
        print("\n--- PASO 7: Transfiriendo música del CSV a la nueva cuenta Tidal ---")
        print("  [TuneMyMusic] Esperando bloqueo global para TuneMyMusic (Importación)...")
        tmm_lock.acquire()
        print("  [TuneMyMusic] Bloqueo global adquirido. Iniciando...")
        self.asegurar_navegador_abierto()
        
        # Asegurar que esté logueado en la nueva cuenta Tidal
        try:
            self.asegurar_login_cuenta_nueva()
        except Exception as e:
            print(f"  [Paso 7] [WARN] No se pudo realizar el inicio de sesión automático previo: {e}. Se intentará continuar...")
        tmm_page = self.page
        try:
            # Cancelar de forma proactiva cualquier redirección o navegación en segundo plano 
            # de Tidal que haya quedado pendiente (evita el error de navegación interrumpida)
            try:
                tmm_page.evaluate("() => window.stop()")
                time.sleep(1.0)
            except Exception:
                pass
                
            # No borramos las cookies de tunemymusic.com para mantener la sesión de usuario iniciada
                
            tmm_page.goto("https://www.tunemymusic.com/es/transfer", wait_until="domcontentloaded")
            time.sleep(2.0)
            
            try:
                tmm_page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
                tmm_page.reload(wait_until="domcontentloaded")
                time.sleep(2.0)
            except Exception:
                pass
                
            aceptar_cookies_con_espera(tmm_page)
            
            # Seleccionar "Subir archivo" (Archivo local)
            print("  [Paso 7] Esperando el boton 'Subir archivo' (hasta 20 segundos)...")
            btn_upload = tmm_page.locator("button[name='FromFile']").or_(tmm_page.locator("button[aria-label='Subir archivo']")).first
            if esperar_visibilidad(btn_upload, 20000):
                btn_upload.click(force=True)
            else:
                if not hacer_click_por_textos(tmm_page, ["Subir archivo", "Archivo", "File"]):
                    raise RuntimeError("No se encontró la opción de subir archivo/CSV.")
            time.sleep(3.0)
            
            # Esperar a que el input de carga de archivos esté adjunto en el DOM
            print("  [Paso 7] Preparando carga del CSV...")
            input_file = tmm_page.locator("input[type='file']").first
            input_file.wait_for(state="attached", timeout=20000)
            input_file.set_input_files(str(self.csv_path))
            time.sleep(3.0)
            
            btn_next = tmm_page.locator("button:has-text('Siguiente')").or_(tmm_page.locator("button:has-text('Continuar')")).or_(tmm_page.locator("text=Siguiente")).or_(tmm_page.locator("text=Continuar")).first
            if esperar_visibilidad(btn_next, 10000):
                try:
                    btn_next.evaluate("el => el.click()")
                except Exception:
                    btn_next.click(force=True)
            else:
                if not hacer_click_por_textos(tmm_page, ["Siguiente", "Next", "Continuar", "Continue"]):
                    raise RuntimeError("No se encontró el botón de Siguiente o Continuar tras subir el CSV.")
            time.sleep(2.0)
            
            # Esperar y pulsar el botón "Elige destino" (Choose destination) antes de seleccionar TIDAL
            print("  [Paso 7] Esperando el boton 'Elige destino'...")
            btn_choose_dest = tmm_page.locator("button:has-text('Elige destino')").or_(tmm_page.locator("text=Elige destino")).or_(tmm_page.locator("button:has-text('Choose destination')")).first
            if esperar_visibilidad(btn_choose_dest, 15000):
                try:
                    btn_choose_dest.evaluate("el => el.click()")
                except Exception:
                    btn_choose_dest.click(force=True)
            else:
                if not hacer_click_por_textos(tmm_page, ["Elige destino", "Choose destination"]):
                    print("  [Paso 7] [WARN] No se localizó el botón 'Elige destino', intentando continuar...")
            time.sleep(3.0)
                
            # Seleccionar TIDAL como destino
            btn_tidal = tmm_page.locator("button[name='Tidal']").or_(tmm_page.locator("button[aria-label='TIDAL']")).first
            if esperar_visibilidad(btn_tidal, 6000):
                try:
                    btn_tidal.evaluate("el => el.click()")
                except Exception:
                    btn_tidal.click(force=True)
            else:
                if not hacer_click_por_textos(tmm_page, ["TIDAL", "Tidal"]):
                    raise RuntimeError("No se encontró el botón de TIDAL como destino.")
            time.sleep(3.0)
            
            # Manejar flujo de autorización para la nueva cuenta
            autorizar_tidal_en_tmm(tmm_page, self.client_email)
                
            # Esperar y pulsar el botón para iniciar transferencia (puede tardar en calcular tracks)
            print("  [Paso 7] Esperando el boton de inicio de transferencia (hasta 20 segundos)...")
            btn_start = tmm_page.locator("button:has-text('Comenzar transferencia')") \
                .or_(tmm_page.locator("button:has-text('COMENZAR TRANSFERENCIA')")) \
                .or_(tmm_page.locator("button:has-text('Comenzar a mover mi música')")) \
                .or_(tmm_page.locator("button:has-text('Start Transfer')")) \
                .or_(tmm_page.locator("button:has-text('START TRANSFER')")).first
                
            if esperar_visibilidad(btn_start, 20000):
                try:
                    btn_start.evaluate("el => el.click()")
                except Exception:
                    btn_start.click(force=True)
                print("  [Paso 7] Boton de inicio de transferencia pulsado exitosamente.")
            else:
                # Intentar pulsar por texto
                intentado_por_texto = False
                for txt_start in ["COMENZAR TRANSFERENCIA", "Comenzar transferencia", "Comenzar a mover mi música", "START TRANSFER", "Start Transfer"]:
                    try:
                        btn_texto = tmm_page.locator(f"button:has-text('{txt_start}')").first
                        if btn_texto.count() > 0 and btn_texto.is_visible():
                            btn_texto.evaluate("el => el.click()")
                            intentado_por_texto = True
                            print(f"  [Paso 7] Boton de inicio pulsado por texto ('{txt_start}').")
                            break
                    except Exception:
                        pass
                
                if not intentado_por_texto:
                    raise RuntimeError("No se encontró el botón para comenzar la transferencia.")
            time.sleep(1.0)
            print("  [Paso 7] Copiando playlists a la nueva cuenta TIDAL... (Esto puede tomar unos momentos)")
            
            status_text = tmm_page.locator("text=Transferencia completada").or_(tmm_page.locator("text=Transfer completed").or_(tmm_page.locator("text=Completado"))).first
            try:
                # Aumentado a 10 minutos (600,000 ms) para soportar cuentas masivas sin interrupciones
                status_text.wait_for(state="visible", timeout=600000)
                print("  [Paso 7] Transferencia terminada con éxito en TuneMyMusic.")
            except Exception:
                print("  [WARN] El indicador de transferencia completada tardó demasiado. Continuando...")
                
        except Exception as e:
            try:
                tmm_page.screenshot(path="tmm_error_screenshot_step7.png")
                print("  [Error Debug] Captura de pantalla del error guardada como 'tmm_error_screenshot_step7.png'.")
            except Exception:
                pass
            raise e
        finally:
            try:
                if tmm_page != self.page:
                    tmm_page.close()
            except Exception:
                pass
            tmm_lock.release()
            print("  [TuneMyMusic] Bloqueo global de TuneMyMusic liberado.")
        print("  [Paso 7] [OK] Playlists copiadas con éxito a la nueva cuenta.")

    # --- PASO 8: Solicitar restablecimiento de contraseña para la nueva cuenta ---
    def step8_request_password_reset(self):
        if self.cuenta_abortada:
            return
        print("\n--- PASO 8: Solicitando restablecimiento de contraseña para la nueva cuenta ---")
        print("  [Paso 8] Esperando turno para solicitar restablecimiento (cola serializada)...")
        reset_lock.acquire()
        try:
            self.asegurar_navegador_abierto()
            # Capturar baseline DENTRO del lock para que cada hilo tenga un ID único
            self.reset_baseline_id = obtener_max_email_id(self.client_email, "tidal")
            print(f"  [Paso 8] ID de correo base para restablecimiento: {self.reset_baseline_id}")
            
            proxy_dict = None
            if self.use_proxy and self.proxy_pe_server:
                proxy_dict = {"server": self.proxy_pe_server}
                if self.proxy_pe_user:
                    proxy_dict["username"] = self.proxy_pe_user
                if self.proxy_pe_pass:
                    proxy_dict["password"] = self.proxy_pe_pass
            
            context_kwargs = {
                "viewport": {"width": 1280, "height": 800}, 
                "locale": "es-ES",
                "proxy": proxy_dict
            }
            if self.headless:
                context_kwargs["user_agent"] = self.user_agent
            temp_context = self.context.browser.new_context(**context_kwargs)
            temp_context.set_default_navigation_timeout(45000)
            temp_context.set_default_timeout(35000)
            self.registrar_contador_datos(temp_context)
            temp_context.add_init_script(STEALTH_SCRIPT)
            temp_page = temp_context.new_page()
            temp_page.client_email = self.client_email
            temp_page.manager = self
            try:
                # Inicializar cookies de Datadome y sesión base
                print("  [Paso 8] Inicializando sesión base de Tidal para evitar sospechas de Datadome...")
                temp_page.goto("https://account.tidal.com/", wait_until="domcontentloaded", timeout=30000)
                time.sleep(2.0)
                aceptar_cookies_con_espera(temp_page)
                
                print("  [Paso 8] Navegando a la página de restablecimiento de contraseña...")
                temp_page.goto("https://login.tidal.com/resetpass", wait_until="domcontentloaded", timeout=30000)
                time.sleep(2.0)
                aceptar_cookies_con_espera(temp_page)
                
                manejar_bloqueos_e_intervencion(temp_page, "Restablecer Contraseña (Carga)")
                
                email_input = esperar_locator_en_frames(
                    temp_page, 
                    ['input[type="email"]', 'input[type="text"]', 'input[placeholder*="email" i]', 'input[placeholder*="usuario" i]'], 
                    timeout_s=15.0
                )
                if not email_input:
                    raise RuntimeError("No se localizó el campo de correo para iniciar restablecimiento en resetpass.")
                
                rellenar_campo_humanizado(email_input, self.client_email)
                time.sleep(0.5)
                
                # Asegurar de aceptar/remover cookies si aparecieron tarde tras rellenar el campo
                print("  [Paso 8] Verificando/removiendo banner de cookies antes de continuar...")
                aceptar_cookies_con_espera(temp_page)
                
                btn_continue = esperar_locator_en_frames(
                    temp_page,
                    ["button:has-text('Continuar')", "button:has-text('Continue')", "button[type='submit']"],
                    timeout_s=8.0
                )
                if not btn_continue:
                    raise RuntimeError("No se encontró el botón 'Continuar' en resetpass.")
                
                # Pulsar el botón Continuar de forma robusta
                try:
                    btn_continue.evaluate("el => el.click()")
                except Exception:
                    btn_continue.click(force=True, timeout=3000)
                
                manejar_bloqueos_e_intervencion(temp_page, "Restablecer Contraseña (Envío)")
                time.sleep(3.0)
                
                success_title = temp_page.locator(
                    "text=Se ha enviado el enlace"
                ).or_(temp_page.locator("text=enlace de verificación")
                ).or_(temp_page.locator("text=instrucciones")
                ).or_(temp_page.locator("text=check your")
                ).or_(temp_page.locator("text=sent a verification")
                ).or_(temp_page.locator("text=Hemos enviado")).first
                if esperar_visibilidad(success_title, 10000):
                    print("  [Paso 8] [OK] Solicitud de cambio de contraseña enviada. El correo llegará en breve.")
                else:
                    print("  [Paso 8] [WARN] No se pudo comprobar el mensaje de confirmación de envío. Continuando...")
            finally:
                temp_context.close()
            
            # Esperar brevemente para que el correo de Tidal llegue al buzón antes de liberar el turno
            print("  [Paso 8] Esperando 8 segundos para que el correo de reset llegue al buzón...")
            time.sleep(8.0)
        finally:
            reset_lock.release()
            print("  [Paso 8] Turno de restablecimiento liberado.")

    # --- HELPER DE GESTION DE PLAN FAMILIAR TITULAR ---
    def cargar_familiares_titulares(self, file_path) -> list:
        """Lee el archivo familiar_titular.txt y devuelve la lista de diccionarios de cuentas."""
        cuentas = []
        if not file_path.exists():
            # Crear plantilla si no existe
            content = (
                "# Cuentas Familiares Titulares de Tidal (Nigeria)\n"
                "# Formato: correo_titular, miembros_actuales, estado, miembros_detalles\n"
                "# Ejemplo: cakeseller1234@gmail.com, 0, disponible, []\n"
            )
            file_path.write_text(content, encoding="utf-8")
            return cuentas
            
        for line in file_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",", 3)]
            if len(parts) >= 3:
                correo = parts[0]
                try:
                    miembros_actuales = int(parts[1])
                except ValueError:
                    miembros_actuales = 0
                estado = parts[2]
                detalles_str = parts[3] if len(parts) > 3 else "[]"
                try:
                    import ast
                    detalles = ast.literal_eval(detalles_str)
                    if not isinstance(detalles, list):
                        detalles = []
                except Exception:
                    detalles = []
                    
                cuentas.append({
                    "correo": correo,
                    "miembros_actuales": miembros_actuales,
                    "estado": estado,
                    "detalles": detalles
                })
        return cuentas

    def guardar_familiares_titulares(self, file_path, cuentas):
        """Guarda la lista de cuentas en el archivo familiar_titular.txt."""
        lines = [
            "# Cuentas Familiares Titulares de Tidal (Nigeria)",
            "# Formato: correo_titular, miembros_actuales, estado, miembros_detalles",
            "# Ejemplo: cakeseller1234@gmail.com, 0, disponible, []"
        ]
        for c in cuentas:
            lines.append(f"{c['correo']}, {c['miembros_actuales']}, {c['estado']}, {list(c['detalles'])}")
        file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def emails_coinciden_sin_puntos(self, email1: str, email2: str) -> bool:
        if not email1 or not email2:
            return False
        e1 = email1.lower().strip()
        e2 = email2.lower().strip()
        if "@" in e1 and "@" in e2:
            usr1, dom1 = e1.split("@", 1)
            usr2, dom2 = e2.split("@", 1)
            if dom1 == dom2:
                return usr1.replace(".", "") == usr2.replace(".", "")
        return e1.replace(".", "") == e2.replace(".", "")

    def obtener_email_logueado_tidal(self, page) -> str | None:
        """Detecta qué correo está logueado actualmente en Tidal cargando la página de perfil."""
        try:
            page.goto("https://account.tidal.com/profile", wait_until="domcontentloaded", timeout=15000)
            time.sleep(2.0)
            if "login.tidal.com" in page.url:
                return None
                
            email_el = page.locator("input[type='email']").or_(page.locator("input[name='email']")).first
            if email_el.count() > 0:
                val = email_el.input_value()
                if val and "@" in val:
                    return val.strip().lower()
                    
            body_text = page.evaluate("() => document.body.innerText")
            emails_found = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", body_text)
            if emails_found:
                for email in emails_found:
                    email_low = email.lower()
                    if "tidal.com" not in email_low:
                        return email_low
        except Exception:
            pass
        return None

    def logout_tidal_total(self, page):
        """Cierra la sesión de Tidal de forma absoluta borrando cookies y almacenamiento local/sesión."""
        print("  [Titular Logout] Borrando cookies y almacenamiento de Tidal para asegurar sesión limpia...")
        try:
            # Limpiar absolutamente todas las cookies del contexto del navegador
            page.context.clear_cookies()
            print("  [Titular Logout] Todas las cookies del contexto del navegador han sido eliminadas.")
        except Exception as e:
            print(f"  [Titular Logout] [WARN] Error al borrar cookies: {e}")
            
        # Borrar localStorage y sessionStorage visitando los dominios de Tidal
        for url in ["https://account.tidal.com/", "https://login.tidal.com/"]:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=12000)
                page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
            except Exception:
                pass

    def login_familiar_titular_con_codigo(self, page, email_titular) -> bool:
        """Realiza el login automático de la cuenta familiar titular usando un código de verificación de Tidal vía IMAP."""
        print(f"  [Titular Login] Iniciando sesión para la cuenta familiar: {email_titular}")
        
        # 1. Obtener baseline del ID de correo antes de que Tidal envíe el código
        max_id_previo = obtener_max_email_id(email_titular, "tidal")
        print(f"  [Titular Login] ID de correo base previo: {max_id_previo}")
        
        # 2. Cargar página de login
        navegar_con_bypass_referencia(page, "https://account.tidal.com/")
        time.sleep(2.0)
        aceptar_cookies_con_espera(page)
        
        # 3. Introducir correo
        email_selectors = ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]', '#email']
        email_input = esperar_locator_en_frames(page, email_selectors, label_regex=re.compile(r"correo|email", re.I), timeout_s=15.0)
        if not email_input:
            print("  [Titular Login] [Error] No se localizó el campo de correo.")
            return False
            
        rellenar_campo_humanizado(email_input, email_titular)
        time.sleep(0.5)
        
        btn_continue = esperar_locator_en_frames(
            page, 
            ["button:has-text('Continuar')", "button:has-text('Continue')", "button[type='submit']"],
            text_regex=re.compile(r"continuar|continue", re.I),
            timeout_s=5.0
        )
        if not btn_continue:
            print("  [Titular Login] [Error] No se localizó el botón 'Continuar'.")
            return False
        btn_continue.click()
        time.sleep(3.0)
        
        # 4. Detectar si pide contraseña o si directamente pide código, o si hay que cambiar a modo código
        pwd_input = esperar_locator_en_frames(page, ['input[type="password"]', 'input[name="password"]'], timeout_s=4.0)
        if pwd_input:
            print("  [Titular Login] Pantalla de contraseña detectada. Buscando opción de código...")
            btn_code_mode = esperar_locator_en_frames(
                page,
                ["a:has-text('contraseña')", "button:has-text('contraseña')",
                 "a:has-text('código')", "button:has-text('código')",
                 "a:has-text('code')", "button:has-text('code')",
                 "a:has-text('password')", "button:has-text('password')",
                 "text='Inicia sesión sin contraseña'", "text='Sign in without password'"],
                text_regex=re.compile(r"sin contrase|with a code|con un c|passwordless", re.I),
                timeout_s=5.0
            )
            if btn_code_mode:
                print("  [Titular Login] Cambiando a inicio de sesión con código...")
                btn_code_mode.click()
                time.sleep(3.0)
                
        # 5. Esperar a que el input del código esté visible
        code_input = esperar_locator_en_frames(
            page,
            ['input[name="code"]', 'input[placeholder*="código" i]', 'input[placeholder*="code" i]',
             'input[type="text"]', 'input[inputmode="numeric"]'],
            timeout_s=10.0
        )
        
        if not code_input:
            print("  [Titular Login] [Error] No se detectó la pantalla para introducir el código.")
            return False
            
        # 6. Buscar el código en Gmail via IMAP
        print("  [Titular Login] Buscando código de inicio de sesión enviado al correo (hasta 1.5 minutos)...")
        codigo = None
        for intento in range(1, 8):
            print(f"  [Titular Login] Intento {intento}/7: Buscando correo de Tidal...")
            codigo = obtener_codigo_de_gmail(page, email_destinatario=email_titular, query="from:tidal", required_keywords=["código", "code", "inici"], after_email_id=max_id_previo)
            if codigo:
                break
            if intento < 7:
                print("  [Titular Login] Correo no encontrado aun. Esperando 12 segundos...")
                time.sleep(12.0)
                
        if not codigo:
            print("\n  [Titular Login] [Error] No se pudo obtener el código automáticamente.")
            codigo = input_concurrente(">>> Introduce el código de inicio de sesión enviado al titular de forma manual: ").strip()
            
        if codigo:
            print(f"  [Titular Login] Introduciendo código: {codigo}")
            escribir_codigo_verificacion_inteligente(page, codigo)
            # Esperar dinámicamente hasta 15 segundos a que la URL cambie a account.tidal.com o profile
            try:
                page.wait_for_url(re.compile(r"https://account\.tidal\.com"), timeout=15000)
            except Exception:
                pass
            
            # Comprobar si logueó con éxito
            url_actual = page.url.lower()
            if "login.tidal.com" not in url_actual:
                print("  [Titular Login] [OK] Sesión iniciada correctamente. Esperando que finalicen las redirecciones...")
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                time.sleep(4.5)  # Pausa de seguridad para que se guarden las cookies y se estabilice el redireccionamiento
                return True
                
        print("  [Titular Login] [Error] No se pudo completar el inicio de sesión.")
        return False

    def contar_miembros_familia(self, page) -> int:
        """Cuenta cuántos miembros adicionales hay en el plan familiar actualmente (activos y pendientes)."""
        page.wait_for_load_state("domcontentloaded")
        time.sleep(4.0)
        
        remove_buttons = page.locator("button:has-text('Eliminar')").or_(page.locator("button:has-text('Remove')")).or_(page.locator("button:has-text('Reenviar')")).or_(page.locator("button:has-text('Resend')"))
        count = remove_buttons.count()
        
        if count == 0:
            custom_buttons = page.locator("button, a").all()
            for btn in custom_buttons:
                try:
                    txt = btn.inner_text().lower()
                    if any(x in txt for x in ["eliminar", "remove", "resend", "reenviar", "cancelar", "cancel"]):
                        if not any(x in txt for x in ["invitar", "invite", "añadir", "add", "salir", "logout"]):
                            count += 1
                except Exception:
                    continue
                    
        print(f"  [Familia] Se detectaron {count} miembros adicionales en el plan familiar.")
        return count

    # --- PASO 9: Agregar al plan familiar ---
    def step9_invite_to_family_plan(self, p):
        if self.cuenta_abortada:
            return
        global family_invite_queue
        print("\n--- PASO 9: Creando invitación familiar en la cuenta del titular ---")
        
        # Capturar baseline de invitación ANTES de depositar en la cola
        self.invite_baseline_id = obtener_max_email_id(self.client_email, "tidal")
        print(f"  [Paso 9] ID de correo base para invitacion: {self.invite_baseline_id}")
        
        # Depositar correo en la cola global
        family_invite_queue.append(self.client_email)
        print(f"  [Paso 9] Correo {self.client_email} agregado a la cola de invitaciones familiares.")
        
        # Sincronización bloqueante usando parent_lock
        with parent_lock:
            # Si el correo de este hilo ya no está en la cola, significa que otro hilo ya procesó su invitación
            if self.client_email not in family_invite_queue:
                print(f"  [Paso 9] El correo {self.client_email} ya fue invitado por otro hilo. Saltando...")
                return
                
            # Esperar 5 segundos para asegurarnos de recopilar todos los correos del lote
            print("  [Paso 9] Esperando 5 segundos para recoger todos los correos del lote...")
            time.sleep(5.0)
            
            emails_a_invitar = list(family_invite_queue)
            if not emails_a_invitar:
                return
                
            print(f"  [Paso 9] Se procesarán las invitaciones para: {emails_a_invitar}")
            
            familiar_titular_txt = SCRIPT_DIR / "titular_familiar.txt"
            
            # Bucle para procesar las invitaciones de todos los correos en cola
            while emails_a_invitar:
                # 1. Cargar base de datos de titulares
                titulares = self.cargar_familiares_titulares(familiar_titular_txt)
                
                # Buscar el primer titular disponible
                titular_seleccionado = None
                for t in titulares:
                    if t["estado"] == "disponible" and t["miembros_actuales"] < 5:
                        titular_seleccionado = t
                        break
                        
                while not titular_seleccionado:
                    print("\n" + "!" * 80)
                    print("  [Paso 9] [ERROR] ¡No hay cuentas familiares titulares disponibles con espacios libres!")
                    print(f"  Por favor, añade cuentas en el archivo: {familiar_titular_txt}")
                    print("  Formato por línea: correo_titular, miembros_actuales, estado, miembros_detalles")
                    print("  Ejemplo: cakeseller1234@gmail.com, 0, disponible, []")
                    print("!" * 80 + "\n")
                    self.input_concurrente(">>> Una vez agregadas las cuentas, presiona Enter para recargar la lista <<<")
                    titulares = self.cargar_familiares_titulares(familiar_titular_txt)
                    for t in titulares:
                        if t["estado"] == "disponible" and t["miembros_actuales"] < 5:
                            titular_seleccionado = t
                            break
                
                email_titular = titular_seleccionado["correo"]
                print(f"\n  [Paso 9] Trabajando con la cuenta titular: {email_titular}")
                
                # Intentar iniciar sesión y navegar a /family con reintentos y rotación de proxy
                parent_context = None
                parent_page = None
                conexion_exitosa = False
                
                for intento_conexion in range(1, 4):
                    try:
                        # Si no es el primer intento, rotar el proxy PE
                        if intento_conexion > 1:
                            print(f"  [Paso 9] [Reintento {intento_conexion}/3] Rotando proxy PE para restablecer conexión...")
                            
                            # Obtener una rotación de proxy PE de forma manual (sin tocar el self.context principal)
                            global valid_pe_list
                            if valid_pe_list:
                                import random
                                p_pe = random.choice(valid_pe_list)
                                self.proxy_pe_server = p_pe["server"]
                                self.proxy_pe_user = p_pe["username"]
                                self.proxy_pe_pass = p_pe["password"]
                                print(f"  [Paso 9] Nuevo proxy PE seleccionado: {self.proxy_pe_server}")
                        
                        # 2. Abrir navegador con perfil del plan familiar titular
                        launch_args = [
                            "--disable-blink-features=AutomationControlled",
                            "--disable-gpu",
                            "--disable-software-rasterizer"
                        ]
                        proxy_dict = None
                        if self.use_proxy and self.proxy_pe_server:
                            proxy_dict = {"server": self.proxy_pe_server}
                            if self.proxy_pe_user:
                                proxy_dict["username"] = self.proxy_pe_user
                            if self.proxy_pe_pass:
                                proxy_dict["password"] = self.proxy_pe_pass

                        print(f"  [Paso 9] Iniciando navegador familiar titular (Intento {intento_conexion}/3)...")
                        launch_kwargs = {
                            "user_data_dir": str(self.parent_profile),
                            "headless": self.headless,
                            "args": launch_args,
                            "ignore_default_args": ["--enable-automation"],
                            "viewport": {"width": 1280, "height": 800},
                            "locale": "es-ES",
                            "proxy": proxy_dict
                        }
                        if not proxy_dict:
                            launch_kwargs["channel"] = "chrome"
                        if self.headless:
                            launch_kwargs["user_agent"] = self.user_agent
                        parent_context = p.chromium.launch_persistent_context(**launch_kwargs)
                        parent_context.set_default_navigation_timeout(45000)
                        parent_context.set_default_timeout(35000)
                        self.registrar_contador_datos(parent_context)
                        
                        parent_page = parent_context.pages[0] if parent_context.pages else parent_context.new_page()
                        parent_page.manager = self
                        
                        # 3. Comprobar si ya está logueado en la cuenta titular seleccionada
                        email_activo = self.obtener_email_logueado_tidal(parent_page)
                        
                        if email_activo and self.emails_coinciden_sin_puntos(email_activo, email_titular):
                            print(f"  [Paso 9] Confirmada sesión activa para el titular {email_titular}.")
                        else:
                            if email_activo:
                                print(f"  [Paso 9] Sesión incorrecta detectada ({email_activo}). Cerrando sesión...")
                                self.logout_tidal_total(parent_page)
                            else:
                                print("  [Paso 9] No hay sesión activa. Iniciando sesión...")
                                
                            login_ok = self.login_familiar_titular_con_codigo(parent_page, email_titular)
                            if not login_ok:
                                print(f"  [Paso 9] [ERROR] No se pudo iniciar sesión en {email_titular}.")
                                raise RuntimeError("Error al iniciar sesión en la cuenta titular.")
                                
                        # 4. Ir a la página de familia y sincronizar miembros reales
                        print("  [Paso 9] Navegando a la página de familia...")
                        parent_page.goto("https://account.tidal.com/family", wait_until="domcontentloaded", timeout=45000)
                        time.sleep(3.0)
                        aceptar_cookies_con_espera(parent_page)
                        manejar_bloqueos_e_intervencion(parent_page, "Carga de Familia")
                        
                        conexion_exitosa = True
                        break  # Éxito, salimos del bucle de reintentos
                        
                    except Exception as e:
                        print(f"  [Paso 9] [WARN] Intento {intento_conexion}/3 falló debido a problemas de conexión o proxy: {e}")
                        if parent_context:
                            try:
                                parent_context.close()
                            except Exception:
                                pass
                            parent_context = None
                            parent_page = None
                        time.sleep(3.0)
                
                if not conexion_exitosa:
                    print(f"  [Paso 9] [ERROR] No se pudo establecer conexión con el titular {email_titular} tras 3 intentos. Saltando titular...")
                    continue
                
                miembros_reales = self.contar_miembros_familia(parent_page)
                
                # Actualizar base de datos local con miembros reales
                titular_seleccionado["miembros_actuales"] = miembros_reales
                if miembros_reales >= 5:
                    titular_seleccionado["estado"] = "lleno"
                self.guardar_familiares_titulares(familiar_titular_txt, titulares)
                
                if miembros_reales >= 5:
                    print(f"  [Paso 9] La cuenta titular {email_titular} ya está llena ({miembros_reales}/5). Buscando la siguiente...")
                    parent_context.close()
                    continue
                    
                slots_disponibles = 5 - miembros_reales
                emails_este_titular = emails_a_invitar[:slots_disponibles]
                
                # 5. Invitar correos
                for invite_email in emails_este_titular:
                    print(f"  [Paso 9] Invitando a {invite_email}...")
                    
                    btn_invitar = esperar_locator_en_frames(
                        parent_page,
                        ["button:has-text('Invitar a un familiar')", "button:has-text('Invitar miembro')", 
                         "button:has-text('Invite member')", "button:has-text('Añadir')", 
                         "text=Invitar a un familiar", "text=Invitar miembro"],
                        timeout_s=12.0
                    )
                    if not btn_invitar:
                        for d in parent_page.locator("button, a, div, span").all():
                            try:
                                text = d.inner_text().lower()
                                if any(x in text for x in ["invitar", "invite", "añadir"]):
                                    btn_invitar = d
                                    break
                            except Exception:
                                continue
                                
                    if btn_invitar:
                        btn_invitar.click(force=True)
                        time.sleep(2.0)
                        
                        email_invite = parent_page.locator("input[placeholder*='Correo' i]") \
                            .or_(parent_page.locator("input[placeholder*='email' i]")) \
                            .or_(parent_page.locator("input[type='email']")) \
                            .or_(parent_page.locator("input[type='text']")).first
                            
                        if esperar_visibilidad(email_invite, 6000):
                            rellenar_campo_humanizado(email_invite, invite_email)
                            time.sleep(1.0)
                            
                            import re
                            btn_send = parent_page.locator("button").filter(has_text=re.compile(r"^(Invite|Invitar|Enviar)$", re.I)).first
                            if esperar_visibilidad(btn_send, 5000):
                                btn_send.click(force=True)
                            else:
                                hacer_click_por_textos(parent_page, ["Invitar", "Invite", "Enviar", "Send"])
                            time.sleep(3.5)
                            print(f"  [Paso 9] Invitación enviada a {invite_email}.")
                            
                            # Actualizar contabilidad en familiar_titular.txt
                            titular_seleccionado["miembros_actuales"] += 1
                            if invite_email not in titular_seleccionado["detalles"]:
                                titular_seleccionado["detalles"].append(invite_email)
                                
                            if titular_seleccionado["miembros_actuales"] >= 5:
                                titular_seleccionado["estado"] = "lleno"
                                
                            self.guardar_familiares_titulares(familiar_titular_txt, titulares)
                        else:
                            print(f"  [WARN] Campo de correo no encontrado para {invite_email}.")
                    else:
                        print(f"  [WARN] Botón de invitar no encontrado para {invite_email}.")
                        
                    # Eliminar de la cola global y local
                    if invite_email in family_invite_queue:
                        family_invite_queue.remove(invite_email)
                    emails_a_invitar.remove(invite_email)
                    
                    # Recargar si quedan más para este titular
                    if emails_este_titular.index(invite_email) < len(emails_este_titular) - 1:
                        parent_page.goto("https://account.tidal.com/family", wait_until="domcontentloaded")
                        time.sleep(2.0)
                        aceptar_cookies_con_espera(parent_page)
                
                parent_context.close()
                print(f"  [Paso 9] Finalizada ronda de invitaciones para {email_titular}.")

    # --- PASO 10: Buscar enlace y colocar la nueva contraseña ---
    def step10_complete_password_reset(self):
        if self.cuenta_abortada:
            return
        print("\n--- PASO 10: Buscando enlace de reinicio de contraseña y cambiando contraseña ---")
        
        enlace_reset = None
        max_reintentos_solicitud = 1
        
        for intento_solicitud in range(1, max_reintentos_solicitud + 1):
            print(f"  [Paso 10] Esperando enlace de reinicio en el correo (intento de espera {intento_solicitud}/{max_reintentos_solicitud})...")
            
            # Bucle de sondeo (polling) de 3 minutos (180 segundos) con esperas de 10 segundos
            tiempo_inicio = time.time()
            while time.time() - tiempo_inicio < 180:
                enlace_reset = obtener_codigo_de_gmail(
                    self.page, self.client_email,
                    query="from:tidal",
                    required_keywords=["resetting your tidal password", "restablecer tu contraseña de tidal", "reset your password", "link to reset your password"],
                    query_exclude="invited to a tidal family",
                    after_email_id=self.reset_baseline_id,
                    solo_link=True
                )
                if enlace_reset and enlace_reset.startswith("http"):
                    break
                
                time.sleep(10.0)
            
            if enlace_reset and enlace_reset.startswith("http"):
                print(f"  [Paso 10] [OK] Enlace de reinicio detectado con éxito: {enlace_reset}")
                break
            
            # Si pasaron los 3 minutos y no llegó, volver a solicitar el restablecimiento
            if intento_solicitud < max_reintentos_solicitud:
                print(f"\n  [Paso 10] [WARN] El correo de restablecimiento no llegó tras 3 minutos.")
                print(f"  --> Solicitando de nuevo el restablecimiento de contraseña para {self.client_email}...")
                
                try:
                    self.step8_request_password_reset()
                except Exception as e:
                    print(f"  [Paso 10] [WARN] Error al re-solicitar restablecimiento: {e}")
                
                # Darle 8 segundos antes de iniciar el siguiente bucle de espera
                time.sleep(8.0)
                
        if not enlace_reset or not enlace_reset.startswith("http"):
            print("\n  [Gmail Auto] [WARN] No se pudo extraer el enlace de reinicio automáticamente tras varios intentos.")
            enlace_reset = self.input_concurrente(">>> Copia y pega el enlace de reinicio de contraseña recibido en el correo del cliente: ").strip()
            
        if enlace_reset:
            reset_page = self.context.new_page()
            reset_page.goto(enlace_reset, wait_until="domcontentloaded")
            time.sleep(3.0)
            
            pwd_new1 = reset_page.locator('input[name="newPassword"], input[type="password"], input[name="password"]').first
            
            # Esperar hasta 20 segundos a que el formulario cargue e inyecte los elementos en el DOM
            if esperar_visibilidad(pwd_new1, 20000):
                rellenar_campo_humanizado(pwd_new1, self.target_pwd)
                
                # Opcional: Confirmar contraseña si la página tiene un segundo input
                try:
                    pwd_new2 = reset_page.locator('input[name="confirmNewPassword"], input[id*="confirm" i]').first
                    if pwd_new2.count() > 0 and pwd_new2.is_visible():
                        rellenar_campo_humanizado(pwd_new2, self.target_pwd)
                except Exception:
                    pass
                time.sleep(1.0)
                
                # Encontrar y pulsar el botón submit de forma robusta
                btn_submit = reset_page.locator("button[type='submit']").or_(reset_page.locator("button:has-text('Restablecer contraseña')")).or_(reset_page.locator("button:has-text('Guardar')")).or_(reset_page.locator("button:has-text('Reset password')")).first
                if esperar_visibilidad(btn_submit, 8000):
                    try:
                        btn_submit.evaluate("el => el.click()")
                    except Exception:
                        btn_submit.click(force=True)
                else:
                    # Fallback click directo
                    reset_page.keyboard.press("Enter")
                    
                time.sleep(6.0)
                print("  [Paso 10] Contraseña actualizada en la página de reinicio.")
            else:
                print("  [WARN] No se encontró el formulario de restablecimiento de contraseña tras 20s. Realízalo manualmente.")
                self.input_concurrente(">>> Una vez cambiada la contraseña manualmente en la ventana abierta, presiona Enter para continuar <<<")
            reset_page.close()
        else:
            print("  [WARN] No se pudo obtener el enlace de reinicio. Por favor realízalo manualmente.")
            self.input_concurrente(">>> Once cambiada la contraseña de Tidal del cliente, presiona Enter para continuar <<<")

        print("  [Paso 10] [OK] Contraseña cambiada con éxito.")

    # --- PASO 11: Ingresar al correo del cliente y aceptar invitación familiar ---
    def step11_accept_family_invite(self):
        if self.cuenta_abortada:
            return
        print("\n--- PASO 11: Aceptando invitación familiar en el correo del cliente ---")
        self.asegurar_navegador_abierto()


        print("  [Paso 11] Buscando invitación del plan familiar en el Gmail (hasta 1 minuto)...")
        link_invite = None
        for intento in range(1, 6):
            print(f"  [Paso 11] Intento {intento}/5: Buscando correo de invitacion...")
            link_invite = obtener_codigo_de_gmail(
                self.page, 
                email_destinatario=self.client_email, 
                query="from:tidal", 
                required_keywords=["invites you to join", "welcome to the family", "family plan", "family subscription", "plan familiar", "bienvenida a la familia", "unirte a su plan", "invited to a tidal family"], 
                after_email_id=self.invite_baseline_id, 
                solo_link=True
            )
            if link_invite:
                break
            if intento < 5:
                print("  [Paso 11] Correo no encontrado aun. Esperando 12 segundos...")
                time.sleep(12.0)
        
        if not link_invite or not link_invite.startswith("http"):
            print("\n  [Gmail Auto] [WARN] No se pudo extraer el enlace de invitación de forma automática.")
            link_invite = self.input_concurrente(">>> Copia y pega el enlace de invitación familiar recibido en el correo del cliente: ").strip()
            
        if link_invite:
            print(f"  [Paso 11] Abriendo enlace de invitación en la misma ventana: {link_invite}")
            self.page.goto(link_invite, wait_until="domcontentloaded")
            time.sleep(3.0)
            
            aceptar_cookies_con_espera(self.page)
            manejar_bloqueos_e_intervencion(self.page, "Aceptar Invitación Familiar")
            
            # --- MANEJAR RE-AUTENTICACIÓN REQUERIDA POR TIDAL ---
            # 1. Pantalla inicial de re-login: presionar 'Continuar'
            btn_cont = self.page.locator("button:has-text('Continuar')").or_(self.page.locator("button[type='submit']")).first
            if esperar_visibilidad(btn_cont, 5000):
                print("  [Paso 11] Pantalla de re-login detectada. Pulsando 'Continuar'...")
                btn_cont.click()
                time.sleep(3.0)
                
            # 2. Pantalla de contraseña: rellenar la contraseña target_pwd y pulsar 'Iniciar sesión'
            pwd_input = self.page.locator('input[type="password"]').first
            if not esperar_visibilidad(pwd_input, 4000):
                print("  [Paso 11] No se encontró campo de contraseña. Verificando si pide código...")
                btn_pwd_mode = esperar_locator_en_frames(
                    self.page,
                    ["a:has-text('contraseña')", "button:has-text('contraseña')",
                     "a:has-text('password')", "button:has-text('password')",
                     "text='Inicia sesión con contraseña'", "text='Sign in with password'"],
                    text_regex=re.compile(r"con contrase|with password|iniciar.*contrase|sign.*password", re.I),
                    timeout_s=5.0
                )
                if btn_pwd_mode:
                    print("  [Paso 11] Pantalla de código detectada. Pulsando 'Inicia sesión con contraseña'...")
                    btn_pwd_mode.click()
                    time.sleep(3.0)
                    pwd_input = self.page.locator('input[type="password"]').first

            if esperar_visibilidad(pwd_input, 5000):
                print("  [Paso 11] Introduciendo contraseña de la cuenta...")
                rellenar_campo_humanizado(pwd_input, self.target_pwd)
                time.sleep(0.5)
                btn_login = self.page.locator("button:has-text('Iniciar sesión')").or_(self.page.locator("button[type='submit']")).first
                if btn_login:
                    btn_login.click()
                time.sleep(4.0)
            
            # --- VERIFICAR SI LA INVITACIÓN YA FUE ACEPTADA AUTOMÁTICAMENTE ---
            # Después del re-login, Tidal redirige a .../accept-invite/.../success automáticamente.
            # Detectar esta URL o el mensaje de éxito antes de buscar ningún botón.
            time.sleep(2.0)
            url_actual = self.page.url.lower()
            ya_aceptada = (
                "/success" in url_actual
                or "family" in url_actual
                or esperar_visibilidad(self.page.locator("text=Ya está todo").or_(self.page.locator("text=You're all set")).or_(self.page.locator("text=all set")).or_(self.page.locator("text=preparado")).first, 4000)
            )
            
            if ya_aceptada:
                print(f"  [Paso 11] Invitacion aceptada automaticamente. URL: {self.page.url}")
            else:
                # Solo si no se detectó éxito automático, intentar clic en botón
                btn_aceptar = self.page.locator("button:has-text('Aceptar invitación')").or_(self.page.locator("button:has-text('Join family')")).or_(self.page.locator("button:has-text('Unirse')")).or_(self.page.locator("button:has-text('Aceptar')")).first
                if esperar_visibilidad(btn_aceptar, 5000):
                    btn_aceptar.click()
                    time.sleep(3.0)
                    print("  [Paso 11] Invitación aceptada mediante botón.")
                else:
                    # Si no hay botón, asumir que ya fue procesada (el re-login la aceptó automáticamente)
                    print("  [Paso 11] No se encontró botón adicional — la invitación fue procesada durante el inicio de sesión.")
        else:
            print("  [WARN] No se pudo obtener el enlace de invitation familiar.")
            self.input_concurrente(">>> Acepta la invitación manualmente en el navegador y luego pulsa Enter <<<")
            
        print("  [Paso 11] [OK] Invitación familiar aceptada.")


def obtener_user_agent_limpio(playwright, channel=None):
    """Lanza una instancia temporal del navegador en segundo plano para obtener su User-Agent real y limpiar la marca Headless."""
    try:
        launch_kwargs = {"headless": True}
        if channel:
            launch_kwargs["channel"] = channel
        browser = playwright.chromium.launch(**launch_kwargs)
        page = browser.new_page()
        ua = page.evaluate("navigator.userAgent")
        browser.close()
        if "HeadlessChrome" in ua:
            ua = ua.replace("HeadlessChrome/", "Chrome/")
        return ua
    except Exception as e:
        print(f"  [User-Agent] [WARN] No se pudo obtener el User-Agent dinámico: {e}. Usando fallback.")
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def configurar_perfiles():
    """Abre los navegadores para que el usuario pueda iniciar sesión manualmente en los servicios y guardar las cookies."""
    with sync_playwright() as p:
        print("\n" + "="*70)
        print("  CONFIGURACIÓN DEL PERFIL PRINCIPAL")
        print("  (Se abrirá el navegador para Gmail y TuneMyMusic)")
        print("="*70)
        
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-gpu",
            "--disable-software-rasterizer"
        ]
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR_MAIN),
            channel="chrome",
            headless=False,
            args=launch_args,
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 800},
            locale="es-ES"
        )
        
        # Pestaña 1: TuneMyMusic (Obligatorio)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.tunemymusic.com/es/")
        
        print("\n[INSTRUCCIONES - PERFIL PRINCIPAL]:")
        print("  GMAIL YA NO ES NECESARIO: La lectura de codigos se realiza de forma 100% automatica via IMAP.")
        print("  (Asegurate de haber configurado tu 'gmail_app_password=xxxx' en passwords.txt)")
        print("  ")
        print("  OBLIGATORIO:")
        print("    1. En la pestaña abierta, inicia sesión en tu cuenta Premium de TuneMyMusic.")
        print("    2. Una vez que hayas iniciado sesión en TuneMyMusic, regresa aquí y presiona Enter.")
        
        input("\n>>> Presiona Enter cuando hayas completado el inicio de sesión en TuneMyMusic <<<")
        context.close()
        
        print("\n" + "="*70)
        print("  CONFIGURACIÓN DEL PERFIL FAMILIAR (TITULAR)")
        print("  (Se abrirá el navegador para TIDAL Familiar)")
        print("="*70)
        
        time.sleep(2.0)  # Dar tiempo a Chrome para liberar el proceso anterior
        context_parent = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR_PARENT),
            channel="chrome",
            headless=False,
            args=launch_args,
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 800},
            locale="es-ES"
        )
        
        page_parent = context_parent.pages[0] if context_parent.pages else context_parent.new_page()
        page_parent.goto("https://account.tidal.com/family")
        
        print("\n[INSTRUCCIONES - PERFIL FAMILIAR]:")
        print("  1. En la pestaña abierta, inicia sesión en TIDAL con la cuenta del Titular del Plan Familiar.")
        print("  2. Asegúrate de que cargue la lista de miembros de la familia.")
        print("  3. Una vez logueado, regresa a esta consola y presiona Enter.")
        
        input("\n>>> Presiona Enter cuando hayas iniciado sesión en la cuenta familiar titular <<<")
        context_parent.close()
        
        print("\n[OK] Configuración de perfiles finalizada con éxito. Las sesiones han quedado guardadas.")


def cargar_proxies_desde_txt(filepath="proxies.txt"):
    proxies_cfg = {
        "proxy_pe_list": [],
        "proxy_ng_list": [],
        "proxy_pe_server": None,
        "proxy_pe_user": None,
        "proxy_pe_pass": None,
        "proxy_ng_server": None,
        "proxy_ng_user": None,
        "proxy_ng_pass": None,
    }
    
    def parsear_linea_proxy(line_clean):
        if not line_clean or line_clean.startswith("#"):
            return None
        raw_line = line_clean
        if raw_line.lower().startswith("http://"):
            raw_line = raw_line[7:]
        elif raw_line.lower().startswith("https://"):
            raw_line = raw_line[8:]
            
        server, username, password = None, None, None
        if ";" in raw_line:
            parts = raw_line.split(";")
            if len(parts) >= 4:
                host = parts[0].strip()
                port = parts[1].strip()
                username = parts[2].strip()
                password = parts[3].strip()
                server = f"http://{host}:{port}"
            elif len(parts) == 2:
                host = parts[0].strip()
                port = parts[1].strip()
                server = f"http://{host}:{port}"
        elif raw_line.count(":") >= 3 and "@" not in raw_line:
            parts = raw_line.split(":")
            host = parts[0].strip()
            port = parts[1].strip()
            username = parts[2].strip()
            password = parts[3].strip()
            server = f"http://{host}:{port}"
        elif "@" in raw_line:
            part_user_pass, part_host_port = raw_line.split("@", 1)
            server = f"http://{part_host_port.strip()}"
            if ":" in part_user_pass:
                username, password = part_user_pass.split(":", 1)
                username = username.strip()
                password = password.strip()
        elif ":" in raw_line:
            parts = raw_line.split(":")
            if len(parts) == 2:
                host = parts[0].strip()
                port = parts[1].strip()
                server = f"http://{host}:{port}"
                
        if server:
            if password and password.endswith("@"):
                password = password[:-1].strip()
            return {
                "server": server,
                "username": username,
                "password": password
            }
        return None

    # 1. Intentar cargar desde lista_proxies_pe.txt
    pe_file = os.path.join(os.path.dirname(__file__) if "__file__" in globals() else "", "lista_proxies_pe.txt")
    if os.path.exists(pe_file):
        try:
            with open(pe_file, "r", encoding="utf-8") as f:
                for line in f:
                    pe_p = parsear_linea_proxy(line.strip())
                    if pe_p:
                        proxies_cfg["proxy_pe_list"].append(pe_p)
            print(f"  [Proxy Load] Cargados {len(proxies_cfg['proxy_pe_list'])} proxies de Perú desde lista_proxies_pe.txt")
        except Exception as e:
            print(f"  [Proxy Load] [WARN] Error al cargar lista_proxies_pe.txt: {e}")

    # 2. Intentar cargar desde lista_proxies_ng.txt
    ng_file = os.path.join(os.path.dirname(__file__) if "__file__" in globals() else "", "lista_proxies_ng.txt")
    if os.path.exists(ng_file):
        try:
            with open(ng_file, "r", encoding="utf-8") as f:
                for line in f:
                    ng_p = parsear_linea_proxy(line.strip())
                    if ng_p:
                        proxies_cfg["proxy_ng_list"].append(ng_p)
            print(f"  [Proxy Load] Cargados {len(proxies_cfg['proxy_ng_list'])} proxies de Nigeria desde lista_proxies_ng.txt")
        except Exception as e:
            print(f"  [Proxy Load] [WARN] Error al cargar lista_proxies_ng.txt: {e}")

    # 3. Si siguen vacíos, leer de proxies.txt
    if not proxies_cfg["proxy_pe_list"] and not proxies_cfg["proxy_ng_list"]:
        if not os.path.exists(filepath):
            return None
        try:
            current_section = None
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line_clean = line.strip()
                    if not line_clean or line_clean.startswith("#"):
                        continue
                    if line_clean.upper() in ("[PROXIES_PE]", "[PROXIES_PERU]"):
                        current_section = "PE"
                        continue
                    elif line_clean.upper() in ("[PROXIES_NG]", "[PROXIES_NIGERIA]"):
                        current_section = "NG"
                        continue
                    
                    if current_section and "=" not in line_clean:
                        pe_p = parsear_linea_proxy(line_clean)
                        if pe_p:
                            if current_section == "PE":
                                proxies_cfg["proxy_pe_list"].append(pe_p)
                            else:
                                proxies_cfg["proxy_ng_list"].append(pe_p)
            print(f"  [Proxy Load] Cargados {len(proxies_cfg['proxy_pe_list'])} PE y {len(proxies_cfg['proxy_ng_list'])} NG proxies desde proxies.txt")
        except Exception as e:
            print(f"  [Proxy Load] [WARN] Error al cargar proxies.txt: {e}")
            
    if proxies_cfg["proxy_pe_list"] or proxies_cfg["proxy_ng_list"]:
        return proxies_cfg
    return None

def probar_y_seleccionar_mejor_proxy(proxy_list, region="Peru", cantidad_necesaria=5):
    import requests
    import time
    import random
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    if not proxy_list:
        return []
        
    # Mezclar proxies aleatoriamente para balancear carga e IPs utilizadas
    proxies_shuffled = list(proxy_list)
    random.shuffle(proxies_shuffled)
    
    # Probamos hasta 30 proxies o el triple de la cantidad necesaria (lo que sea mayor)
    limite = min(len(proxies_shuffled), max(30, cantidad_necesaria * 3))
    proxies_a_probar = proxies_shuffled[:limite]
    
    print(f"\n  [Proxy Test] Probando {len(proxies_a_probar)} proxy(s) aleatorios de un total de {len(proxy_list)} para {region} en paralelo...")
    
    def test_uno(proxy):
        server = proxy["server"]
        username = proxy["username"]
        password = proxy["password"]
        
        if password and password.endswith("@"):
            password = password[:-1].strip()
            
        if server and not server.startswith("http"):
            server = "http://" + server
            
        formatted_proxy = {
            "http": f"http://{username}:{password}@{server.replace('http://', '')}" if username else server,
            "https": f"http://{username}:{password}@{server.replace('http://', '')}" if username else server,
        }
        
        start_time = time.time()
        try:
            r = requests.get("https://httpbin.org/ip", proxies=formatted_proxy, timeout=5)
            latency = time.time() - start_time
            if r.status_code == 200:
                return {
                    "success": True,
                    "proxy": {
                        "server": server,
                        "username": username,
                        "password": password
                    },
                    "latency": latency,
                    "ip": r.json().get("origin")
                }
        except Exception as e:
            err_str = str(e)
            if "503" in err_str:
                msg = "503 No exit node"
            elif "407" in err_str:
                msg = "407 Auth Required"
            else:
                msg = "Fallo de conexión"
            return {"success": False, "server": server, "msg": msg}
        return {"success": False, "server": server, "msg": "Código no 200"}

    resultados = []
    workers = min(30, len(proxies_a_probar))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(test_uno, p): p for p in proxies_a_probar}
        
        for idx, future in enumerate(as_completed(futures), 1):
            res = future.result()
            if res["success"]:
                print(f"    - Probando proxy {idx}/{len(proxies_a_probar)} ({res['proxy']['server']})... [OK] Latencia: {res['latency']:.2f}s | IP: {res['ip']}")
                resultados.append(res)
            else:
                print(f"    - Probando proxy {idx}/{len(proxies_a_probar)} ({res['server']})... [ERROR] ({res['msg']})")
                
    if resultados:
        resultados.sort(key=lambda x: x["latency"])
        print(f"  [Proxy Test] [OK] Seleccionado el proxy más rápido para {region} (Latencia: {resultados[0]['latency']:.2f}s | Server: {resultados[0]['proxy']['server']}).")
        return [r["proxy"] for r in resultados]
        
    print(f"  [Proxy Test] [WARN] Ninguno de los proxies para {region} funcionó.")
    return []


def main():
    global valid_pe_list, valid_ng_list
    valid_pe_list = []
    valid_ng_list = []
    
    # Limpiar perfiles temporales huérfanos de ejecuciones anteriores que no estén bloqueados
    try:
        tmm_lock.release()
    except Exception:
        pass
    try:
        perfiles_dir = PROFILE_DIR_MAIN.parent
        if perfiles_dir.exists():
            for p_dir in perfiles_dir.iterdir():
                if p_dir.is_dir() and p_dir.name.startswith("temp_profile_"):
                    try:
                        shutil.rmtree(p_dir, ignore_errors=True)
                    except Exception:
                        pass
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Automatización de migración de cuentas TIDAL en 10 pasos.")
    parser.add_argument("--client-email", type=str, help="Correo electrónico de TIDAL del cliente.")
    parser.add_argument("--client-pwd", type=str, help="Contraseña actual de TIDAL del cliente.")
    parser.add_argument("--target-pwd", type=str, help="Contraseña definitiva para la cuenta del cliente.")
    parser.add_argument("--setup", action="store_true", help="Iniciar el asistente de inicio de sesión manual para configurar perfiles.")
    parser.add_argument("--batch-file", "-f", type=str, help="Ruta de un archivo .txt con la lista de cuentas a migrar en lote.")
    parser.add_argument("--start-step", type=int, default=1, choices=range(1, 12), help="Paso desde el que iniciar la migración (1-11).")
    parser.add_argument("--headless", action="store_true", help="Ejecutar el navegador en segundo plano (headless) para ahorrar RAM.")
    parser.add_argument("--clear-profiles", action="store_true", help="Borrar los perfiles persistentes de Chrome guardados para iniciar con una sesión limpia.")
    
    # Argumentos de proxy residencial general/fallback
    parser.add_argument("--use-proxy", action="store_true", help="Usar proxy residencial y activar optimización de ancho de banda.")
    parser.add_argument("--proxy-server", type=str, help="Servidor de proxy general (usado para ambos países si no se especifican los otros).")
    parser.add_argument("--proxy-user", type=str, help="Usuario del proxy general.")
    parser.add_argument("--proxy-pass", type=str, help="Contraseña del proxy general.")
    
    # Argumentos específicos por país
    parser.add_argument("--proxy-pe-server", type=str, help="Servidor de proxy para Perú (General).")
    parser.add_argument("--proxy-pe-user", type=str, help="Usuario del proxy de Perú.")
    parser.add_argument("--proxy-pe-pass", type=str, help="Contraseña del proxy de Perú.")
    parser.add_argument("--proxy-ng-server", type=str, help="Servidor de proxy para Nigeria (Creación).")
    parser.add_argument("--proxy-ng-user", type=str, help="Usuario del proxy de Nigeria.")
    parser.add_argument("--proxy-ng-pass", type=str, help="Contraseña del proxy de Nigeria.")
    
    args = parser.parse_args()

    if args.clear_profiles:
        print("\n[Limpieza] Borrando perfiles persistentes de Chrome por solicitud del usuario...")
        try:
            if PROFILE_DIR_MAIN.exists():
                shutil.rmtree(PROFILE_DIR_MAIN, ignore_errors=True)
            if PROFILE_DIR_PARENT.exists():
                shutil.rmtree(PROFILE_DIR_PARENT, ignore_errors=True)
            print("  [Limpieza] [OK] Perfiles borrados con éxito.")
        except Exception as e:
            print(f"  [Limpieza] [WARN] Error al borrar perfiles: {e}")

    if args.setup:
        configurar_perfiles()
        sys.exit(0)

    print("=" * 70)
    print("  ASISTENTE DE MIGRACIÓN DE CUENTAS TIDAL")
    print("=" * 70)
    print("  Selecciona la acción a realizar:")
    print("  1 - Iniciar migración automática (un cliente o en lote)")
    print("  2 - Configurar perfiles (Iniciar sesión en Gmail, TuneMyMusic y Tidal Titular)")
    print("=" * 70)
    
    opcion = ""
    while opcion not in ("1", "2"):
        opcion = input("  Selecciona una opción (1 o 2): ").strip()
        
    if opcion == "2":
        configurar_perfiles()
        sys.exit(0)

    start_step = args.start_step
    # Preguntar directamente por el paso de inicio para evitar confusiones
    step_input = input("\n¿Desde qué paso deseas iniciar la migración? (1-11, por defecto '1'): ").strip()
    if step_input:
        try:
            start_step = int(step_input)
            if start_step not in range(1, 12):
                start_step = 1
        except Exception:
            start_step = 1

    # Preguntar si desean ejecutar en segundo plano (headless)
    headless = args.headless
    if not headless:
        headless_opt = input("\n¿Deseas ejecutar los navegadores en segundo plano (headless) para ahorrar RAM? (s/n, por defecto 's'): ").strip().lower()
        if headless_opt in ('', 's', 'si', 'yes', 'y'):
            headless = True

    # Si se elige el paso 7, dar la opción de restablecer la contraseña primero
    reset_password_first = False
    if start_step == 7:
        reset_opt = input("¿Deseas restablecer primero las contraseñas de las nuevas cuentas Tidal antes de transferir playlists? (s/n, por defecto 'n'): ").strip().lower()
        if reset_opt in ('s', 'si', 'yes', 'y'):
            reset_password_first = True

    # Identificar si es migración en lote
    use_batch = False
    batch_filepath = args.batch_file

    if not batch_filepath:
        # Comprobar si existe 'cuentas.txt' por defecto
        default_batch = "cuentas.txt"
        has_default_batch = False
        default_count = 0
        if os.path.exists(default_batch):
            try:
                with open(default_batch, "r", encoding="utf-8") as f:
                    for line in f:
                        line_clean = line.strip()
                        if line_clean and not line_clean.startswith("#"):
                            has_default_batch = True
                            default_count += 1
            except Exception:
                pass
                
        if has_default_batch:
            batch_opt = input(f"\n¿Deseas procesar el lote de {default_count} cuentas en '{default_batch}'? (s/n, por defecto 's'): ").strip().lower()
            if batch_opt in ('', 's', 'si', 'yes', 'y'):
                use_batch = True
                batch_filepath = default_batch
            else:
                use_batch = False
        else:
            batch_opt = input("\n¿Deseas migrar un lote de cuentas en cola desde un archivo .txt? (s/n): ").strip().lower()
            if batch_opt in ('s', 'si', 'yes', 'y'):
                use_batch = True
                batch_filepath = input("Introduce la ruta del archivo .txt (por defecto 'cuentas.txt'): ").strip()
                if not batch_filepath:
                    batch_filepath = "cuentas.txt"
    else:
        use_batch = True

    cuentas = []
    if use_batch:
        if not os.path.exists(batch_filepath):
            print(f"\n[ERROR] Error: El archivo de lote '{batch_filepath}' no existe.")
            sys.exit(1)
            
        try:
            with open(batch_filepath, "r", encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    line_clean = line.strip()
                    if not line_clean or line_clean.startswith("#"):
                        continue
                    # Separadores admitidos: pipe |, punto y coma ;, o coma ,
                    parts = []
                    if "|" in line_clean:
                        parts = [p.strip() for p in line_clean.split("|")]
                    elif ";" in line_clean:
                        parts = [p.strip() for p in line_clean.split(";")]
                    elif "," in line_clean:
                        parts = [p.strip() for p in line_clean.split(",")]
                    
                    if len(parts) >= 3:
                        cuentas.append((parts[0], parts[1], parts[2]))
                    elif len(parts) == 2:
                        # Si solo hay email y contraseña, usar la contraseña actual como la nueva por defecto
                        cuentas.append((parts[0], parts[1], parts[1]))
                    else:
                        print(f"  [WARN] Línea {i} ignorada (formato inválido: '{line_clean}'). Debe tener al menos email y contraseña (separados por |, ; o ,)")
        except Exception as e:
            print(f"\n[ERROR] Error al leer el archivo de cuentas: {e}")
            sys.exit(1)
            
        if not cuentas:
            print(f"\n[ERROR] Error: No se encontraron cuentas válidas para procesar en '{batch_filepath}'.")
            sys.exit(1)
    else:
        # Migración individual normal
        client_email = args.client_email
        client_pwd = args.client_pwd
        target_pwd = args.target_pwd
        
        print("\n--- Datos del Cliente ---")
        if not client_email:
            client_email = input("Introduce el correo electrónico actual del cliente: ").strip()
        if not client_pwd:
            client_pwd = input("Introduce la contraseña actual del cliente: ").strip()
        if not target_pwd:
            target_pwd = input("Introduce la nueva contraseña definitiva para el cliente: ").strip()

        if not client_email or not client_pwd or not target_pwd:
            print("\n[ERROR] Error: Todos los campos de credenciales son obligatorios.")
            sys.exit(1)
            
        cuentas.append((client_email, client_pwd, target_pwd))

    # Configuración de proxies
    use_proxy = args.use_proxy
    proxy_pe_server = args.proxy_pe_server or args.proxy_server
    proxy_pe_user = args.proxy_pe_user or args.proxy_user
    proxy_pe_pass = args.proxy_pe_pass or args.proxy_pass
    proxy_ng_server = args.proxy_ng_server or args.proxy_server
    proxy_ng_user = args.proxy_ng_user or args.proxy_user
    proxy_ng_pass = args.proxy_ng_pass or args.proxy_pass

    # Intentar cargar desde el archivo TXT
    proxies_desde_txt = cargar_proxies_desde_txt()
    if proxies_desde_txt:
        # Preguntar si desean usarlos o correr el test sin proxy
        usar_proxies_opt = input("\nSe detectó 'proxies.txt'. ¿Deseas usar los proxies residenciales o correr un test local con tu IP normal/VPN? (p/t, por defecto 'p'): ").strip().lower()
        if usar_proxies_opt in ('t', 'test', 'local'):
            use_proxy = False
            proxy_pe_server = None
            proxy_pe_user = None
            proxy_pe_pass = None
            proxy_ng_server = None
            proxy_ng_user = None
            proxy_ng_pass = None
            print("  [INFO] Iniciando modo test (sin proxy, usando tu IP local/VPN).")
        else:
            use_proxy = True
            
            # Probar y seleccionar los proxies de Perú en paralelo
            cantidad_necesaria = len(cuentas) if cuentas else 5
            pe_list = proxies_desde_txt.get("proxy_pe_list", [])
            valid_pe_list = probar_y_seleccionar_mejor_proxy(pe_list, "PERÚ", cantidad_necesaria)
            if valid_pe_list:
                proxy_pe_server = valid_pe_list[0]["server"]
                proxy_pe_user = valid_pe_list[0]["username"]
                proxy_pe_pass = valid_pe_list[0]["password"]
            else:
                print("\n[ERROR] No se pudo encontrar ningún proxy de PERÚ funcional en 'proxies.txt'.")
                confirm = input("Como tu IP local/VPN está bloqueada por Tidal, continuar sin proxy fallará. ¿Deseas forzar continuar de todas formas con tu IP local? (s/n): ").strip().lower()
                if confirm not in ('s', 'si', 'yes', 'y'):
                    print("Abortando la migración por seguridad.")
                    sys.exit(1)
                proxy_pe_server = None
                proxy_pe_user = None
                proxy_pe_pass = None
                
            # Probar y seleccionar los proxies de Nigeria en paralelo
            ng_list = proxies_desde_txt.get("proxy_ng_list", [])
            valid_ng_list = probar_y_seleccionar_mejor_proxy(ng_list, "NIGERIA", cantidad_necesaria)
            if valid_ng_list:
                proxy_ng_server = valid_ng_list[0]["server"]
                proxy_ng_user = valid_ng_list[0]["username"]
                proxy_ng_pass = valid_ng_list[0]["password"]
            else:
                print("\n[ERROR] No se pudo encontrar ningún proxy de NIGERIA funcional en 'proxies.txt'.")
                confirm = input("Como tu IP local/VPN está bloqueada por Tidal, continuar sin proxy fallará. ¿Deseas forzar continuar de todas formas con tu IP local? (s/n): ").strip().lower()
                if confirm not in ('s', 'si', 'yes', 'y'):
                    print("Abortando la migración por seguridad.")
                    sys.exit(1)
                proxy_ng_server = None
                proxy_ng_user = None
                proxy_ng_pass = None
                
            print("  [INFO] Configuración de proxies residenciales finalizada.")
    else:
        if not use_proxy:
            p_opt = input("\n¿Deseas activar la optimización de datos (ahorro de GB bloqueando imágenes/fuentes)? (s/n): ").strip().lower()
            if p_opt in ('s', 'si', 'yes', 'y'):
                use_proxy = True

        if use_proxy:
            # Preguntar si además quieren configurar IPs Residenciales (Proxies)
            if not (proxy_pe_server or proxy_ng_server):
                use_p_ip = input("¿Deseas configurar IPs Residenciales (Proxies) para esta sesión? (s/n): ").strip().lower()
                if use_p_ip in ('s', 'si', 'yes', 'y'):
                    print("\n--- Configuración de Proxy Residencial para PERÚ (General) ---")
                    proxy_pe_server = input("Introduce el servidor del proxy de Perú (dejar en blanco para usar IP local): ").strip()
                    if proxy_pe_server:
                        proxy_pe_user = input("Introduce el usuario del proxy de Perú (dejar en blanco si no tiene): ").strip()
                        proxy_pe_pass = input("Introduce la contraseña del proxy de Perú (dejar en blanco si no tiene): ").strip()
                    
                    print("\n--- Configuración de Proxy Residencial para NIGERIA (Creación) ---")
                    proxy_ng_server = input("Introduce el servidor del proxy de Nigeria (dejar en blanco para usar IP local/VPN): ").strip()
                    if proxy_ng_server:
                        proxy_ng_user = input("Introduce el usuario del proxy de Nigeria (dejar en blanco si no tiene): ").strip()
                        proxy_ng_pass = input("Introduce la contraseña del proxy de Nigeria (dejar en blanco si no tiene): ").strip()
                    
                    # Guardar en archivo para la próxima vez
                    guardar_opt = input("\n¿Deseas guardar estas credenciales de proxy en 'proxies.txt' para futuras sesiones? (s/n, por defecto 's'): ").strip().lower()
                    if guardar_opt in ('', 's', 'si', 'yes', 'y'):
                        try:
                            with open("proxies.txt", "w", encoding="utf-8") as f:
                                f.write("# Configuración de proxies residenciales generada automáticamente\n\n")
                                f.write(f"PROXY_PE_SERVER={proxy_pe_server}\n")
                                f.write(f"PROXY_PE_USER={proxy_pe_user}\n")
                                f.write(f"PROXY_PE_PASS={proxy_pe_pass}\n\n")
                                f.write(f"PROXY_NG_SERVER={proxy_ng_server}\n")
                                f.write(f"PROXY_NG_USER={proxy_ng_user}\n")
                                f.write(f"PROXY_NG_PASS={proxy_ng_pass}\n")
                            print("  [INFO] Proxies guardados con éxito en 'proxies.txt'.")
                        except Exception as e:
                            print(f"  [WARN] No se pudo escribir 'proxies.txt': {e}")
                else:
                    print("  [INFO] Modo optimizado activo usando tu IP local/normal.")

    # Validar formatos mínimos de servidores
    if proxy_pe_server and not proxy_pe_server.startswith("http"):
        proxy_pe_server = "http://" + proxy_pe_server
    if proxy_ng_server and not proxy_ng_server.startswith("http"):
        proxy_ng_server = "http://" + proxy_ng_server

    # Filtrar cuentas ya completadas en el progreso
    cuentas_filtradas = []
    import json
    progreso_file = Path("progreso_migraciones.json")
    progreso_data = {}
    if progreso_file.exists():
        try:
            progreso_data = json.loads(progreso_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [Progreso] [WARN] No se pudo leer 'progreso_migraciones.json' al iniciar: {e}")
            
    for c_email, c_pwd, c_target_pwd in cuentas:
        email_key = c_email.lower().strip()
        if email_key in progreso_data and progreso_data[email_key] >= 11:
            print(f"  [Progreso] Cuenta {c_email} ya completó la migración (Paso 11). Se excluye de la cola.")
        else:
            cuentas_filtradas.append((c_email, c_pwd, c_target_pwd))
            
    cuentas = cuentas_filtradas
    if not cuentas:
        print("\n[INFO] Todas las cuentas en la lista ya han completado la migración (Paso 11).")
        sys.exit(0)

    print(f"\n[INFO] Se detectaron {len(cuentas)} cuenta(s) a procesar en cola.")
    print("Asegúrate de haber completado la opción 2 (Configurar perfiles) antes.")
    input(">>> Presiona Enter para iniciar el proceso de migración <<<")

    start_time_global = time.time()
    start_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time_global))
    print(f"\n[CRONÓMETRO] Proceso iniciado a las: {start_time_str}")

    exitos = 0
    fallas = []
    total_data_bytes = 0

    max_hilos = 5
    
    # Activar banderas globales si procesamos en paralelo/lotes
    global BATCH_MODE, BATCH_MODE_VPN
    if len(cuentas) > 1:
        BATCH_MODE = True
        
    usar_vpn_global = False
    if BATCH_MODE and not use_proxy:
        usar_vpn_global = True
        BATCH_MODE_VPN = True

    # Dividir las cuentas en lotes de tamaño 5
    lotes_cuentas = [cuentas[i:i + max_hilos] for i in range(0, len(cuentas), max_hilos)]

    for lote_idx, lote in enumerate(lotes_cuentas, 1):
        print("\n" + "=" * 80)
        print(f"  INICIANDO LOTE {lote_idx}/{len(lotes_cuentas)} ({len(lote)} cuentas en paralelo)")
        print("=" * 80 + "\n")
        
        # Inicializar las barreras de sincronización para que todos los hilos corran a la par
        global barrier_step1, barrier_step2, barrier_step3_4, barrier_step5, barrier_step6, barrier_step7, barrier_step8, barrier_step9, barrier_step10
        if len(lote) > 1:
            barrier_step1 = threading.Barrier(len(lote))
            barrier_step2 = threading.Barrier(len(lote))
            barrier_step3_4 = threading.Barrier(len(lote))
            
            if usar_vpn_global:
                def conectar_vpn_global():
                    print("\n[Lote] Todas las cuentas del lote completaron el Paso 5. Conectando VPN global a Nigeria...")
                    if not vpn_surfshark_conectar("nigeria"):
                        print("  [Surfshark] [WARN] No se pudo conectar la VPN global de forma automática. Conéctala a NIGERIA manualmente.")
                        input(">>> Presiona Enter cuando la VPN a Nigeria esté ACTIVA <<<")
                    else:
                        print("  [Surfshark] VPN activada.")
                    
                    # Limpiar caché DNS y esperar estabilización
                    import subprocess as _sp
                    print("  [Surfshark] Limpiando caché DNS y esperando 8 segundos...")
                    _sp.run(["ipconfig", "/flushdns"], capture_output=True)
                    time.sleep(8.0)

                def desconectar_vpn_global():
                    print("\n[Lote] Todas las cuentas del lote completaron el Paso 6. Desconectando VPN global...")
                    if not vpn_surfshark_desconectar():
                        print("  [Surfshark] [WARN] No se pudo desactivar la VPN de forma automática. Desconéctala manualmente.")
                        input(">>> Presiona Enter cuando la VPN esté DESACTIVADA <<<")
                    else:
                        print("  [Surfshark] VPN desactivada.")
                    time.sleep(3.0)

                barrier_step5 = threading.Barrier(len(lote), action=conectar_vpn_global)
                barrier_step6 = threading.Barrier(len(lote), action=desconectar_vpn_global)
            else:
                barrier_step5 = threading.Barrier(len(lote))
                barrier_step6 = threading.Barrier(len(lote))
                
            barrier_step7 = threading.Barrier(len(lote))
            barrier_step8 = threading.Barrier(len(lote))
            barrier_step9 = threading.Barrier(len(lote))
            barrier_step10 = threading.Barrier(len(lote))
        else:
            barrier_step1 = None
            barrier_step2 = None
            barrier_step3_4 = None
            barrier_step5 = None
            barrier_step6 = None
            barrier_step7 = None
            barrier_step8 = None
            barrier_step9 = None
            barrier_step10 = None

        # 2. Ejecutar las cuentas del lote en paralelo
        def ejecutar_cuenta(cuenta_args):
            idx_in_lote, cuenta_item = cuenta_args
            c_email, c_pwd, c_target_pwd = cuenta_item
            print(f"  [Lote {lote_idx}] Hilo iniciado para: {c_email}")
            
            # Asignar un proxy único para este hilo si use_proxy está activo
            thread_proxy_pe_server = proxy_pe_server
            thread_proxy_pe_user = proxy_pe_user
            thread_proxy_pe_pass = proxy_pe_pass
            
            thread_proxy_ng_server = proxy_ng_server
            thread_proxy_ng_user = proxy_ng_user
            thread_proxy_ng_pass = proxy_ng_pass
            
            if use_proxy:
                if valid_pe_list:
                    proxy_index = idx_in_lote % len(valid_pe_list)
                    p_pe = valid_pe_list[proxy_index]
                    thread_proxy_pe_server = p_pe["server"]
                    thread_proxy_pe_user = p_pe["username"]
                    thread_proxy_pe_pass = p_pe["password"]
                else:
                    thread_proxy_pe_server = None
                    thread_proxy_pe_user = None
                    thread_proxy_pe_pass = None
                    
                if valid_ng_list:
                    proxy_index = idx_in_lote % len(valid_ng_list)
                    p_ng = valid_ng_list[proxy_index]
                    thread_proxy_ng_server = p_ng["server"]
                    thread_proxy_ng_user = p_ng["username"]
                    thread_proxy_ng_pass = p_ng["password"]
                else:
                    thread_proxy_ng_server = None
                    thread_proxy_ng_user = None
                    thread_proxy_ng_pass = None
            
            # Obtener paso de inicio dinámico para esta cuenta específica
            cuenta_start_step = start_step
            if start_step == 1:
                progreso_file = Path("progreso_migraciones.json")
                if progreso_file.exists():
                    try:
                        import json
                        progreso_data = json.loads(progreso_file.read_text(encoding="utf-8"))
                        email_key = c_email.lower().strip()
                        if email_key in progreso_data:
                            paso_completado = progreso_data[email_key]
                            if paso_completado >= 11:
                                return c_email, True, 0, None
                            cuenta_start_step = paso_completado + 1
                            print(f"  [Lote] Cuenta {c_email} reanudará desde el Paso {cuenta_start_step} (último paso completado: {paso_completado} según progreso_migraciones.json)")
                    except Exception as e:
                        print(f"  [Lote] [WARN] Error al leer progreso_migraciones.json para {c_email}: {e}")

            manager = TidalMigrationManager(
                main_profile=PROFILE_DIR_MAIN,
                parent_profile=PROFILE_DIR_PARENT,
                client_email=c_email,
                client_pwd=c_pwd,
                target_pwd=c_target_pwd,
                use_proxy=use_proxy,
                proxy_pe_server=thread_proxy_pe_server,
                proxy_pe_user=thread_proxy_pe_user,
                proxy_pe_pass=thread_proxy_pe_pass,
                proxy_ng_server=thread_proxy_ng_server,
                proxy_ng_user=thread_proxy_ng_user,
                proxy_ng_pass=thread_proxy_ng_pass,
                batch_mode=BATCH_MODE,
                start_step=cuenta_start_step,
                reset_password_first=reset_password_first,
                headless=headless
            )
            
            try:
                manager.run_pipeline()
                if manager.cuenta_abortada:
                    err_msg = manager.abort_reason if manager.abort_reason else "Cuenta abortada"
                    print(f"\n[ERROR] Cuenta {c_email} falló: {err_msg}")
                    return c_email, False, manager.total_bytes_transferred, err_msg
                print(f"\n[OK] Cuenta {c_email} migrada con éxito.")
                return c_email, True, manager.total_bytes_transferred, None
            except Exception as e:
                import traceback
                error_trace = traceback.format_exc()
                print(f"\n[ERROR] Error al migrar cuenta {c_email}: {e}\n{error_trace}")
                return c_email, False, manager.total_bytes_transferred, str(e)

        resultados_lote = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(lote)) as executor:
            futuros = {}
            for idx, cuenta in enumerate(lote):
                if idx > 0:
                    print(f"  [Lote] Esperando 3 segundos antes de iniciar el siguiente hilo para evitar bloqueos concurrentes...")
                    time.sleep(3.0)
                futuros[executor.submit(ejecutar_cuenta, (idx, cuenta))] = cuenta
                
            for futuro in concurrent.futures.as_completed(futuros):
                try:
                    res_email, res_status, res_bytes, res_error = futuro.result()
                    resultados_lote.append((res_email, res_status, res_bytes, res_error))
                except Exception as ex:
                    c_err = futuros[futuro]
                    resultados_lote.append((c_err[0], False, 0, str(ex)))

        # Registrar resultados del lote
        for res_email, res_status, res_bytes, res_error in resultados_lote:
            total_data_bytes += res_bytes
            if res_status:
                exitos += 1
            else:
                fallas.append((res_email, res_error))
                
        # Breve respiro entre lotes para que el sistema libere recursos de Chrome
        if lote_idx < len(lotes_cuentas):
            print(f"\n[Lote] Esperando 10 segundos antes de pasar al siguiente lote...")
            time.sleep(10.0)

    # Registrar marcas de tiempo finales
    end_time_global = time.time()
    end_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_time_global))
    duration_secs = int(end_time_global - start_time_global)
    hours, remainder = divmod(duration_secs, 3600)
    minutes, seconds = divmod(remainder, 60)
    duration_str = f"{hours:02d}h {minutes:02d}m {seconds:02d}s"

    # Mostrar resumen final para lote de cuentas
    print("\n" + "=" * 80)
    print("  RESUMEN FINAL DE EJECUCIÓN (COLA)")
    print("=" * 80)
    print(f"  Hora de inicio:           {start_time_str}")
    print(f"  Hora de finalización:     {end_time_str}")
    print(f"  Tiempo total transcurrido: {duration_str}")
    print("-" * 80)
    print(f"  Total cuentas procesadas: {len(cuentas)}")
    print(f"  Migraciones exitosas:     {exitos}")
    print(f"  Migraciones fallidas:     {len(fallas)}")
    if fallas:
        print("\n  Detalle de cuentas fallidas:")
        for email_failed, err_msg in fallas:
            print(f"    - {email_failed}: {err_msg}")
            
    consumo_facturable = (total_data_bytes * 1.1) / (1024 * 1024)
    print(f"\n  Consumo de datos total acumulado de red: {total_data_bytes / (1024 * 1024):.2f} MB")
    print(f"  Estimación de facturación acumulada por Proxy (+10%): {consumo_facturable:.2f} MB")
    print("=" * 80 + "\n")

if __name__ == "__main__":
    main()
