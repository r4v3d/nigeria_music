"""
Abre (o enfoca) Surfshark en Windows y simula el flujo:
  Desconectar → espera → Conexión rápida

Requiere: pip install pywinauto
Uso:
  python surfshark_reconectar.py
  python surfshark_reconectar.py --espera 5
  python surfshark_reconectar.py --solo-si-corre --exe "C:\\Program Files\\Surfshark\\Surfshark.exe"
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def _exe_por_defecto() -> Path:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for base in (Path(pf), Path(pfx86)):
        cand = base / "Surfshark" / "Surfshark.exe"
        if cand.is_file():
            return cand
    return Path(pf) / "Surfshark" / "Surfshark.exe"


def _obtener_surfshark_pids() -> list[int]:
    import os
    pids = []
    try:
        with os.popen('tasklist /FI "IMAGENAME eq Surfshark.exe" /NH') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].lower() == 'surfshark.exe':
                    try:
                        pids.append(int(parts[1]))
                    except ValueError:
                        pass
    except Exception:
        pass
    return pids


def _conectar_app_surfshark(exe: Path, solo_si_corre: bool):
    from pywinauto import Application
    import subprocess

    # 1. Buscar si ya está corriendo en el sistema
    pids = _obtener_surfshark_pids()
    if pids:
        print(f"  Surfshark: Detectado proceso en ejecución (PID {pids[0]}). Conectando...")
        try:
            app = Application(backend="uia").connect(process=pids[0], timeout=5)
            # Verificar si la ventana existe y está visible
            wnd = app.window(title_re=".*Surfshark.*", visible_only=False)
            if wnd.exists() and wnd.is_visible():
                return app
            print("  Surfshark: Proceso conectado pero la ventana está minimizada/oculta.")
        except Exception:
            pass

    # 2. Si no corre o la ventana está oculta, lanzamos el ejecutable para abrirla/restaurarla
    if solo_si_corre and not pids:
        raise RuntimeError(
            "Surfshark no está en ejecución y --solo-si-corre está activo. "
            "Abre la app a mano o quita ese flag."
        )
    if not exe.is_file():
        raise FileNotFoundError(f"No se encontró el ejecutable: {exe}")

    print(f"Iniciando/Restaurando Surfshark: {exe}")
    # Lanzar el proceso de forma asíncrona para despertar la ventana
    try:
        subprocess.Popen([str(exe)])
    except Exception as e:
        print(f"  Surfshark: Error al lanzar el proceso: {e}")
        
    time.sleep(5.0)  # Esperar a que se abra/restaure la ventana

    # 3. Reconectar al PID (el nuevo proceso o el existente que se despertó)
    pids = _obtener_surfshark_pids()
    if pids:
        print(f"  Surfshark: Reconectando al PID {pids[0]}...")
        return Application(backend="uia").connect(process=pids[0], timeout=15)

    raise RuntimeError("No se pudo iniciar o conectar a Surfshark.")


def _ventana_principal(app):
    return app.window(title_re=".*Surfshark.*", visible_only=False)


def _texto_control(ctrl) -> str:
    try:
        return (ctrl.window_text() or "").strip()
    except Exception:
        return ""


def _pulsar_boton_por_textos(wnd, textos: tuple[str, ...], timeout_s: float) -> bool:
    """Busca un botón cuyo texto coincida (sin distinguir mayúsculas) y hace click."""
    textos_norm = tuple(t.casefold() for t in textos)
    fin = time.time() + timeout_s
    while time.time() < fin:
        try:
            wnd.wait("exists enabled visible", timeout=2)
        except Exception:
            time.sleep(0.2)
            continue
        try:
            for btn in wnd.descendants(control_type="Button"):
                t = _texto_control(btn).casefold()
                if not t:
                    continue
                # Coincidencia exacta o si la palabra buscada está contenida en el texto del botón
                # (Evita que 'connect' coincida con 'disconnect' al no buscar 't' dentro del candidato)
                if t in textos_norm or any(t == x for x in textos_norm) or any(x in t for x in textos_norm):
                    try:
                        btn.click_input()
                    except Exception:
                        btn.invoke()
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _escribir_busqueda_surfshark(wnd, texto: str, timeout_s: float) -> bool:
    """Busca el campo de texto de buscar ubicaciones y escribe en él."""
    import time
    fin = time.time() + timeout_s
    while time.time() < fin:
        try:
            # Buscar primero por el nombre exacto de UIA 'Search box'
            for edit in wnd.descendants():
                info = edit.element_info
                name = (info.name or "").strip().lower()
                ctype = info.control_type or ""
                class_name = (info.class_name or "").strip().lower()
                
                # Coincidencia exacta con el mapa de UIA de Surfshark 6.12.0
                if name == "search box" or class_name == "textbox" or ctype == "Edit":
                    try:
                        edit.set_focus()
                    except Exception:
                        pass
                    
                    edit.click_input()
                    time.sleep(0.5)
                    
                    # Intentar escribir directamente con la API nativa de UIA (muy fiable)
                    try:
                        edit.set_edit_text(texto)
                        return True
                    except Exception:
                        pass
                        
                    # Fallback a type_keys
                    try:
                        edit.type_keys("^a{BACKSPACE}", protect_first=False)
                        edit.type_keys(texto, with_spaces=True)
                        return True
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _pulsar_resultado_ubicacion(wnd, texto_pais: str, timeout_s: float) -> bool:
    """Busca en los resultados de la lista el país indicado y le hace clic."""
    import time
    texto_pais_norm = texto_pais.casefold().strip()
    fin = time.time() + timeout_s
    while time.time() < fin:
        try:
            for elem in wnd.descendants():
                try:
                    info = elem.element_info
                    tipo = info.control_type or ""
                    name = (info.name or "").strip().casefold()
                    
                    # Buscamos en elementos tipo ListItem, Text, Button, Group o DataItem
                    if tipo in ("Text", "ListItem", "Button", "Group", "DataItem", "ListBoxItem"):
                        if name == texto_pais_norm or texto_pais_norm in name:
                            # Hacer clic sobre el elemento encontrado
                            try:
                                elem.click_input()
                            except Exception:
                                try:
                                    elem.invoke()
                                except Exception:
                                    pass
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _ciclo_ui_surfshark(
    app,
    *,
    espera_entre_desconectar_y_rapida: float,
    timeout_busqueda: float,
    verbose: bool,
    ubicacion: str | None = None,
    solo_desconectar: bool = False,
) -> bool:
    """Desconectar → espera → Conexión rápida o búsqueda de ubicación específica."""
    wnd = _ventana_principal(app)
    wnd.wait("exists enabled", timeout=25)
    try:
        wnd.set_focus()
    except Exception:
        pass

    desconectar = ("Desconectar", "Disconnect")

    if verbose:
        print("  Surfshark: buscando «Desconectar»…")
    if _pulsar_boton_por_textos(wnd, desconectar, timeout_busqueda):
        if verbose:
            print("  Surfshark: pulsado Desconectar.")
        if solo_desconectar:
            return True
    elif verbose:
        print(
            "  Surfshark: no apareció «Desconectar» a tiempo (¿ya desconectado?)."
        )
        if solo_desconectar:
            return True

    if solo_desconectar:
        return True

    time.sleep(espera_entre_desconectar_y_rapida)

    if ubicacion:
        if verbose:
            print(f"  Surfshark: buscando el buscador para «{ubicacion}»…")
        if _escribir_busqueda_surfshark(wnd, ubicacion, timeout_busqueda):
            if verbose:
                print(f"  Surfshark: escrito «{ubicacion}». Buscando el resultado en la lista…")
            time.sleep(1.5)  # Espera a que se filtre la lista
            if _pulsar_resultado_ubicacion(wnd, ubicacion, timeout_busqueda):
                if verbose:
                    print(f"  Surfshark: seleccionado «{ubicacion}» para conectar.")
                return True
            else:
                if verbose:
                    print(f"  Surfshark: no se pudo encontrar el resultado de «{ubicacion}».")
        else:
            if verbose:
                print("  Surfshark: no se pudo encontrar el campo de búsqueda de ubicaciones.")
        return False
    else:
        rapida = (
            "Conexión rápida",
            "Conexion rapida",
            "Quick connect",
            "Quick Connect",
            "Connexion rapide",
        )
        if verbose:
            print("  Surfshark: buscando «Conexión rápida»…")
        if _pulsar_boton_por_textos(wnd, rapida, timeout_busqueda):
            if verbose:
                print("  Surfshark: pulsado Conexión rápida.")
            return True
        if verbose:
            print("  Surfshark: no se encontró «Conexión rápida».")
        return False


def ejecutar_reconexion_surfshark(
    *,
    exe: Path | None = None,
    espera_entre_desconectar_y_rapida: float = 4.0,
    timeout_busqueda: float = 15.0,
    solo_si_corre: bool = False,
    verbose: bool = True,
    ubicacion: str | None = None,
    solo_desconectar: bool = False,
) -> bool:
    """
    Conecta o inicia Surfshark y ejecuta Desconectar → espera → Ubicación específica o Conexión rápida.
    """
    if sys.platform != "win32":
        if verbose:
            print("  Surfshark: omitido (no es Windows).")
        return False
    try:
        import pywinauto  # noqa: F401
    except ImportError:
        if verbose:
            print("  Surfshark: falta pywinauto. Instala: pip install pywinauto")
        return False

    ruta = exe if exe is not None else _exe_por_defecto()
    try:
        app = _conectar_app_surfshark(ruta, solo_si_corre)
    except (RuntimeError, FileNotFoundError) as e:
        if verbose:
            print(f"  Surfshark: {e}")
        return False
    except Exception as e:
        if verbose:
            print(f"  Surfshark: no se pudo iniciar o conectar — {e}")
        return False

    try:
        return _ciclo_ui_surfshark(
            app,
            espera_entre_desconectar_y_rapida=espera_entre_desconectar_y_rapida,
            timeout_busqueda=timeout_busqueda,
            verbose=verbose,
            ubicacion=ubicacion,
            solo_desconectar=solo_desconectar,
        )
    except Exception as e:
        if verbose:
            print(f"  Surfshark: error en la UI — {e}")
        return False


def main() -> None:
    if sys.platform != "win32":
        print("Este script solo está pensado para Windows.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Surfshark: Desconectar, esperar, Conectar a Ubicación / Conexión rápida.")
    parser.add_argument(
        "--exe",
        type=Path,
        default=_exe_por_defecto(),
        help="Ruta a Surfshark.exe (por defecto Program Files).",
    )
    parser.add_argument(
        "--espera",
        type=float,
        default=4.0,
        metavar="S",
        help="Segundos de espera entre Desconectar y reconectar (por defecto 4).",
    )
    parser.add_argument(
        "--solo-si-corre",
        action="store_true",
        help="No lanzar Surfshark si no hay ventana; solo conectar a un proceso ya abierto.",
    )
    parser.add_argument(
        "--timeout-busqueda",
        type=float,
        default=15.0,
        help="Tiempo máximo (s) para encontrar cada botón/elemento.",
    )
    parser.add_argument(
        "--ubicacion",
        type=str,
        default=None,
        help="Nombre de la ubicación a buscar y conectar en Surfshark (ej. nigeria). Si se omite, usa Conexión rápida.",
    )
    parser.add_argument(
        "--solo-desconectar",
        action="store_true",
        help="Solo desconectar la VPN de Surfshark, sin volver a conectar.",
    )
    args = parser.parse_args()

    ok = ejecutar_reconexion_surfshark(
        exe=args.exe,
        espera_entre_desconectar_y_rapida=args.espera,
        timeout_busqueda=args.timeout_busqueda,
        solo_si_corre=args.solo_si_corre,
        verbose=True,
        ubicacion=args.ubicacion,
        solo_desconectar=args.solo_desconectar,
    )
    sys.exit(0 if ok else 3)


if __name__ == "__main__":
    main()
