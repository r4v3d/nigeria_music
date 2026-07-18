"""
Abre TIDAL en Chrome (ventanas privadas / incógnito) en fases rápidas:
1) abre todas las ventanas,
2) rellena correos (emails.txt: el número de ventanas = número de correos en ese archivo),
3) acepta cookies en cada ventana,
4) pulsa «Continuar» en cada ventana,
5) rellena contraseñas (passwords.txt, mismo orden que los correos),
6) pulsa «Inicia sesión» en cada ventana,
7) abre en cada una la página Familia de TIDAL (account.tidal.com/family).
8) opcional: elimina del plan Familiar el miembro indicado en el archivo de eliminar miembros
   (p. ej. eliminar_miembros.txt o «Eliminar miembros.txt» en Windows), misma línea que emails/LINKS;
   expande la fila si solo se ve nickname, pulsa «Eliminar del plan» y «Confirmar la eliminación».

Tras cada fase (1–8), si se detecta antibot/captcha de TIDAL o error 403 de CloudFront en alguna ventana:
  el script se detiene y espera intervención manual para resolver los problemas.
  Una vez resueltos, el usuario puede continuar con Enter.

Opcional: --pausa-manual detiene el script hasta Enter (p. ej. para resolver captcha).

Playwright usa un solo Chrome y un contexto aislado por ventana (una por línea de correo en emails.txt).
LINKS.txt es opcional por línea: si hay menos líneas que correos, se usa la URL de login por defecto
y la etiqueta «Perfil-N» en consola. Si hay más líneas en LINKS que correos, las sobrantes se ignoran.
  python abrir_links_chrome.py --solo-subprocess
"""
from __future__ import annotations

import argparse
import os
import platform
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
# URL por defecto: flujo de cuenta (suele pedir inicio de sesión si no hay sesión)
DEFAULT_TIDAL_LOGIN_URL = "https://account.tidal.com/login"
DEFAULT_TIDAL_FAMILY_URL = "https://account.tidal.com/family"

# Archivos por defecto (misma carpeta que este script)
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_LINKS_FILE = SCRIPT_DIR / "LINKS.txt"
DEFAULT_EMAILS_FILE = SCRIPT_DIR / "emails.txt"
DEFAULT_PASSWORDS_FILE = SCRIPT_DIR / "passwords.txt"
DEFAULT_ELIMINAR_MIEMBROS_FILE = SCRIPT_DIR / "eliminar_miembros.txt"


def _candidatos_archivos_eliminar_miembros() -> tuple[Path, ...]:
    """
    Rutas habituales en la carpeta del script (Windows/Notepad suelen usar espacios y mayúsculas).
    El orden define la prioridad si hay varios archivos.
    """
    d = SCRIPT_DIR
    return (
        d / "eliminar_miembros.txt",
        d / "Eliminar miembros.txt",
        d / "eliminar miembros.txt",
        d / "Eliminar_miembros.txt",
    )


def _resolver_archivo_eliminar_miembros(ruta_explicita: Path | None) -> Path | None:
    """Devuelve la ruta del archivo a leer, o None si no hay ninguno válido."""
    if ruta_explicita is not None:
        return ruta_explicita if ruta_explicita.is_file() else None
    for p in _candidatos_archivos_eliminar_miembros():
        if p.is_file():
            return p
    return None


@dataclass
class CaptchaTidManejoCfg:
    """Configuración para manejo manual de antibot TIDAL o 403 CloudFront (pausa manual para intervención)."""

    usar_surfshark: bool = False  # Desactivado - ahora es manual
    surfshark_exe: Path | None = None
    surfshark_espera_desconectar_rapida_s: float = 4.0
    espera_tras_surfshark_s: float = 7.0
    surfshark_timeout_botones_s: float = 15.0


def _normalizar_pausa_manual(modo: str) -> str:
    if modo == "forzosa":
        return "abrir-y-cookies"
    return modo


def pausa_manual_forzada(subtitulo: str, segundos_sin_tty: float = 90.0) -> None:
    """Detiene el script hasta Enter (o espera fija si no hay consola interactiva)."""
    bar = "=" * 62
    print(f"\n{bar}\n  PAUSA MANUAL — {subtitulo}\n{bar}")
    print(
        "  El script queda detenido. Resuelve captchas en las ventanas, revisa la página, etc.\n"
        "  Cuando esté todo listo, vuelve a ESTA ventana de consola y pulsa Enter para seguir.\n"
    )
    try:
        if sys.stdin.isatty():
            input("  >>> Pulsa Enter para continuar el script <<<  ")
        else:
            print(f"  (Sin TTY: esperando {segundos_sin_tty:.0f}s y continuando…)\n")
            time.sleep(segundos_sin_tty)
    except EOFError:
        time.sleep(segundos_sin_tty)


def _generar_trayectoria_humana(distance: float) -> list[tuple[int, int, float]]:
    if distance <= 0:
        return []

    track = []
    
    current_x = 0.0
    current_y = 0.0
    v_x = 0.0
    v_y = 0.0
    
    target_x = float(distance)
    target_y = 0.0
    
    kp = 3.0   # Spring stiffness
    kd = 0.8   # Friction damping
    dt = 0.04  # Time step size (seconds)
    
    # 60% chance of overshoot to simulate human imprecision
    overshoot_offset = random.randint(2, 7) if random.random() < 0.6 else 0
    temp_target_x = target_x + overshoot_offset
    
    has_corrected = False
    max_steps = 200
    step = 0
    
    while step < max_steps:
        step += 1
        
        if overshoot_offset > 0 and not has_corrected and current_x >= target_x:
            temp_target_x = target_x
            has_corrected = True
            v_x *= 0.4  # Decelerates sharply before correcting back
            
        dx = temp_target_x - current_x
        dy = target_y - current_y
        
        dist_factor = max(0.1, min(1.0, abs(dx) / distance))
        noise_x = random.uniform(-2.0, 2.0) * dist_factor
        noise_y = random.uniform(-0.8, 0.8) * dist_factor
        
        ax = kp * dx - kd * v_x + noise_x
        ay = kp * dy - kd * v_y + noise_y
        
        v_x += ax * dt
        v_y += ay * dt
        
        move_x = v_x * dt
        move_y = v_y * dt
        
        if random.random() < 0.15:
            move_y += random.choice([-1.0, 1.0])
            
        current_x += move_x
        current_y += move_y
        
        rx = round(move_x)
        ry = round(move_y)
        
        step_sleep = random.uniform(0.008, 0.022)
        
        if rx != 0 or ry != 0:
            track.append((rx, ry, step_sleep))
            
        if abs(current_x - target_x) < 0.7 and (overshoot_offset == 0 or has_corrected):
            break
            
    total_x_moved = sum(t[0] for t in track)
    diff = int(round(distance - total_x_moved))
    if diff != 0:
        track.append((diff, 0, random.uniform(0.01, 0.03)))
        
    return track


def resolver_slider_captcha_single(page) -> bool:
    """Intenta detectar y deslizar el slider de verificación humana en la página."""
    from playwright.sync_api import Error as PWErr
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    import time
    import random

    try:
        page.bring_to_front()
        time.sleep(0.35)
    except Exception:
        pass

    # 1. Esperar hasta 4.0 segundos a que cargue la interfaz del captcha en algún frame
    start_wait = time.monotonic()
    target_frame = None
    captcha_data = None
    
    js_finder = """
    () => {
        // Buscar el elemento handle del slider
        const handle = document.querySelector(".slider") || document.querySelector(".slider-button") || document.querySelector(".captcha_verify_slide_button") || document.querySelector("[class*='thumb' i]") || document.querySelector("[class*='handle' i]");
        if (!handle) return null;
        
        // Buscar el contenedor de la pista (track)
        const track = document.querySelector(".sliderContainer") || document.querySelector(".sliderbg") || document.querySelector(".sliderText") || handle.parentElement;
        if (!track) return null;
        
        const rHandle = handle.getBoundingClientRect();
        const rTrack = track.getBoundingClientRect();
        
        // Validar dimensiones lógicas para evitar falsos positivos
        if (rHandle.width >= 20 && rHandle.width <= 100 && rTrack.width >= 150 && rTrack.width <= 450) {
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
    
    while time.monotonic() - start_wait < 4.0:
        for frame in _frames_visibles(page):
            try:
                # Comprobar si podemos encontrar datos de captcha en este frame
                data = frame.evaluate(js_finder)
                if data:
                    target_frame = frame
                    captcha_data = data
                    break
            except Exception:
                continue
        if captcha_data:
            break
        time.sleep(0.25)

    # Diagnóstico detallado en consola si no se detectó nada
    if not captcha_data or not target_frame:
        print("    [Anti-bot Auto Debug] No se pudo encontrar el captcha en ningún frame. Ejecutando diagnóstico de frames:")
        for idx, f in enumerate(_frames_visibles(page)):
            try:
                print(f"      Frame [{idx}] URL: {f.url}")
                # Ejecutar un escaneo completo de elementos de la interfaz dentro de los contenedores
                deep_elements = f.evaluate("""
                () => {
                    const root = document.getElementById("ddv1-captcha-container") || document.getElementById("captcha-container") || document.body;
                    if (!root) return [];
                    const els = Array.from(root.querySelectorAll("*"));
                    return els.map(el => {
                        const rect = el.getBoundingClientRect();
                        return {
                            tag: el.tagName,
                            id: el.id,
                            class: el.className,
                            text: el.innerText ? el.innerText.trim().substring(0, 30) : "",
                            w: rect.width,
                            h: rect.height,
                            x: rect.left,
                            y: rect.top
                        };
                    }).filter(e => e.w > 0 && e.h > 0).slice(0, 40);
                }
                """)
                print(f"      Frame [{idx}] Elementos Visibles: {deep_elements}")
            except Exception as e_diag:
                print(f"      Frame [{idx}] Error de diagnóstico: {e_diag}")
        return False

    try:
        # 2. Hacer scroll de los contenedores para asegurar visibilidad en el viewport
        try:
            iframe_handle = target_frame.frame_element()
            if iframe_handle:
                iframe_handle.scroll_into_view_if_needed()
                
            # Scroll del handle dentro del iframe
            target_frame.evaluate("""
            () => {
                const handle = document.querySelector(".slider") || document.querySelector(".slider-button") || document.querySelector(".captcha_verify_slide_button") || document.querySelector("[class*='thumb' i]") || document.querySelector("[class*='handle' i]");
                if (handle) {
                    handle.scrollIntoView({block: "center", inline: "center"});
                }
            }
            """)
            time.sleep(0.2)  # Pausa para que el scroll se complete
            
            # Re-evaluar los datos para obtener las coordenadas físicas correctas post-scroll
            captcha_data = target_frame.evaluate(js_finder)
        except Exception as e_scroll:
            print(f"    [Anti-bot Auto Debug] Aviso scroll-into-view: {e_scroll}")

        if not captcha_data:
            print("    [Anti-bot Auto Debug] Error: No se pudieron re-evaluar las coordenadas del captcha post-scroll.")
            return False

        iframe_handle = target_frame.frame_element()
        iframe_box = iframe_handle.bounding_box()
        if not iframe_box:
            print("    [Anti-bot Auto Debug] Error: No se pudo obtener el bounding box del iframe.")
            return False
            
        # Calcular coordenadas absolutas de inicio (centro del handle) en el viewport de la página principal
        start_x = iframe_box["x"] + captcha_data["handleX"] + captcha_data["handleW"] / 2
        start_y = iframe_box["y"] + captcha_data["handleY"] + captcha_data["handleH"] / 2
        
        # Calcular la distancia del arrastre
        width_track = captcha_data["trackW"]
        distance = width_track - captcha_data["handleW"]
        if distance <= 0:
            distance = 240
            
        print(f"    [Anti-bot Auto Debug] Captcha encontrado en frame: {target_frame.url}")
        print(f"    [Anti-bot Auto Debug] Ejecutando arrastre: distancia = {distance}px (pista: {width_track}px, handle: {captcha_data['handleW']}px)")
        print(f"    [Anti-bot Auto Debug] Coordenadas de inicio mouse: ({start_x:.1f}, {start_y:.1f})")

        # 3. Mover el mouse a las coordenadas y arrastrar
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        time.sleep(random.uniform(0.18, 0.35))
        
        # Dividir el recorrido en 3 segmentos para simular aceleración, velocidad y desaceleración
        pasos_totales = []
        
        # Segmento 1: Aceleración (0% a 30%)
        seg1_pasos = random.randint(6, 10)
        for i in range(1, seg1_pasos + 1):
            ratio = (i / seg1_pasos) * 0.30
            pasos_totales.append(ratio)
            
        # Segmento 2: Velocidad constante/alta (30% a 85%)
        seg2_pasos = random.randint(12, 18)
        for i in range(1, seg2_pasos + 1):
            ratio = 0.30 + (i / seg2_pasos) * 0.55
            pasos_totales.append(ratio)
            
        # Segmento 3: Desaceleración y acople (85% a 100%)
        seg3_pasos = random.randint(8, 12)
        for i in range(1, seg3_pasos + 1):
            ratio = 0.85 + (i / seg3_pasos) * 0.15
            pasos_totales.append(ratio)

        # Ejecutar el arrastre a lo largo de los pasos calculados
        distancia_total = distance + random.uniform(8.0, 16.0)

        for ratio in pasos_totales:
            target_step_x = start_x + (distancia_total * ratio)
            # Pequeña variación de ruido vertical (Y)
            target_step_y = start_y + random.uniform(-1.2, 1.2)
            
            # Movimiento del mouse
            page.mouse.move(target_step_x, target_step_y)
            
            # Retraso entre pasos: simula respuesta biológica
            time.sleep(random.uniform(0.012, 0.022))

        # Espera al final del arrastre manteniendo pulsado (asentamiento)
        time.sleep(random.uniform(0.2, 0.35))

        # Wobble de asentamiento: un leve retroceso y vuelta al extremo derecho para consolidar
        wobble_x = start_x + distancia_total - random.uniform(2.0, 4.0)
        page.mouse.move(wobble_x, start_y + random.uniform(-0.5, 0.5))
        time.sleep(random.uniform(0.04, 0.08))
        page.mouse.move(start_x + distancia_total, start_y)
        time.sleep(random.uniform(0.2, 0.35))

        # Soltar el mouse
        page.mouse.up()
        return True
    except Exception as e:
        print(f"    [Anti-bot Auto Debug] Error durante la simulación de arrastre: {e}")
        try:
            page.mouse.up()
        except Exception:
            pass
        return False


def detectar_captcha_caducado(page) -> bool:
    """Detecta si el captcha de la página ha caducado y necesita recargarse."""
    from playwright.sync_api import Error as PWErr
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        if page.is_closed():
            return False
    except PWErr:
        return False

    # Patrones de texto que indican que el captcha venció, falló o está sin conexión
    patrones = (
        re.compile(r"caduc[aó]", re.I),
        re.compile(r"expired", re.I),
        re.compile(r"vuelve\s+a\s+cargar", re.I),
        re.compile(r"reload\s+the\s+page", re.I),
        re.compile(r"recargar", re.I),
        re.compile(r"refrescar", re.I),
        re.compile(r"reintentar", re.I),
        re.compile(r"retry", re.I),
        re.compile(r"sin\s+acceso\s+a\s+internet", re.I),
        re.compile(r"offline", re.I),
    )

    for frame in _frames_visibles(page):
        # 1. Comprobar por texto
        for pat in patrones:
            try:
                loc = frame.get_by_text(pat)
                if loc.count() > 0 and loc.first.is_visible(timeout=400):
                    return True
            except PlaywrightTimeout:
                continue
            except Exception:
                continue
                
        # 2. Comprobar selectores específicos de botones de reintento/offline de DataDome
        selectores_error = [
            "#captcha_reload_button",
            ".retryLink",
            "#captcha_offline",
            "[class*='retry' i]",
            "[id*='offline' i]"
        ]
        for sel in selectores_error:
            try:
                loc = frame.locator(sel)
                if loc.count() > 0 and loc.first.is_visible(timeout=400):
                    return True
            except PlaywrightTimeout:
                continue
            except Exception:
                continue
                
    return False


def intentar_autoresolver_sliders(trabajos: list[dict]) -> None:
    """Escanea todas las ventanas y busca si tienen slider captcha activo para resolverlo."""
    from playwright.sync_api import Error as PWErr

    # Primero hacemos una verificación rápida de si alguna ventana tiene captcha
    ventanas_con_captcha = []
    for t in trabajos:
        page = t["page"]
        try:
            if not page.is_closed() and (detectar_pantalla_antirobot_tid(page) or detectar_captcha_caducado(page)):
                ventanas_con_captcha.append(t)
        except PWErr:
            continue
            
    if not ventanas_con_captcha:
        return
        
    print(f"\n  [Anti-bot Auto] Detectado slider en {len(ventanas_con_captcha)} ventana(s). Intentando resolver...")
    
    for t in ventanas_con_captcha:
        page, n, perfil = t["page"], t["n"], t["perfil"]
        try:
            if page.is_closed():
                continue
                
            # Verificar si ha caducado antes de intentar resolver
            if detectar_captcha_caducado(page):
                print(f"    [{n}] {perfil}: Captcha caducado antes de iniciar. Recargando ventana...")
                page.bring_to_front()
                try:
                    page.reload(wait_until="commit", timeout=40_000)
                except Exception:
                    pass
                time.sleep(1.5)  # Esperar a que cargue el nuevo captcha
            
            # Intentar resolver
            if resolver_slider_captcha_single(page):
                time.sleep(1.5)  # Espera para verificar la respuesta del servidor
                
                # Si sigue apareciendo captcha, verificar si es que caducó justo al resolver
                # o si falló el intento.
                if detectar_pantalla_antirobot_tid(page):
                    if detectar_captcha_caducado(page):
                        print(f"    [{n}] {perfil}: Captcha caducó tras resolver. Reintentando con recarga...")
                        page.reload(wait_until="commit", timeout=40_000)
                        time.sleep(1.5)
                        if resolver_slider_captcha_single(page):
                            time.sleep(1.5)
                            
                if not detectar_pantalla_antirobot_tid(page):
                    print(f"    [{n}] {perfil}: ¡Slider captcha resuelto automáticamente!")
                else:
                    print(f"    [{n}] {perfil}: Se intentó resolver, pero sigue bloqueado.")
            else:
                # Si falló porque estaba caducado o no se encontró el slider
                if detectar_captcha_caducado(page):
                    print(f"    [{n}] {perfil}: Falló por captcha caducado. Recargando y reintentando...")
                    page.reload(wait_until="commit", timeout=40_000)
                    time.sleep(1.5)
                    if resolver_slider_captcha_single(page):
                        time.sleep(1.5)
                        if not detectar_pantalla_antirobot_tid(page):
                            print(f"    [{n}] {perfil}: ¡Slider captcha resuelto automáticamente tras recargar!")
                            continue
                print(f"    [{n}] {perfil}: No se pudo encontrar o resolver el slider automáticamente.")
        except Exception as e:
            print(f"    [{n}] {perfil}: Error en auto-resolución — {e}")
    print("  [Anti-bot Auto] Proceso de auto-resolución completado.\n")


def detectar_pantalla_antirobot_tid(page) -> bool:
    """
    Detecta la pantalla de verificación humana / antibot de TIDAL (texto del muro o del deslizador).
    """
    from playwright.sync_api import Error as PWErr
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        if page.is_closed():
            return False
    except PWErr:
        return False

    patrones = (
        re.compile(r"nos\s+aseguramos\s+de\s+que", re.I),
        re.compile(r"no\s+a\s+un\s+robot", re.I),
        re.compile(r"desliza\s+hacia\s+la\s+derecha", re.I),
        re.compile(r"velocidad\s+sobrehumana", re.I),
        re.compile(r"making\s+sure.*robot", re.I),
        re.compile(r"not\s+a\s+robot", re.I),
        re.compile(r"slide\s+to\s+(the\s+)?right", re.I),
        re.compile(r"superhuman\s+speed", re.I),
        re.compile(r"caduc[aó]", re.I),
        re.compile(r"expired", re.I),
        re.compile(r"vuelve\s+a\s+cargar", re.I),
        re.compile(r"reload\s+the\s+page", re.I),
        re.compile(r"recargar", re.I),
        re.compile(r"refrescar", re.I),
        re.compile(r"reintentar", re.I),
        re.compile(r"retry", re.I),
        re.compile(r"sin\s+acceso\s+a\s+internet", re.I),
        re.compile(r"offline", re.I),
        re.compile(r"acceso\s+está\s+restringido", re.I),
        re.compile(r"restringido\s+temporalmente", re.I),
        re.compile(r"restringido", re.I),
        re.compile(r"restringida", re.I),
        re.compile(r"restricted", re.I),
        re.compile(r"blocked", re.I),
    )

    for frame in _frames_visibles(page):
        for pat in patrones:
            try:
                loc = frame.get_by_text(pat)
                if loc.count() == 0:
                    continue
                if loc.first.is_visible(timeout=700):
                    return True
            except PlaywrightTimeout:
                continue
            except PWErr:
                return False
            except Exception:
                continue
    return False


def detectar_error_cloudfront_403(page) -> bool:
    """
    Detecta la página 403 de CloudFront («Request blocked») que a veces muestra login.tidal.com.
    """
    from playwright.sync_api import Error as PWErr
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        if page.is_closed():
            return False
    except PWErr:
        return False

    try:
        titulo = page.title()
        if re.search(r"403|request could not be satisfied", titulo, re.I):
            return True
    except Exception:
        pass

    patrones = (
        re.compile(r"403\s*ERROR", re.I),
        re.compile(r"the request could not be satisfied", re.I),
        re.compile(r"request blocked", re.I),
        re.compile(r"generated by cloudfront", re.I),
        re.compile(r"we can'?t connect to the server", re.I),
        re.compile(r"demasiado tr[aá]fico", re.I),
    )

    for frame in _frames_visibles(page):
        for pat in patrones:
            try:
                loc = frame.get_by_text(pat)
                if loc.count() == 0:
                    continue
                if loc.first.is_visible(timeout=700):
                    return True
            except PlaywrightTimeout:
                continue
            except PWErr:
                return False
            except Exception:
                continue
        try:
            txt = frame.locator("body").inner_text(timeout=1500)
            if re.search(
                r"403\s*ERROR|generated by cloudfront|request blocked",
                txt,
                re.I,
            ):
                return True
        except Exception:
            continue
    return False


def _motivo_bloqueo_tid(page) -> str | None:
    if detectar_pantalla_antirobot_tid(page):
        return "antibot"
    if detectar_error_cloudfront_403(page):
        return "403 CloudFront"
    if _login_tid_muestra_error(page):
        return "error login TIDAL"
    return None


def _ventanas_tid_requieren_surfshark(trabajos: list[dict]) -> list[tuple[int, str, str]]:
    from playwright.sync_api import Error as PWErr

    out: list[tuple[int, str, str]] = []
    for t in trabajos:
        page = t["page"]
        try:
            motivo = _motivo_bloqueo_tid(page)
            if motivo:
                out.append((t["n"], t["perfil"], motivo))
        except PWErr:
            continue
        except Exception:
            continue
    return out


def recargar_todas_pestanas_tid(trabajos: list[dict]) -> None:
    """Recarga cada pestaña (o goto a la misma URL) sin preguntar en consola."""
    from playwright.sync_api import Error as PWErr

    print("\n  Recargando todas las ventanas TIDAL…")
    for t in trabajos:
        page, n, perfil = t["page"], t["n"], t["perfil"]
        try:
            if page.is_closed():
                print(f"    [{n}] {perfil}: pestaña cerrada, omitida.")
                continue
            url = page.url
            try:
                page.reload(wait_until="commit", timeout=45_000)
            except Exception:
                page.goto(url, wait_until="commit", timeout=45_000)
            print(f"    [{n}] {perfil}: recargada.", flush=True)
        except PWErr:
            print(f"    [{n}] {perfil}: destino cerrado.", flush=True)
        except Exception as e:
            print(f"    [{n}] {perfil}: error — {e}", flush=True)
        time.sleep(0.06)
    print("  Recarga global completada.\n")
    # Cookies solo en este punto: recarga tras el ciclo antibot/captcha (ver manejar_captcha_tid_si_aplica).
    cookies_tras_recarga_en_pestanas(trabajos)


def cookies_tras_recarga_en_pestanas(
    trabajos: list[dict],
    *,
    espera_banner_s: float = 0.4,
) -> None:
    """
    Tras la recarga del ciclo antibot/captcha: una revisión breve por ventana; si no hay CMP, sigue.
    """
    from playwright.sync_api import Error as PWErr

    if not trabajos:
        return
    if espera_banner_s > 0:
        time.sleep(espera_banner_s)
    n_ok = 0
    for t in trabajos:
        page = t["page"]
        try:
            if page.is_closed():
                continue
        except PWErr:
            continue
        if aceptar_cookies_con_espera(
            page,
            intentos=2,
            pausa_s=0.12,
            esperar_networkidle=False,
        ):
            n_ok += 1
    if n_ok:
        print(f"  Cookies post-recarga: banner cerrado en {n_ok} ventana(s).", flush=True)


def _campo_email_visible(page) -> bool:
    """Verifica si el campo de email está visible en algún frame."""
    from playwright.sync_api import Error as PWErr
    for frame in _frames_visibles(page):
        try:
            selectores = [
                'input[type="email"]',
                'input[name="email"]',
                'input[autocomplete="email"]',
                'input[id*="email" i]',
            ]
            for sel in selectores:
                loc = frame.locator(sel).first
                if loc.count() and loc.is_visible(timeout=150):
                    return True
        except (PWErr, Exception):
            continue
    return False


def _es_pantalla_consentimiento(page, expected_email: str = "") -> bool:
    """Detecta si la página actual presenta la pantalla de consentimiento de 'Sí, continuar'."""
    from playwright.sync_api import Error as PWErr
    import re
    try:
        if page.is_closed():
            return False
            
        # Verificar si hay un botón 'Sí, continuar' visible
        rx = re.compile(r"sí,\s*continuar|si,\s*continuar|yes,\s*continue|continuar|continue", re.I)
        consent_visible = False
        target_frame = None
        for frame in _frames_visibles(page):
            loc = frame.locator('button, [role="button"], a, input[type="submit"]').filter(has_text=rx)
            count = loc.count()
            for idx in range(count):
                if loc.nth(idx).is_visible():
                    consent_visible = True
                    target_frame = frame
                    break
            if consent_visible:
                break
                
        if not consent_visible or not target_frame:
            return False
            
        # Si se especificó un correo esperado, verificar que aparezca en el texto del mismo frame
        if expected_email:
            body_text = target_frame.locator("body").inner_text().lower()
            if expected_email.lower() not in body_text:
                print(f"    [Consent] Alerta: El correo en pantalla no coincide con el esperado '{expected_email}'")
                return False
                
        return True
    except (PWErr, Exception):
        pass
    return False


def _es_pagina_login(page) -> bool:
    """Detecta si la página es de inicio de sesión (OAuth o local de TIDAL)."""
    from playwright.sync_api import Error as PWErr
    try:
        if page.is_closed():
            return False
        url = page.url.lower()
        if "login.tidal.com" in url or "account.tidal.com/login" in url:
            return True
        if "login" in url and "tidal.com" in url:
            return True
        return False
    except PWErr:
        return False
    except Exception:
        return False


def navegar_con_bypass_referencia(page, url: str) -> None:
    """Navega a la URL especificada utilizando el bypass de referencia de dominio si es la URL de login por defecto."""
    if url == DEFAULT_TIDAL_LOGIN_URL or "tidal.com/pricing" in url or "account.tidal.com" in url:
        try:
            print("  [Bypass] Cargando tidal.com/pricing primero para acumular reputación...")
            page.goto("https://tidal.com/pricing", wait_until="domcontentloaded", timeout=30_000)
            time.sleep(random.uniform(2.5, 4.0))  # Esperar que termine de cargar la página unos segundos
            aceptar_cookies_con_espera(page, intentos=2, pausa_s=0.15, esperar_networkidle=False)
            time.sleep(random.uniform(0.6, 1.2))
            
            print("  [Bypass] Buscando botón 'Prueba gratis' para simular clic...")
            btn_prueba = None
            for selector in [
                "a:has-text('Prueba gratis')",
                "button:has-text('Prueba gratis')",
                "a:has-text('Free trial')",
                "a:has-text('Try for free')",
                "text=Prueba gratis",
                "text=Free trial",
                "text=Try for free"
            ]:
                try:
                    loc = page.locator(selector).first
                    if loc.count() > 0 and loc.is_visible(timeout=500):
                        btn_prueba = loc
                        break
                except Exception:
                    continue
            
            if btn_prueba:
                print("  [Bypass] Pulsando 'Prueba gratis'...")
                btn_prueba.click(timeout=3000)
                return
            
            # Fallback si falla la interacción de clics
            print("  [Bypass] Advertencia: No se pudo realizar el clic en 'Prueba gratis'. Redirigiendo vía goto...")
            page.goto(DEFAULT_TIDAL_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000, referer="https://tidal.com/pricing")
        except Exception as e:
            print(f"  [Bypass] Error al redirigir con referer: {e}")
            try:
                page.goto(DEFAULT_TIDAL_LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)
            except Exception:
                pass
    else:
        try:
            page.goto(url, wait_until="commit", timeout=45_000)
        except Exception:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)


def poner_ventana_al_dia(t, fase_objetivo: str, max_iteraciones: int = 10) -> bool:
    """
    Lleva la ventana `t` desde su estado actual hasta la `fase_objetivo` mediante una máquina de estados.
    Fases: 'abrir' -> 'correo' -> 'consentimiento' -> 'password' -> 'iniciar_sesion' -> 'familia'
    """
    page, n, perfil = t["page"], t["n"], t["perfil"]
    from playwright.sync_api import Error as PWErr
    import time
    
    orden_fases = {
        "abrir": 1,
        "correo": 2,
        "consentimiento": 3,
        "password": 4,
        "iniciar_sesion": 5,
        "familia": 6
    }
    
    target_val = orden_fases.get(fase_objetivo, 1)
    
    for iteracion in range(max_iteraciones):
        try:
            if page.is_closed():
                return False
        except PWErr:
            return False
            
        # 1. Si hay captcha, error 403 o bloqueo, no podemos avanzar esta ventana aún
        if _motivo_bloqueo_tid(page):
            return False
            
        # 2. Identificar el estado actual
        if "account.tidal.com/family" in page.url:
            current_fase = "familia"
        elif "pricing" in page.url or page.url.strip("/") in ("https://tidal.com", "http://tidal.com", "https://www.tidal.com"):
            # Si está en landing o en pricing, se debe iniciar/abrir desde cero (bypass de referencia)
            current_fase = "abrir"
        elif _es_pantalla_consentimiento(page, expected_email=t.get("email", "")):
            current_fase = "consentimiento"
        elif not _es_pagina_login(page) and "captcha-delivery.com" not in page.url and ("account.tidal.com" in page.url or "listen.tidal.com" in page.url):
            current_fase = "iniciar_sesion"
        elif _campo_email_visible(page):
            current_fase = "correo"
        elif esperar_campo_password(page, timeout_s=0.4):
            current_fase = "password"
        else:
            current_fase = "abrir"
            
        current_val = orden_fases.get(current_fase, 1)
        
        # Si ya estamos en la fase objetivo o una posterior, todo listo
        if current_val >= target_val:
            return True
            
        print(f"    [{n}] {perfil}: Estado actual '{current_fase}' -> Fase objetivo '{fase_objetivo}' (Paso {iteracion + 1})")
        
        # 3. Avanzar al siguiente estado
        if current_fase == "abrir":
            url = t.get("url", DEFAULT_TIDAL_LOGIN_URL)
            if "login" not in page.url:
                try:
                    navegar_con_bypass_referencia(page, url)
                    _esperar_carga_post_goto_tid(page, timeout_dom_ms=8000, margen_s=0.4)
                except Exception:
                    pass
            else:
                # Ya estamos en la página de login, sólo esperamos a que aparezcan los elementos/JS
                time.sleep(1.0)
                
        elif current_fase == "correo":
            # Aceptar cookies si las hay
            aceptar_cookies_con_espera(page, intentos=2, pausa_s=0.1)
            
            email = t.get("email", "")
            if email:
                if _campo_email_vacio(page):
                    rellenar_email_con_reintentos(page, email, intentos=2, fase_rapida=True)
                pulsar_continuar_con_reintentos(page, intentos=2, pausa_s=0.2)
            time.sleep(0.8)  # Tiempo de transición
            
        elif current_fase == "consentimiento":
            print(f"    [{n}] {perfil}: Pantalla de consentimiento detectada para '{t.get('email')}'")
            if pulsar_si_continuar_auth(page, timeout_s=4.0):
                print(f"    [{n}] {perfil}: Esperando carga tras consentimiento y redirigiendo de frente a Familia...")
                time.sleep(3.0)
                url_fam = t.get("url_familia", DEFAULT_TIDAL_FAMILY_URL)
                try:
                    page.goto(url_fam, wait_until="commit", timeout=25_000)
                    _esperar_carga_post_goto_tid(page, timeout_dom_ms=8000, margen_s=0.4)
                except Exception:
                    try:
                        page.goto(url_fam, wait_until="domcontentloaded", timeout=20_000)
                    except Exception:
                        pass
            
        elif current_fase == "password":
            password = t.get("password", "")
            if password:
                rellenar_password_con_reintentos(page, password, intentos=2, fase_rapida=True)
                pulsar_iniciar_sesion_con_reintentos(page, intentos=2, pausa_s=0.2)
            time.sleep(1.0)  # Tiempo de transición/login
            
        elif current_fase == "iniciar_sesion":
            if fase_objetivo == "familia":
                url_fam = t.get("url_familia", DEFAULT_TIDAL_FAMILY_URL)
                try:
                    page.goto(url_fam, wait_until="commit", timeout=25_000)
                    _esperar_carga_post_goto_tid(page, timeout_dom_ms=8000, margen_s=0.4)
                except Exception:
                    pass
                    
    return False


def verificar_y_reintentar_fase_anterior(
    trabajos: list[dict],
    momento: str,
    *,
    pausa_s: float = 0.5,
) -> None:
    """
    Verifica que el último paso se haya ejecutado con éxito en cada ventana
    después de la intervención manual y reintenta la fase anterior si es necesario.
    Usa una máquina de estados para reconstruir la sesión en caso de resets por captcha.
    """
    from playwright.sync_api import Error as PWErr
    
    if pausa_s > 0:
        time.sleep(pausa_s)
    
    print(f"\n  Verificando estado de las ventanas tras resolución manual...")
    
    # Determinar qué fase probablemente fue interrumpida basada en el momento
    fase_interrumpida = _identificar_fase_interrumpida(momento)
    
    # Mapear fase interrumpida a la fase objetivo de la máquina de estados
    fase_objetivo = "abrir"
    if fase_interrumpida == "abrir":
        fase_objetivo = "correo"
    elif fase_interrumpida in ("correo", "cookies", "cookies_espera"):
        fase_objetivo = "correo"
    elif fase_interrumpida in ("continuar", "continuar_espera", "password", "password_espera"):
        fase_objetivo = "password"
    elif fase_interrumpida in ("iniciar_sesion", "inicia_sesion"):
        fase_objetivo = "iniciar_sesion"
    elif fase_interrumpida in ("familia", "eliminar_miembro"):
        fase_objetivo = "familia"
        
    for t in trabajos:
        page, n, perfil = t["page"], t["n"], t["perfil"]
        try:
            if page.is_closed():
                print(f"    [{n}] {perfil}: ❌ Pestaña cerrada")
                continue
        except PWErr:
            print(f"    [{n}] {perfil}: ❌ Error al acceder a la pestaña")
            continue
        
        # Verificar si aún hay bloqueos
        motivo = _motivo_bloqueo_tid(page)
        if motivo:
            print(f"    [{n}] {perfil}: ⚠️  Aún presenta {motivo}")
            continue
            
        # Poner la ventana al día usando la máquina de estados
        if poner_ventana_al_dia(t, fase_objetivo):
            print(f"    [{n}] {perfil}: ✅ OK - Ventana al día")
        else:
            print(f"    [{n}] {perfil}: ⚠️  No se pudo poner la ventana al día (fase target: {fase_objetivo})")
    
    print("  Verificación completada.")


def _identificar_fase_interrumpida(momento: str) -> str:
    """Identifica qué fase fue probablemente interrumpida basado en el momento del error."""
    momento_lower = momento.lower()
    
    # Manejar frases de transición "antes de" para evitar falsos positivos
    if "antes del correo" in momento_lower or "antes de la fase 2" in momento_lower:
        return "abrir"
    if "antes de «continuar»" in momento_lower or "antes de continuar" in momento_lower:
        return "cookies_espera"  # ya se aceptaron cookies, pero no se ha pulsado continuar
    if "antes de la fase de contraseña" in momento_lower or "antes de contraseña" in momento_lower:
        return "continuar_espera"
    if "antes de «inicia sesión»" in momento_lower or "antes de inicia sesión" in momento_lower:
        return "password_espera"
    
    if "correo" in momento_lower or "fase 2" in momento_lower:
        return "correo"
    elif "cookies" in momento_lower or "fase 3" in momento_lower:
        return "cookies"
    elif "continuar" in momento_lower or "fase 4" in momento_lower:
        return "continuar"
    elif "contraseña" in momento_lower or "password" in momento_lower or "fase 5" in momento_lower:
        return "password"
    elif "inicia sesión" in momento_lower or "fase 6" in momento_lower:
        return "iniciar_sesion"
    elif "familia" in momento_lower or "fase 7" in momento_lower:
        return "familia"
    elif "eliminar" in momento_lower or "fase 8" in momento_lower:
        return "eliminar_miembro"
    else:
        return "desconocida"


def _verificar_y_ejecutar_accion_pendiente(
    page, trabajo: dict, fase_interrumpida: str
) -> tuple[bool, bool]:
    """
    Verifica si una ventana necesita acciones pendientes de la fase anterior
    y las ejecuta automáticamente si es posible.
    Returns (necesita_accion, accion_realizada_exitosamente)
    """
    from playwright.sync_api import Error as PWErr
    
    try:
        if page.is_closed():
            return False, False
    except PWErr:
        return False, False
    
    if fase_interrumpida == "correo":
        # Verificar si el correo fue rellenado pero necesita continuar
        if _campo_email_vacio(page):
            email = trabajo.get("email", "")
            if email and rellenar_email_con_reintentos(page, email, intentos=2, fase_rapida=True):
                return True, True
            return True, False
        
        # Si hay correo pero no se presionó continuar
        if pulsar_continuar_con_reintentos(page, intentos=3, pausa_s=0.2):
            return True, True
        return False, False
    
    elif fase_interrumpida == "continuar":
        # Verificar si hay botón Continuar visible y presionarlo
        if pulsar_continuar_con_reintentos(page, intentos=3, pausa_s=0.2):
            return True, True
        return False, False
    
    elif fase_interrumpida == "password":
        # Verificar si hay campo de contraseña visible
        if esperar_campo_password(page, timeout_s=2):
            password = trabajo.get("password", "")
            if password and rellenar_password_con_reintentos(page, password, intentos=2, fase_rapida=True):
                return True, True
            return True, False
        return False, False
    
    elif fase_interrumpida == "iniciar_sesion":
        # Verificar si hay botón Iniciar sesión visible y presionarlo
        if pulsar_iniciar_sesion_con_reintentos(page, intentos=3, pausa_s=0.2):
            return True, True
        return False, False
    
    elif fase_interrumpida == "cookies":
        # Intentar aceptar cookies si hay banner
        if aceptar_cookies_con_espera(page, intentos=2, pausa_s=0.1):
            return True, True
        return False, False
    
    # Para otras fases, no hay acciones automáticas específicas
    return False, False


def _campo_email_vacio(page) -> bool:
    """Verifica si el campo de email está vacío o no fue rellenado."""
    from playwright.sync_api import Error as PWErr
    
    for frame in _frames_visibles(page):
        try:
            selectores = [
                'input[type="email"]',
                'input[name="email"]',
                'input[autocomplete="email"]',
                'input[id*="email" i]',
            ]
            for sel in selectores:
                loc = frame.locator(sel).first
                if loc.count() and loc.is_visible(timeout=500):
                    valor = loc.input_value(timeout=1000)
                    if not valor or valor.strip() == "":
                        return True
        except (PWErr, Exception):
            continue
    return False


def manejar_captcha_tid_si_aplica(
    trabajos: list[dict],
    momento: str | None,
    *,
    pausa_dom_s: float,
    cfg: CaptchaTidManejoCfg,
) -> None:
    """Detecta antibot/403 CloudFront y pausa para intervención manual (sin Surfshark automático)."""
    if pausa_dom_s > 0:
        time.sleep(pausa_dom_s)
        
    # Intentar resolver automáticamente cualquier slider antes de pausar
    intentar_autoresolver_sliders(trabajos)
        
    while True:
        afectadas = _ventanas_tid_requieren_surfshark(trabajos)
        if not afectadas:
            break

        motivos = {m for _, _, m in afectadas}
        if "403 CloudFront" in motivos and "antibot" in motivos:
            titulo_base = "BLOQUEO TIDAL (antibot y 403 CloudFront)"
        elif "403 CloudFront" in motivos:
            titulo_base = "ERROR 403 CLOUDFRONT — detectado"
        elif "error login TIDAL" in motivos:
            titulo_base = "ERROR LOGIN TIDAL ('Algo salió mal') — detectado"
        else:
            titulo_base = "VERIFICACIÓN ANTI-ROBOT / BLOQUEO (TIDAL) — detectado"
        
        bar = "=" * 62
        titulo = titulo_base
        if momento:
            titulo = f"{titulo}  ({momento})"
        print(f"\n{bar}\n  {titulo}\n{bar}")
        for n, perfil, motivo in afectadas:
            print(f"   • Ventana [{n}] — {perfil} ({motivo})")
        
        print(f"\n  ⚠️ [IP BLOQUEADA] SE REQUIERE ROTAR DE IP (VPN / PROXY / ROUTER).")
        print(f"  ACCIÓN REQUERIDA:")
        print(f"  1. Rota de IP ahora mismo ( VPN, reconectar router, etc. ).")
        print(f"  2. Resuelve captchas si es que aparecen en las ventanas.")
        print(f"  3. Pulsa Enter AQUÍ. El script mandará las ventanas afectadas a https://tidal.com/pricing para reiniciar.")
        print(f"\n  El script esperará hasta que pulses Enter para continuar...")
        
        pausa_manual_forzada(f"Resolución manual de {titulo_base}")
        
        # Guardar las afectadas antes de redireccionarlas
        afectadas_antes = list(afectadas)
        
        # Redirigir ventanas afectadas a tidal.com/pricing si no han sido solucionadas / adelantadas
        print("\n  [Bypass] Verificando ventanas afectadas tras la pausa...")
        for n_af, _, _ in afectadas_antes:
            for t in trabajos:
                if t["n"] == n_af:
                    page = t["page"]
                    try:
                        if page.is_closed():
                            continue
                            
                        # Comprobar si sigue bloqueada
                        sigue_bloqueada = _motivo_bloqueo_tid(page) is not None
                        
                        # Determinar fase actual y fase objetivo del script
                        fase_interrumpida = _identificar_fase_interrumpida(momento or "")
                        orden_fases = {
                            "abrir": 1,
                            "correo": 2,
                            "consentimiento": 3,
                            "password": 4,
                            "iniciar_sesion": 5,
                            "familia": 6
                        }
                        
                        fase_objetivo = "abrir"
                        if fase_interrumpida == "abrir":
                            fase_objetivo = "correo"
                        elif fase_interrumpida in ("correo", "cookies", "cookies_espera"):
                            fase_objetivo = "correo"
                        elif fase_interrumpida in ("continuar", "continuar_espera", "password", "password_espera"):
                            fase_objetivo = "password"
                        elif fase_interrumpida in ("iniciar_sesion", "inica_sesion"):
                            fase_objetivo = "iniciar_sesion"
                        elif fase_interrumpida in ("familia", "eliminar_miembro"):
                            fase_objetivo = "familia"
                            
                        target_val = orden_fases.get(fase_objetivo, 1)
                        
                        # Determinar fase actual de la pestaña
                        if "account.tidal.com/family" in page.url:
                            current_fase = "familia"
                        elif "pricing" in page.url or page.url.strip("/") in ("https://tidal.com", "http://tidal.com", "https://www.tidal.com"):
                            current_fase = "abrir"
                        elif _es_pantalla_consentimiento(page, expected_email=t.get("email", "")):
                            current_fase = "consentimiento"
                        elif not _es_pagina_login(page) and "captcha-delivery.com" not in page.url and ("account.tidal.com" in page.url or "listen.tidal.com" in page.url):
                            current_fase = "iniciar_sesion"
                        elif _campo_email_visible(page):
                            current_fase = "correo"
                        elif esperar_campo_password(page, timeout_s=0.4):
                            current_fase = "password"
                        else:
                            current_fase = "abrir"
                            
                        current_val = orden_fases.get(current_fase, 1)
                        
                        # Si no está bloqueada Y ya está en la fase objetivo o adelantada
                        if not sigue_bloqueada and current_val >= target_val:
                            print(f"    [{n_af}] {t['perfil']}: Ya está solucionado o adelantado (Fase actual: '{current_fase}' >= objetivo: '{fase_objetivo}'). No se reinicia.")
                            continue
                        
                        if sigue_bloqueada:
                            print(f"    [{n_af}] {t['perfil']}: Sigue bloqueada. Redirigiendo a https://tidal.com/pricing para reiniciar...")
                            t["page"].goto("https://tidal.com/pricing", wait_until="domcontentloaded", timeout=25_000)
                        else:
                            print(f"    [{n_af}] {t['perfil']}: No está bloqueada. Continuando desde fase actual '{current_fase}'...")
                            
                    except Exception as e_nav:
                        print(f"    Error al evaluar/redirigir ventana [{n_af}]: {e_nav}")
        
        # Después de la intervención manual, verificar que el último paso se completó con éxito
        verificar_y_reintentar_fase_anterior(trabajos, momento or "intervención manual", pausa_s=0.8)
        
        # Breve espera antes de re-escanear
        time.sleep(0.5)


def comprobar_captcha_post_fase(
    trabajos: list[dict],
    momento: str,
    *,
    pausa_s: float = 0.18,
    captcha_cfg: CaptchaTidManejoCfg | None = None,
) -> None:
    """Tras una fase: breve espera al DOM y, si hay antibot, 403 CloudFront o error login TIDAL, pausa manual para intervención."""
    cfg = captcha_cfg if captcha_cfg is not None else CaptchaTidManejoCfg()
    manejar_captcha_tid_si_aplica(trabajos, momento, pausa_dom_s=pausa_s, cfg=cfg)


def _chrome_user_data_dir() -> Path:
    sistema = platform.system()
    if sistema == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        if not local:
            raise RuntimeError("LOCALAPPDATA no está definido")
        return Path(local) / "Google" / "Chrome" / "User Data"
    if sistema == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    return Path.home() / ".config" / "google-chrome"


def _get_chrome_exe() -> Path:
    sistema = platform.system()
    if sistema == "Windows":
        return Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if sistema == "Darwin":
        return Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return Path("/usr/bin/google-chrome")


def leer_lineas_utiles(path: Path) -> list[str]:
    if not path.exists():
        print(f"Error: no existe el archivo '{path}'")
        return []
    out: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    return out


def leer_lineas_si_existe(path: Path) -> list[str]:
    """Igual que leer_lineas_utiles pero sin mensaje de error si el archivo no existe."""
    if not path.exists():
        return []
    out: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    return out


def parsear_linea_link(linea: str) -> tuple[str, str]:
    """Devuelve (url, perfil). Si no hay separador, usa URL por defecto y la línea como perfil."""
    for sep in (",", ";", "|"):
        if sep in linea:
            url, perfil = linea.split(sep, 1)
            return url.strip(), perfil.strip()
    # Solo nombre de perfil en la línea
    return DEFAULT_TIDAL_LOGIN_URL, linea.strip()


def cargar_sesiones(
    links_path: Path,
    emails_path: Path,
    passwords_path: Path,
    eliminar_miembros_path: Path | None = None,
    *,
    cargar_eliminar_miembros: bool = True,
) -> list[dict]:
    lineas_links = leer_lineas_si_existe(links_path)
    lineas_emails = leer_lineas_utiles(emails_path)
    lineas_passwords = leer_lineas_utiles(passwords_path)
    if cargar_eliminar_miembros:
        ruta_elim = _resolver_archivo_eliminar_miembros(eliminar_miembros_path)
        if eliminar_miembros_path is not None and ruta_elim is None:
            print(
                f"Aviso: el archivo de --eliminar-miembros no existe o no es un fichero: "
                f"{eliminar_miembros_path}"
            )
            lineas_eliminar = []
        elif ruta_elim is not None:
            lineas_eliminar = leer_lineas_si_existe(ruta_elim)
            if lineas_eliminar:
                print(
                    f"Fase 8: {len(lineas_eliminar)} línea(s) con correos a eliminar "
                    f"desde «{ruta_elim.name}»."
                )
        else:
            lineas_eliminar = []
    else:
        lineas_eliminar = []
    if not lineas_emails:
        print(f"No hay correos en '{emails_path.name}' (archivo vacío o solo comentarios).")
        return []

    n = len(lineas_emails)
    if len(lineas_links) > n:
        print(
            f"Aviso: {links_path.name} tiene {len(lineas_links)} línea(s) y "
            f"{emails_path.name} tiene {n} correo(s). Solo se abrirán {n} ventana(s) "
            "(una por correo); las líneas sobrantes de LINKS no se usan."
        )
    elif len(lineas_links) < n and lineas_links:
        print(
            f"Aviso: {links_path.name} tiene {len(lineas_links)} línea(s) y hay {n} correo(s). "
            f"Las ventanas {len(lineas_links) + 1}–{n} usarán la URL de login por defecto y "
            "etiqueta «Perfil-N» en consola."
        )
    elif not lineas_links:
        print(
            f"Aviso: no hay entradas en '{links_path.name}' (vacío o ausente). "
            f"Las {n} ventana(s) usarán la URL de login por defecto y «Perfil-1»…«Perfil-{n}»."
        )

    sesiones: list[dict] = []
    for i in range(n):
        linea = lineas_links[i] if i < len(lineas_links) else ""
        if linea.strip():
            url, perfil = parsear_linea_link(linea)
        else:
            url, perfil = DEFAULT_TIDAL_LOGIN_URL, f"Perfil-{i + 1}"
        email = lineas_emails[i].strip()
        password = lineas_passwords[i].strip() if i < len(lineas_passwords) else ""
        eliminar_miembro = lineas_eliminar[i].strip() if i < len(lineas_eliminar) else ""
        if not email:
            print(
                f"Aviso: línea {i + 1} de '{emails_path.name}' está vacía; "
                f"esa ventana no tendrá correo para rellenar."
            )
        if not password:
            print(
                f"Aviso: línea {i + 1} ({perfil}): no hay contraseña en la línea {i + 1} "
                f"de '{passwords_path.name}'."
            )
        sesiones.append(
            {
                "url": url,
                "perfil": perfil,
                "email": email,
                "password": password,
                "eliminar_miembro": eliminar_miembro,
            }
        )
    return sesiones


def abrir_solo_chrome(url: str, perfil: str, chrome_exe: Path, user_data: Path, usar_incognito: bool = False, habilitar_accesibilidad: bool = False) -> bool:
    try:
        launch_args = [
            str(chrome_exe),
            f"--user-data-dir={user_data}",
            f"--profile-directory={perfil}",
        ]
        if usar_incognito:
            launch_args.append("--incognito")
        if habilitar_accesibilidad:
            launch_args.append("--force-renderer-accessibility")
        launch_args.append(url)
        subprocess.Popen(launch_args)
        print(f"Abierto (solo navegador): {url} | perfil: {perfil}")
        return True
    except FileNotFoundError:
        print(f"No se encontró Chrome en {chrome_exe}")
        return False
    except Exception as e:
        print(f"Error al abrir: {e}")
        return False


def _detectar_captcha_uia(wnd) -> bool:
    """Detecta si hay una pantalla de captcha o bloqueo de DataDome en el árbol UIA."""
    palabras_captcha = [
        "datadome", "captcha", "bloqueado", "verificación", "verification", 
        "restringido", "restringida", "sobrehumana", "velocidad", "restricted", "blocked"
    ]
    try:
        for d in wnd.descendants():
            name = (d.element_info.name or "").lower()
            text = (d.window_text() or "").lower()
            if any(k in name or k in text for k in palabras_captcha):
                return True
    except Exception:
        pass
    return False


def _obtener_url_actual_uia(wnd) -> str:
    """Busca el control de la barra de direcciones de Chrome y lee su valor."""
    try:
        for d in wnd.descendants(control_type="Edit"):
            name = (d.element_info.name or "").lower()
            if "direcci" in name or "address" in name or "buscar" in name:
                val = d.get_value() or ""
                if val:
                    return val.lower()
    except Exception:
        pass
    return ""


def _buscar_control_uia(wnd, control_type: str, palabras_clave: list[str]):
    """Busca un control del tipo especificado que contenga alguna de las palabras clave en su nombre."""
    try:
        for d in wnd.descendants(control_type=control_type):
            name = (d.element_info.name or "").lower()
            if any(k in name for k in palabras_clave):
                return d
    except Exception:
        pass
    return None


def _rellenar_campo_uia(wnd, palabras_clave: list[str], valor: str) -> bool:
    """Encuentra un campo de texto (Edit) y lo rellena con el valor especificado escribiendo carácter por carácter."""
    try:
        edit = _buscar_control_uia(wnd, "Edit", palabras_clave)
        if not edit:
            # Fallback: si no coincide por nombre, pero es el único Edit visible, lo usamos
            edits = [e for e in wnd.descendants(control_type="Edit") if e.is_visible()]
            if len(edits) == 1:
                edit = edits[0]
        if edit:
            edit.click_input()
            # Limpiar el campo usando Ctrl+A y Backspace
            edit.type_keys("^a{BACKSPACE}")
            time.sleep(random.uniform(0.18, 0.35))
            
            # Escribir carácter por carácter con velocidad humana
            for char in valor:
                # Retraso variable simulando ritmo biológico
                delay = random.uniform(0.045, 0.125)
                time.sleep(delay)
                
                # Escapar caracteres de control especiales en pywinauto
                if char in ("+", "^", "%", "~", "(", ")", "{", "}"):
                    edit.type_keys(f"{{{char}}}")
                elif char == " ":
                    edit.type_keys("{SPACE}")
                else:
                    edit.type_keys(char, with_spaces=True)
            return True
    except Exception:
        pass
    return False


def _hacer_clic_boton_uia(wnd, palabras_clave: list[str]) -> bool:
    """Encuentra un botón y hace clic en él."""
    try:
        btn = _buscar_control_uia(wnd, "Button", palabras_clave)
        if btn:
            btn.click_input()
            return True
    except Exception:
        pass
    return False


def _aceptar_cookies_uia(wnd) -> bool:
    """Intenta aceptar el banner de cookies si está presente en la ventana."""
    palabras_cookies = ["aceptar", "accept", "agree", "consentir", "permitir", "allow all"]
    try:
        for d in wnd.descendants(control_type="Button"):
            name = (d.element_info.name or "").lower()
            if any(k in name for k in palabras_cookies):
                print("    Cerrando banner de cookies...")
                d.click_input()
                return True
    except Exception:
        pass
    return False


def _pulsar_si_continuar_auth_uia(wnd, timeout_s: float = 6.0) -> bool:
    """Detecta y hace clic en 'Sí, continuar' usando UIA en modo subprocess."""
    import time
    start_time = time.time()
    keywords = ["sí, continuar", "si, continuar", "yes, continue", "continuar", "continue"]
    while time.time() - start_time < timeout_s:
        try:
            # Buscar en todos los descendientes sin restricción de tipo
            for d in wnd.descendants():
                try:
                    name = (d.element_info.name or "").lower()
                    text = (d.window_text() or "").lower()
                    if any(k in name or k in text for k in keywords):
                        if d.is_visible():
                            print(f"  [Consent UIA] Detectado control visible '{d.element_info.control_type}' con nombre '{d.element_info.name}'. Haciendo clic...")
                            try:
                                d.click_input()
                            except Exception:
                                try:
                                    d.click()
                                except Exception:
                                    try:
                                        d.set_focus()
                                        wnd.type_keys("{ENTER}")
                                    except Exception:
                                        pass
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _eliminar_miembro_uia(wnd, email_miembro: str) -> bool:
    """Ejecuta el flujo UIA para eliminar a un miembro del plan familiar en account.tidal.com/family."""
    try:
        # 1. Buscar el texto del email del miembro
        target_item = None
        for d in wnd.descendants():
            name = (d.element_info.name or "")
            text = (d.window_text() or "")
            if email_miembro.lower() in name.lower() or email_miembro.lower() in text.lower():
                target_item = d
                break
        if not target_item:
            print(f"      No se encontró al miembro {email_miembro} en la lista.")
            return False
            
        print(f"      Miembro {email_miembro} encontrado. Seleccionando fila...")
        target_item.click_input()
        time.sleep(1.2)
        
        # 2. Buscar el botón "Eliminar del plan" que ahora debería estar visible
        btn_eliminar = None
        for d in wnd.descendants(control_type="Button"):
            name = (d.element_info.name or "").lower()
            if "eliminar del plan" in name or "remove" in name or "eliminar" in name:
                btn_eliminar = d
                break
        if not btn_eliminar:
            print("      No se encontró el botón 'Eliminar del plan'.")
            return False
            
        print("      Pulsando 'Eliminar del plan'...")
        btn_eliminar.click_input()
        time.sleep(1.2)
        
        # 3. Confirmar la eliminación en el diálogo emergente
        btn_confirmar = None
        for d in wnd.descendants(control_type="Button"):
            name = (d.element_info.name or "").lower()
            if "confirmar" in name or "eliminar" in name or "confirm" in name:
                btn_confirmar = d
                if btn_confirmar != btn_eliminar:
                    break
        if btn_confirmar:
            print("      Confirmando eliminación...")
            btn_confirmar.click_input()
            time.sleep(1.2)
            return True
        else:
            print("      No se encontró el botón de confirmación.")
            return False
    except Exception as e:
        print(f"      Error en eliminación UIA: {e}")
        return False


def _pw_error_types():
    """Errores de Playwright cuando el navegador o el frame ya se cerró."""
    from playwright.sync_api import Error as PlaywrightError

    return (PlaywrightError,)


def _frames_visibles(page):
    """Todos los frames de la pestaña (login TIDAL suele estar en iframe)."""
    try:
        return list(page.frames)
    except _pw_error_types():
        return []


def intentar_aceptar_cookies(page) -> bool:
    """CMP / cookies en español o inglés (TIDAL, OneTrust, TCF, etc.)."""
    from playwright.sync_api import Error as PWErr
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        if page.is_closed():
            return False
    except PWErr:
        return False

    nombres_boton = (
        re.compile(r"^\s*aceptar\s*$", re.I),
        re.compile(r"^\s*aceptar todo", re.I),
        re.compile(r"^\s*aceptar todas", re.I),
        re.compile(r"^\s*accept\s*$", re.I),
        re.compile(r"^\s*accept all", re.I),
        re.compile(r"^\s*i agree", re.I),
        re.compile(r"^\s*consentir", re.I),
        re.compile(r"^\s*allow all", re.I),
    )

    selectores_css = [
        "#onetrust-accept-btn-handler",
        "button#onetrust-accept-btn-handler",
        "button.sp_choice_type_11",
        "button[data-testid*='accept' i]",
        '[id*="accept" i][id*="cookie" i]',
        '[class*="cookie" i] button[class*="accept" i]',
        "#save",
        'button[id*="accept" i]',
    ]

    for frame in _frames_visibles(page):
        for rx in nombres_boton:
            try:
                loc = frame.get_by_role("button", name=rx)
                if loc.count() == 0:
                    continue
                candidato = loc.last if loc.count() > 1 else loc.first
                candidato.scroll_into_view_if_needed(timeout=1200)
                if candidato.is_visible(timeout=700):
                    candidato.click(timeout=2200)
                    return True
            except PlaywrightTimeout:
                continue
            except PWErr:
                return False
            except Exception:
                continue

        try:
            dlg = frame.get_by_role("dialog")
            if dlg.count():
                btn = dlg.get_by_role("button", name=re.compile(r"aceptar", re.I)).last
                if btn.count() and btn.is_visible(timeout=450):
                    btn.scroll_into_view_if_needed(timeout=900)
                    btn.click(timeout=2200)
                    return True
        except PWErr:
            return False
        except Exception:
            pass

        for sel in selectores_css:
            try:
                loc = frame.locator(sel).last
                if loc.count() and loc.is_visible(timeout=450):
                    loc.scroll_into_view_if_needed(timeout=900)
                    loc.click(timeout=2200)
                    return True
            except PWErr:
                return False
            except Exception:
                continue

        try:
            loc = frame.locator(
                '[aria-label*="Aceptar" i], [aria-label*="Accept" i], '
                '[title*="Aceptar" i], [title*="Accept" i]'
            ).last
            if loc.count() and loc.is_visible(timeout=450):
                loc.scroll_into_view_if_needed(timeout=900)
                loc.click(timeout=2200)
                return True
        except PWErr:
            return False
        except Exception:
            pass

        try:
            loc = frame.locator(
                'button:visible, [role="button"]:visible, a[role="button"]:visible'
            ).filter(has_text=re.compile(r"^\s*Aceptar\s*$", re.I))
            if loc.count():
                loc.last.scroll_into_view_if_needed(timeout=900)
                loc.last.click(timeout=2200)
                return True
        except PWErr:
            return False
        except Exception:
            pass

        for patron in (
            re.compile(r"^\s*accept\s+all(\s+cookies)?\s*$", re.I),
            re.compile(r"^\s*aceptar\s+tod", re.I),
        ):
            try:
                items = frame.get_by_text(patron).all()
            except PWErr:
                return False
            except Exception:
                continue
            for loc in items:
                try:
                    if not loc.is_visible(timeout=320):
                        continue
                    el = loc.locator(
                        'xpath=ancestor-or-self::button[1] | ancestor-or-self::*[@role="button"][1]'
                    )
                    if el.count():
                        el.first.click(timeout=2000)
                        return True
                    loc.click(timeout=2000)
                    return True
                except PWErr:
                    return False
                except Exception:
                    continue

    for frame in _frames_visibles(page):
        for rx in (re.compile(r"^\s*rechazar\s*$", re.I), re.compile(r"^\s*reject\s*$", re.I)):
            try:
                loc = frame.get_by_role("button", name=rx)
                if loc.count() == 0:
                    continue
                b = loc.last if loc.count() > 1 else loc.first
                b.scroll_into_view_if_needed(timeout=1200)
                if b.is_visible(timeout=700):
                    b.click(timeout=2200)
                    return True
            except PlaywrightTimeout:
                continue
            except PWErr:
                return False
            except Exception:
                continue

    return False


def aceptar_cookies_con_espera(
    page,
    intentos: int = 4,
    pausa_s: float = 0.08,
    *,
    esperar_networkidle: bool = False,
) -> bool:
    from playwright.sync_api import Error as PWErr

    for _ in range(intentos):
        try:
            if page.is_closed():
                return False
        except PWErr:
            return False
        if intentar_aceptar_cookies(page):
            return True
        time.sleep(pausa_s)
        if esperar_networkidle:
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except PWErr:
                return False
            except Exception:
                pass
        else:
            time.sleep(0.03)
    return False


def rellenar_campo_humanizado(loc, valor: str, t_click: int = 3000, t_fill: int = 8000) -> bool:
    """Rellena un input simulando escritura humana carácter por carácter."""
    from playwright.sync_api import Error as PWErr
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        loc.scroll_into_view_if_needed(timeout=t_click)
        loc.click(timeout=t_click)
        loc.press("Control+a", timeout=2000)
        loc.press("Backspace", timeout=2000)
        
        # Escribir carácter por carácter con delay variable aleatorio
        for char in valor:
            delay = random.uniform(0.04, 0.11)
            time.sleep(delay)
            loc.press_sequentially(char, delay=0, timeout=1000)
        
        # Breve espera adicional al finalizar de escribir
        time.sleep(random.uniform(0.15, 0.35))
        return True
    except (PlaywrightTimeout, PWErr, Exception):
        # Fallback a la escritura estándar de Playwright si falla el método manual
        try:
            loc.click(timeout=t_click)
            loc.fill("", timeout=t_fill)
            loc.fill(valor, timeout=t_fill)
            return True
        except Exception:
            return False


def hacer_clic_humanizado(page, loc) -> bool:
    """Mueve el mouse simulando trayectorias humanas antes de hacer clic en un elemento."""
    import random
    from playwright.sync_api import Error as PWErr
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        loc.scroll_into_view_if_needed(timeout=2000)
        box = loc.bounding_box()
        if not box:
            # Si no se puede obtener el bounding box, hacer clic directo
            loc.click(timeout=5000, force=True)
            return True

        # Calcular coordenadas con una ligera desviación aleatoria para no hacer clic siempre en el centro matemático
        target_x = box["x"] + box["width"] * random.uniform(0.2, 0.8)
        target_y = box["y"] + box["height"] * random.uniform(0.2, 0.8)

        # Simular trayectoria del ratón en varios pasos
        pasos = random.randint(6, 12)
        page.mouse.move(target_x, target_y, steps=pasos)

        # Pausa aleatoria para simular la reacción humana antes del clic
        time.sleep(random.uniform(0.1, 0.25))

        # Realizar el clic con el mouse
        page.mouse.click(target_x, target_y)
        return True
    except (PlaywrightTimeout, PWErr, Exception):
        # Fallback a clic estándar de Playwright
        try:
            loc.click(timeout=5000, force=True)
            return True
        except Exception:
            return False


def rellenar_email_login(page, email: str, *, fase_rapida: bool = False) -> bool:
    if not email:
        return False
    from playwright.sync_api import Error as PWErr
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        if page.is_closed():
            return False
    except PWErr:
        return False

    try:
        dom_wait = 8000 if fase_rapida else 30_000
        page.wait_for_load_state("domcontentloaded", timeout=dom_wait)
    except PlaywrightTimeout:
        pass
    except PWErr:
        return False

    t_iframe = 1800 if fase_rapida else 12_000
    t_visible = 500 if fase_rapida else 2500
    t_click = 800 if fase_rapida else 3000
    t_fill = 2200 if fase_rapida else 8000
    post_dom = 0.02 if fase_rapida else 0.4

    try:
        page.wait_for_selector("iframe", timeout=t_iframe)
    except PlaywrightTimeout:
        pass
    except PWErr:
        return False

    time.sleep(post_dom)

    selectores = [
        'input[type="email"]',
        'input[type="text"]',
        'input[name="email"]',
        'input[name="username"]',
        'input[name="login"]',
        'input[autocomplete="email"]',
        'input[autocomplete="username"]',
        'input[id*="email" i]',
        'input[id*="username" i]',
        'input[id*="user" i]',
        'input[placeholder*="mail" i]',
        'input[placeholder*="email" i]',
        'input[placeholder*="correo" i]',
        'input[placeholder*="usuario" i]',
        'input[data-testid*="email" i]',
        'input[data-testid*="user" i]',
    ]

    for frame in _frames_visibles(page):
        plac_pat = re.compile(r"(email|mail|correo|usuario|username|introduce)", re.I)

        if fase_rapida:
            try:
                loc = frame.get_by_placeholder(plac_pat).first
                if loc.count() and loc.is_visible(timeout=t_visible):
                    if rellenar_campo_humanizado(loc, email, t_click=t_click, t_fill=t_fill):
                        return True
            except PlaywrightTimeout:
                pass
            except PWErr:
                return False
            except Exception:
                pass

        for rx in (
            re.compile(r"(correo|email|usuario|username|identif)", re.I),
        ):
            try:
                loc = frame.get_by_label(rx).first
                if loc.count() and loc.is_visible(timeout=t_visible):
                    if rellenar_campo_humanizado(loc, email, t_click=t_click, t_fill=t_fill):
                        return True
            except PlaywrightTimeout:
                continue
            except PWErr:
                return False
            except Exception:
                continue

        try:
            loc = frame.get_by_role(
                "textbox",
                name=re.compile(r"(correo|email|usuario|username|identif)", re.I),
            ).first
            if loc.count() and loc.is_visible(timeout=t_visible):
                if rellenar_campo_humanizado(loc, email, t_click=t_click, t_fill=t_fill):
                    return True
        except PlaywrightTimeout:
            pass
        except PWErr:
            return False
        except Exception:
            pass

        if not fase_rapida:
            try:
                loc = frame.get_by_placeholder(plac_pat).first
                if loc.count() and loc.is_visible(timeout=t_visible):
                    if rellenar_campo_humanizado(loc, email, t_click=t_click, t_fill=t_fill):
                        return True
            except PlaywrightTimeout:
                continue
            except PWErr:
                return False
            except Exception:
                continue

        for sel in selectores:
            try:
                loc = frame.locator(sel).first
                if not loc.count():
                    continue
                if not loc.is_visible(timeout=max(400, t_visible - 150)):
                    continue
                if rellenar_campo_humanizado(loc, email, t_click=t_click, t_fill=t_fill):
                    return True
            except PlaywrightTimeout:
                continue
            except PWErr:
                return False
            except Exception:
                continue

    return False


def rellenar_email_con_reintentos(
    page,
    email: str,
    intentos: int = 2,
    pausa_s: float = 0.02,
    *,
    fase_rapida: bool = False,
) -> bool:
    for _ in range(intentos):
        if rellenar_email_login(page, email, fase_rapida=fase_rapida):
            return True
        time.sleep(pausa_s)
    return False


def pulsar_continuar(page) -> bool:
    """Pulsa el botón Continuar / Continue del paso de email (todos los frames)."""
    from playwright.sync_api import Error as PWErr
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        if page.is_closed():
            return False
    except PWErr:
        return False

    nombres = (
        re.compile(r"^\s*continuar\s*$", re.I),
        re.compile(r"^\s*continue\s*$", re.I),
        re.compile(r"^\s*siguiente\s*$", re.I),
        re.compile(r"^\s*next\s*$", re.I),
    )

    for frame in _frames_visibles(page):
        for rx in nombres:
            try:
                loc = frame.get_by_role("button", name=rx).first
                if not loc.count():
                    continue
                if not loc.is_visible(timeout=2200):
                    continue
                try:
                    if not loc.is_enabled():
                        continue
                except Exception:
                    pass
                if hacer_clic_humanizado(page, loc):
                    return True
            except PlaywrightTimeout:
                continue
            except PWErr:
                return False
            except Exception:
                continue

        try:
            loc = frame.locator(
                'button:visible, [role="button"]:visible, input[type="submit"]:visible'
            ).filter(has_text=re.compile(r"continuar|continue|siguiente|next", re.I))
            if loc.count():
                cand = loc.last
                try:
                    if not cand.is_enabled():
                        continue
                except Exception:
                    pass
                if hacer_clic_humanizado(page, cand):
                    return True
        except PWErr:
            return False
        except Exception:
            pass

        try:
            loc = frame.locator(
                'input[type="submit"][value*="Continuar" i], '
                'input[type="submit"][value*="Continue" i], '
                'input[type="submit"][value*="Siguiente" i]'
            ).first
            if loc.count() and loc.is_visible(timeout=1800):
                if hacer_clic_humanizado(page, loc):
                    return True
        except PlaywrightTimeout:
            continue
        except PWErr:
            return False
        except Exception:
            continue

    return False


def pulsar_continuar_con_reintentos(page, intentos: int = 10, pausa_s: float = 0.28) -> bool:
    for _ in range(intentos):
        if pulsar_continuar(page):
            return True
        time.sleep(pausa_s)
    return False


def pulsar_iniciar_sesion(page) -> bool:
    """Pulsa Inicia sesión / Sign in tras el paso de contraseña (todos los frames)."""
    from playwright.sync_api import Error as PWErr
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        if page.is_closed():
            return False
    except PWErr:
        return False

    nombres = (
        re.compile(r"^\s*inicia\s+sesión\s*$", re.I),
        re.compile(r"^\s*iniciar\s+sesión\s*$", re.I),
        re.compile(r"^\s*inicia\s+session\s*$", re.I),
        re.compile(r"^\s*sign\s*in\s*$", re.I),
        re.compile(r"^\s*log\s*in\s*$", re.I),
        re.compile(r"^\s*anmelden\s*$", re.I),
    )

    for frame in _frames_visibles(page):
        for rx in nombres:
            try:
                loc = frame.get_by_role("button", name=rx).first
                if not loc.count():
                    continue
                if not loc.is_visible(timeout=2000):
                    continue
                try:
                    if not loc.is_enabled():
                        continue
                except Exception:
                    pass
                if hacer_clic_humanizado(page, loc):
                    return True
            except PlaywrightTimeout:
                continue
            except PWErr:
                return False
            except Exception:
                continue

        try:
            loc = frame.locator(
                'button:visible, [role="button"]:visible, input[type="submit"]:visible'
            ).filter(
                has_text=re.compile(
                    r"inicia\s*sesión|iniciar\s*sesión|sign\s*in|log\s*in|anmelden",
                    re.I,
                )
            )
            if loc.count():
                cand = loc.last
                if hacer_clic_humanizado(page, cand):
                    return True
        except PWErr:
            return False
        except Exception:
            pass

        try:
            loc = frame.locator(
                'input[type="submit"][value*="Inicia" i], '
                'input[type="submit"][value*="sesión" i], '
                'input[type="submit"][value*="Sign" i], '
                'input[type="submit"][value*="Log" i]'
            ).first
            if loc.count() and loc.is_visible(timeout=1600):
                if hacer_clic_humanizado(page, loc):
                    return True
        except PlaywrightTimeout:
            continue
        except PWErr:
            return False
        except Exception:
            continue

    return False


def pulsar_iniciar_sesion_con_reintentos(page, intentos: int = 5, pausa_s: float = 0.08) -> bool:
    for _ in range(intentos):
        if pulsar_iniciar_sesion(page):
            return True
        time.sleep(pausa_s)
    return False


def pulsar_si_continuar_auth(page, timeout_s: float = 6.0) -> bool:
    """Detecta y hace clic en el botón 'Sí, continuar' de la pantalla de consentimiento/autorización de TIDAL."""
    from playwright.sync_api import Error as PWErr
    import time
    import re
    
    start_time = time.time()
    rx_lista = [
        re.compile(r"sí,\s*continuar", re.I),
        re.compile(r"si,\s*continuar", re.I),
        re.compile(r"yes,\s*continue", re.I),
        re.compile(r"continuar", re.I),
        re.compile(r"continue", re.I),
    ]
    
    while time.time() - start_time < timeout_s:
        try:
            if page.is_closed():
                return False
        except PWErr:
            return False
            
        # Comprobar si ya redirigió fuera de la página de login/authorize
        current_url = page.url
        if "authorize" not in current_url and "login" not in current_url and "tidal.com" in current_url:
            return True
            
        for frame in _frames_visibles(page):
            for rx in rx_lista:
                try:
                    # 1. Buscar por get_by_role (button)
                    loc = frame.get_by_role("button", name=rx)
                    count = loc.count()
                    for idx in range(count):
                        cand = loc.nth(idx)
                        if cand.is_visible():
                            print(f"    [Consent] Pulsando botón por get_by_role ('{rx.pattern}')")
                            try:
                                cand.click(timeout=3000)
                            except Exception:
                                cand.click(timeout=3000, force=True)
                            return True
                except Exception:
                    pass
                
                try:
                    # 2. Buscar por get_by_text (cualquier elemento)
                    loc = frame.get_by_text(rx)
                    count = loc.count()
                    for idx in range(count):
                        cand = loc.nth(idx)
                        if cand.is_visible():
                            print(f"    [Consent] Pulsando elemento por get_by_text ('{rx.pattern}')")
                            try:
                                cand.click(timeout=3000)
                            except Exception:
                                cand.click(timeout=3000, force=True)
                            return True
                except Exception:
                    pass
                    
                try:
                    # 3. Buscar usando locator general
                    loc = frame.locator('button, [role="button"], a, input[type="submit"]').filter(has_text=rx)
                    count = loc.count()
                    for idx in range(count):
                        cand = loc.nth(idx)
                        if cand.is_visible():
                            print(f"    [Consent] Pulsando botón/link por locator general ('{rx.pattern}')")
                            try:
                                cand.click(timeout=3000)
                            except Exception:
                                cand.click(timeout=3000, force=True)
                            return True
                except Exception:
                    pass
                    
        time.sleep(0.25)
    return False


_LOGIN_TID_ERROR_RX = re.compile(
    r"algo\s+salió\s+mal|something\s+went\s+wrong|inténtalo\s+de\s+nuevo|try\s+again",
    re.I,
)
_PASSWORD_SELECTORES = (
    'input[type="password"]',
    'input[name="password"]',
    'input[name="passwd"]',
    'input[autocomplete="current-password"]',
    'input[autocomplete="new-password"]',
    'input[id*="password" i]',
    'input[placeholder*="contraseña" i]',
    'input[placeholder*="password" i]',
    'input[data-testid*="password" i]',
)


def _login_tid_muestra_error(page) -> bool:
    from playwright.sync_api import Error as PWErr
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        if page.is_closed():
            return False
    except PWErr:
        return False
        
    for frame in _frames_visibles(page):
        try:
            loc = frame.get_by_text(_LOGIN_TID_ERROR_RX)
            if loc.count() > 0 and loc.first.is_visible(timeout=400):
                return True
        except (PlaywrightTimeout, PWErr):
            continue
        except Exception:
            continue
    return False


def _locator_campo_password(frame):
    from playwright.sync_api import Error as PWErr
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    t_vis = 500
    for rx in (re.compile(r"^\s*(contraseña|password|clave)\s*$", re.I),):
        try:
            loc = frame.get_by_label(rx).first
            if loc.count() and loc.is_visible(timeout=t_vis):
                return loc
        except (PlaywrightTimeout, PWErr, Exception):
            pass

    try:
        loc = frame.get_by_role(
            "textbox",
            name=re.compile(r"^\s*(contraseña|password|clave)\s*$", re.I),
        ).first
        if loc.count() and loc.is_visible(timeout=t_vis):
            return loc
    except (PlaywrightTimeout, PWErr, Exception):
        pass

    try:
        loc = frame.get_by_placeholder(
            re.compile(r"(contraseña|password|clave)", re.I)
        ).first
        if loc.count() and loc.is_visible(timeout=t_vis):
            return loc
    except (PlaywrightTimeout, PWErr, Exception):
        pass

    for sel in _PASSWORD_SELECTORES:
        try:
            loc = frame.locator(sel).first
            if loc.count() and loc.is_visible(timeout=t_vis):
                return loc
        except (PlaywrightTimeout, PWErr, Exception):
            continue
    return None


def resolver_pantalla_otp_codigo(page) -> bool:
    """
    Detecta si la página está solicitando el código de 6 dígitos (OTP) enviado por correo
    y hace clic en 'Inicia sesión con contraseña' para volver al flujo normal de contraseña.
    """
    from playwright.sync_api import Error as PWErr
    import re
    import time

    try:
        if page.is_closed():
            return False
    except PWErr:
        return False

    patrones_boton = (
        re.compile(r"inicia\s+sesión\s+con\s+contraseña", re.I),
        re.compile(r"iniciar\s+sesión\s+con\s+contraseña", re.I),
        re.compile(r"log\s*in\s*with\s*password", re.I),
        re.compile(r"sign\s*in\s*with\s*password", re.I),
        re.compile(r"con\s+contraseña", re.I),
        re.compile(r"with\s+password", re.I),
    )

    clicado = False
    for frame in _frames_visibles(page):
        for pat in patrones_boton:
            try:
                # Buscar botones
                btn = frame.get_by_role("button", name=pat).first
                if btn.count() > 0 and btn.is_visible(timeout=200):
                    btn.click(timeout=1000)
                    clicado = True
                    break
                # Buscar enlaces
                lnk = frame.get_by_role("link", name=pat).first
                if lnk.count() > 0 and lnk.is_visible(timeout=200):
                    lnk.click(timeout=1000)
                    clicado = True
                    break
            except Exception:
                continue
        if clicado:
            break

        for pat in patrones_boton:
            try:
                loc = frame.get_by_text(pat).first
                if loc.count() > 0 and loc.is_visible(timeout=200):
                    try:
                        loc.click(timeout=1000)
                        clicado = True
                        break
                    except Exception:
                        anc = loc.locator('xpath=ancestor-or-self::button[1] | ancestor-or-self::a[1]').first
                        if anc.count() > 0:
                            anc.click(timeout=1000)
                            clicado = True
                            break
            except Exception:
                continue
        if clicado:
            break

    if clicado:
        print("    [TIDAL OTP] Detectada interfaz de código. Pulsando 'Inicia sesión con contraseña'.")
        time.sleep(1.0)  # Espera de transición
        return True
    return False


def esperar_campo_password(page, *, timeout_s: float = 8.0) -> bool:
    """Espera a que aparezca un input de contraseña visible (login TIDAL en iframe)."""
    from playwright.sync_api import Error as PWErr

    try:
        if page.is_closed():
            return False
    except PWErr:
        return False
        
    # Si el campo de correo sigue visible en la página, aún no estamos en la pantalla de contraseña
    if _campo_email_visible(page):
        return False

    otp_intentado = False
    deadline = time.monotonic() + max(0.5, timeout_s)
    while time.monotonic() < deadline:
        # Si a mitad de la espera vuelve a estar visible el email (p. ej. por un reset)
        if _campo_email_visible(page):
            return False
            
        # Si aparece la pantalla de código OTP, hacer clic para volver a contraseña
        if not otp_intentado:
            if resolver_pantalla_otp_codigo(page):
                otp_intentado = True
            
        for frame in _frames_visibles(page):
            if _locator_campo_password(frame) is not None:
                return True
        time.sleep(0.12)
    return False


def _valor_campo_coincide(loc, valor: str) -> bool:
    try:
        return loc.input_value(timeout=1500) == valor
    except Exception:
        return False


def _rellenar_input_verificado(loc, valor: str, *, t_click: int = 3200, t_fill: int = 9000) -> bool:
    if not valor:
        return False
    return rellenar_campo_humanizado(loc, valor, t_click=t_click, t_fill=t_fill)


def recuperar_pantalla_password_si_error(
    page,
    *,
    continuar_reintentos: int = 5,
    continuar_pausa_s: float = 0.35,
    espera_password_s: float = 8.0,
) -> bool:
    """Si TIDAL muestra «Algo salió mal» en el paso de email, reintenta Continuar y espera contraseña."""
    if esperar_campo_password(page, timeout_s=0.6):
        return True
    if not _login_tid_muestra_error(page):
        return esperar_campo_password(page, timeout_s=espera_password_s)
    pulsar_continuar_con_reintentos(
        page, intentos=continuar_reintentos, pausa_s=continuar_pausa_s
    )
    time.sleep(0.55)
    return esperar_campo_password(page, timeout_s=espera_password_s)


def rellenar_password_login(page, password: str, *, fase_rapida: bool = False) -> bool:
    if not password:
        return False
    from playwright.sync_api import Error as PWErr

    try:
        if page.is_closed():
            return False
    except PWErr:
        return False

    t_click = 2400 if fase_rapida else 3200
    t_fill = 7000 if fase_rapida else 9000

    for frame in _frames_visibles(page):
        loc = _locator_campo_password(frame)
        if loc is None:
            continue
        if _rellenar_input_verificado(loc, password, t_click=t_click, t_fill=t_fill):
            return True

    return False


def rellenar_password_con_reintentos(
    page,
    password: str,
    intentos: int = 8,
    pausa_s: float = 0.28,
    *,
    fase_rapida: bool = True,
) -> bool:
    for _ in range(intentos):
        if rellenar_password_login(page, password, fase_rapida=fase_rapida):
            return True
        time.sleep(pausa_s)
    return False


def eliminar_miembro_plan_familiar_tid(
    page,
    email_objetivo: str,
    *,
    pausa_s: float = 0.45,
) -> bool:
    """
    En account.tidal.com/family: localiza al miembro (fila puede mostrar nickname o correo),
    expande, pulsa «Eliminar del plan» y «Confirmar la eliminación» (o equivalentes en inglés).
    """
    from playwright.sync_api import Error as PWErr

    email_objetivo = (email_objetivo or "").strip()
    if not email_objetivo:
        return False
    obj_cf = email_objetivo.casefold()

    try:
        if page.is_closed():
            return False
    except PWErr:
        return False

    rx_eliminar = re.compile(r"eliminar\s+del\s+plan", re.I)
    rx_remove = re.compile(r"remove\s+from\s+plan", re.I)
    rx_confirmar = re.compile(r"confirmar\s+la\s+eliminaci|confirmar\s+eliminaci", re.I)
    rx_confirm_en = re.compile(r"confirm\s+(the\s+)?(removal|deletion)", re.I)
    rx_confirmar_texto = re.compile(
        r"confirmar\s+la\s+eliminaci[oó]n|confirmar\s+eliminaci",
        re.I,
    )

    try:
        page.wait_for_load_state("domcontentloaded", timeout=12_000)
    except Exception:
        pass

    if page.locator("main").count():
        main_scope = page.locator("main")
        main = main_scope.first
    else:
        main_scope = page.locator("body")
        main = main_scope

    def _texto_panel_eliminar() -> str:
        loc = main_scope.get_by_role("button", name=rx_eliminar)
        if loc.count() == 0:
            loc = main_scope.get_by_role("button", name=rx_remove)
        if loc.count() == 0:
            loc = main_scope.get_by_role("link", name=rx_eliminar)
        if loc.count() == 0:
            return ""
        try:
            n = loc.count()
            for i in range(n - 1, -1, -1):
                b = loc.nth(i)
                if not b.is_visible(timeout=500):
                    continue
                return (
                    b.evaluate(
                        """el => {
                          let p = el.closest('article') || el.closest('section')
                            || el.closest('[class*=card]') || el.parentElement?.parentElement;
                          return p ? (p.innerText || '') : '';
                        }"""
                    )
                    or ""
                )
        except Exception:
            return ""
        return ""

    def _confirmar_eliminacion_todavia_visible() -> bool:
        """True si el flujo de confirmación sigue en pantalla (clic sin efecto o overlay de cookies)."""
        try:
            if page.is_closed():
                return False
        except PWErr:
            return False
        try:
            for rx in (rx_confirmar, rx_confirm_en):
                loc = main_scope.get_by_role("button", name=rx)
                n = loc.count()
                for i in range(n):
                    try:
                        if loc.nth(i).is_visible(timeout=400):
                            return True
                    except Exception:
                        pass
                loc_l = main_scope.get_by_role("link", name=rx)
                n2 = loc_l.count()
                for i in range(n2):
                    try:
                        if loc_l.nth(i).is_visible(timeout=400):
                            return True
                    except Exception:
                        pass
            tloc = main_scope.get_by_text(rx_confirmar_texto)
            for i in range(min(tloc.count(), 6)):
                try:
                    if tloc.nth(i).is_visible(timeout=400):
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False

    def _pulsar_confirmacion() -> bool:
        """El botón rojo puede tardar en el DOM; a veces no expone bien el nombre accesible."""
        deadline = time.time() + 16.0
        while time.time() < deadline:
            candidatos = []
            try:
                for rx in (rx_confirmar, rx_confirm_en):
                    loc = main_scope.get_by_role("button", name=rx)
                    n = loc.count()
                    for i in range(n - 1, -1, -1):
                        lb = loc.nth(i)
                        try:
                            if lb.is_visible(timeout=450):
                                candidatos.append(lb)
                        except Exception:
                            pass
                    loc_l = main_scope.get_by_role("link", name=rx)
                    n2 = loc_l.count()
                    for i in range(n2 - 1, -1, -1):
                        lb = loc_l.nth(i)
                        try:
                            if lb.is_visible(timeout=450):
                                candidatos.append(lb)
                        except Exception:
                            pass
            except Exception:
                pass

            try:
                txt_nodes = main_scope.get_by_text(rx_confirmar_texto)
                for i in range(min(txt_nodes.count(), 8)):
                    t = txt_nodes.nth(i)
                    try:
                        if not t.is_visible(timeout=400):
                            continue
                        anc = t.locator(
                            'xpath=ancestor-or-self::button[1] | '
                            'ancestor-or-self::*[@role="button"][1]'
                        )
                        if anc.count():
                            a0 = anc.first
                            if a0.is_visible(timeout=400):
                                candidatos.append(a0)
                    except Exception:
                        continue
            except Exception:
                pass

            try:
                bf = main_scope.locator("button,a[role='button']").filter(
                    has_text=rx_confirmar_texto
                )
                n = bf.count()
                for i in range(n - 1, -1, -1):
                    b = bf.nth(i)
                    try:
                        if b.is_visible(timeout=450):
                            candidatos.append(b)
                    except Exception:
                        pass
            except Exception:
                pass

            for btn in candidatos:
                try:
                    btn.scroll_into_view_if_needed(timeout=6000)
                    btn.click(timeout=10_000, force=True)
                    time.sleep(0.72)
                    if _confirmar_eliminacion_todavia_visible():
                        try:
                            btn.scroll_into_view_if_needed(timeout=4000)
                            btn.click(timeout=10_000, force=True)
                        except Exception:
                            pass
                        time.sleep(0.65)
                    if _confirmar_eliminacion_todavia_visible():
                        continue
                    time.sleep(max(0.12, pausa_s * 0.45))
                    return True
                except Exception:
                    continue
            time.sleep(0.42)
        return False

    def _pulsar_eliminar_del_plan() -> bool:
        bases = (
            main_scope.get_by_role("button", name=rx_eliminar),
            main_scope.get_by_role("button", name=rx_remove),
            main_scope.get_by_role("link", name=rx_eliminar),
            main_scope.get_by_role("link", name=rx_remove),
        )
        for base in bases:
            try:
                n = base.count()
            except Exception:
                continue
            for i in range(n - 1, -1, -1):
                try:
                    b = base.nth(i)
                    if not b.is_visible(timeout=900):
                        continue
                    b.scroll_into_view_if_needed(timeout=6000)
                    b.click(timeout=8000, force=True)
                    time.sleep(max(pausa_s, 0.55) + 0.45)
                    if _pulsar_confirmacion():
                        return True
                except Exception:
                    continue
        if _pulsar_confirmacion():
            return True
        return False

    def _fila_correcta() -> bool:
        t = _texto_panel_eliminar()
        return obj_cf in t.casefold() if t else False

    # 1) Clic directo si el correo ya es visible (fila colapsada con email)
    try:
        cand = main.get_by_text(email_objetivo, exact=True)
        for i in range(min(cand.count(), 8)):
            try:
                el = cand.nth(i)
                if not el.is_visible(timeout=700):
                    continue
                el.scroll_into_view_if_needed(timeout=4000)
                el.click(timeout=4000)
                time.sleep(pausa_s + 0.2)
                if _fila_correcta() and _pulsar_eliminar_del_plan():
                    return True
            except PWErr:
                return False
            except Exception:
                continue
    except PWErr:
        return False
    except Exception:
        pass

    # 2) Recorrer filas por «Activa» (cada miembro del plan)
    try:
        act_loc = main.get_by_text(re.compile(r"^\s*Activa\s*$"), exact=False)
        n_act = min(act_loc.count(), 12)
    except Exception:
        n_act = 0

    for idx in range(n_act):
        act = None
        try:
            act = act_loc.nth(idx)
            if not act.is_visible(timeout=900):
                continue
            act.scroll_into_view_if_needed(timeout=5000)
            act.click(timeout=5000)
            time.sleep(pausa_s + 0.28)
        except PWErr:
            return False
        except Exception:
            continue

        if _fila_correcta():
            if _pulsar_eliminar_del_plan():
                return True

        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(0.15)
        if act is not None:
            try:
                act.click(timeout=2500)
            except Exception:
                pass
        time.sleep(0.22)

    return False


def eliminar_miembro_plan_familiar_con_reintentos(
    page,
    email_objetivo: str,
    intentos: int = 2,
    pausa_s: float = 0.45,
) -> bool:
    for _ in range(max(1, intentos)):
        if eliminar_miembro_plan_familiar_tid(page, email_objetivo, pausa_s=pausa_s):
            return True
        time.sleep(pausa_s)
    return False


def _esperar_carga_post_goto_tid(
    page,
    *,
    timeout_dom_ms: int = 24_000,
    margen_s: float = 0.5,
) -> None:
    """
    Tras page.goto: espera domcontentloaded/load y un margen fijo para iframes y JS de TIDAL.
    """
    from playwright.sync_api import Error as PWErr

    try:
        if page.is_closed():
            return
    except PWErr:
        return
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_dom_ms)
    except Exception:
        pass
    try:
        if page.is_closed():
            return
    except PWErr:
        return
    try:
        page.wait_for_load_state("load", timeout=min(14_000, timeout_dom_ms))
    except Exception:
        pass
    if margen_s > 0:
        time.sleep(margen_s)


def comprobar_y_reintentar_ventanas_fallidas(
    trabajos: list[dict],
    url_familia: str,
    cfg_captcha: CaptchaTidManejoCfg,
) -> None:
    """
    Identifica las ventanas que no pudieron iniciar sesión (que siguen en la página de login o bloqueadas)
    y ofrece reintentar el login completo para ponerlas al día.
    """
    from playwright.sync_api import Error as PWErr
    import sys
    import time
    
    ventanas_fallidas = []
    for t in trabajos:
        page = t["page"]
        try:
            if page.is_closed():
                continue
            url = page.url
            if (_es_pagina_login(page) or 
                "captcha-delivery.com" in url or 
                "pricing" in url or 
                url.strip("/") in ("https://tidal.com", "http://tidal.com", "https://www.tidal.com") or
                _motivo_bloqueo_tid(page) is not None):
                ventanas_fallidas.append(t)
        except Exception:
            continue
            
    if not ventanas_fallidas:
        print("\n✅ Todas las ventanas iniciaron sesión con éxito (página de login superada).")
        return
        
    print(f"\n⚠️  Se detectaron {len(ventanas_fallidas)} ventana(s) que NO pudieron iniciar sesión:")
    for t in ventanas_fallidas:
        try:
            url_actual = t["page"].url
        except Exception:
            url_actual = "Desconocida"
        print(f"   • Ventana [{t['n']}] — {t['perfil']} (URL actual: {url_actual})")
        
    # Preguntar si se desea intentar re-iniciar sesión
    intentar = False
    try:
        if sys.stdin.isatty():
            res = input("\n¿Deseas intentar re-iniciar sesión automáticamente en estas ventanas? (si/no): ").strip().lower()
            if res in ("si", "sí", "yes", "s", "y"):
                intentar = True
        else:
            # En modo no interactivo, reintentamos automáticamente
            print("\n(Entrada no interactiva: reintentando login automático en ventanas fallidas...)")
            intentar = True
    except EOFError:
        intentar = True

    if intentar:
        print("\n=== Iniciando barrido de re-login automático ===")
        
        # Primero, comprobar y resolver captchas en caso de que alguna estuviera bloqueada antes de empezar
        manejar_captcha_tid_si_aplica(ventanas_fallidas, "antes del re-login final", pausa_dom_s=0.2, cfg=cfg_captcha)
        
        for t in ventanas_fallidas:
            page, n, perfil = t["page"], t["n"], t["perfil"]
            print(f"\n  [{n}] {perfil}: Procesando re-login...")
            try:
                if page.is_closed():
                    print(f"    [{n}] {perfil}: ❌ Pestaña cerrada")
                    continue
                
                # Traer al frente para enfocar
                page.bring_to_front()
                time.sleep(0.3)
                
                # Usar la máquina de estados para llevarla a la fase 'familia' (login + navegación)
                if poner_ventana_al_dia(t, "familia"):
                    # Comprobar captcha después de re-loguear
                    manejar_captcha_tid_si_aplica(trabajos, f"re-login final ventana {n}", pausa_dom_s=0.2, cfg=cfg_captcha)
                    # Forzar navegación final
                    try:
                        page.goto(url_familia, wait_until="commit", timeout=25_000)
                        print(f"    [{n}] {perfil}: ✅ Re-login exitoso. Redirigido a Familia.")
                    except Exception:
                        pass
                else:
                    print(f"    [{n}] {perfil}: ❌ No se pudo completar el inicio de sesión automático.")
            except Exception as e:
                print(f"    [{n}] {perfil}: Error durante re-login — {e}")
                
        print("\n=== Barrido de re-login completado ===\n")


def generar_correo_con_puntos(base_email: str = "cakeseller1234@gmail.com") -> str:
    """
    Toma un correo base (por ejemplo cakeseller1234@gmail.com), extrae la parte del usuario
    e inserta puntos (.) de manera aleatoria en posiciones intermedias (sin duplicar puntos
    ni ponerlos al principio o al final).
    """
    if "@" not in base_email:
        return base_email
    username, domain = base_email.split("@", 1)
    if len(username) <= 1:
        return base_email
    
    chars = list(username)
    posiciones = list(range(1, len(username)))
    num_puntos = random.randint(1, min(4, len(posiciones)))
    
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
        
    return "".join(chars) + "@" + domain


def cambiar_correo_perfil_playwright(page, nuevo_correo: str) -> bool:
    """
    En account.tidal.com/profile:
    1. Hace clic en "Editar información".
    2. Espera a que aparezca el campo de correo electrónico.
    3. Rellena el correo electrónico con el nuevo_correo.
    4. Hace clic en "Guardar".
    5. Espera a que aparezca el mensaje de éxito.
    """
    from playwright.sync_api import Error as PWErr
    import time
    
    try:
        if page.is_closed():
            return False
            
        print(f"    [Playwright Correo] Navegando a perfil: https://account.tidal.com/profile")
        page.goto("https://account.tidal.com/profile", wait_until="domcontentloaded", timeout=30_000)
        time.sleep(1.5)
        
        aceptar_cookies_con_espera(page, intentos=2, pausa_s=0.15, esperar_networkidle=False)
        
        btn_editar = None
        rx_editar = re.compile(r"editar\s+información|editar\s+informacion|edit\s+information", re.I)
        for frame in _frames_visibles(page):
            loc = frame.locator('button, [role="button"], a').filter(has_text=rx_editar)
            if loc.count() > 0 and loc.first.is_visible():
                btn_editar = loc.first
                break
                
        if not btn_editar:
            for frame in _frames_visibles(page):
                loc = frame.get_by_text(rx_editar).first
                if loc.count() > 0 and loc.is_visible():
                    btn_editar = loc
                    anc = loc.locator('xpath=ancestor-or-self::button[1] | ancestor-or-self::a[1]').first
                    if anc.count() > 0:
                        btn_editar = anc
                    break
                    
        if not btn_editar:
            print("    [Playwright Correo] ❌ No se encontró el botón 'Editar información'.")
            return False
            
        print("    [Playwright Correo] Pulsando 'Editar información'...")
        btn_editar.click(timeout=5000, force=True)
        time.sleep(1.5)
        
        campo_email = None
        for attempt in range(15):
            for frame in _frames_visibles(page):
                selectores = [
                    'input[type="email"]',
                    'input[name="email"]',
                    'input[id*="email" i]',
                    'input[placeholder*="correo" i]',
                    'input[placeholder*="email" i]',
                ]
                for sel in selectores:
                    loc = frame.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        campo_email = loc
                        break
                if campo_email:
                    break
            if campo_email:
                break
            time.sleep(0.3)
            
        if not campo_email:
            print("    [Playwright Correo] ❌ No se encontró el campo de correo electrónico para editar.")
            return False
            
        print(f"    [Playwright Correo] Escribiendo nuevo correo: {nuevo_correo}")
        campo_email.click(timeout=3000)
        page.keyboard.press("Control+A")
        time.sleep(0.1)
        page.keyboard.press("Backspace")
        time.sleep(0.15)
        
        for char in nuevo_correo:
            page.keyboard.type(char)
            time.sleep(random.uniform(0.045, 0.125))
            
        time.sleep(0.5)
        
        btn_guardar = None
        rx_guardar = re.compile(r"guardar|save", re.I)
        for frame in _frames_visibles(page):
            loc = frame.locator('button, [role="button"], a, input[type="submit"]').filter(has_text=rx_guardar)
            if loc.count() > 0 and loc.first.is_visible():
                btn_guardar = loc.first
                break
                
        if not btn_guardar:
            for frame in _frames_visibles(page):
                loc = frame.get_by_text(rx_guardar).first
                if loc.count() > 0 and loc.is_visible():
                    btn_guardar = loc
                    anc = loc.locator('xpath=ancestor-or-self::button[1] | ancestor-or-self::a[1]').first
                    if anc.count() > 0:
                        btn_guardar = anc
                    break
                    
        if not btn_guardar:
            print("    [Playwright Correo] ❌ No se encontró el botón 'Guardar'. Intentando enviar con Enter...")
            page.keyboard.press("Enter")
            time.sleep(2.0)
        else: 
            print("    [Playwright Correo] Pulsando 'Guardar'...")
            btn_guardar.click(timeout=5000, force=True)
            time.sleep(2.0)
            
        rx_exito = re.compile(r"actualizada|updated|guardado|saved|información\s+de\s+perfil\s+actualizada", re.I)
        exito_detectado = False
        for attempt in range(20):
            for frame in _frames_visibles(page):
                loc = frame.get_by_text(rx_exito)
                if loc.count() > 0 and loc.first.is_visible():
                    exito_detectado = True
                    break
            if exito_detectado:
                break
            time.sleep(0.3)
            
        if exito_detectado:
            print("    [Playwright Correo] ✅ ¡Correo actualizado con éxito!")
            return True
        else:
            print("    [Playwright Correo] ⚠️ No se detectó el mensaje de éxito de actualización, revisa la ventana.")
            return False
            
    except Exception as e:
        print(f"    [Playwright Correo] ❌ Error al cambiar el correo: {e}")
        return False


def cambiar_correo_perfil_uia(wnd, nuevo_correo: str) -> bool:
    """
    En modo UIA subprocess:
    1. Va a la barra de direcciones con Ctrl+L y navega a https://account.tidal.com/profile.
    2. Espera a que cargue.
    3. Busca y hace clic en el botón "Editar información".
    4. Busca el campo de correo, borra su contenido, escribe nuevo_correo.
    5. Busca y hace clic en el botón "Guardar".
    """
    import time
    import random
    
    try:
        print("  [UIA Correo] Navegando a https://account.tidal.com/profile...")
        wnd.set_focus()
        wnd.type_keys("^l")
        time.sleep(0.3)
        wnd.type_keys("https://account.tidal.com/profile{ENTER}", with_spaces=True)
        time.sleep(4.5)
        
        _aceptar_cookies_uia(wnd)
        time.sleep(1.0)
        
        print("  [UIA Correo] Buscando botón 'Editar información'...")
        btn_editar = _buscar_control_uia(wnd, "Button", ["editar información", "editar informacion", "edit information", "editar"])
        if not btn_editar:
            for d in wnd.descendants():
                name = (d.element_info.name or "").lower()
                if "editar información" in name or "editar informacion" in name or "edit information" in name:
                    btn_editar = d
                    break
                    
        if not btn_editar:
            print("  ⚠️ [UIA Correo] No se encontró el botón 'Editar información'.")
            return False
            
        print("  [UIA Correo] Pulsando 'Editar información'...")
        btn_editar.click_input()
        time.sleep(2.0)
        
        print("  [UIA Correo] Buscando campo de correo electrónico...")
        campo_email = None
        for attempt in range(10):
            for d in wnd.descendants():
                ctrl_type = d.element_info.control_type
                if ctrl_type == "Edit":
                    name = (d.element_info.name or "").lower()
                    value = (d.get_value() or "").lower() if hasattr(d, "get_value") else ""
                    if "correo" in name or "email" in name or "@" in value:
                        campo_email = d
                        break
            if campo_email:
                break
            time.sleep(0.5)
            
        if not campo_email:
            print("  ⚠️ [UIA Correo] No se encontró el campo de correo electrónico.")
            return False
            
        print(f"  [UIA Correo] Escribiendo nuevo correo: {nuevo_correo}")
        campo_email.click_input()
        campo_email.type_keys("^a{BACKSPACE}")
        time.sleep(0.3)
        
        for char in nuevo_correo:
            delay = random.uniform(0.045, 0.125)
            time.sleep(delay)
            if char in ("+", "^", "%", "~", "(", ")", "{", "}"):
                wnd.type_keys(f"{{{char}}}")
            elif char == " ":
                wnd.type_keys("{SPACE}")
            else:
                wnd.type_keys(char, with_spaces=True)
                
        time.sleep(0.8)
        
        print("  [UIA Correo] Buscando botón 'Guardar'...")
        btn_guardar = _buscar_control_uia(wnd, "Button", ["guardar", "save"])
        if not btn_guardar:
            for d in wnd.descendants():
                name = (d.element_info.name or "").lower()
                if "guardar" in name or "save" in name:
                    btn_guardar = d
                    break
                    
        if btn_guardar:
            print("  [UIA Correo] Pulsando 'Guardar'...")
            btn_guardar.click_input()
            time.sleep(2.0)
        else:
            print("  ⚠️ [UIA Correo] No se encontró el botón 'Guardar'. Pulsando Enter...")
            wnd.type_keys("{ENTER}")
            time.sleep(2.0)
            
        print("  [UIA Correo] Esperando mensaje de confirmación...")
        exito = False
        for attempt in range(15):
            for d in wnd.descendants():
                name = (d.element_info.name or "").lower()
                if "actualizada" in name or "updated" in name or "guardado" in name or "exito" in name:
                    exito = True
                    break
            if exito:
                break
            time.sleep(0.5)
            
        if exito:
            print("  ✅ [UIA Correo] Correo cambiado con éxito en esta ventana.")
            return True
        else:
            print("  ⚠️ [UIA Correo] No se detectó mensaje de éxito. Revisa la ventana.")
            return False
            
    except Exception as e:
        print(f"  ❌ [UIA Correo] Error al cambiar correo: {e}")
        return False


def ejecutar_playwright(
    sesiones: list[dict],
    delay_entre_aperturas: float,
    espera_tras_abrir_todas: float,
    email_reintentos: int,
    email_pausa_s: float,
    cookie_reintentos: int,
    cookie_pausa_s: float,
    *,
    espera_post_goto_cada_ventana_s: float = 0.6,
    espera_tras_cookies_s: float = 0.65,
    continuar_reintentos: int = 10,
    continuar_pausa_s: float = 0.8,
    espera_tras_continuar_s: float = 2.5,
    password_reintentos: int = 10,
    password_pausa_s: float = 0.28,
    pausa_manual: str = "no",
    espera_tras_password_s: float = 1.2,
    iniciar_sesion_reintentos: int = 5,
    iniciar_sesion_pausa_s: float = 0.5,
    delay_entre_iniciar_sesion: float = 0.6,
    url_familia_tidal: str = DEFAULT_TIDAL_FAMILY_URL,
    delay_entre_familia: float = 0.08,
    captcha_tid: CaptchaTidManejoCfg | None = None,
    omitir_fase_eliminar_miembros: bool = False,
    eliminar_miembro_reintentos: int = 2,
    eliminar_miembro_pausa_s: float = 0.45,
    delay_entre_eliminar_miembro: float = 0.25,
    usar_incognito: bool = False,
    cambiar_correo: bool = False,
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Falta Playwright. Instala dependencias en esta carpeta:\n"
            "  pip install -r requirements.txt\n"
            "  playwright install chrome\n"
        )
        sys.exit(1)

    contextos_abiertos: list = []
    total = len(sesiones)
    with sync_playwright() as p:
        # Un solo Chrome + varios contextos: cada uno es una sesión privada aislada
        # (equivalente práctico a incógnito). Evita exitCode 21 por User Data bloqueado
        # o por mezclar --incognito con launch_persistent_context en el perfil real.
        try:
            launch_args = ["--disable-blink-features=AutomationControlled"]
            if usar_incognito:
                launch_args.append("--incognito")
            browser = p.chromium.launch(
                channel="chrome",
                headless=False,
                args=launch_args,
            )
        except Exception as e_inc:
            if usar_incognito:
                print(
                    f"Aviso: Chrome no arrancó con --incognito ({e_inc}). "
                    "Reintentando sin ese flag (ventanas igual aisladas por contexto).\n"
                )
            try:
                browser = p.chromium.launch(
                    channel="chrome",
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception as e:
                print(f"No se pudo iniciar Chrome: {e}")
                sys.exit(1)

        try:
            pm = _normalizar_pausa_manual(pausa_manual)
            trabajos: list[dict] = []
            for i, s in enumerate(sesiones):
                url = s["url"]
                perfil = s["perfil"]
                email = s["email"]
                print(f"\n[Fase 1 — abrir] [{i + 1}/{total}] {perfil}")

                try:
                    context = browser.new_context(
                        viewport={"width": 1280, "height": 800},
                        locale="es-ES",
                    )
                    # Inyectar script de stealth para enmascarar señales de automatización (CDP/Playwright)
                    context.add_init_script("""
                        // Ocultar la propiedad navigator.webdriver
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined
                        });

                        // Simular el objeto window.chrome estándar de Google Chrome
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

                        // Parchear navigator.permissions.query (comportamiento típico detectado por DataDome)
                        const originalQuery = window.navigator.permissions.query;
                        window.navigator.permissions.query = (parameters) => (
                            parameters.name === 'notifications'
                                ? Promise.resolve({ state: Notification.permission })
                                : originalQuery(parameters)
                        );

                        // Asegurar navigator.plugins y navigator.mimeTypes estándar
                        if (!navigator.plugins || navigator.plugins.length === 0) {
                            Object.defineProperty(navigator, 'plugins', {
                                get: () => [
                                    { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                                    { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }
                                ]
                            });
                        }
                    """)
                except Exception as e:
                    print(f"  No se pudo crear el contexto: {e}")
                    continue

                page = context.new_page()
                try:
                    navegar_con_bypass_referencia(page, url)
                except Exception as e_nav:
                    print(f"  Navegación falló: {e_nav}")
                    try:
                        context.close()
                    except Exception:
                        pass
                    continue

                _esperar_carga_post_goto_tid(
                    page,
                    margen_s=max(0.0, espera_post_goto_cada_ventana_s),
                )

                contextos_abiertos.append(context)
                trabajos.append(
                    {
                        "page": page,
                        "context": context,
                        "email": email,
                        "password": s.get("password", ""),
                        "perfil": perfil,
                        "n": i + 1,
                        "eliminar_miembro": (s.get("eliminar_miembro") or "").strip(),
                        "url": url,
                        "url_familia": url_familia_tidal,
                    }
                )
                if i < total - 1 and delay_entre_aperturas > 0:
                    time.sleep(delay_entre_aperturas)

            if not contextos_abiertos and sesiones:
                print("Ninguna sesión pudo abrirse con Playwright.")
                return
            if not trabajos:
                return

            cfg_captcha = captcha_tid or CaptchaTidManejoCfg()

            print(
                "\n  Última pasada de carga en cada pestaña (las primeras suelen haber terminado antes)…"
            )
            for t in trabajos:
                try:
                    _esperar_carga_post_goto_tid(
                        t["page"],
                        timeout_dom_ms=12_000,
                        margen_s=min(0.42, max(0.12, espera_post_goto_cada_ventana_s * 0.4)),
                    )
                except Exception:
                    pass

            print(
                f"\n[Fase 1 lista] {len(trabajos)} ventana(s). "
                f"Pausa global {espera_tras_abrir_todas:g}s para iframes / formularios…"
            )
            time.sleep(espera_tras_abrir_todas)

            if pm in ("solo-abrir", "abrir-y-cookies"):
                pausa_manual_forzada("Tras abrir todas las ventanas (antes del correo)")

            comprobar_captcha_post_fase(
                trabajos,
                "después de fase 1 — antes del correo",
                pausa_s=0.18,
                captcha_cfg=cfg_captcha,
            )

            print("\n=== Fase 2 — correo en cada ventana ===")
            for t in trabajos:
                perfil, email, page, n = t["perfil"], t["email"], t["page"], t["n"]
                print(f"\n  [{n}/{total}] {perfil}")
                if not email:
                    print("  (sin correo en emails.txt para esta línea)")
                    continue
                if rellenar_email_con_reintentos(
                    page,
                    email,
                    intentos=email_reintentos,
                    pausa_s=email_pausa_s,
                    fase_rapida=True,
                ):
                    print(f"  OK correo: {email}")
                else:
                    print(
                        f"  No se pudo rellenar: {email} "
                        "(sube --email-reintentos o --espera-tras-abrir si falla a menudo)."
                    )

            comprobar_captcha_post_fase(
                trabajos, "después de fase 2 — correo", pausa_s=0.18, captcha_cfg=cfg_captcha
            )

            print("\n=== Fase 3 — cookies, una ventana tras otra ===")
            for t in trabajos:
                perfil, page, n = t["perfil"], t["page"], t["n"]
                print(f"\n  [{n}/{total}] cookies — {perfil}")
                if aceptar_cookies_con_espera(
                    page,
                    intentos=cookie_reintentos,
                    pausa_s=cookie_pausa_s,
                    esperar_networkidle=False,
                ):
                    print("  OK cookies (Aceptar o equivalente).")
                else:
                    print("  Sin banner reconocido o ya aceptado.")

            print(
                f"\nPausa {espera_tras_cookies_s:g}s tras cookies, antes de «Continuar»…"
            )
            time.sleep(espera_tras_cookies_s)

            if pm in ("solo-cookies", "abrir-y-cookies"):
                pausa_manual_forzada("Tras cookies (antes de «Continuar»)")

            comprobar_captcha_post_fase(
                trabajos, "después de fase 3 — cookies", pausa_s=0.18, captcha_cfg=cfg_captcha
            )

            print("\n=== Fase 4 — Continuar en cada ventana ===")
            for t in trabajos:
                perfil, page, n = t["perfil"], t["page"], t["n"]
                print(f"\n  [{n}/{total}] Continuar — {perfil}")
                if pulsar_continuar_con_reintentos(
                    page,
                    intentos=continuar_reintentos,
                    pausa_s=continuar_pausa_s,
                ):
                    print("  OK «Continuar».")
                else:
                    print(
                        "  No se encontró el botón Continuar habilitado "
                        "(¿correo incompleto o otro idioma?)."
                    )

            comprobar_captcha_post_fase(
                trabajos, "después de fase 4 — Continuar", pausa_s=0.18, captcha_cfg=cfg_captcha
            )

            espera_password_visible_s = max(espera_tras_continuar_s, 6.0)
            print("\n  [Espera] Esperando 2 segundos tras finalizar el bloque de cuentas (correo)...")
            time.sleep(2.0)

            print("\n=== Fase 5 — contraseña en cada ventana (no se muestra en consola) ===")
            for t in trabajos:
                perfil, page, n, pwd = t["perfil"], t["page"], t["n"], t.get("password", "")
                print(f"\n  [{n}/{total}] contraseña — {perfil}")
                if not pwd:
                    print("  (sin contraseña en passwords.txt para esta línea)")
                    continue
                
                # Si el campo de contraseña no está visible, poner al día la ventana primero (en caso de reset por captcha)
                if not esperar_campo_password(page, timeout_s=0.8):
                    poner_ventana_al_dia(t, "password")
                    
                if not recuperar_pantalla_password_si_error(
                    page,
                    continuar_reintentos=min(continuar_reintentos, 6),
                    continuar_pausa_s=continuar_pausa_s,
                    espera_password_s=espera_password_visible_s,
                ):
                    if _login_tid_muestra_error(page):
                        print(
                            "  Error TIDAL («Algo salió mal»): no apareció el campo de contraseña."
                        )
                    else:
                        print(
                            "  No apareció el campo de contraseña a tiempo "
                            f"(espera ~{espera_password_visible_s:g}s por ventana)."
                        )
                    continue
                if rellenar_password_con_reintentos(
                    page,
                    pwd,
                    intentos=password_reintentos,
                    pausa_s=password_pausa_s,
                    fase_rapida=True,
                ):
                    print("  OK contraseña escrita y verificada en el campo.")
                else:
                    print(
                        "  No se pudo escribir o verificar la contraseña "
                        "(sube --password-reintentos o revisa la ventana)."
                    )

            comprobar_captcha_post_fase(
                trabajos, "después de fase 5 — contraseña", pausa_s=0.18, captcha_cfg=cfg_captcha
            )

            print(
                f"\nPausa {espera_tras_password_s:g}s antes de «Inicia sesión» en todas las ventanas…"
            )
            time.sleep(espera_tras_password_s)

            print("\n=== Fase 6 — Inicia sesión (rápido, ventana por ventana) ===")
            for j, t in enumerate(trabajos):
                perfil, page, n = t["perfil"], t["page"], t["n"]
                print(f"  [{n}/{total}] Inicia sesión — {perfil}", flush=True)
                
                # Si la sesión se reinició por completo o se deslogueó
                if not esperar_campo_password(page, timeout_s=0.5):
                    if _es_pagina_login(page) or not ("account.tidal.com" in page.url or "listen.tidal.com" in page.url):
                        poner_ventana_al_dia(t, "password")
                        
                if pulsar_iniciar_sesion_con_reintentos(
                    page,
                    intentos=iniciar_sesion_reintentos,
                    pausa_s=iniciar_sesion_pausa_s,
                ):
                    print("    OK")
                else:
                    # Comprobar si ya está logueado
                    if not _es_pagina_login(page):
                        print("    OK (ya logueado)")
                    else:
                        print("    No encontrado o botón deshabilitado (revisa la ventana).")
                
                # Intentar pulsar 'Sí, continuar' si aparece la pantalla de consentimiento/autorización
                print("    Esperando pantalla de consentimiento 'Sí, continuar'...")
                if pulsar_si_continuar_auth(page, timeout_s=6.0):
                    print("    OK 'Sí, continuar' pulsado o ya redirigido. Esperando carga y redirigiendo de frente a Familia...")
                    time.sleep(3.0)
                    url_fam = t.get("url_familia", DEFAULT_TIDAL_FAMILY_URL)
                    try:
                        page.goto(url_fam, wait_until="commit", timeout=25_000)
                    except Exception:
                        try:
                            page.goto(url_fam, wait_until="domcontentloaded", timeout=20_000)
                        except Exception:
                            pass
                else:
                    print("    No se requirió o no se encontró 'Sí, continuar'.")

                if j < len(trabajos) - 1 and delay_entre_iniciar_sesion > 0:
                    time.sleep(delay_entre_iniciar_sesion)

            comprobar_captcha_post_fase(
                trabajos,
                "después de fase 6 — Inicia sesión",
                pausa_s=0.2,
                captcha_cfg=cfg_captcha,
            )

            print("\n  [Espera] Esperando 2 segundos tras finalizar el bloque de iniciar sesión...")
            time.sleep(2.0)

            print("\n=== Fase 7 — página Familia TIDAL (rápido, ventana por ventana) ===")
            print(f"  URL: {url_familia_tidal}")
            for j, t in enumerate(trabajos):
                perfil, page, n = t["perfil"], t["page"], t["n"]
                print(f"  [{n}/{total}] familia — {perfil}", flush=True)
                
                # Si se cerró la sesión o se reinició por completo
                if _es_pagina_login(page) or not ("account.tidal.com" in page.url or "listen.tidal.com" in page.url):
                    print(f"    [{n}] {perfil}: Sesión cerrada o fuera de cuenta. Iniciando sesión de nuevo...")
                    poner_ventana_al_dia(t, "iniciar_sesion")
                    
                try:
                    page.goto(url_familia_tidal, wait_until="commit", timeout=45_000)
                except Exception:
                    try:
                        page.goto(url_familia_tidal, wait_until="domcontentloaded", timeout=45_000)
                    except Exception as e_nav:
                        print(f"    Error: {e_nav}")
                if j < len(trabajos) - 1 and delay_entre_familia > 0:
                    time.sleep(delay_entre_familia)

            comprobar_captcha_post_fase(
                trabajos,
                "después de fase 7 — página Familia",
                pausa_s=0.25,
                captcha_cfg=cfg_captcha,
            )

            if not omitir_fase_eliminar_miembros and any(
                (t.get("eliminar_miembro") or "").strip() for t in trabajos
            ):
                print(
                    "\n=== Fase 8 — eliminar miembro del plan Familiar ==="
                )
                for j, t in enumerate(trabajos):
                    perfil, page, n = t["perfil"], t["page"], t["n"]
                    emiem = (t.get("eliminar_miembro") or "").strip()
                    if not emiem:
                        print(f"  [{n}/{total}] {perfil}: (sin correo a eliminar en esta línea del archivo)")
                        continue
                    print(f"  [{n}/{total}] {perfil} — eliminar miembro: {emiem}", flush=True)
                    try:
                        if page.is_closed():
                            print("    pestaña cerrada, omitida.")
                            continue
                        if eliminar_miembro_plan_familiar_con_reintentos(
                            page,
                            emiem,
                            intentos=eliminar_miembro_reintentos,
                            pausa_s=eliminar_miembro_pausa_s,
                        ):
                            print(
                                "    OK: se pulsó confirmar eliminación; "
                                "verifica en TIDAL que el miembro ya no figure en la lista."
                            )
                        else:
                            print(
                                "    No se pudo completar (¿lista distinta, idioma o ya eliminado?)."
                            )
                    except Exception as e:
                        print(f"    Error: {e}")
                    if j < len(trabajos) - 1 and delay_entre_eliminar_miembro > 0:
                        time.sleep(delay_entre_eliminar_miembro)

                comprobar_captcha_post_fase(
                    trabajos,
                    "después de fase 8 — eliminar miembro",
                    pausa_s=0.3,
                    captcha_cfg=cfg_captcha,
                )

            if cambiar_correo:
                print("\n=== Fase 8B — cambiar correo de información del perfil ===")
                for j, t in enumerate(trabajos):
                    perfil, page, n = t["perfil"], t["page"], t["n"]
                    email_con_puntos = generar_correo_con_puntos("cakeseller1234@gmail.com")
                    print(f"  [{n}/{total}] {perfil} — Cambiando correo a: {email_con_puntos}", flush=True)
                    try:
                        if page.is_closed():
                            print("    pestaña cerrada, omitida.")
                            continue
                        if cambiar_correo_perfil_playwright(page, email_con_puntos):
                            print("    OK: correo cambiado.")
                        else:
                            print("    No se pudo completar el cambio de correo.")
                    except Exception as e:
                        print(f"    Error: {e}")
                    if j < len(trabajos) - 1:
                        time.sleep(1.0)

                comprobar_captcha_post_fase(
                    trabajos,
                    "después de fase 8B — cambiar correo",
                    pausa_s=0.3,
                    captcha_cfg=cfg_captcha,
                )

            # Opcional: Barrido final para verificar e intentar loguear las ventanas que fallaron
            comprobar_y_reintentar_ventanas_fallidas(trabajos, url_familia_tidal, cfg_captcha)

            print(
                "\nListo. Revisa cada ventana por si TIDAL pide un paso extra (captcha, 2FA, etc.).\n"
                "Cuando hayas verificado todo, escribe 'si' y pulsa Enter para cerrar las ventanas."
            )
            try:
                if sys.stdin.isatty():
                    while True:
                        respuesta = input("\n¿Cerrar todas las ventanas de Chrome? Escribe 'si' para confirmar: ").strip().lower()
                        if respuesta in ("si", "sí", "yes", "s", "y"):
                            break
                        print("  (No se cerraron las ventanas. Sigue revisando y escribe 'si' cuando estés listo.)")
                else:
                    print("\n(Entrada no interactiva: esperando 120 s antes de cerrar ventanas.)")
                    time.sleep(120)
            except EOFError:
                time.sleep(30)
            for ctx in contextos_abiertos:
                try:
                    ctx.close()
                except Exception:
                    pass
            print("Instancias cerradas.")
        finally:
            try:
                browser.close()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Abre TIDAL en Chrome (ventanas privadas): una ventana por correo en emails.txt, "
            "login automatizado, página Familia y opcionalmente fase 8 para eliminar un miembro del plan. "
            "LINKS.txt alinea URL/perfil por línea (opcional: menos líneas que correos = URL por defecto). "
            "Playwright no usa perfiles reales de Chrome."
        )
    )
    parser.add_argument(
        "--solo-subprocess",
        action="store_true",
        help="Solo abrir Chrome (comportamiento antiguo), sin Playwright ni relleno de email.",
    )
    parser.add_argument(
        "--links",
        type=Path,
        default=DEFAULT_LINKS_FILE,
        help=(
            f"URL y perfil por línea, alineado con cada correo de emails.txt (por defecto: "
            f"{DEFAULT_LINKS_FILE.name}). Puede tener menos líneas que correos (se rellena con URL "
            f"por defecto y Perfil-N) o más (se ignoran las sobrantes)."
        ),
    )
    parser.add_argument(
        "--emails",
        type=Path,
        default=DEFAULT_EMAILS_FILE,
        help=(
            f"Un correo por línea: define cuántas ventanas se abren (por defecto: {DEFAULT_EMAILS_FILE.name}). "
            "Cada línea alinea contraseña, LINKS y eliminar-miembros en la misma posición."
        ),
    )
    parser.add_argument(
        "--passwords",
        type=Path,
        default=DEFAULT_PASSWORDS_FILE,
        help=f"Una contraseña por línea, mismo orden que los correos (por defecto: {DEFAULT_PASSWORDS_FILE.name})",
    )
    parser.add_argument(
        "--delay-apertura",
        type=float,
        default=0.22,
        help="Playwright, fase 1: pausa entre abrir una ventana y la siguiente (por defecto 0.22).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Solo con --solo-subprocess: segundos entre cada lanzamiento de Chrome.",
    )
    parser.add_argument(
        "--espera-tras-abrir",
        "--espera-tras-relleno",
        type=float,
        default=2.75,
        dest="espera_tras_abrir",
        help=(
            "Playwright: segundos de espera global tras abrir todas las ventanas, antes de la fase 2 "
            "(por defecto 2.75). Sube a 4–6 si TIDAL o la red cargan muy lento."
        ),
    )
    parser.add_argument(
        "--espera-post-goto",
        type=float,
        default=0.6,
        dest="espera_post_goto",
        help=(
            "Playwright, fase 1: margen en segundos tras domcontentloaded/load en cada ventana "
            "al abrir la URL (por defecto 0.6). Sube a 1–1.5 si el formulario de correo tarda en aparecer."
        ),
    )
    parser.add_argument(
        "--email-reintentos",
        type=int,
        default=2,
        help="Playwright, fase 2: intentos por ventana para el campo de correo (por defecto 2; sube si falla).",
    )
    parser.add_argument(
        "--email-pausa",
        type=float,
        default=0.02,
        help="Playwright, fase 2: segundos entre reintentos de correo (por defecto 0.02).",
    )
    parser.add_argument(
        "--cookie-reintentos",
        type=int,
        default=3,
        help="Playwright, fase 3: intentos por ventana para el banner de cookies (por defecto 3; sube si falla).",
    )
    parser.add_argument(
        "--cookie-pausa",
        type=float,
        default=0.06,
        help="Playwright, fase 3: segundos entre intentos de cookies (por defecto 0.06).",
    )
    parser.add_argument(
        "--espera-tras-cookies",
        type=float,
        default=0.65,
        dest="espera_tras_cookies",
        help="Playwright: segundos tras la fase de cookies, antes de pulsar Continuar (por defecto 0.65).",
    )
    parser.add_argument(
        "--espera-tras-continuar",
        type=float,
        default=2.5,
        dest="espera_tras_continuar",
        help=(
            "Playwright: tiempo base para esperar el campo de contraseña por ventana "
            "(mín. 6 s efectivos; por defecto 1.25)."
        ),
    )
    parser.add_argument(
        "--continuar-reintentos",
        type=int,
        default=10,
        help="Playwright, fase 4: intentos por ventana para el botón Continuar (por defecto 10).",
    )
    parser.add_argument(
        "--continuar-pausa",
        type=float,
        default=0.8,
        help="Playwright, fase 4: segundos entre reintentos de Continuar (por defecto 0.8).",
    )
    parser.add_argument(
        "--password-reintentos",
        type=int,
        default=10,
        help="Playwright, fase 5: intentos por ventana para el campo de contraseña (por defecto 10).",
    )
    parser.add_argument(
        "--password-pausa",
        type=float,
        default=0.28,
        help="Playwright, fase 5: segundos entre reintentos de contraseña (por defecto 0.28).",
    )
    parser.add_argument(
        "--pausa-manual",
        choices=["no", "solo-abrir", "solo-cookies", "abrir-y-cookies", "forzosa"],
        default="no",
        help=(
            "Detiene el script hasta Enter en la consola: solo-abrir = tras abrir ventanas (antes del correo); "
            "solo-cookies = tras cookies (antes de Continuar); abrir-y-cookies o forzosa = ambas pausas "
            "(útil si aparece captcha y quieres resolverlo a mano antes de seguir)."
        ),
    )
    parser.add_argument(
        "--espera-tras-password",
        type=float,
        default=1.2,
        dest="espera_tras_password",
        help="Playwright: segundos tras rellenar contraseñas, antes de pulsar Inicia sesión (por defecto 1.2).",
    )
    parser.add_argument(
        "--iniciar-sesion-reintentos",
        type=int,
        default=5,
        help="Playwright, fase 6: intentos por ventana para el botón Inicia sesión (por defecto 5).",
    )
    parser.add_argument(
        "--iniciar-sesion-pausa",
        type=float,
        default=0.5,
        help="Playwright, fase 6: segundos entre reintentos dentro de la misma ventana (por defecto 0.5).",
    )
    parser.add_argument(
        "--delay-iniciar-sesion",
        type=float,
        default=0.6,
        dest="delay_iniciar_sesion",
        help="Playwright, fase 6: pausa entre ventana y ventana al pulsar Inicia sesión (por defecto 0.6).",
    )
    parser.add_argument(
        "--url-familia-tidal",
        type=str,
        default=DEFAULT_TIDAL_FAMILY_URL,
        dest="url_familia_tidal",
        help=f"Playwright, fase 7: URL a abrir en cada ventana al final (por defecto: {DEFAULT_TIDAL_FAMILY_URL}).",
    )
    parser.add_argument(
        "--delay-familia",
        type=float,
        default=0.08,
        dest="delay_familia",
        help="Playwright, fase 7: pausa entre cada goto a la página familia (por defecto 0.08).",
    )
    parser.add_argument(
        "--sin-surfshark-captcha",
        action="store_true",
        default=True,  # Cambiado a True por defecto - ahora es manual
        help=(
            "Modo manual: tras detectar antibot TIDAL o 403 CloudFront, el script se detiene "
            "para intervención manual en lugar de usar Surfshark automático."
        ),
    )
    parser.add_argument(
        "--surfshark-exe",
        type=Path,
        default=None,
        dest="surfshark_exe",
        help="Ruta a Surfshark.exe si no está en Program Files (solo Windows).",
    )
    parser.add_argument(
        "--captcha-espera-surfshark",
        type=float,
        default=4.0,
        dest="captcha_espera_surfshark_interna",
        metavar="S",
        help="Segundos entre Desconectar y Conexión rápida en Surfshark (por defecto 4).",
    )
    parser.add_argument(
        "--captcha-espera-tras-surfshark",
        type=float,
        default=7.0,
        dest="captcha_espera_tras_surfshark",
        metavar="S",
        help=(
            "Segundos tras Surfshark (o tras omitirlo) antes de recargar todas las ventanas TIDAL "
            "(por defecto 7; da tiempo a que el formulario de correo quede estable)."
        ),
    )
    parser.add_argument(
        "--captcha-timeout-surfshark-botones",
        type=float,
        default=15.0,
        dest="captcha_timeout_surfshark",
        metavar="S",
        help="Timeout (s) para localizar cada botón en Surfshark (por defecto 15).",
    )
    parser.add_argument(
        "--eliminar-miembros",
        type=Path,
        default=None,
        dest="eliminar_miembros",
        help=(
            "Un correo de miembro a eliminar por línea, mismo orden que LINKS/emails. "
            "Si omites esta opción, se busca en la carpeta del script, en este orden: "
            "eliminar_miembros.txt, «Eliminar miembros.txt», eliminar miembros.txt, Eliminar_miembros.txt."
        ),
    )
    parser.add_argument(
        "--omitir-fase-eliminar-miembros",
        action="store_true",
        dest="omitir_fase_eliminar_miembros",
        help="No ejecutar la fase 8 (eliminar miembro del plan Familiar en TIDAL).",
    )
    parser.add_argument(
        "--cambiar-correo",
        action="store_true",
        dest="cambiar_correo",
        help="Cambiar el correo de información del perfil por cakeseller1234@gmail.com (con puntos aleatorios).",
    )
    parser.add_argument(
        "--usar-incognito",
        action="store_true",
        dest="usar_incognito",
        help="Iniciar Chrome en modo incógnito explícito (puede causar bloqueos anti-bot en algunas redes).",
    )
    parser.add_argument(
        "--eliminar-miembro-reintentos",
        type=int,
        default=2,
        dest="eliminar_miembro_reintentos",
        help="Fase 8: reintentos del flujo expandir + eliminar + confirmar (por defecto 2).",
    )
    parser.add_argument(
        "--eliminar-miembro-pausa",
        type=float,
        default=0.45,
        dest="eliminar_miembro_pausa",
        help="Fase 8: segundos entre pasos de la UI (por defecto 0.45).",
    )
    parser.add_argument(
        "--delay-eliminar-miembro",
        type=float,
        default=0.25,
        dest="delay_eliminar_miembro",
        help="Fase 8: pausa entre ventana y ventana (por defecto 0.25).",
    )
    args = parser.parse_args()

    # Opción interactiva al inicio
    args.cambiar_correo = getattr(args, "cambiar_correo", False)
    if sys.stdin.isatty():
        print("\n" + "=" * 70)
        print("  SELECCIONE LA ACCIÓN A REALIZAR DESPUÉS DE INICIAR SESIÓN EN TIDAL:")
        print("  1 - [Opción A] Eliminar miembros del plan Familiar (Fase 8)")
        print("  2 - [Opción B] Cambiar el correo del perfil por cakeseller1234@gmail.com")
        print("                 (con variaciones de puntos al azar)")
        print("  3 - Ninguna (Solo iniciar sesión e ir a la página familiar)")
        print("=" * 70)
        opcion = ""
        while opcion not in ("1", "2", "3"):
            opcion = input("  Seleccione una opción (1, 2 o 3): ").strip()
        print("=" * 70 + "\n")
        if opcion == "1":
            args.omitir_fase_eliminar_miembros = False
            args.cambiar_correo = False
        elif opcion == "2":
            args.omitir_fase_eliminar_miembros = True
            args.cambiar_correo = True
        else:
            args.omitir_fase_eliminar_miembros = True
            args.cambiar_correo = False

    sesiones = cargar_sesiones(
        args.links,
        args.emails,
        args.passwords,
        eliminar_miembros_path=args.eliminar_miembros,
        cargar_eliminar_miembros=not args.omitir_fase_eliminar_miembros,
    )
    if not sesiones:
        sys.exit(1)

    print(f"Entradas: {len(sesiones)}")

    if args.solo_subprocess:
        # Intentar importar pywinauto
        try:
            from pywinauto import Application
        except ImportError:
            print("\n❌ Error: Se requiere la librería 'pywinauto' instalada para automatizar el modo subprocess.")
            print("Instálala ejecutando: pip install pywinauto\n")
            sys.exit(1)

        user_data = _chrome_user_data_dir()
        chrome_exe = _get_chrome_exe()
        print(f"Carpeta User Data de Chrome: {user_data}")
        
        # Preguntar al usuario si desea automatización
        print("\n" + "="*60)
        print("  MODO SUBPROCESS CON AUTOMATIZACIÓN UIA")
        print("  - Las ventanas de Chrome se abrirán como navegadores normales.")
        print("  - El script rellenará correo, cookies, contraseña y navegará a familia.")
        print("  - Si detecta un Captcha o bloqueo, pausará para que lo resuelvas.")
        print("="*60 + "\n")
        
        ok = 0
        hwnd_procesados = set()
        for i, s in enumerate(sesiones):
            perfil = s["perfil"]
            email = s["email"]
            password = s["password"]
            url = s["url"]
            eliminar_miembro = s.get("eliminar_miembro", "").strip()
            
            print(f"\n--- [{i + 1}/{len(sesiones)}] Iniciando y conectando perfil: {perfil} ({email}) ---")
            
            # Aplicar bypass de referencia cargando la página principal / pricing de TIDAL primero
            usar_bypass_referencia = (url == DEFAULT_TIDAL_LOGIN_URL or "tidal.com/pricing" in url or "account.tidal.com" in url)
            url_inicial = "https://tidal.com/pricing" if usar_bypass_referencia else url
            if usar_bypass_referencia:
                print("  [Bypass] Cargando tidal.com/pricing primero para acumular reputación...")

            # Lanzamos la ventana con accesibilidad habilitada
            if abrir_solo_chrome(url_inicial, perfil, chrome_exe, user_data, usar_incognito=args.usar_incognito, habilitar_accesibilidad=True):
                ok += 1
            else:
                print(f"❌ Error al abrir la ventana para el perfil {perfil}.")
                continue
                
            # Esperar a que la ventana aparezca y el DOM inicial cargue
            print("  Esperando a que la ventana de Chrome responda...")
            time.sleep(3.5)
            
            wnd = None
            # Intentar conectar con la ventana usando find_elements y handle para evitar colisiones
            try:
                from pywinauto.findwindows import find_elements
                # Esperar hasta 12 segundos a que aparezca la ventana
                elements = []
                for _ in range(24): # 24 * 0.5s = 12s
                    elements = find_elements(title_re="(?i).*tidal.*", backend="uia")
                    if elements:
                        break
                    time.sleep(0.5)
                
                if not elements:
                    raise RuntimeError("No se encontró ninguna ventana que coincida.")
                
                # Seleccionar la ventana correcta (que tenga HWND handle y no haya sido procesada)
                target_el = None
                for el in elements:
                    try:
                        handle = getattr(el, "handle", None)
                        name = getattr(el, "name", "") or ""
                        if handle and handle not in hwnd_procesados:
                            if "Google Chrome" in name:
                                target_el = el
                                break
                            if not target_el:
                                target_el = el
                    except Exception:
                        continue
                
                if not target_el:
                    # Si todas fueron marcadas como procesadas, elegir la primera disponible como fallback
                    target_el = elements[0]
                
                # Conectar por handle único
                app = Application(backend="uia").connect(handle=target_el.handle)
                wnd = app.window(handle=target_el.handle)
                wnd.set_focus()
                hwnd_procesados.add(target_el.handle)
            except Exception as e:
                print(f"  ⚠️ No se pudo conectar pywinauto a esta ventana: {e}")
                print("  Se continuará con la apertura de la siguiente ventana, pero deberás rellenar esta manualmente.")
                if i < len(sesiones) - 1:
                    time.sleep(args.delay)
                continue

            # Lazo de interacción UIA con reintentos en caso de detección de bloqueo / cambio de IP
            intentos_login = 0
            necesita_bypass = usar_bypass_referencia
            ya_logueado = False
            
            # Comprobar si ya está logueada
            url_actual = _obtener_url_actual_uia(wnd)
            if ("family" in url_actual or "profile" in url_actual or "listen.tidal.com" in url_actual) and not _detectar_captcha_uia(wnd):
                print(f"  [UIA] La ventana ya se encuentra logueada/adelantada en: {url_actual}. Omitiendo login.")
                ya_logueado = True

            if not ya_logueado:
                while intentos_login < 5:
                    # 1. Comprobar si hay un captcha de DataDome o bloqueo de entrada
                    if _detectar_captcha_uia(wnd):
                        print("\n  ⚠️ [BLOQUEO / RESTRICCIÓN DE IP] Se ha detectado una pantalla de bloqueo.")
                        print("  --> POR FAVOR, ROTA DE IP (VPN / PROXY / ROUTER) AHORA.")
                        input("  >>> Pulsa Enter AQUÍ una vez que hayas rotado de IP y desees reiniciar desde pricing <<<  ")
                        wnd.set_focus()
                        
                        if _detectar_captcha_uia(wnd):
                            print("  [Bypass] Redirigiendo a https://tidal.com/pricing para reiniciar...")
                            wnd.type_keys("^l")
                            time.sleep(0.25)
                            wnd.type_keys("https://tidal.com/pricing{ENTER}", with_spaces=True)
                            time.sleep(4.0)
                            necesita_bypass = True
                            intentos_login += 1
                            continue
                        else:
                            print("  [UIA] Bloqueo resuelto durante la pausa. Verificando estado...")
                            url_actual = _obtener_url_actual_uia(wnd)
                            if ("family" in url_actual or "profile" in url_actual or "listen.tidal.com" in url_actual) and not _detectar_captcha_uia(wnd):
                                print(f"  [UIA] Ya se encuentra logueado/adelantado en: {url_actual}. Omitiendo resto de login.")
                                ya_logueado = True
                                break
                
                # Si necesitamos el bypass, ejecutarlo
                if necesita_bypass:
                    print("  [Bypass] Esperando carga de página principal (3.5s)...")
                    time.sleep(3.5)
                    _aceptar_cookies_uia(wnd)
                    time.sleep(0.8)
                    
                    print("  [Bypass] Pulsando 'Prueba gratis'...")
                    btn_prueba = _buscar_control_uia(wnd, "Button", ["prueba gratis", "free trial", "try for free", "prueba"])
                    if not btn_prueba:
                        for d in wnd.descendants():
                            name = (d.element_info.name or "").lower()
                            if "prueba gratis" in name or "free trial" in name or "try for free" in name:
                                btn_prueba = d
                                break
                                
                    if btn_prueba:
                        btn_prueba.click_input()
                        time.sleep(4.0)
                    else:
                        print("  ⚠️ No se encontró el botón 'Prueba gratis'. Redirigiendo vía barra de direcciones...")
                        wnd.type_keys("^l")
                        time.sleep(0.3)
                        for char in DEFAULT_TIDAL_LOGIN_URL:
                            if char in ("+", "^", "%", "~", "(", ")", "{", "}"):
                                wnd.type_keys(f"{{{char}}}")
                            elif char == " ":
                                wnd.type_keys("{SPACE}")
                            else:
                                wnd.type_keys(char, with_spaces=True)
                            time.sleep(0.015)
                        time.sleep(0.2)
                        wnd.type_keys("{ENTER}")
                        time.sleep(4.0)
                    necesita_bypass = False
                    
                # 2. Aceptar Cookies
                _aceptar_cookies_uia(wnd)
                time.sleep(random.uniform(0.8, 1.5))

                # 3. Rellenar Correo
                print("  Rellenando correo...")
                if _rellenar_campo_uia(wnd, ["email", "correo", "usuario", "username"], email):
                    time.sleep(random.uniform(0.7, 1.3))
                    # Pulsar continuar
                    if not _hacer_clic_boton_uia(wnd, ["continuar", "continue"]):
                        wnd.type_keys("{ENTER}")
                else:
                    # Fallback escribiendo directamente (asumiendo focus)
                    wnd.type_keys("^a{BACKSPACE}")
                    time.sleep(0.2)
                    for char in email:
                        delay = random.uniform(0.045, 0.125)
                        time.sleep(delay)
                        if char in ("+", "^", "%", "~", "(", ")", "{", "}"):
                            wnd.type_keys(f"{{{char}}}")
                        elif char == " ":
                            wnd.type_keys("{SPACE}")
                        else:
                            wnd.type_keys(char, with_spaces=True)
                    time.sleep(random.uniform(0.7, 1.3))
                    wnd.type_keys("{ENTER}")
                    
                # Esperar 2 segundos después de finalizar el bloque de correo
                print("  Esperando 2 segundos tras el envío del correo...")
                time.sleep(2.0)
                
                # Comprobar si apareció bloqueo tras el correo
                if _detectar_captcha_uia(wnd):
                    print("\n  ⚠️ [BLOQUEO / RESTRICCIÓN DE IP] Se ha detectado una pantalla de bloqueo tras el correo.")
                    print("  --> POR FAVOR, ROTA DE IP (VPN / PROXY / ROUTER) AHORA.")
                    input("  >>> Pulsa Enter AQUÍ una vez que hayas rotado de IP y desees reiniciar desde pricing <<<  ")
                    wnd.set_focus()
                    
                    if _detectar_captcha_uia(wnd):
                        print("  [Bypass] Redirigiendo a https://tidal.com/pricing para reiniciar...")
                        wnd.type_keys("^l")
                        time.sleep(0.25)
                        wnd.type_keys("https://tidal.com/pricing{ENTER}", with_spaces=True)
                        time.sleep(4.0)
                        necesita_bypass = True
                        intentos_login += 1
                        continue
                    else:
                        print("  [UIA] Bloqueo resuelto durante la pausa. Verificando estado...")
                        url_actual = _obtener_url_actual_uia(wnd)
                        if ("family" in url_actual or "profile" in url_actual or "listen.tidal.com" in url_actual) and not _detectar_captcha_uia(wnd):
                            print(f"  [UIA] Ya se encuentra logueado/adelantado en: {url_actual}. Omitiendo resto de login.")
                            ya_logueado = True
                            break
                    
                # 4. Esperar al campo de contraseña
                print("  Esperando a que aparezca el campo de contraseña...")
                pwd_field = None
                bloqueado_en_pwd = False
                for attempt in range(16): # Hasta 8 segundos
                    if _detectar_captcha_uia(wnd):
                        print("\n  ⚠️ [BLOQUEO / RESTRICCIÓN DE IP] Se ha detectado una pantalla de bloqueo.")
                        print("  --> POR FAVOR, ROTA DE IP (VPN / PROXY / ROUTER) AHORA.")
                        input("  >>> Pulsa Enter AQUÍ una vez que hayas rotado de IP y desees reiniciar desde pricing <<<  ")
                        wnd.set_focus()
                        
                        if _detectar_captcha_uia(wnd):
                            print("  [Bypass] Redirigiendo a https://tidal.com/pricing para reiniciar...")
                            wnd.type_keys("^l")
                            time.sleep(0.25)
                            wnd.type_keys("https://tidal.com/pricing{ENTER}", with_spaces=True)
                            time.sleep(4.0)
                            necesita_bypass = True
                            bloqueado_en_pwd = True
                            break
                        else:
                            print("  [UIA] Bloqueo resuelto durante la pausa. Verificando estado...")
                            url_actual = _obtener_url_actual_uia(wnd)
                            if ("family" in url_actual or "profile" in url_actual or "listen.tidal.com" in url_actual) and not _detectar_captcha_uia(wnd):
                                print(f"  [UIA] Ya se encuentra logueado/adelantado en: {url_actual}. Omitiendo resto de login.")
                                ya_logueado = True
                                break
                    pwd_field = _buscar_control_uia(wnd, "Edit", ["password", "contrase"])
                    if pwd_field:
                        break
                    time.sleep(0.5)
                    
                if bloqueado_en_pwd:
                    intentos_login += 1
                    continue

                # 5. Rellenar Contraseña
                time.sleep(random.uniform(0.9, 1.6))
                if pwd_field:
                    print("  Rellenando contraseña...")
                    pwd_field.click_input()
                    pwd_field.type_keys("^a{BACKSPACE}")
                    _rellenar_campo_uia(wnd, ["password", "contrase"], password)
                    time.sleep(random.uniform(0.7, 1.3))
                    if not _hacer_clic_boton_uia(wnd, ["iniciar", "log in", "session", "sesión"]):
                        wnd.type_keys("{ENTER}")
                else:
                    print("  ⚠️ No se encontró el campo de contraseña en el árbol UIA. Intentando escribir directamente...")
                    for char in password:
                        delay = random.uniform(0.045, 0.125)
                        time.sleep(delay)
                        if char in ("+", "^", "%", "~", "(", ")", "{", "}"):
                            wnd.type_keys(f"{{{char}}}")
                        elif char == " ":
                            wnd.type_keys("{SPACE}")
                        else:
                            wnd.type_keys(char, with_spaces=True)
                    time.sleep(random.uniform(0.7, 1.3))
                    wnd.type_keys("{ENTER}")

                # Intentar pulsar 'Sí, continuar' si aparece la pantalla de consentimiento/autorización
                print("  Esperando pantalla de consentimiento 'Sí, continuar'...")
                if _pulsar_si_continuar_auth_uia(wnd, timeout_s=6.0):
                    print("  OK 'Sí, continuar' pulsado o ya redirigido. Esperando carga y redirigiendo de frente a Familia...")
                    time.sleep(3.0)
                    url_fam = args.url_familia_tidal or DEFAULT_TIDAL_FAMILY_URL
                    wnd.type_keys("^l")
                    time.sleep(0.25)
                    wnd.type_keys(url_fam + "{ENTER}", with_spaces=True)
                    time.sleep(4.0)
                else:
                    print("  No se requirió o no se encontró 'Sí, continuar'.")
                    
                # Esperar 2 segundos después de iniciar sesión / bloque de contraseña
                print("  Esperando 2 segundos tras el inicio de sesión...")
                time.sleep(2.0)
                
                # Comprobar si se detectó bloqueo tras el login
                if _detectar_captcha_uia(wnd):
                    print("\n  ⚠️ [BLOQUEO / RESTRICCIÓN DE IP] Se ha detectado una pantalla de bloqueo tras el login.")
                    print("  --> POR FAVOR, ROTA DE IP (VPN / PROXY / ROUTER) AHORA.")
                    input("  >>> Pulsa Enter AQUÍ una vez que hayas rotado de IP y desees reiniciar desde pricing <<<  ")
                    wnd.set_focus()
                    
                    if _detectar_captcha_uia(wnd):
                        print("  [Bypass] Redirigiendo a https://tidal.com/pricing para reiniciar...")
                        wnd.type_keys("^l")
                        time.sleep(0.25)
                        wnd.type_keys("https://tidal.com/pricing{ENTER}", with_spaces=True)
                        time.sleep(4.0)
                        necesita_bypass = True
                        intentos_login += 1
                        continue
                    else:
                        print("  [UIA] Bloqueo resuelto durante la pausa. Verificando estado...")
                        url_actual = _obtener_url_actual_uia(wnd)
                        if ("family" in url_actual or "profile" in url_actual or "listen.tidal.com" in url_actual) and not _detectar_captcha_uia(wnd):
                            print(f"  [UIA] Ya se encuentra logueado/adelantado en: {url_actual}. Omitiendo resto de login.")
                            ya_logueado = True
                            break
                
                break

            if not ya_logueado:
                # 6. Esperar a que se realice el login
                print("  Esperando inicio de sesión (5s)...")
                time.sleep(5.0)
                
                if _detectar_captcha_uia(wnd):
                    print("\n  ⚠️ [BLOQUEO] Se ha detectado una verificación de seguridad tras el login.")
                    input("  >>> Resuélvelo y pulsa Enter AQUÍ para continuar <<<  ")
                    wnd.set_focus()

                # 7. Redirigir a la página de familia
                url_fam = args.url_familia_tidal or DEFAULT_TIDAL_FAMILY_URL
                print(f"  Navegando a la página Familia: {url_fam}")
                wnd.type_keys("^l")  # Enfocar barra de direcciones
                time.sleep(0.25)
                wnd.type_keys(url_fam + "{ENTER}", with_spaces=True)
                
                # Esperar a que cargue la página de familia
                print("  Cargando página de familia (4s)...")
                time.sleep(4.0)
            else:
                # Si ya está logueado, y la fase 8 está activa (no omitida), asegurar que vamos a familia
                if eliminar_miembro and not args.omitir_fase_eliminar_miembros:
                    url_fam = args.url_familia_tidal or DEFAULT_TIDAL_FAMILY_URL
                    url_actual = _obtener_url_actual_uia(wnd)
                    if "family" not in url_actual:
                        print(f"  [UIA] Navegando a la página Familia: {url_fam}")
                        wnd.type_keys("^l")
                        time.sleep(0.25)
                        wnd.type_keys(url_fam + "{ENTER}", with_spaces=True)
                        time.sleep(4.0)

            # 8. Eliminar miembro si aplica
            if eliminar_miembro and not args.omitir_fase_eliminar_miembros:
                print(f"  [Fase 8] Intentando eliminar miembro del plan familiar: {eliminar_miembro}")
                if _eliminar_miembro_uia(wnd, eliminar_miembro):
                    print("  ✅ Miembro eliminado correctamente.")
                else:
                    print("  ⚠️ No se pudo eliminar al miembro de manera automática. Revisa la ventana.")

            # 8B. Cambiar correo de la cuenta si aplica
            if args.cambiar_correo:
                email_con_puntos = generar_correo_con_puntos("cakeseller1234@gmail.com")
                print(f"  [Fase 8B] Cambiando correo del perfil a: {email_con_puntos}")
                if cambiar_correo_perfil_uia(wnd, email_con_puntos):
                    print("  ✅ Correo modificado con éxito en UIA.")
                else:
                    print("  ⚠️ No se pudo cambiar el correo de manera automática. Revisa la ventana.")

            if i < len(sesiones) - 1:
                print(f"  Esperando delay de apertura ({args.delay}s) antes de abrir el siguiente perfil...")
                time.sleep(args.delay)

        print(f"\nResumen: {ok}/{len(sesiones)} procesados en modo subprocess.")
        return

    print(
        "Modo Playwright: un Chrome con varias ventanas privadas / incógnito (no se usa tu carpeta "
        "User Data; evita cierres con código 21).\n"
    )
    captcha_tid_cfg = CaptchaTidManejoCfg(
        usar_surfshark=not args.sin_surfshark_captcha,  # Ahora False por defecto
        surfshark_exe=args.surfshark_exe,
        surfshark_espera_desconectar_rapida_s=args.captcha_espera_surfshark_interna,
        espera_tras_surfshark_s=args.captcha_espera_tras_surfshark,
        surfshark_timeout_botones_s=args.captcha_timeout_surfshark,
    )
    ejecutar_playwright(
        sesiones,
        delay_entre_aperturas=args.delay_apertura,
        espera_tras_abrir_todas=args.espera_tras_abrir,
        email_reintentos=args.email_reintentos,
        email_pausa_s=args.email_pausa,
        cookie_reintentos=args.cookie_reintentos,
        cookie_pausa_s=args.cookie_pausa,
        espera_post_goto_cada_ventana_s=args.espera_post_goto,
        espera_tras_cookies_s=args.espera_tras_cookies,
        continuar_reintentos=args.continuar_reintentos,
        continuar_pausa_s=args.continuar_pausa,
        espera_tras_continuar_s=args.espera_tras_continuar,
        password_reintentos=args.password_reintentos,
        password_pausa_s=args.password_pausa,
        pausa_manual=args.pausa_manual,
        espera_tras_password_s=args.espera_tras_password,
        iniciar_sesion_reintentos=args.iniciar_sesion_reintentos,
        iniciar_sesion_pausa_s=args.iniciar_sesion_pausa,
        delay_entre_iniciar_sesion=args.delay_iniciar_sesion,
        url_familia_tidal=args.url_familia_tidal,
        delay_entre_familia=args.delay_familia,
        captcha_tid=captcha_tid_cfg,
        omitir_fase_eliminar_miembros=args.omitir_fase_eliminar_miembros,
        eliminar_miembro_reintentos=args.eliminar_miembro_reintentos,
        eliminar_miembro_pausa_s=args.eliminar_miembro_pausa,
        delay_entre_eliminar_miembro=args.delay_eliminar_miembro,
        usar_incognito=args.usar_incognito,
        cambiar_correo=args.cambiar_correo,
    )


if __name__ == "__main__":
    main()
