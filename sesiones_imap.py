#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import imaplib
import email
import webbrowser
import winreg
import tempfile
import subprocess
import shutil
import requests
import json
import random
import threading
import queue
import contextlib
from pathlib import Path
from email.header import decode_header
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed


# Configurar salida estándar para UTF-8 en Windows
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent

# Gmail rechaza las conexiones cuando se superan ~15 sesiones IMAP simultáneas por buzón.
# Con lotes de 20 (opción 9) un semáforo de 5 dejaba a muchos hilos minutos en cola: cuando
# entraban, el UID ya lo había reclamado otro alias del mismo buzón. 10 equilibra throughput
# vs el techo de Gmail (~15).
IMAP_SEMAFORO = threading.BoundedSemaphore(10)


class BarreraTolerante:
    """Sincronización tipo Barrier donde un hilo puede desertar sin romper al resto del lote.

    threading.Barrier.abort() deja BrokenBarrierError permanente: el resto 'continúa' la
    sincronización en silencio y se desincroniza (falso avance / carreras en opción 9).
    """

    def __init__(self, parties: int):
        self._parties = max(1, int(parties))
        self._cond = threading.Condition()
        self._waiting = 0
        self._gen = 0
        self._desertores = 0

    def desertar(self) -> None:
        with self._cond:
            self._desertores += 1
            self._cond.notify_all()

    def wait(self, timeout: float = 180.0) -> None:
        deadline = time.time() + max(0.0, float(timeout))
        with self._cond:
            gen = self._gen
            self._waiting += 1
            try:
                while True:
                    if gen != self._gen:
                        return
                    needed = max(0, self._parties - self._desertores)
                    if needed == 0 or self._waiting >= needed:
                        if gen == self._gen:
                            self._gen += 1
                            self._waiting = 0
                            self._cond.notify_all()
                        return
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise TimeoutError(f"Timeout en BarreraTolerante ({timeout}s)")
                    self._cond.wait(min(1.0, remaining))
            except Exception:
                # Si este hilo falla/timeout antes de liberar la generación, no deje el cupo inflado
                if gen == self._gen:
                    self._waiting = max(0, self._waiting - 1)
                    self._cond.notify_all()
                raise

# Protege 'tmm_cookies.json' de escrituras solapadas entre ventanas simultáneas.
TMM_COOKIES_LOCK = threading.Lock()
TMM_COOKIES_PATH = SCRIPT_DIR / "tmm_cookies.json"

# Serializa escrituras de CSV en descargas/ (OneDrive + varias ventanas a la vez).
DESCARGAS_CSV_LOCK = threading.Lock()
DESCARGAS_DIR = SCRIPT_DIR / "descargas"
# Reserva exclusiva archivo→cuenta (opción 8): evita que dos ventanas suban el mismo CSV.
CSV_RESERVAS_LOCK = threading.Lock()
CSV_RESERVAS: dict[str, str] = {}  # str(Path.resolve()) -> email dueño

# Protege el fichero de titulares familiares: varias ventanas terminan su upgrade a la vez y
# leer/escribir sin sincronizar hacía que unas cuentas sobrescribieran a otras.
TITULARES_FILE_LOCK = threading.Lock()

# Igual para 'sesiones_imap_cuentas.txt', donde se anotan las contraseñas de las cuentas creadas.
CUENTAS_FILE_LOCK = threading.Lock()

# Colores ANSI para la terminal
class Color:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def cargar_cookies_tmm() -> list:
    """Lee las cookies de TuneMyMusic descartando las expiradas, tolerando lecturas parciales."""
    with TMM_COOKIES_LOCK:
        if not TMM_COOKIES_PATH.exists():
            return []
        try:
            cookies_data = json.loads(TMM_COOKIES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    ahora = time.time()
    return [c for c in cookies_data if not ("expires" in c and c["expires"] < ahora)]


def guardar_cookies_tmm(cookies_context: list) -> bool:
    """Fusiona y guarda las cookies de TuneMyMusic de forma atómica y serializada entre hilos."""
    with TMM_COOKIES_LOCK:
        fusionadas = {}
        if TMM_COOKIES_PATH.exists():
            try:
                for c in json.loads(TMM_COOKIES_PATH.read_text(encoding="utf-8")):
                    if "tunemymusic.com" in c.get("domain", ""):
                        fusionadas[f"{c.get('domain','')}:{c.get('name','')}"] = c
            except Exception:
                pass
        for c in cookies_context or []:
            if "tunemymusic.com" in c.get("domain", ""):
                fusionadas[f"{c.get('domain','')}:{c.get('name','')}"] = c
        if not fusionadas:
            return False
        try:
            tmp_path = TMM_COOKIES_PATH.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(list(fusionadas.values()), indent=4), encoding="utf-8")
            os.replace(tmp_path, TMM_COOKIES_PATH)
            return True
        except Exception:
            return False


def csv_parece_valido(path: Path, min_bytes: int = 32) -> bool:
    """True si el CSV existe, no está vacío y no es un HTML de error a medias."""
    try:
        if not path or not path.exists() or not path.is_file():
            return False
        size = path.stat().st_size
        if size < min_bytes:
            return False
        # Cabecera mínima: al menos una línea con contenido
        with open(path, "rb") as fh:
            head = fh.read(256)
        if not head or head.lstrip().startswith(b"<!"):
            return False
        return True
    except Exception:
        return False


def _csv_clave_reserva(path: Path) -> str:
    try:
        return str(path.resolve())
    except Exception:
        return str(path)


def csv_pertenece_a_cuenta(path: Path | None, client_email: str) -> bool:
    """True solo si el nombre del CSV es el correo (o alias Gmail inequívoco)."""
    if not path or not client_email:
        return False
    stem = path.stem.strip().lower()
    email_l = client_email.strip().lower()
    if not stem or "@" not in stem:
        return False
    return stem == email_l or son_correos_equivalentes(stem, email_l)


def reservar_csv_para_cuenta(path: Path, client_email: str) -> bool:
    """Reserva exclusiva del archivo para una cuenta. False si otra cuenta ya lo tiene."""
    if not path or not client_email:
        return False
    email_l = client_email.strip().lower()
    clave = _csv_clave_reserva(path)
    with CSV_RESERVAS_LOCK:
        dueño = CSV_RESERVAS.get(clave)
        if dueño and dueño != email_l:
            return False
        CSV_RESERVAS[clave] = email_l
        return True


def liberar_reservas_csv_cuenta(client_email: str) -> None:
    email_l = (client_email or "").strip().lower()
    if not email_l:
        return
    with CSV_RESERVAS_LOCK:
        for clave in [k for k, v in CSV_RESERVAS.items() if v == email_l]:
            CSV_RESERVAS.pop(clave, None)


def _csv_reservado_por_otra(path: Path, client_email: str) -> bool:
    email_l = (client_email or "").strip().lower()
    with CSV_RESERVAS_LOCK:
        dueño = CSV_RESERVAS.get(_csv_clave_reserva(path))
    return bool(dueño and dueño != email_l)


def _listar_csvs_descargas() -> list[Path]:
    dest_dir = DESCARGAS_DIR
    if not dest_dir.exists():
        return []
    out = []
    for f in dest_dir.glob("*.csv"):
        if f.name.startswith("."):
            continue
        if csv_parece_valido(f):
            out.append(f)
    return out


def _elegir_csv_preferido(candidatos: list[Path], client_email: str) -> Path | None:
    if not candidatos:
        return None
    email_l = client_email.strip().lower()

    def _clave(p: Path):
        try:
            mtime = p.stat().st_mtime
        except Exception:
            mtime = 0.0
        exact_case = 0 if p.stem == client_email else (1 if p.stem.lower() == email_l else 2)
        return (exact_case, -mtime)

    return sorted(candidatos, key=_clave)[0]


def resolver_csv_cuenta(
    client_email: str,
    *,
    permitir_alias: bool = True,
    respetar_reservas: bool = True,
) -> Path | None:
    """Localiza el CSV de UNA cuenta en descargas/.

    Orden: nombre exacto (case-insensitive) → alias Gmail solo si hay un único candidato
    libre. Nunca usa startswith. Respeta reservas exclusivas de otras cuentas.
    """
    email_l = (client_email or "").strip().lower()
    if not email_l:
        return None
    archivos = _listar_csvs_descargas()
    if not archivos:
        return None

    exactos = []
    aliases = []
    for f in archivos:
        if respetar_reservas and _csv_reservado_por_otra(f, email_l):
            continue
        stem = f.stem.strip().lower()
        if stem == email_l:
            exactos.append(f)
        elif permitir_alias and "@" in stem and son_correos_equivalentes(stem, email_l):
            aliases.append(f)

    if exactos:
        return _elegir_csv_preferido(exactos, client_email)
    if permitir_alias and len(aliases) == 1:
        return aliases[0]
    return None


def asignar_csvs_a_cuentas(correos: list[str]) -> dict[str, Path | None]:
    """Asigna CSV↔cuenta 1:1 para un lote (opción 8). Exacto primero; alias Gmail solo si único.

    Un mismo archivo nunca se asigna a dos cuentas. Devuelve dict correo → Path|None.
    """
    resultado: dict[str, Path | None] = {c: None for c in correos}
    if not correos:
        return resultado

    usados: set[str] = set()
    archivos = _listar_csvs_descargas()

    for correo in correos:
        email_l = correo.strip().lower()
        exactos = [
            f for f in archivos
            if f.stem.strip().lower() == email_l and _csv_clave_reserva(f) not in usados
        ]
        elegido = _elegir_csv_preferido(exactos, correo)
        if elegido:
            resultado[correo] = elegido
            usados.add(_csv_clave_reserva(elegido))
            reservar_csv_para_cuenta(elegido, correo)

    for correo in correos:
        if resultado[correo] is not None:
            continue
        email_l = correo.strip().lower()
        aliases = []
        for f in archivos:
            clave = _csv_clave_reserva(f)
            if clave in usados:
                continue
            stem = f.stem.strip().lower()
            if "@" not in stem:
                continue
            if son_correos_equivalentes(stem, email_l):
                aliases.append(f)
        if len(aliases) == 1:
            elegido = aliases[0]
            resultado[correo] = elegido
            usados.add(_csv_clave_reserva(elegido))
            reservar_csv_para_cuenta(elegido, correo)
        elif len(aliases) > 1:
            nombres = ", ".join(a.name for a in aliases[:5])
            print(f"  {Color.WARNING}[CSV] [{correo}] Alias Gmail ambiguo "
                  f"({len(aliases)} archivos: {nombres}). "
                  f"Renombra a '{correo}.csv' para emparejarlo.{Color.ENDC}")

    return resultado


def obtener_csv_para_subida(manager_o_email, *, reintentar_s: float = 0.0) -> Path | None:
    """CSV definitivo para subir a TuneMyMusic: asignación previa del lote, validada.

    Si hay `csv_asignado` válido y perteneciente a la cuenta, se usa ese (no se re-resuelve
    a otro archivo). Si falta, se resuelve respetando reservas exclusivas.
    """
    if hasattr(manager_o_email, "client_email"):
        email = manager_o_email.client_email
        asignado = getattr(manager_o_email, "csv_asignado", None)
    else:
        email = manager_o_email
        asignado = None

    def _ok(path: Path | None) -> Path | None:
        if not path:
            return None
        if not csv_parece_valido(path):
            return None
        if not csv_pertenece_a_cuenta(path, email):
            print(f"  {Color.FAIL}[CSV] [{email}] Rechazado '{getattr(path, 'name', path)}': "
                  f"no coincide con la cuenta Tidal.{Color.ENDC}")
            return None
        if not reservar_csv_para_cuenta(path, email):
            print(f"  {Color.FAIL}[CSV] [{email}] '{path.name}' ya está reservado para otra cuenta.{Color.ENDC}")
            return None
        return path

    path = _ok(asignado if isinstance(asignado, Path) else None)
    if path:
        return path

    limite = time.time() + max(0.0, float(reintentar_s or 0.0))
    while True:
        path = _ok(resolver_csv_cuenta(email, permitir_alias=True, respetar_reservas=True))
        if path:
            if hasattr(manager_o_email, "client_email"):
                manager_o_email.csv_asignado = path
            return path
        if time.time() >= limite:
            return None
        time.sleep(3.0)


def guardar_csv_descarga_playwright(download, client_email: str) -> Path | None:
    """Guarda una descarga Playwright como CSV de la cuenta, con reintentos y verificación.

    OneDrive a veces bloquea save_as directo; se escribe a un .tmp local y luego se reemplaza.
    """
    dest_dir = DESCARGAS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / f"{client_email}.csv"
    suggested = ""
    try:
        suggested = (download.suggested_filename or "").strip()
    except Exception:
        pass

    last_err = None
    with DESCARGAS_CSV_LOCK:
        for intento in range(1, 5):
            tmp_path = dest_dir / f".{client_email}.csv.part.{random.randint(1000, 9999)}"
            try:
                # 1) Intento vía save_as a temporal
                try:
                    download.save_as(str(tmp_path))
                except Exception as e_save:
                    last_err = e_save
                    # 2) Fallback: path del fichero temporal de Playwright + copy
                    try:
                        src = Path(download.path())
                        if src.exists():
                            shutil.copy2(src, tmp_path)
                        else:
                            raise e_save
                    except Exception as e2:
                        last_err = e2
                        time.sleep(0.6 * intento)
                        continue

                if not csv_parece_valido(tmp_path):
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    last_err = RuntimeError(f"CSV temporal inválido o vacío (sugerido: {suggested})")
                    time.sleep(0.5 * intento)
                    continue

                # Reemplazo atómico cuando es posible
                try:
                    os.replace(tmp_path, dest_file)
                except Exception:
                    shutil.copy2(tmp_path, dest_file)
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass

                if csv_parece_valido(dest_file):
                    return dest_file
                last_err = RuntimeError("CSV destino no pasó la verificación post-escritura")
            except Exception as e:
                last_err = e
                try:
                    if tmp_path.exists():
                        tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                time.sleep(0.6 * intento)

    print(f"  [Descarga] [{client_email}] [ERROR] No se pudo persistir el CSV tras reintentos: {last_err}")
    return None


def buscar_ruta_chrome():
    """Busca la ruta del ejecutable de Google Chrome en Windows."""
    for hkey in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hkey, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                path, _ = winreg.QueryValueEx(key, "")
                if os.path.exists(path):
                    return path
        except OSError:
            pass
            
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Clients\StartMenuInternet\Google Chrome\shell\open\command") as key:
            raw_cmd, _ = winreg.QueryValueEx(key, "")
            path = raw_cmd.strip('"')
            if os.path.exists(path):
                return path
    except OSError:
        pass
        
    rutas_por_defecto = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")
    ]
    for ruta in rutas_por_defecto:
        if os.path.exists(ruta):
            return ruta
            
    return None


def abrir_enlace_en_perfil_chrome(url: str, correo: str) -> None:
    """Abre el enlace en un perfil de Chrome aislado para evitar choques de sesión."""
    chrome_path = buscar_ruta_chrome()
    if chrome_path:
        email_safe = re.sub(r'[^a-zA-Z0-9]', '_', correo)
        profile_dir = os.path.join(tempfile.gettempdir(), f"tidal_chrome_profile_{email_safe}_{random.randint(1000, 9999)}")
        
        cmd = [
            chrome_path,
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            url
        ]
        try:
            subprocess.Popen(cmd)
            print(f"    [Chrome] Enlace abierto en ventana independiente de Chrome.")
        except Exception as e:
            print(f"    [Error] No se pudo abrir Chrome: {e}")
            webbrowser.open(url)
    else:
        webbrowser.open(url)

def clean_email(email_str: str) -> str:
    """Limpia espacios, caracteres especiales finales y convierte un correo a minúsculas."""
    if not email_str:
        return ""
    email_str = email_str.strip().lower()
    email_str = re.sub(r'[\s.,]+$', '', email_str)
    return email_str

def buscar_contrasena_cuenta(correo_solicitado: str) -> str | None:
    """Busca la contraseña de una cuenta en sesiones_imap_cuentas.txt o passwords.txt.

    Acepta coincidencia exacta y alias Gmail equivalentes (puntos / mayúsculas), para que
    las opciones 4 y 5 encuentren la contraseña anotada aunque el correo procesado lleve
    puntos distintos a los del fichero.
    """
    if not correo_solicitado:
        return None
    correo_clean = clean_email(correo_solicitado)

    def _correo_coincide(candidato: str) -> bool:
        c = clean_email(candidato)
        if not c:
            return False
        if c == correo_clean:
            return True
        try:
            return son_correos_equivalentes(c, correo_clean)
        except Exception:
            return False

    # 1. Buscar en sesiones_imap_cuentas.txt
    path_cuentas = SCRIPT_DIR / "sesiones_imap_cuentas.txt"
    if path_cuentas.exists():
        try:
            for line in path_cuentas.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Soportar separadores: espacios, comillas, coma, tab, signo igual
                line_normalized = line.replace(",", " ").replace("=", " ")
                parts = line_normalized.split()
                if len(parts) >= 2:
                    c = parts[0].strip().strip('"').strip("'")
                    p = parts[1].strip().strip('"').strip("'")
                    if _correo_coincide(c) and p:
                        return p
        except Exception:
            pass

    # 2. Fallback a passwords.txt
    path_pwds = SCRIPT_DIR / "passwords.txt"
    if path_pwds.exists():
        try:
            for line in path_pwds.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k_clean = k.strip().lower()
                v_clean = v.strip().strip('"').strip("'")
                if not v_clean:
                    continue
                # Claves tipo email=pwd o gmail_app_password_email=...
                if _correo_coincide(k_clean) or correo_clean in clean_email(k_clean):
                    return v_clean
        except Exception:
            pass

    return None


def cargar_mapa_cuentas_sesiones() -> dict[str, str]:
    """Lee sesiones_imap_cuentas.txt → {correo: contraseña} (orden de archivo)."""
    path_cuentas = SCRIPT_DIR / "sesiones_imap_cuentas.txt"
    cuentas_map: dict[str, str] = {}
    if not path_cuentas.exists():
        return cuentas_map
    try:
        for line in path_cuentas.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line_normalized = line.replace(",", " ").replace("=", " ")
            parts = line_normalized.split()
            if len(parts) >= 2:
                correo = parts[0].strip().strip('"').strip("'")
                pwd = parts[1].strip().strip('"').strip("'")
                if correo and "@" in correo and pwd:
                    cuentas_map[correo] = pwd
    except Exception:
        pass
    return cuentas_map


def filtrar_cuentas_por_correos_activos(
    cuentas_map: dict[str, str],
    correos_activos: list[str] | None,
) -> dict[str, str] | None:
    """Deja solo las cuentas del archivo que coinciden con los correos activos del menú.

    Los correos activos se fijan al inicio o con la opción 6. Así varias instancias del
    script pueden compartir el mismo sesiones_imap_cuentas.txt y procesar subsets distintos
    en paralelo, sin preguntar.

    Devuelve None si había activos pero ninguno está en el archivo (caller debe abortar).
    Si correos_activos está vacío/None, devuelve el mapa completo.
    """
    if not cuentas_map:
        return {}
    if not correos_activos:
        return dict(cuentas_map)

    filtrado: dict[str, str] = {}
    usados_arch: set[str] = set()
    sin_match: list[str] = []

    for c_menu in correos_activos:
        c_menu = (c_menu or "").strip()
        if not c_menu or "@" not in c_menu:
            continue
        elegido = None
        for c_arch in cuentas_map:
            if c_arch in usados_arch:
                continue
            if clean_email(c_arch) == clean_email(c_menu) or son_correos_equivalentes(c_arch, c_menu):
                elegido = c_arch
                break
        if elegido is None:
            sin_match.append(c_menu)
            continue
        filtrado[elegido] = cuentas_map[elegido]
        usados_arch.add(elegido)

    total_arch = len(cuentas_map)
    print(f"\n{Color.CYAN}[Correos activos]{Color.ENDC} Menú: {len(correos_activos)} | "
          f"Archivo: {total_arch} | A procesar: {len(filtrado)}")
    for c_arch in filtrado:
        print(f"  {Color.GREEN}✓{Color.ENDC} {c_arch}")
    if sin_match:
        print(f"{Color.WARNING}[Correos activos] Sin fila en sesiones_imap_cuentas.txt "
              f"(se omiten):{Color.ENDC}")
        for c in sin_match:
            print(f"  {Color.FAIL}✗{Color.ENDC} {c}")

    if not filtrado:
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} Ningún correo activo del menú está en "
              f"sesiones_imap_cuentas.txt. Usa la opción 6 o anota correo+contraseña en el archivo.")
        return None

    if len(filtrado) < total_arch:
        print(f"{Color.CYAN}[Correos activos]{Color.ENDC} Se omiten {total_arch - len(filtrado)} "
              f"cuenta(s) del archivo que no están en el menú (otras instancias pueden usarlas).")
    return filtrado


def guardar_credencial_cuenta(correo: str, pwd: str) -> bool:
    """Registra 'correo<TAB>contraseña' en sesiones_imap_cuentas.txt si aún no está.

    Sin esto, las cuentas creadas por la opción 14 quedaban sin su contraseña de Tidal anotada
    y las opciones 9/10/11 no podían iniciar sesión con ellas después.
    """
    if not correo or not pwd:
        return False
    path_cuentas = SCRIPT_DIR / "sesiones_imap_cuentas.txt"
    correo_clean = clean_email(correo)
    with CUENTAS_FILE_LOCK:
        try:
            existentes = []
            if path_cuentas.exists():
                existentes = path_cuentas.read_text(encoding="utf-8").splitlines()
            for line in existentes:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.replace(",", " ").replace("=", " ").split()
                if parts and clean_email(parts[0].strip().strip('"').strip("'")) == correo_clean:
                    return False
            with open(path_cuentas, "a", encoding="utf-8") as f:
                if existentes and existentes[-1].strip():
                    f.write("\n")
                f.write(f"{correo}\t{pwd}")
            return True
        except Exception as e:
            print(f"  {Color.WARNING}[Cuentas] [WARN] No se pudo anotar la contraseña de {correo}: {e}{Color.ENDC}")
            return False


# Patrón del enlace "Inicia sesión con contraseña" (Tidal lo pinta como texto suelto, no <button>).
_PATRON_MODO_CONTRASENA_INVITE = (
    r"(?:inicia|iniciar|usar|use|sign\s*in|log\s*in|entrar)[^\n]{0,24}(?:contrase|password)"
)


# Inyectado en el contexto de la opción 4: mata OneTrust en cuanto aparece (cada navegación).
_INVITE_COOKIE_KILLER_INIT = """
(() => {
    const KILL_SEL = '#onetrust-consent-sdk, #onetrust-banner-sdk, #onetrust-style, .onetrust-pc-dark-filter, .ot-sdk-container, .ot-cookie-policy, [id*="onetrust" i], [class*="onetrust" i], #cookie-consent, #cookiebanner, [id*="cookie-banner" i], [class*="cookie-banner" i], [class*="cookie-consent" i], [class*="cookiebanner" i]';
    const matar = () => {
        try {
            document.querySelectorAll(KILL_SEL).forEach(el => {
                try {
                    el.style.setProperty('display', 'none', 'important');
                    el.style.setProperty('visibility', 'hidden', 'important');
                    el.style.setProperty('pointer-events', 'none', 'important');
                    el.remove();
                } catch (e) {}
            });
        } catch (e) {}
    };
    const arrancar = () => {
        if (!document.getElementById('anti-cookie-overlay-style-invite')) {
            const st = document.createElement('style');
            st.id = 'anti-cookie-overlay-style-invite';
            st.textContent = KILL_SEL + '{display:none!important;visibility:hidden!important;pointer-events:none!important;opacity:0!important;z-index:-1!important;}';
            (document.head || document.documentElement).appendChild(st);
        }
        matar();
        if (!window.__tidalInviteCookieKiller) {
            window.__tidalInviteCookieKiller = true;
            try {
                new MutationObserver(matar).observe(document.documentElement, {childList:true, subtree:true});
            } catch (e) {}
            setInterval(matar, 700);
        }
    };
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', arrancar);
    } else {
        arrancar();
    }
})();
"""


def _invite_limpiar_cookies_agresivo(page) -> None:
    """Elimina el banner/overlay de cookies de forma agresiva (sin caché de 5s).

    En la opción 4 el OneTrust tapa Continuar / contraseña / Aceptar invitación.
    aceptar_cookies_con_espera() a veces se salta por _chequeo_reciente_limpio.
    """
    if not page:
        return
    try:
        if page.is_closed():
            return
    except Exception:
        return

    # 1) Click directo en botones OneTrust (sin el 'Aceptar' genérico que puede confundirse)
    try:
        for frame in page.frames:
            try:
                frame.evaluate("""() => {
                    const sels = [
                        '#onetrust-accept-btn-handler',
                        '#onetrust-reject-all-handler',
                        '.onetrust-close-btn-handler',
                        '#onetrust-close-btn-container button',
                        '#accept-recommended-btn-handler',
                        'button[id*="accept" i][id*="cookie" i]',
                        'button[class*="accept" i][class*="cookie" i]',
                    ];
                    for (const sel of sels) {
                        const btn = document.querySelector(sel);
                        if (btn && (btn.offsetWidth || btn.offsetHeight || btn.getClientRects().length)) {
                            try { btn.click(); } catch(e) {}
                            return true;
                        }
                    }
                    // Texto explícito de cookies (no 'Aceptar' suelto del CTA de invitación)
                    const re = /aceptar todas|aceptar todo|accept all|reject all|rechazar todas|confirmar mis preferencias/i;
                    for (const btn of document.querySelectorAll('button, a, [role="button"]')) {
                        const t = (btn.textContent || '').trim();
                        if (!t || t.length > 60) continue;
                        if (!re.test(t)) continue;
                        if (btn.offsetWidth || btn.offsetHeight || btn.getClientRects().length) {
                            try { btn.click(); } catch(e) {}
                            return true;
                        }
                    }
                    return false;
                }""")
            except Exception:
                continue
    except Exception:
        pass

    # 2) Arrancar DOM + CSS + MutationObserver persistente (reaparece tras cada navegación)
    try:
        page.evaluate("""() => {
            const KILL_SEL = [
                '#onetrust-consent-sdk', '#onetrust-banner-sdk', '#onetrust-style',
                '.onetrust-pc-dark-filter', '.ot-sdk-container', '.ot-cookie-policy',
                '[id*="onetrust" i]', '[class*="onetrust" i]',
                '#cookie-consent', '#cookiebanner',
                '[id*="cookie-banner" i]', '[class*="cookie-banner" i]',
                '[class*="cookie-consent" i]', '[class*="cookiebanner" i]',
                '[aria-label*="cookie" i]', '[aria-label*="consent" i]'
            ].join(',');

            const matar = () => {
                try {
                    document.querySelectorAll(KILL_SEL).forEach(el => {
                        try {
                            el.style.setProperty('display', 'none', 'important');
                            el.style.setProperty('visibility', 'hidden', 'important');
                            el.style.setProperty('pointer-events', 'none', 'important');
                            el.style.setProperty('opacity', '0', 'important');
                            el.style.setProperty('z-index', '-1', 'important');
                            el.remove();
                        } catch(e) {}
                    });
                    // Filtros oscuros fijos que bloquean clics aunque no digan "cookie"
                    document.querySelectorAll('div, section, aside').forEach(el => {
                        try {
                            if (el.id === 'anti-cookie-overlay-style') return;
                            const st = window.getComputedStyle(el);
                            if (st.position !== 'fixed' && st.position !== 'absolute') return;
                            const zi = parseInt(st.zIndex || '0', 10);
                            if (zi < 1000) return;
                            const idc = ((el.id || '') + ' ' + (el.className || '')).toLowerCase();
                            if (!/cookie|consent|onetrust|ot-sdk|banner/.test(idc)) return;
                            el.style.setProperty('pointer-events', 'none', 'important');
                            el.style.setProperty('display', 'none', 'important');
                            el.remove();
                        } catch(e) {}
                    });
                } catch(e) {}
            };

            matar();

            if (!document.getElementById('anti-cookie-overlay-style-invite')) {
                const st = document.createElement('style');
                st.id = 'anti-cookie-overlay-style-invite';
                st.textContent = `
                    #onetrust-consent-sdk, #onetrust-banner-sdk, .onetrust-pc-dark-filter,
                    .ot-sdk-container, .ot-cookie-policy, [id*="onetrust" i],
                    [class*="onetrust" i], [id*="cookie-banner" i], [class*="cookie-banner" i],
                    [class*="cookie-consent" i], [class*="cookiebanner" i] {
                        display: none !important;
                        visibility: hidden !important;
                        pointer-events: none !important;
                        opacity: 0 !important;
                        z-index: -1 !important;
                        height: 0 !important;
                        max-height: 0 !important;
                        overflow: hidden !important;
                    }
                `;
                (document.head || document.documentElement).appendChild(st);
            }

            if (!window.__tidalInviteCookieKiller) {
                window.__tidalInviteCookieKiller = true;
                const obs = new MutationObserver(() => matar());
                try {
                    obs.observe(document.documentElement, { childList: true, subtree: true });
                } catch(e) {}
                setInterval(matar, 800);
            }
            // body libre de bloqueo residual
            try {
                document.documentElement.style.overflow = '';
                document.body.style.overflow = '';
                document.body.style.pointerEvents = '';
            } catch(e) {}
        }""")
    except Exception:
        pass

    # Invalidar caché de aceptar_cookies para que un call posterior no se salte
    try:
        marcas = getattr(page, "_marcas_chequeo", None)
        if isinstance(marcas, dict):
            marcas.pop("cookies", None)
    except Exception:
        pass


def _invite_eval_modo_contrasena(page, accion: str):
    """Localiza/pulsa 'Inicia sesión con contraseña'. accion: 'existe' | 'coords' | 'click'."""
    js = """(args) => {
        const re = new RegExp(args.patron, 'i');
        const visible = (el) => {
            const st = window.getComputedStyle(el);
            if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') < 0.1) return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        const buscar = (root) => {
            const cand = Array.from(root.querySelectorAll('a, button, [role="button"], span, div, p, li, label'))
                .filter(el => visible(el))
                .filter(el => {
                    const t = (el.textContent || '').trim();
                    return t.length > 0 && t.length < 80 && re.test(t);
                });
            if (cand.length) {
                cand.sort((a, b) => (a.textContent || '').trim().length - (b.textContent || '').trim().length);
                return cand[0];
            }
            return null;
        };
        const el = buscar(document);
        if (!el) return null;
        const objetivo = el.closest('a, button, [role="button"]') || el;
        if (args.accion === 'existe') return true;
        if (args.accion === 'click') { objetivo.click(); return true; }
        el.scrollIntoView({block: 'center'});
        const r = objetivo.getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    }"""
    try:
        return page.evaluate(js, {
            "patron": _PATRON_MODO_CONTRASENA_INVITE,
            "accion": accion,
        })
    except Exception:
        return None


def _invite_hay_pantalla_codigo(page) -> bool:
    try:
        return bool(page.evaluate("""() => {
            const txt = document.body ? document.body.innerText.toLowerCase() : '';
            const frases = ['revisa tu correo', 'check your email', 'te hemos enviado un código',
                            'te hemos enviado un codigo', "we've sent", 'we have sent',
                            'reenviar código', 'reenviar codigo', 'resend code',
                            'código de acceso', 'access code', 'one-time'];
            if (frases.some(f => txt.includes(f))) return true;
            return document.querySelectorAll('input[maxlength="1"], input[autocomplete="one-time-code"]').length >= 4;
        }"""))
    except Exception:
        return False


def _invite_clic_modo_contrasena(page) -> bool:
    coords = _invite_eval_modo_contrasena(page, "coords")
    if coords:
        try:
            page.mouse.click(coords["x"], coords["y"])
            return True
        except Exception:
            pass
    return bool(_invite_eval_modo_contrasena(page, "click"))


def _invite_detectar_exito(page) -> bool:
    """True si la invitación familiar ya quedó aceptada."""
    try:
        u = (page.url or "").lower()
    except Exception:
        u = ""
    if "/success" in u:
        return True
    if "family" in u and "/accept" not in u and "/login" not in u and "signin" not in u and "authorize" not in u:
        if "tidal.com" in u or "account." in u:
            return True
    try:
        if page.locator(
            "text=Ya está todo"
        ).or_(page.locator("text=You're all set")).or_(
            page.locator("text=all set")
        ).or_(page.locator("text=preparado")).or_(
            page.locator("text=bienvenido")
        ).or_(page.locator("text=Welcome to the family")).or_(
            page.locator("text=te has unido")
        ).or_(page.locator("text=You've joined")).or_(
            page.locator("text=joined the family")
        ).count() > 0:
            return True
    except Exception:
        pass
    # Texto en body (más tolerante a markup)
    try:
        txt = (page.inner_text("body") or "").lower()
        for frag in (
            "ya está todo", "you're all set", "youre all set", "all set",
            "welcome to the family", "te has unido", "you've joined",
            "joined the family", "formas parte", "you're in",
        ):
            if frag in txt:
                return True
    except Exception:
        pass
    return False


def _invite_pulsar_aceptar(page) -> bool:
    """Pulsa el CTA real de aceptación (nunca el 'Aceptar' de cookies)."""
    _invite_limpiar_cookies_agresivo(page)
    selectores = [
        "button:has-text('Aceptar invitación')",
        "button:has-text('Accept invitation')",
        "button:has-text('Join family')",
        "button:has-text('Join the family')",
        "button:has-text('Unirse a la familia')",
        "button:has-text('Unirse al plan')",
        "button:has-text('Unirse')",
        "a:has-text('Aceptar invitación')",
        "a:has-text('Accept invitation')",
        "a:has-text('Join family')",
        "a:has-text('Join the family')",
        "button[data-test*='accept' i]",
        "button[data-testid*='accept' i]",
    ]
    # Sin timeout largo: este helper se llama en un bucle 1/s
    btn = encontrar_locator_en_frames(page, selectores)
    if btn:
        try:
            if not btn.is_visible():
                btn = None
        except Exception:
            pass
    if btn:
        try:
            btn.click(timeout=3000, force=True)
            return True
        except Exception:
            try:
                btn.evaluate("b => b.click()")
                return True
            except Exception:
                pass
    # Fallback JS: buscar botón/enlace con texto de invitación (evita cookies)
    try:
        clicked = page.evaluate("""() => {
            const re = /aceptar invitaci[oó]n|accept invitation|join family|join the family|unirse a la familia|unirse al plan/i;
            const skip = /cookie|preferenc|configur|settings|manage|onetrust/i;
            const cand = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            for (const el of cand) {
                const t = (el.innerText || el.textContent || '').trim();
                if (!t || t.length > 80) continue;
                if (skip.test(t)) continue;
                if (!re.test(t)) continue;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                const r = el.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) continue;
                el.click();
                return true;
            }
            return false;
        }""")
        return bool(clicked)
    except Exception:
        return False


def _invite_pulsar_continuar_o_login(page) -> bool:
    """Pulsa Continuar / Inicia Sesión. NUNCA 'Iniciar sesión con código'."""
    _invite_limpiar_cookies_agresivo(page)

    # Preferir el CTA principal por JS: excluye explícitamente el enlace de código
    # (has-text('Iniciar sesión') coincidía con "Iniciar sesión con código").
    try:
        clicked = page.evaluate("""() => {
            const esCodigo = (t) => /c[oó]digo|code|otp|one[- ]?time/i.test(t);
            const visible = (el) => {
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') < 0.1) return false;
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2;
            };
            const candidatos = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'))
                .filter(visible)
                .map(el => ({ el, t: (el.innerText || el.textContent || el.value || '').trim() }))
                .filter(x => x.t && !esCodigo(x.t));

            // 1) Exacto: Inicia Sesión / Log in / Continuar
            const exactos = [
                /^inicia\\s*sesi[oó]n$/i,
                /^iniciar\\s*sesi[oó]n$/i,
                /^log\\s*in$/i,
                /^sign\\s*in$/i,
                /^continuar$/i,
                /^continue$/i,
                /^login$/i,
            ];
            for (const re of exactos) {
                const hit = candidatos.find(x => re.test(x.t));
                if (hit) { hit.el.click(); return hit.t; }
            }

            // 2) type=submit visible sin texto de código
            const submit = candidatos.find(x =>
                (x.el.tagName === 'BUTTON' || x.el.tagName === 'INPUT') &&
                ((x.el.getAttribute('type') || '').toLowerCase() === 'submit' ||
                 x.el.closest('form'))
            );
            // Preferir submit con texto de login/continuar
            const submitLogin = candidatos.find(x =>
                (x.el.getAttribute('type') || '').toLowerCase() === 'submit' &&
                /sesi[oó]n|log\\s*in|sign\\s*in|continuar|continue/i.test(x.t)
            );
            if (submitLogin) { submitLogin.el.click(); return submitLogin.t; }

            // 3) Botón blanco/primario grande con texto corto de login
            const primario = candidatos.find(x =>
                x.t.length <= 24 &&
                /inicia\\s*sesi[oó]n|iniciar\\s*sesi[oó]n|log\\s*in|sign\\s*in|continuar|continue/i.test(x.t)
            );
            if (primario) { primario.el.click(); return primario.t; }

            if (submit && submit.t.length <= 30) { submit.el.click(); return submit.t || 'submit'; }
            return null;
        }""")
        if clicked:
            print(f"    [Invitación] Pulsado CTA de login: {clicked}")
            return True
    except Exception:
        pass

    # Fallback Playwright con exclusiones
    selectores = [
        "button[type='submit']",
        "button:has-text('Inicia Sesión')",
        "button:has-text('Inicia sesión')",
        "button:has-text('Continuar')",
        "button:has-text('Continue')",
        "button:has-text('Log in')",
        "button:has-text('Sign in')",
    ]
    for sel in selectores:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if not loc.is_visible():
                continue
            txt = ""
            try:
                txt = (loc.inner_text() or "").strip().lower()
            except Exception:
                pass
            if any(x in txt for x in ("código", "codigo", "code", "otp")):
                continue
            loc.click(timeout=2500, force=True)
            return True
        except Exception:
            continue
    return False


def _invite_avanzar_login(page, correo: str, pwd_cuenta: str | None, estado: dict) -> str:
    """Avanza un paso del login en la invitación. Devuelve: 'ok' | 'progreso' | 'esperar' | 'sin_pwd'."""
    _invite_limpiar_cookies_agresivo(page)

    # 1) ¿Ya hay botón de aceptar? (sesión lista)
    if _invite_pulsar_aceptar(page):
        print(f"    [Invitación] [{correo}] Pulsado botón de aceptación.")
        time.sleep(2.0)
        if _invite_detectar_exito(page):
            return "ok"
        return "progreso"

    if _invite_detectar_exito(page):
        return "ok"

    email_selectors = [
        'input[type="email"]', 'input[name="email"]',
        'input[autocomplete="email"]', '#email',
    ]
    pwd_selectors = ['input[type="password"]', 'input[name="password"]']

    # 2) Pantalla de código → forzar modo contraseña
    if _invite_hay_pantalla_codigo(page) or _invite_eval_modo_contrasena(page, "existe"):
        if _invite_eval_modo_contrasena(page, "existe"):
            if estado.get("intentos_modo_pwd", 0) < 5:
                estado["intentos_modo_pwd"] = estado.get("intentos_modo_pwd", 0) + 1
                print(f"    [Invitación] [{correo}] Pantalla de código detectada. "
                      f"Cambiando a modo contraseña ({estado['intentos_modo_pwd']}/5)...")
                _invite_clic_modo_contrasena(page)
                time.sleep(1.5)
                return "progreso"
        # Sin enlace a contraseña → usar código IMAP
        if not encontrar_locator_en_frames(page, pwd_selectors):
            if not pwd_cuenta and estado.get("codigo_intentado"):
                return "esperar"
            if estado.get("codigo_intentado"):
                return "esperar"
            estado["codigo_intentado"] = True
            print(f"    [Invitación] [{correo}] Sin modo contraseña visible. "
                  f"Obteniendo código de acceso por IMAP...")
            if not estado.get("baseline_id"):
                try:
                    estado["baseline_id"] = obtener_max_email_id(correo, "tidal")
                except Exception:
                    estado["baseline_id"] = 0
            codigo = None
            for intento in range(1, 13):
                codigo = obtener_codigo_via_imap(
                    gmail_user=correo,
                    required_keywords=["código", "code", "inici"],
                    query_exclude="cancel",
                    after_email_id=estado.get("baseline_id") or 0,
                )
                if codigo:
                    break
                print(f"    [Invitación] [{correo}] Esperando código IMAP ({intento}/12)...")
                time.sleep(8.0)
            if not codigo:
                print(f"    {Color.FAIL}[Invitación] [{correo}] No llegó el código de acceso.{Color.ENDC}")
                return "esperar"
            print(f"    [Invitación] [{correo}] Código obtenido: {codigo}. Escribiéndolo...")
            if escribir_codigo_verificacion_inteligente(page, codigo):
                time.sleep(1.0)
                _invite_pulsar_continuar_o_login(page)
                time.sleep(2.5)
                return "progreso"
            return "esperar"

    # 3) Campo de contraseña → rellenar y enviar
    pwd_inp = encontrar_locator_en_frames(page, pwd_selectors)
    if pwd_inp:
        try:
            visible = pwd_inp.is_visible()
        except Exception:
            visible = True
        if visible:
            if not pwd_cuenta:
                if not estado.get("aviso_sin_pwd"):
                    estado["aviso_sin_pwd"] = True
                    print(f"    {Color.FAIL}[Invitación] [{correo}] Campo contraseña visible pero "
                          f"no hay entrada en sesiones_imap_cuentas.txt.{Color.ENDC}")
                return "sin_pwd"
            try:
                val_p = pwd_inp.input_value()
            except Exception:
                val_p = ""
            if not val_p or val_p != pwd_cuenta:
                print(f"    [Invitación] [{correo}] Auto-completando contraseña "
                      f"desde sesiones_imap_cuentas.txt...")
                try:
                    rellenar_campo_humanizado(pwd_inp, pwd_cuenta)
                except Exception:
                    try:
                        pwd_inp.fill(pwd_cuenta)
                    except Exception:
                        pass
                time.sleep(0.3)
                try:
                    pwd_inp.dispatch_event("input")
                    pwd_inp.dispatch_event("change")
                except Exception:
                    pass
            if not _invite_pulsar_continuar_o_login(page):
                try:
                    pwd_inp.press("Enter")
                except Exception:
                    pass
            time.sleep(2.5)
            # Tras login puede aparecer el CTA de aceptar
            if _invite_pulsar_aceptar(page):
                time.sleep(2.0)
            if _invite_detectar_exito(page):
                return "ok"
            return "progreso"

    # 4) Campo de correo (ya viene rellenado por el enlace) → solo Continuar
    email_inp = encontrar_locator_en_frames(page, email_selectors)
    if email_inp:
        try:
            visible = email_inp.is_visible()
        except Exception:
            visible = True
        if visible:
            # El link de invitación ya trae el correo escrito: no reescribirlo
            # (reescribirlo puede romper el token / estado del formulario).
            try:
                val = (email_inp.input_value() or "").strip()
            except Exception:
                val = ""
            if not val:
                # Solo si Tidal dejó el campo vacío (raro): rellenar una vez
                print(f"    [Invitación] [{correo}] Correo vacío; colocándolo una vez...")
                try:
                    rellenar_campo_humanizado(email_inp, correo)
                except Exception:
                    try:
                        email_inp.fill(correo)
                    except Exception:
                        pass
                time.sleep(0.3)
            if not estado.get("baseline_id"):
                try:
                    estado["baseline_id"] = obtener_max_email_id(correo, "tidal")
                except Exception:
                    estado["baseline_id"] = 0
            if not estado.get("continuar_correo_pulsado"):
                print(f"    [Invitación] [{correo}] Correo ya presente. Pulsando Continuar...")
                estado["continuar_correo_pulsado"] = True
            if not _invite_pulsar_continuar_o_login(page):
                try:
                    email_inp.press("Enter")
                except Exception:
                    pass
            time.sleep(2.0)
            return "progreso"

    # 5) CTA genérico de unirse si hay texto de plan familiar
    try:
        txt = (page.inner_text("body") or "").lower()
        if any(x in txt for x in ("familia", "family", "invitaci", "invite", "plan")):
            if _invite_pulsar_aceptar(page):
                time.sleep(2.0)
                if _invite_detectar_exito(page):
                    return "ok"
                return "progreso"
            # A veces hay "Inicia sesión para aceptar" / "Log in to join" sin input aún
            if any(x in txt for x in ("inicia sesión", "iniciar sesión", "log in", "sign in")):
                # Reutilizar el CTA seguro (excluye "con código")
                if _invite_pulsar_continuar_o_login(page):
                    time.sleep(2.0)
                    return "progreso"
    except Exception:
        pass

    return "esperar"


def abrir_enlace_familia_con_autocierre(url: str, correo: str, proxy_pe: dict | None = None) -> None:
    """Abre el enlace de invitación, completa login+aceptación al 100% y cierra Chrome al éxito."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"{Color.WARNING}[Navegador]{Color.ENDC} Playwright no está instalado. Usando fallback...")
        abrir_enlace_en_perfil_chrome(url, correo)
        return

    if not proxy_pe or not proxy_pe.get("server"):
        # Nunca salir por la IP real: DataDome la marcaría y bloquearía todo el proceso a futuro
        print(f"    {Color.FAIL}[Navegador] [{correo}] Sin proxy de Perú disponible. Se omite la invitación "
              f"antes de exponer tu IP real.{Color.ENDC}")
        return

    email_safe = re.sub(r'[^a-zA-Z0-9]', '_', correo)
    profile_dir = Path(tempfile.gettempdir()) / f"tidal_chrome_profile_{email_safe}_{random.randint(1000, 9999)}"
    reparar_perfil_corrupto(profile_dir)
    
    print(f"    {Color.CYAN}[Navegador]{Color.ENDC} Iniciando automatización para unirse al plan familiar ({correo})...")
    
    with sync_playwright() as p:
        base_launch_kwargs = {
            "user_data_dir": str(profile_dir),
            "headless": False,
            "args": list(CHROME_SILENT_ARGS),
            "ignore_default_args": ["--enable-automation", "--no-sandbox"],
            "viewport": {"width": 1280, "height": 800},
            "locale": "es-ES",
            "channel": "chrome"
        }

        def abrir_contexto(proxy, prof_dir):
            kwargs = dict(base_launch_kwargs)
            kwargs["user_data_dir"] = str(prof_dir)
            p_serv = proxy.get("server", "")
            if p_serv and not p_serv.startswith("http"):
                p_serv = "http://" + p_serv
            kwargs["proxy"] = {
                "server": p_serv,
                "username": proxy.get("username", ""),
                "password": proxy.get("password", "")
            }
            print(f"    [Proxy PE] [{correo}] Conectando mediante proxy de Perú: {p_serv}")
            try:
                ctx = p.chromium.launch_persistent_context(**kwargs)
            except Exception as e:
                print(f"    [Navegador] [WARN] Falló el lanzamiento inicial para {correo}: {e}. Reparando y reintentando...")
                reparar_perfil_corrupto(prof_dir)
                ctx = p.chromium.launch_persistent_context(**kwargs)
            ctx.set_default_navigation_timeout(60000)
            ctx.set_default_timeout(35000)
            ctx.add_init_script(STEALTH_SCRIPT)
            ctx.add_init_script(_INVITE_COOKIE_KILLER_INIT)
            return ctx

        # 1. Cargar el enlace de invitación. Entrar en frío al enlace desde una IP recién estrenada
        #    dispara el antirobot: primero se visita una página pública para que DataDome le asigne
        #    reputación a la IP y sólo entonces se salta al enlace con un referer orgánico.
        #    Ante ERR_TUNNEL/timeout (proxy muerto) hay que rotar IP — antes solo se rotaba
        #    ante antibot y el mismo proxy muerto se reintentaba 3 veces (get.mushroom2.0.48).
        current_proxy = proxy_pe
        context = None
        page = None
        nav_inv_ok = False
        motivo_fallo = "desconocido"
        _max_intentos_inv = 5

        def _cerrar_contexto():
            nonlocal context, page
            try:
                if context:
                    context.close()
            except Exception:
                pass
            context = None
            page = None

        def _rotar_proxy_y_perfil(razon: str) -> bool:
            """Rota a un proxy PE limpio y descarta el perfil contaminado/roto. False si no hay IP."""
            nonlocal current_proxy, profile_dir
            print(f"    {Color.WARNING}[Invitación] [{correo}] {razon}. Rotando a un proxy de Perú limpio...{Color.ENDC}")
            nuevo_proxy = GLOBAL_PE_PROXY_POOL.rotar_y_marcar_bloqueado(
                (current_proxy or {}).get("server")
            )
            if not nuevo_proxy or not nuevo_proxy.get("server"):
                print(f"    {Color.FAIL}[Invitación] [{correo}] No quedan proxies de Perú limpios.{Color.ENDC}")
                return False
            current_proxy = nuevo_proxy
            _cerrar_contexto()
            perfil_quemado = profile_dir
            profile_dir = Path(tempfile.gettempdir()) / f"tidal_chrome_profile_{email_safe}_{random.randint(1000, 9999)}"
            reparar_perfil_corrupto(profile_dir)
            try:
                shutil.rmtree(perfil_quemado, ignore_errors=True)
            except Exception:
                pass
            return True

        for intento_inv in range(1, _max_intentos_inv + 1):
            if context is None:
                context = abrir_contexto(current_proxy, profile_dir)
                page = context.pages[0] if context.pages else context.new_page()

            try:
                print(f"    [Invitación] [{correo}] Calentando reputación en tidal.com/pricing "
                      f"(intento {intento_inv}/{_max_intentos_inv})...")
                navegar_tidal_tolerante(page, "https://tidal.com/pricing", timeout_ms=45000)
                time.sleep(random.uniform(2.0, 3.5))
                aceptar_cookies_con_espera(page)
                _invite_limpiar_cookies_agresivo(page)
                time.sleep(random.uniform(0.5, 1.0))

                print(f"    [Invitación] [{correo}] Cargando enlace de invitación con referer orgánico...")
                navegar_tidal_tolerante(
                    page, url,
                    referer="https://tidal.com/pricing",
                    timeout_ms=60000,
                )
                time.sleep(2.0)
                _invite_limpiar_cookies_agresivo(page)
                # Defensa: ERR_ABORTED no debe dejar la pestaña en /pricing como "éxito"
                try:
                    url_post = (page.url or "").lower()
                except Exception:
                    url_post = ""
                if url_es_pagina_marketing(url_post) or not url_es_flujo_invitacion_familiar(url_post):
                    raise RuntimeError(
                        f"Tras abrir el enlace de invitación la pestaña sigue en "
                        f"{(url_post or '?')[:90]} (se esperaba login/accept/family, no pricing)."
                    )
            except Exception as e_inv:
                print(f"    [Invitación] [WARN] Intento {intento_inv}/{_max_intentos_inv} de carga "
                      f"falló para {correo}: {e_inv}")
                motivo_fallo = "proxy/red"
                if intento_inv >= _max_intentos_inv:
                    break
                # Túnel muerto / timeout / quedarse en pricing: rotar IP.
                # ERR_ABORTED suave sin quedarse en marketing: reintentar mismo proxy.
                msg_inv = str(e_inv).lower()
                quedo_en_pricing = "pricing" in msg_inv or "sigue en" in msg_inv
                if es_error_proxy_o_red(e_inv) or "timeout" in msg_inv or quedo_en_pricing:
                    if not _rotar_proxy_y_perfil("Fallo de túnel/proxy o redirección a pricing al abrir la invitación"):
                        break
                elif es_error_navegacion_abortada(e_inv):
                    time.sleep(2.0)
                else:
                    time.sleep(2.0)
                    if not _rotar_proxy_y_perfil("Error de carga al abrir la invitación"):
                        break
                continue

            if not detectar_pantalla_antirobot(page):
                # Doble check: no marcar OK si seguimos en marketing
                try:
                    if url_es_pagina_marketing(page.url or ""):
                        raise RuntimeError("Antirobot limpio pero la pestaña sigue en pricing/marketing.")
                except RuntimeError:
                    motivo_fallo = "proxy/red"
                    if intento_inv >= _max_intentos_inv:
                        break
                    if not _rotar_proxy_y_perfil("Pestaña quedó en pricing tras el enlace"):
                        break
                    continue
                nav_inv_ok = True
                break

            motivo_fallo = "antirobot"
            if intento_inv >= _max_intentos_inv:
                break
            if not _rotar_proxy_y_perfil("Antirobot detectado"):
                break

        if not nav_inv_ok:
            if motivo_fallo == "proxy/red":
                print(f"    {Color.FAIL}[Invitación] [{correo}] No se pudo abrir el enlace por fallo "
                      f"de proxy/red tras {_max_intentos_inv} intentos. Se omite esta invitación.{Color.ENDC}")
            else:
                print(f"    {Color.FAIL}[Invitación] [{correo}] No se pudo abrir el enlace sin bloqueo "
                      f"antirobot. Se omite esta invitación.{Color.ENDC}")
            _cerrar_contexto()
            try:
                GLOBAL_PE_PROXY_POOL.liberar_proxy((current_proxy or {}).get("server"))
            except Exception:
                pass
            return

        aceptar_cookies_con_espera(page)
        _invite_limpiar_cookies_agresivo(page)

        pwd_cuenta = buscar_contrasena_cuenta(correo)
        if pwd_cuenta:
            print(f"    [Invitación] [{correo}] Contraseña cargada desde sesiones_imap_cuentas.txt.")
        else:
            print(f"    {Color.WARNING}[Invitación] [{correo}] No hay contraseña en "
                  f"sesiones_imap_cuentas.txt — se intentará código IMAP si Tidal lo pide. "
                  f"Anota 'correo<TAB>contraseña' para login por contraseña.{Color.ENDC}")

        # 2. Login + aceptación 100% automática (correo → modo pwd/código → aceptar → cerrar)
        success_detected = False
        reabiertos_enlace = 0
        estado_login = {}
        print(f"    [Invitación] [{correo}] Completando aceptación automática (hasta 5 minutos)...")
        for check_sec in range(300):
            try:
                if page is None:
                    break
                try:
                    url_actual = (page.url or "").lower()
                except Exception:
                    url_actual = ""

                # Si volvimos a /pricing (enlace abortado), reabrir el invite
                if url_es_pagina_marketing(url_actual):
                    if reabiertos_enlace < 4:
                        reabiertos_enlace += 1
                        print(f"    [Invitación] [{correo}] Pestaña en marketing/pricing. "
                              f"Reabriendo enlace ({reabiertos_enlace}/4)...")
                        try:
                            navegar_tidal_tolerante(
                                page, url,
                                referer="https://tidal.com/pricing",
                                timeout_ms=45000,
                            )
                            time.sleep(2.0)
                            _invite_limpiar_cookies_agresivo(page)
                        except Exception as e_re:
                            print(f"    [Invitación] [{correo}] [WARN] Reapertura falló: {e_re}")
                    time.sleep(1.0)
                    continue

                # Cookies/overlays: cada 2s (el banner OneTrust reaparece y tapa Continuar)
                if check_sec % 2 == 0:
                    _invite_limpiar_cookies_agresivo(page)

                if _invite_detectar_exito(page):
                    success_detected = True
                    break

                resultado = _invite_avanzar_login(page, correo, pwd_cuenta, estado_login)
                if resultado == "ok":
                    success_detected = True
                    break
                if resultado == "sin_pwd":
                    # Sin contraseña y sin avance: seguir intentando código IMAP si aparece
                    time.sleep(1.0)
                    continue
                if resultado == "progreso":
                    time.sleep(0.8)
                    continue
                # "esperar": sin acción clara este segundo
            except Exception as e_loop:
                if check_sec % 30 == 0:
                    print(f"    [Invitación] [{correo}] [WARN] Bucle: {e_loop}")
            time.sleep(1.0)

        if success_detected:
            print(f"    {Color.GREEN}[OK] ¡Invitación familiar aceptada correctamente para {correo}! "
                  f"Cerrando ventana de Chrome...{Color.ENDC}")
            _cerrar_contexto()
            try:
                shutil.rmtree(profile_dir, ignore_errors=True)
            except Exception:
                pass
        else:
            print(f"    {Color.WARNING}[WARN] No se completó la aceptación automática para {correo} "
                  f"en el tiempo límite. La ventana permanece abierta para revisión.{Color.ENDC}")

        try:
            GLOBAL_PE_PROXY_POOL.liberar_proxy((current_proxy or {}).get("server"))
        except Exception:
            pass


def abrir_enlace_restablecimiento_con_autocierre(url: str, correo: str, proxy_pe: dict | None = None) -> bool:
    """Abre el enlace de restablecimiento, coloca la contraseña de sesiones_imap_cuentas.txt,
    envía el formulario y cierra Chrome al completar con éxito.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"{Color.WARNING}[Navegador]{Color.ENDC} Playwright no está instalado. Usando fallback...")
        abrir_enlace_en_perfil_chrome(url, correo)
        return False

    pwd_cuenta = buscar_contrasena_cuenta(correo)
    if not pwd_cuenta:
        print(f"    {Color.FAIL}[Reset] [{correo}] No hay contraseña en sesiones_imap_cuentas.txt. "
              f"Añade 'correo\\tcontraseña' y reintenta. Se omite el auto-relleno.{Color.ENDC}")
        # Aun así abrimos el enlace para intervención manual (sin proxy PE no se abre).
    else:
        print(f"    [Reset] [{correo}] Contraseña a restablecer cargada desde sesiones_imap_cuentas.txt.")

    if not proxy_pe or not proxy_pe.get("server"):
        print(f"    {Color.FAIL}[Navegador] [{correo}] Sin proxy de Perú disponible. Se omite el "
              f"restablecimiento antes de exponer tu IP real.{Color.ENDC}")
        return False

    email_safe = re.sub(r'[^a-zA-Z0-9]', '_', correo)
    profile_dir = Path(tempfile.gettempdir()) / f"tidal_reset_link_{email_safe}_{random.randint(1000, 9999)}"
    reparar_perfil_corrupto(profile_dir)

    print(f"    {Color.CYAN}[Navegador]{Color.ENDC} Automatizando restablecimiento de contraseña ({correo})...")

    success_detected = False
    with sync_playwright() as p:
        base_launch_kwargs = {
            "user_data_dir": str(profile_dir),
            "headless": False,
            "args": list(CHROME_SILENT_ARGS),
            "ignore_default_args": ["--enable-automation", "--no-sandbox"],
            "viewport": {"width": 1280, "height": 800},
            "locale": "es-ES",
            "channel": "chrome",
        }

        def abrir_contexto(proxy, prof_dir):
            kwargs = dict(base_launch_kwargs)
            kwargs["user_data_dir"] = str(prof_dir)
            p_serv = proxy.get("server", "")
            if p_serv and not p_serv.startswith("http"):
                p_serv = "http://" + p_serv
            kwargs["proxy"] = {
                "server": p_serv,
                "username": proxy.get("username", ""),
                "password": proxy.get("password", ""),
            }
            print(f"    [Proxy PE] [{correo}] Conectando mediante proxy de Perú: {p_serv}")
            try:
                ctx = p.chromium.launch_persistent_context(**kwargs)
            except Exception as e:
                print(f"    [Navegador] [WARN] Falló el lanzamiento inicial para {correo}: {e}. "
                      f"Reparando y reintentando...")
                reparar_perfil_corrupto(prof_dir)
                ctx = p.chromium.launch_persistent_context(**kwargs)
            ctx.set_default_navigation_timeout(60000)
            ctx.set_default_timeout(35000)
            ctx.add_init_script(STEALTH_SCRIPT)
            return ctx

        current_proxy = proxy_pe
        context = None
        page = None
        nav_ok = False
        motivo_fallo = "desconocido"
        _max_intentos = 5

        def _cerrar_contexto():
            nonlocal context, page
            try:
                if context:
                    context.close()
            except Exception:
                pass
            context = None
            page = None

        def _rotar_proxy_y_perfil(razon: str) -> bool:
            nonlocal current_proxy, profile_dir
            print(f"    {Color.WARNING}[Reset] [{correo}] {razon}. Rotando a un proxy de Perú limpio...{Color.ENDC}")
            nuevo_proxy = GLOBAL_PE_PROXY_POOL.rotar_y_marcar_bloqueado(
                (current_proxy or {}).get("server")
            )
            if not nuevo_proxy or not nuevo_proxy.get("server"):
                print(f"    {Color.FAIL}[Reset] [{correo}] No quedan proxies de Perú limpios.{Color.ENDC}")
                return False
            current_proxy = nuevo_proxy
            _cerrar_contexto()
            perfil_quemado = profile_dir
            profile_dir = Path(tempfile.gettempdir()) / f"tidal_reset_link_{email_safe}_{random.randint(1000, 9999)}"
            reparar_perfil_corrupto(profile_dir)
            try:
                shutil.rmtree(perfil_quemado, ignore_errors=True)
            except Exception:
                pass
            return True

        for intento in range(1, _max_intentos + 1):
            if context is None:
                context = abrir_contexto(current_proxy, profile_dir)
                page = context.pages[0] if context.pages else context.new_page()

            try:
                print(f"    [Reset] [{correo}] Calentando reputación en tidal.com/pricing "
                      f"(intento {intento}/{_max_intentos})...")
                navegar_tidal_tolerante(page, "https://tidal.com/pricing", timeout_ms=45000)
                time.sleep(random.uniform(2.0, 3.5))
                aceptar_cookies_con_espera(page)
                time.sleep(random.uniform(0.5, 1.0))

                print(f"    [Reset] [{correo}] Abriendo enlace de restablecimiento...")
                navegar_tidal_tolerante(
                    page, url,
                    referer="https://tidal.com/pricing",
                    timeout_ms=60000,
                )
                time.sleep(2.0)
                aceptar_cookies_con_espera(page)
            except Exception as e_nav:
                print(f"    [Reset] [WARN] Intento {intento}/{_max_intentos} de carga falló "
                      f"para {correo}: {e_nav}")
                motivo_fallo = "proxy/red"
                if intento >= _max_intentos:
                    break
                msg = str(e_nav).lower()
                if es_error_proxy_o_red(e_nav) or "timeout" in msg:
                    if not _rotar_proxy_y_perfil("Fallo de túnel/proxy al abrir el enlace de reset"):
                        break
                elif es_error_navegacion_abortada(e_nav):
                    time.sleep(2.0)
                else:
                    time.sleep(2.0)
                    if not _rotar_proxy_y_perfil("Error de carga al abrir el enlace de reset"):
                        break
                continue

            if detectar_pantalla_antirobot(page):
                motivo_fallo = "antirobot"
                if intento >= _max_intentos:
                    break
                if not _rotar_proxy_y_perfil("Antirobot detectado"):
                    break
                continue

            # Esperar formulario de nueva contraseña
            pwd_field = None
            try:
                pwd_field = esperar_locator_en_frames(
                    page,
                    [
                        'input[name="newPassword"]',
                        'input[type="password"]',
                        'input[name="password"]',
                        'input[name="confirmNewPassword"]',
                    ],
                    timeout_s=18.0,
                )
            except Exception:
                pwd_field = None

            if pwd_field:
                nav_ok = True
                break

            print(f"    [Reset] [{correo}] Enlace abierto pero sin formulario de contraseña "
                  f"(intento {intento}/{_max_intentos}).")
            motivo_fallo = "sin_formulario"
            if intento >= _max_intentos:
                break
            if not _rotar_proxy_y_perfil("Formulario de reset no visible"):
                break

        if not nav_ok:
            if motivo_fallo == "antirobot":
                print(f"    {Color.FAIL}[Reset] [{correo}] No se pudo abrir el enlace sin bloqueo "
                      f"antirobot.{Color.ENDC}")
            else:
                print(f"    {Color.FAIL}[Reset] [{correo}] No se pudo cargar el formulario de "
                      f"restablecimiento tras {_max_intentos} intentos.{Color.ENDC}")
            _cerrar_contexto()
            try:
                GLOBAL_PE_PROXY_POOL.liberar_proxy((current_proxy or {}).get("server"))
            except Exception:
                pass
            return False

        # Rellenar nueva contraseña + confirmación y enviar
        if pwd_cuenta:
            try:
                print(f"    [Reset] [{correo}] Colocando contraseña a restablecer...")
                pwd_new1 = page.locator(
                    'input[name="newPassword"], input[type="password"], input[name="password"]'
                ).first
                if esperar_visibilidad(pwd_new1, 15000):
                    rellenar_campo_humanizado(pwd_new1, pwd_cuenta)
                    try:
                        pwd_new2 = page.locator(
                            'input[name="confirmNewPassword"], input[id*="confirm" i]'
                        ).first
                        if pwd_new2.count() > 0 and pwd_new2.is_visible():
                            rellenar_campo_humanizado(pwd_new2, pwd_cuenta)
                    except Exception:
                        pass
                    time.sleep(0.8)

                    btn_submit = (
                        page.locator("button[type='submit']")
                        .or_(page.locator("button:has-text('Restablecer contraseña')"))
                        .or_(page.locator("button:has-text('Reset password')"))
                        .or_(page.locator("button:has-text('Guardar')"))
                        .or_(page.locator("button:has-text('Save')"))
                        .or_(page.locator("button:has-text('Continuar')"))
                        .or_(page.locator("button:has-text('Continue')"))
                        .first
                    )
                    if esperar_visibilidad(btn_submit, 8000):
                        try:
                            btn_submit.evaluate("el => el.click()")
                        except Exception:
                            try:
                                btn_submit.click(force=True)
                            except Exception:
                                page.keyboard.press("Enter")
                    else:
                        page.keyboard.press("Enter")

                    # Confirmar éxito (URL / texto) unos segundos
                    for _ in range(20):
                        time.sleep(0.5)
                        try:
                            u = (page.url or "").lower()
                        except Exception:
                            u = ""
                        if any(x in u for x in ("/success", "/login", "signin", "account.tidal")):
                            # Evitar falsos positivos si seguimos en la misma página de reset
                            if "reset" not in u and "newpassword" not in u and "token" not in u:
                                success_detected = True
                                break
                        try:
                            if page.locator(
                                "text=contraseña se ha restablecido"
                            ).or_(page.locator("text=password has been reset")).or_(
                                page.locator("text=Password updated")
                            ).or_(page.locator("text=contraseña actualizada")).or_(
                                page.locator("text=Ya puedes iniciar")
                            ).or_(page.locator("text=You can now")).count() > 0:
                                success_detected = True
                                break
                        except Exception:
                            pass
                        # Si el formulario de nueva contraseña desapareció, dar por bueno
                        try:
                            still = page.locator('input[name="newPassword"]').first
                            if still.count() == 0 or not still.is_visible():
                                # Puede haber navegado; comprobar que no sea error
                                if "error" not in u and "blocked" not in u:
                                    success_detected = True
                                    break
                        except Exception:
                            success_detected = True
                            break

                    if not success_detected:
                        # Tras submit, si no hay error claro, asumir OK (Tidal a veces no cambia URL)
                        try:
                            err = page.locator("text=Algo salió mal").or_(
                                page.locator("text=Something went wrong")
                            ).or_(page.locator("text=invalid")).count()
                            if err == 0:
                                success_detected = True
                        except Exception:
                            success_detected = True
                else:
                    print(f"    {Color.WARNING}[Reset] [{correo}] Campo de nueva contraseña no visible.{Color.ENDC}")
            except Exception as e_fill:
                print(f"    {Color.FAIL}[Reset] [{correo}] Error al rellenar/enviar contraseña: {e_fill}{Color.ENDC}")
        else:
            print(f"    {Color.WARNING}[Reset] [{correo}] Sin contraseña anotada: deja la ventana abierta "
                  f"para completar manualmente.{Color.ENDC}")
            # Esperar un poco por si el usuario completa a mano y detectar éxito
            for _ in range(180):
                time.sleep(1.0)
                try:
                    u = (page.url or "").lower()
                    if ("reset" not in u and "token" not in u) and any(
                        x in u for x in ("/success", "/login", "signin", "account.tidal")
                    ):
                        success_detected = True
                        break
                except Exception:
                    pass

        if success_detected:
            print(f"    {Color.GREEN}[OK] Contraseña restablecida correctamente para {correo}. "
                  f"Cerrando ventana de Chrome...{Color.ENDC}")
            _cerrar_contexto()
        else:
            print(f"    {Color.WARNING}[WARN] No se confirmó el restablecimiento automático para {correo}. "
                  f"La ventana permanecerá abierta para intervención manual.{Color.ENDC}")

        try:
            GLOBAL_PE_PROXY_POOL.liberar_proxy((current_proxy or {}).get("server"))
        except Exception:
            pass

    # Limpiar perfil temporal en segundo plano
    try:
        def _rm_async(p):
            time.sleep(1.0)
            try:
                shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass
        if success_detected:
            threading.Thread(target=_rm_async, args=(profile_dir,), daemon=True).start()
    except Exception:
        pass

    return success_detected


def obtener_credenciales_imap_reales(gmail_user_solicitado: str) -> tuple[str | None, str | None]:
    """Busca en passwords.txt el usuario real de IMAP y su App Password."""
    pwd_file = SCRIPT_DIR / "passwords.txt"
    if not pwd_file.exists():
        print(f"{Color.FAIL}[Error]{Color.ENDC} No se encuentra el archivo 'passwords.txt' en {pwd_file}.")
        return None, None
        
    try:
        lines = pwd_file.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        print(f"{Color.FAIL}[Error]{Color.ENDC} No se pudo leer 'passwords.txt': {e}")
        return None, None
    
    # 1. Limpiar el correo solicitado (remover puntos del username de Gmail)
    gmail_user_solicitado = gmail_user_solicitado.lower().strip()
    if "@gmail.com" in gmail_user_solicitado:
        username, domain = gmail_user_solicitado.split("@", 1)
        solicitado_no_dots = username.replace(".", "") + "@" + domain
    else:
        solicitado_no_dots = gmail_user_solicitado

    user_clean_key = solicitado_no_dots.replace("@", "_at_").replace(".", "_")
    
    # 2. Buscar si hay contraseña específica para el correo solicitado
    for line in lines:
        if "=" in line:
            key, val = line.split("=", 1)
            key_name = key.strip().lower()
            if key_name.startswith("gmail_app_password_") or key_name.startswith("imap_password_"):
                email_part = key_name[19:].strip() if key_name.startswith("gmail_app_password_") else key_name[14:].strip()
                if "@" in email_part:
                    usr, dom = email_part.split("@", 1)
                    email_part_no_dots = usr.replace(".", "") + "@" + dom
                    if email_part_no_dots == solicitado_no_dots:
                        return email_part, val.strip().strip('"').strip("'")
            
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
                if "cakeseller1234" in solicitado_no_dots:
                    return "cakeseller1234@gmail.com", val.strip().strip('"').strip("'")
                return solicitado_no_dots, val.strip().strip('"').strip("'")

    # 4. Fallback a la primera cuenta específica
    for line in lines:
        if "=" in line and line.strip().lower().startswith("gmail_app_password_"):
            key, val = line.split("=", 1)
            key_name = key.strip().lower()
            email_part = key_name[19:].strip()
            if "@" in email_part:
                return email_part, val.strip().strip('"').strip("'")
                
    return None, None


# Conexiones IMAP reutilizables por hilo. Cada consulta abría antes su propia conexión (login +
# logout); con 10 ventanas sondeando el mismo buzón en bucle, Gmail acababa respondiendo
# "Too many simultaneous connections" y la lectura del código fallaba de forma intermitente.
_IMAP_HILO = threading.local()


def _servidor_imap_para(user_real: str) -> str:
    dominio = user_real.split("@")[-1].lower() if "@" in user_real else "gmail.com"
    if any(k in dominio for k in ("outlook", "hotmail", "live.com")):
        return "outlook.office365.com"
    if "yahoo" in dominio:
        return "imap.mail.yahoo.com"
    if "icloud" in dominio:
        return "imap.mail.me.com"
    return "imap.gmail.com"


def cerrar_sesion_imap_hilo() -> None:
    """Cierra la conexión IMAP reutilizable del hilo actual (llamar al terminar cada cuenta)."""
    conexiones = getattr(_IMAP_HILO, "conexiones", None)
    if not conexiones:
        return
    for mail in list(conexiones.values()):
        try:
            mail.logout()
        except Exception:
            pass
    conexiones.clear()


@contextlib.contextmanager
def sesion_imap(user_real: str, app_pwd: str):
    """Entrega una conexión IMAP viva, reutilizando la del hilo si sigue usable.

    Garantiza además la limpieza: antes, si la búsqueda lanzaba una excepción, el logout no
    llegaba a ejecutarse porque no estaba en un finally y la conexión quedaba colgada.
    """
    if not hasattr(_IMAP_HILO, "conexiones"):
        _IMAP_HILO.conexiones = {}
        _IMAP_HILO.profundidad = 0
    clave = user_real.lower()

    # Re-entrante: si ya estamos dentro de una sesión en este hilo no se vuelve a tomar el
    # semáforo, que es acotado y provocaría un bloqueo mutuo.
    anidada = getattr(_IMAP_HILO, "profundidad", 0) > 0
    if not anidada:
        IMAP_SEMAFORO.acquire()
    _IMAP_HILO.profundidad = getattr(_IMAP_HILO, "profundidad", 0) + 1

    try:
        mail = _IMAP_HILO.conexiones.get(clave)
        if mail is not None:
            try:
                mail.noop()
            except Exception:
                try:
                    mail.logout()
                except Exception:
                    pass
                mail = None
                _IMAP_HILO.conexiones.pop(clave, None)

        if mail is None:
            ultimo_error = None
            for intento in range(1, 4):
                try:
                    mail = imaplib.IMAP4_SSL(_servidor_imap_para(user_real))
                    mail.login(user_real, app_pwd)
                    break
                except Exception as e:
                    ultimo_error = e
                    mail = None
                    if any(t in str(e).upper() for t in ("AUTHENTICATIONFAILED", "INVALID CREDENTIALS")):
                        # Contraseña de aplicación incorrecta: reintentar no cambia nada
                        break
                    # Gmail limita conexiones y logins simultáneos: esperar y reintentar
                    time.sleep(2.0 * intento + random.uniform(0.0, 1.0))
            if mail is None:
                raise RuntimeError(f"No se pudo conectar por IMAP a {user_real}: {ultimo_error}")
            _IMAP_HILO.conexiones[clave] = mail

        try:
            # SELECT en cada uso: refresca la vista del buzón para ver los correos recién llegados
            mail.select("INBOX")
            yield mail
        except Exception:
            # Conexión sospechosa: no reutilizarla en la siguiente consulta
            try:
                mail.logout()
            except Exception:
                pass
            _IMAP_HILO.conexiones.pop(clave, None)
            raise
    finally:
        _IMAP_HILO.profundidad = max(0, getattr(_IMAP_HILO, "profundidad", 1) - 1)
        if not anidada:
            try:
                IMAP_SEMAFORO.release()
            except Exception:
                pass


# UIDs de correo ya consumidos por algún hilo. Sin esto, N alias con puntos del mismo Gmail
# (opción 14) se robaban el código entre sí: todos normalizan a la misma cuenta IMAP.
_IMAP_UIDS_RECLAMADOS: dict[str, set[int]] = {}
_IMAP_UIDS_LOCK = threading.Lock()
# Un solo Suscríbete+lectura de código a la vez por buzón Gmail (aliases con puntos).
_IMAP_REGISTRO_LOCKS: dict[str, threading.Lock] = {}
_IMAP_REGISTRO_LOCKS_GUARD = threading.Lock()


def _lock_registro_mismo_buzon(gmail_user: str) -> threading.Lock:
    clave = _norm_dots_gmail(gmail_user)
    with _IMAP_REGISTRO_LOCKS_GUARD:
        lock = _IMAP_REGISTRO_LOCKS.get(clave)
        if lock is None:
            lock = threading.Lock()
            _IMAP_REGISTRO_LOCKS[clave] = lock
        return lock


def _norm_dots_gmail(correo: str) -> str:
    correo = (correo or "").strip().lower()
    if "@" not in correo:
        return correo
    usr, dom = correo.split("@", 1)
    if "gmail.com" in dom or "googlemail.com" in dom:
        usr = usr.split("+", 1)[0].replace(".", "")
    return f"{usr}@{dom}"


def _buzon_imap_clave(gmail_user: str, user_real: str | None = None) -> str:
    return _norm_dots_gmail(user_real or gmail_user)


def _reclamar_uid_correo(buzon_clave: str, uid: int) -> bool:
    """True si este hilo se queda con el UID; False si otro hilo ya lo usó."""
    if not uid:
        return True
    with _IMAP_UIDS_LOCK:
        usados = _IMAP_UIDS_RECLAMADOS.setdefault(buzon_clave, set())
        if uid in usados:
            return False
        usados.add(uid)
        if len(usados) > 400:
            _IMAP_UIDS_RECLAMADOS[buzon_clave] = set(sorted(usados)[-250:])
        return True


def _destinatario_es_para_alias(gmail_user: str, recipients_text: str, cuerpo_text: str = "") -> bool:
    """True solo si el correo de Tidal es para ESTE alias con puntos, no para un hermano.

    Gmail ignora puntos, así que getspoo.ky49.28 y getspoo.ky.4928 comparten buzón. Si se
    compara solo la forma sin puntos, un hilo toma el código del otro y el registro queda
    colgado en /authorize con el código incorrecto.

    Atención (opción 4): si To: solo trae la forma canónica sin puntos, el paso 3 devolvía
    True para TODOS los alias hermanos → un solo UID reclamado y el resto sin enlace.
    Para invitaciones familiares usar asignar_enlaces_invitacion_a_correos().
    """
    objetivo = (gmail_user or "").strip().lower()
    if not objetivo:
        return False
    objetivo_norm = _norm_dots_gmail(objetivo)
    texto = f"{recipients_text or ''} {cuerpo_text or ''}".lower()
    texto = texto.replace("%40", "@")
    addrs = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+", texto)
    # Alias exacto en texto (To/cuerpo), aunque el regex falle con basura HTML
    if objetivo in texto:
        return True
    if not addrs:
        return False

    # 1) Coincidencia exacta del alias con puntos (lo que Tidal suele poner en To:)
    if objetivo in addrs:
        return True

    mismos_buzon = [a for a in addrs if _norm_dots_gmail(a) == objetivo_norm]
    if not mismos_buzon:
        return False

    # 2) Hay otro alias con puntos distinto → este mensaje es de un hermano concurrente
    hermanos = [
        a for a in mismos_buzon
        if a != objetivo and a != objetivo_norm and "." in a.split("@", 1)[0]
    ]
    if hermanos:
        return False

    # 3) Solo aparece la forma canónica del buzón (Tidal a veces escribe el base).
    #    Para invitaciones con N alias concurrentes NO usar esta vía: option 4 llama a
    #    asignar_enlaces_invitacion_a_correos(). Aquí se mantiene True para códigos/login
    #    de un solo hilo (To: canónico + alias con puntos).
    return True


def _extraer_codigos_otp(texto: str) -> list[str]:
    """Extrae códigos OTP de 5–6 dígitos priorizando los junto a 'code'/'código'.

    Antes devolvía el primer número de 6 dígitos del HTML (fechas, tracking, etc.) y
    descartaba OTP reales tipo 202431 por parecer un año → Tidal rechazaba el código.
    """
    if not texto:
        return []
    prioritarios: list[str] = []
    normales: list[str] = []
    vistos: set[str] = set()

    def _es_basura(codigo: str, estricto_anio: bool) -> bool:
        if codigo in {"00000", "000000", "11111", "111111"}:
            return True
        # Fechas YYYYMM sueltas en HTML (202401…) no son OTP; si viene junto a 'code', sí se acepta.
        if estricto_anio and len(codigo) == 6 and codigo.startswith("20"):
            try:
                y, m = int(codigo[:4]), int(codigo[4:6])
                if 2020 <= y <= 2039 and 1 <= m <= 12:
                    return True
            except Exception:
                pass
        return False

    def _add(codigo: str, prioritario: bool = False) -> None:
        codigo = (codigo or "").strip()
        if len(codigo) not in (5, 6) or not codigo.isdigit():
            return
        if _es_basura(codigo, estricto_anio=not prioritario):
            return
        if codigo in vistos:
            return
        vistos.add(codigo)
        (prioritarios if prioritario else normales).append(codigo)

    # 1) Junto a palabras de código (máxima prioridad)
    for m in re.finditer(
        r"(?:c[oó]digo|code|sign[-\s]?up\s*code|verification\s*code|one[-\s]?time|"
        r"introduce|ingresa|enter|confirma|is)[^\d]{0,40}(\d{5,6})",
        texto,
        flags=re.I,
    ):
        _add(m.group(1), prioritario=True)

    # 2) Dígitos partidos por HTML/espacios: "1 2 3 4 5 6"
    for m in re.finditer(r"(?<!\d)(?:\d[\s\-]){4,5}\d(?!\d)", texto):
        _add(re.sub(r"\D", "", m.group(0)), prioritario=True)

    # 3) Cualquier 5–6 dígitos suelto (menor prioridad)
    for m in re.findall(r"\b(\d{5,6})\b", texto):
        _add(m, prioritario=False)

    # Preferir 6 dígitos (Tidal sign-up) dentro de cada grupo
    def _ordenar(grupo: list[str]) -> list[str]:
        return sorted(grupo, key=lambda c: (0 if len(c) == 6 else 1, grupo.index(c)))

    return _ordenar(prioritarios) + _ordenar(normales)


def _texto_indica_codigo_invalido(texto: str) -> bool:
    """True solo ante rechazo claro del OTP (no 'Resend' / textos genéricos)."""
    t = (texto or "").lower()
    if not t:
        return False
    frases = (
        "código no válido", "codigo no valido", "invalid code", "incorrect code",
        "código incorrecto", "codigo incorrecto", "wrong code", "code is incorrect",
        "código erróneo", "codigo erroneo", "el código no es válido",
        "the code is invalid", "code you entered is incorrect",
        "that code isn't valid", "that code isnt valid",
    )
    return any(f in t for f in frases)


KEYWORDS_INVITACION_FAMILIAR = [
    "invites you to join", "welcome to the family", "family plan", "family subscription",
    "plan familiar", "bienvenida a la familia", "unirte a su plan", "invited to a tidal family",
    "join their tidal family", "has invited you", "te ha invitado",
]


def _extraer_cuerpo_y_html_msg(msg) -> tuple[str, str]:
    """Devuelve (texto_plano_aprox, html_crudo) del mensaje IMAP."""
    body_text = ""
    html_raw = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain" and not body_text:
                    try:
                        plain_payload = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        body_text = re.split(
                            r'(?i)-+\s*Original Message\s*-+|^On.*wrote:|^El.*escribió:',
                            plain_payload
                        )[0]
                    except Exception:
                        pass
                elif content_type == "text/html":
                    try:
                        html = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        html_clean = re.sub(r'(?i)<div[^>]+class=["\']gmail_quote["\'][\s\S]*', '', html)
                        html_clean = re.sub(r'(?i)<blockquote[\s\S]*', '', html_clean)
                        if not html_raw:
                            html_raw = html_clean
                        if not body_text:
                            tmp = re.sub(r'<style[^>]*>[\s\S]*?</style>', ' ', html_clean, flags=re.I)
                            tmp = re.sub(r'<script[^>]*>[\s\S]*?</script>', ' ', tmp, flags=re.I)
                            body_text = re.sub(r'<[^>]+>', ' ', tmp)
                    except Exception:
                        pass
        else:
            try:
                raw_payload = msg.get_payload(decode=True).decode("utf-8", errors="replace")
                if msg.get_content_type() == "text/html":
                    html_raw = raw_payload
                    tmp = re.sub(r'<style[^>]*>[\s\S]*?</style>', ' ', raw_payload, flags=re.I)
                    body_text = re.sub(r'<[^>]+>', ' ', tmp)
                else:
                    body_text = re.split(
                        r'(?i)-+\s*Original Message\s*-+|^On.*wrote:|^El.*escribió:',
                        raw_payload
                    )[0]
            except Exception:
                pass
    except Exception:
        pass
    return body_text or "", html_raw or ""


def _extraer_enlace_invitacion_de_contenido(body_text: str, html_raw: str = "") -> str | None:
    """Prioriza enlaces de join/family; fallback a tracking ablink de Tidal."""
    JOIN_TEXTS = ["join", "unir", "nete", "invit", "accept", "acept", "family"]

    # Texto visible http en <a> hacia family/resetpass
    if html_raw:
        try:
            a_tags = re.findall(r'<a[^>]+href=["\'][^"\']+["\'][^>]*>([\s\S]*?)</a>', html_raw, re.I)
            for inner_html in a_tags:
                inner_text = re.sub(r'<[^>]+>', '', inner_html).strip()
                inner_lower = inner_text.lower()
                if inner_text.startswith("http") and (
                    "login.tidal.com/resetpass/" in inner_lower
                    or "login.tidal.com/family/" in inner_lower
                    or "/accept/" in inner_lower
                    or "/join/" in inner_lower
                ):
                    return inner_text
        except Exception:
            pass

        try:
            a_tags_full = re.findall(
                r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', html_raw, re.I
            )
            for href, inner_html in a_tags_full:
                inner_text = re.sub(r'<[^>]+>', '', inner_html).strip().lower()
                if any(jt in inner_text for jt in JOIN_TEXTS):
                    return href
        except Exception:
            pass

    enlaces: list[str] = []
    try:
        enlaces.extend(re.findall(r'https?://[^\s<>"\']+', body_text or ""))
    except Exception:
        pass
    if html_raw:
        try:
            enlaces.extend(re.findall(r'href=["\'](https?://[^"\']+)["\']', html_raw))
        except Exception:
            pass

    skip = ["/privacy", "/terms", "/legal", "support.tidal.com", "tidal.com/es",
            "tidal.com/en", "tidal.com/us"]
    for link in enlaces:
        link_lower = link.lower()
        if any(x in link_lower for x in skip):
            continue
        if ("login.tidal.com/family/" in link_lower or "/accept/" in link_lower
                or "/join/" in link_lower or "login.tidal.com/resetpass/" in link_lower):
            return link
    for link in enlaces:
        link_lower = link.lower()
        if any(x in link_lower for x in skip):
            continue
        if "tidal.com" in link_lower or "ablink.info.tidal.com" in link_lower:
            return link
    return None


def _puntuar_invitacion_para_alias(alias: str, recipients: str, cuerpo: str) -> int:
    """Score de atribución alias↔correo. >=85 fuerte; 1-84 débil/canónico; 0 incompatible."""
    alias = (alias or "").strip().lower()
    if not alias:
        return 0
    alias_norm = _norm_dots_gmail(alias)
    texto = f"{recipients or ''} {cuerpo or ''}".lower()
    addrs = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+", texto)

    if alias in addrs:
        return 100
    if alias in texto:
        return 90
    if alias.replace("@", "%40") in texto:
        return 85

    mismos = [a for a in addrs if _norm_dots_gmail(a) == alias_norm]
    hermanos = [
        a for a in mismos
        if a != alias and a != alias_norm and "." in a.split("@", 1)[0]
    ]
    if hermanos:
        return 0

    if alias_norm in addrs or alias_norm in texto:
        return 10
    # Correo llegó a este buzón IMAP: atribuible de forma débil a algún alias del grupo
    return 5


def listar_invitaciones_familiares_buzon(
    gmail_user: str,
    max_age_minutes: int = 120,
    max_mensajes: int = 40,
) -> list[dict]:
    """Lista invitaciones familiares recientes del buzón (sin filtrar por alias).

    Cada ítem: {uid, recipients, body, link}. Más recientes primero.
    """
    import email
    from email.header import decode_header
    from datetime import datetime, timezone

    user_real, app_pwd = obtener_credenciales_imap_reales(gmail_user)
    if not user_real or not app_pwd:
        print(f"    {Color.WARNING}[IMAP]{Color.ENDC} Sin credenciales IMAP para listar invitaciones de {gmail_user}.")
        return []

    resultados: list[dict] = []
    stack = contextlib.ExitStack()
    try:
        print(f"    {Color.CYAN}[IMAP]{Color.ENDC} Listando invitaciones familiares en "
              f"{_servidor_imap_para(user_real)} ({user_real})...")
        mail = stack.enter_context(sesion_imap(user_real, app_pwd))
        status, messages = mail.uid("search", None, '(FROM "tidal")')
        if status != "OK" or not messages or not messages[0]:
            return []

        msg_ids = messages[0].split()[-max_mensajes:]
        msg_ids.reverse()
        max_age_s = max(60, int((max_age_minutes or 120) * 60))

        for msg_id in msg_ids:
            try:
                msg_id_int = int(msg_id)
            except ValueError:
                msg_id_int = 0

            status, msg_data = mail.uid("fetch", msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])

            is_recent = False
            try:
                from email.utils import parsedate_to_datetime
                date_str = msg.get("Date")
                if date_str:
                    msg_date = parsedate_to_datetime(date_str)
                    age = (datetime.now(timezone.utc) - msg_date.astimezone(timezone.utc)).total_seconds()
                    is_recent = age <= max_age_s
            except Exception:
                is_recent = True
            if not is_recent:
                continue

            subject_text = ""
            try:
                subject_header = msg.get("Subject")
                if subject_header:
                    decoded = decode_header(subject_header)
                    parts = []
                    for part_bytes, charset in decoded:
                        if isinstance(part_bytes, bytes):
                            parts.append(part_bytes.decode(charset or "utf-8", errors="replace"))
                        else:
                            parts.append(part_bytes or "")
                    subject_text = "".join(parts)
            except Exception:
                pass

            body_text, html_raw = _extraer_cuerpo_y_html_msg(msg)
            text_to_check = f"{subject_text} {body_text}".lower()
            if not any(kw.lower() in text_to_check for kw in KEYWORDS_INVITACION_FAMILIAR):
                continue
            if "cancel" in text_to_check:
                continue

            link = _extraer_enlace_invitacion_de_contenido(body_text, html_raw)
            if not link:
                continue

            to_header = (msg.get("To") or "").lower()
            delivered_to = (msg.get("Delivered-To") or "").lower()
            envelope_to = (msg.get("Envelope-To") or "").lower()
            x_original = (msg.get("X-Original-To") or "").lower()
            x_forwarded = (msg.get("X-Forwarded-To") or "").lower()
            recipients = f"{to_header} {delivered_to} {envelope_to} {x_original} {x_forwarded}"

            resultados.append({
                "uid": msg_id_int,
                "recipients": recipients,
                "body": f"{subject_text}\n{body_text}\n{html_raw}",
                "link": link,
            })
    except Exception as e:
        print(f"    {Color.FAIL}[IMAP]{Color.ENDC} Error listando invitaciones: {e}")
    finally:
        stack.close()
    return resultados


def asignar_enlaces_invitacion_a_correos(correos: list[str]) -> dict[str, str]:
    """Asigna 1 enlace de invitación único por correo, coordinado por buzón Gmail.

    Evita el fallo de la opción 4: N alias con puntos del mismo Gmail → To: canónico →
    todos matchean el mismo UID → _reclamar_uid_correo deja pasar 1 y el resto queda sin enlace.
    """
    if not correos:
        return {}

    # Agrupar por buzón canónico (misma App Password / mismo inbox)
    grupos: dict[str, list[str]] = {}
    for correo in correos:
        user_real, _ = obtener_credenciales_imap_reales(correo)
        clave = _buzon_imap_clave(correo, user_real)
        grupos.setdefault(clave, []).append(correo)

    asignados: dict[str, str] = {}

    for buzon_clave, aliases in grupos.items():
        aliases_unicos = []
        vistos = set()
        for a in aliases:
            al = a.strip().lower()
            if al and al not in vistos:
                vistos.add(al)
                aliases_unicos.append(a.strip())

        if len(aliases_unicos) == 1:
            solo = aliases_unicos[0]
            enlace = obtener_codigo_via_imap(
                gmail_user=solo,
                required_keywords=KEYWORDS_INVITACION_FAMILIAR,
                query_exclude="cancel",
                solo_link=True,
                max_age_minutes=120,
            )
            if enlace:
                asignados[solo] = enlace
            continue

        print(f"    {Color.CYAN}[IMAP]{Color.ENDC} Buzón {_norm_dots_gmail(buzon_clave)}: "
              f"{len(aliases_unicos)} alias con puntos — asignación coordinada de invitaciones...")
        invitaciones = listar_invitaciones_familiares_buzon(aliases_unicos[0], max_age_minutes=120)
        if not invitaciones:
            print(f"    {Color.FAIL}[IMAP]{Color.ENDC} No hay invitaciones recientes en el buzón "
                  f"para {aliases_unicos[0]}.")
            continue

        pendientes = list(aliases_unicos)
        uids_usados: set[int] = set()

        def _mejor_alias(inv: dict, candidatos: list[str], min_score: int) -> tuple[str | None, int]:
            best_a, best_s = None, -1
            for a in candidatos:
                sc = _puntuar_invitacion_para_alias(a, inv["recipients"], inv["body"])
                if sc > best_s:
                    best_s, best_a = sc, a
            if best_a is None or best_s < min_score:
                return None, best_s
            return best_a, best_s

        # Pasada 1: matches fuertes (alias exacto en To/Delivered-To/cuerpo)
        for inv in invitaciones:
            if not pendientes:
                break
            uid = inv.get("uid") or 0
            if uid and uid in uids_usados:
                continue
            alias, score = _mejor_alias(inv, pendientes, min_score=85)
            if not alias:
                continue
            if uid and not _reclamar_uid_correo(buzon_clave, uid):
                continue
            asignados[alias] = inv["link"]
            pendientes.remove(alias)
            if uid:
                uids_usados.add(uid)
            print(f"    {Color.GREEN}[IMAP]{Color.ENDC} Invitación UID {uid} → {alias} "
                  f"(match fuerte, score={score})")

        # Pasada 2: To: canónico / sin puntos — repartir 1 UID libre por alias pendiente
        for inv in invitaciones:
            if not pendientes:
                break
            uid = inv.get("uid") or 0
            if uid and uid in uids_usados:
                continue
            alias, score = _mejor_alias(inv, pendientes, min_score=1)
            if not alias:
                # Cualquier invitación del buzón puede ir a un alias pendiente restante
                alias = pendientes[0]
                score = 5
            if uid and not _reclamar_uid_correo(buzon_clave, uid):
                continue
            asignados[alias] = inv["link"]
            pendientes.remove(alias)
            if uid:
                uids_usados.add(uid)
            print(f"    {Color.WARNING}[IMAP]{Color.ENDC} Invitación UID {uid} → {alias} "
                  f"(To: canónico / score={score}). Un enlace distinto por alias.")

        for a in pendientes:
            print(f"    {Color.FAIL}[IMAP]{Color.ENDC} Sin invitación libre en el buzón para {a} "
                  f"(¿faltó que el titular invitara este alias?).")

    return asignados


def obtener_codigo_via_imap(gmail_user="cakeseller1234@gmail.com", gmail_app_password=None, 
                             query_from="tidal", required_keywords=None, query_exclude=None, 
                             max_age_minutes=15, after_email_id=0, solo_link=False) -> str | None:
    """Lee correos de Gmail via IMAP sin necesidad de abrir el navegador.
    Requiere una 'App Password' de Google.
    Busca dinámicamente en passwords.txt la contraseña específica del correo."""
    import imaplib
    import email
    from email.header import decode_header
    from datetime import datetime, timezone
    
    user_real, app_pwd = obtener_credenciales_imap_reales(gmail_user)
    if not user_real or not app_pwd:
        print(f"    {Color.WARNING}[IMAP]{Color.ENDC} No se encontraron credenciales de IMAP válidas para {gmail_user}.")
        return None
    
    # ExitStack en lugar de un 'with' anidado para no reindentar todo el recorrido de mensajes:
    # el cierre queda garantizado igualmente al salir de la función.
    stack = contextlib.ExitStack()
    try:
        print(f"    {Color.CYAN}[IMAP]{Color.ENDC} Consultando {_servidor_imap_para(user_real)} ({user_real})...")
        mail = stack.enter_context(sesion_imap(user_real, app_pwd))

        # Buscar correos recientes del remitente (por UID estable, no por número de secuencia).
        # Si hay baseline, pedir solo UIDs posteriores: menos FETCH bajo el semáforo y menos
        # oportunidades de que otro hilo reclame antes de que leamos el nuestro.
        if after_email_id and int(after_email_id) > 0:
            search_criteria = f'(UID {int(after_email_id) + 1}:* FROM "{query_from}")'
        else:
            search_criteria = f'(FROM "{query_from}")'
        status, messages = mail.uid("search", None, search_criteria)
        if status != "OK" or not messages or not messages[0]:
            # Fallback si el servidor no acepta UID en SEARCH combinado
            if after_email_id and int(after_email_id) > 0:
                status, messages = mail.uid("search", None, f'(FROM "{query_from}")')
        if status != "OK" or not messages or not messages[0]:
            print(f"    {Color.WARNING}[IMAP]{Color.ENDC} No se encontraron correos de '{query_from}'.")
            return None
        
        # Tomar los ultimos correos (mas recientes primero). El margen es amplio porque con
        # 10 ventanas simultaneas el buzon recibe muchos correos de Tidal en paralelo y el de
        # esta cuenta podria quedar fuera de una ventana corta.
        msg_ids = messages[0].split()[-50:]
        msg_ids.reverse()
        
        max_age_s = max(60, int((max_age_minutes or 15) * 60))
        for msg_id in msg_ids:
            msg_id_int = 0
            try:
                msg_id_int = int(msg_id)
            except ValueError:
                pass
                
            status, msg_data = mail.uid("fetch", msg_id, "(RFC822)")
            if status != "OK":
                continue
                
            msg = email.message_from_bytes(msg_data[0][1])
            
            # Aceptar si el correo es más reciente por ID, o si tiene antigüedad dentro de max_age_minutes.
            # (Antes estaba hardcodeado a 180s e ignoraba el parámetro max_age_minutes.)
            is_newer_id = (after_email_id == 0 or msg_id_int > after_email_id)
            is_recent_age = False
            try:
                from email.utils import parsedate_to_datetime
                date_str = msg.get("Date")
                if date_str:
                    msg_date = parsedate_to_datetime(date_str)
                    now_tz = datetime.now(timezone.utc)
                    age_seconds = (now_tz - msg_date.astimezone(timezone.utc)).total_seconds()
                    if age_seconds <= max_age_s:
                        is_recent_age = True
            except Exception:
                pass
                
            if not (is_newer_id or is_recent_age):
                # Ignorar correos antiguos
                continue
            
            # Destinatario: con varios alias con puntos del mismo Gmail hay que atribuir el
            # código al alias EXACTO. Normalizar puntos aquí mezclaba los códigos entre hilos.
            to_header = (msg.get("To") or "").lower()
            delivered_to = (msg.get("Delivered-To") or "").lower()
            envelope_to = (msg.get("Envelope-To") or "").lower()
            x_original = (msg.get("X-Original-To") or "").lower()
            x_forwarded = (msg.get("X-Forwarded-To") or "").lower()
            recipients = f"{to_header} {delivered_to} {envelope_to} {x_original} {x_forwarded}"

            # El cuerpo se mira después; aquí solo headers. Si no hay match por headers aún
            # no descartamos del todo hasta tener body (por si el alias solo aparece ahí).
            match_headers = _destinatario_es_para_alias(gmail_user, recipients, "")
            
            # Extraer asunto
            subject_text = ""
            try:
                subject_header = msg.get("Subject")
                if subject_header:
                    decoded = decode_header(subject_header)
                    subject_parts = []
                    for part_bytes, charset in decoded:
                        if isinstance(part_bytes, bytes):
                            subject_parts.append(part_bytes.decode(charset or "utf-8", errors="replace"))
                        else:
                            subject_parts.append(part_bytes)
                    subject_text = "".join(subject_parts)
            except Exception:
                pass

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

            if not match_headers and not _destinatario_es_para_alias(gmail_user, recipients, body_text):
                continue

            buzon_clave = _buzon_imap_clave(gmail_user, user_real)
            # Saltar UIDs que otro hilo concurrente ya consumió (mismo buzón, otro alias)
            with _IMAP_UIDS_LOCK:
                if msg_id_int and msg_id_int in _IMAP_UIDS_RECLAMADOS.get(buzon_clave, set()):
                    continue
            
            text_to_check = f"{subject_text} {body_text}"
            
            # Verificar keywords requeridas
            if required_keywords:
                cumple = any(kw.lower() in text_to_check.lower() for kw in required_keywords)
                if not cumple:
                    continue
            
            # Verificar exclusion
            if query_exclude and query_exclude.lower() in text_to_check.lower():
                continue
            
            # OTP juntos, partidos por HTML ("1 2 3 4 5 6") o junto a "code/sign-up"
            if not solo_link:
                codigos = _extraer_codigos_otp(text_to_check)
                if codigos:
                    if not _reclamar_uid_correo(buzon_clave, msg_id_int):
                        continue
                    print(f"    {Color.GREEN}[IMAP]{Color.ENDC} Código para {gmail_user} "
                          f"(UID {msg_id_int}, OTP={codigos[0]}, asunto: {(subject_text or '')[:60]!r}).")
                    return codigos[0]
                # Keywords OK pero sin OTP: útil para diagnosticar HTML partido / baseline
                print(f"    {Color.WARNING}[IMAP]{Color.ENDC} Correo Tidal UID {msg_id_int} coincide "
                      f"({(subject_text or '')[:50]!r}) pero no se extrajo OTP de 5-6 dígitos.")
            
            # Buscar enlaces de confirmacion en el texto plano
            enlaces = []
            try:
                enlaces = re.findall(r'https?://[^\s<>"\']+', body_text)
            except Exception:
                pass
            
            # Prioridad 0 (máxima): Buscar la URL directa como TEXTO VISIBLE dentro de <a> tags
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
                if not _reclamar_uid_correo(buzon_clave, msg_id_int):
                    continue
                return enlace_directo_anchor
            
            # Prioridad 0.5: Buscar el href del anchor cuyo texto visible sea "Join Family" u otra variante CTA de invitación
            JOIN_TEXTS = ["join", "unir", "nete", "invit", "accept", "acept", "family"]
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
                if not _reclamar_uid_correo(buzon_clave, msg_id_int):
                    continue
                return enlace_join_btn
            
            # Buscar también de forma robusta enlaces dentro de etiquetas href en las partes HTML
            try:
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/html":
                            html_content = part.get_payload(decode=True).decode("utf-8", errors="replace")
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
                    if not _reclamar_uid_correo(buzon_clave, msg_id_int):
                        break
                    return link
            
            # Prioridad 2: Fallback a cualquier otro enlace dinámico de Tidal (incluyendo tracking click/ablink)
            for link in enlaces:
                link_lower = link.lower()
                if any(x in link_lower for x in ["/privacy", "/terms", "/legal", "support.tidal.com", "tidal.com/es", "tidal.com/en", "tidal.com/us"]):
                    continue
                if "tidal.com" in link_lower:
                    if not _reclamar_uid_correo(buzon_clave, msg_id_int):
                        break
                    return link
        
    except Exception as e:
        print(f"    {Color.FAIL}[Error]{Color.ENDC} Error al interactuar con IMAP: {e}")
    finally:
        stack.close()
    return None
# --- CONSTANTES Y CONFIGURACIONES DE AUTOMATIZACIÓN (PLAYWRIGHT & PROXIES) ---
STEALTH_SCRIPT = """
(() => {
    // Excluir pasarelas de pago para no interferir en compras.
    // Ojo: no se puede excluir todo iframe, porque DataDome corre su detección y su captcha
    // dentro de uno; si el parcheo no llega ahí, ve el navegador sin disfrazar y bloquea.
    try {
        const host = (window.location && window.location.host) ? window.location.host.toLowerCase() : '';
        if (host.includes('stripe') || host.includes('adyen') || host.includes('checkoutshopper') || host.includes('payment')) {
            return;
        }
    } catch (e) {}

    // 1. Eliminar rastros de variables CDP / Automation (cdc_ / dollar) en document y window
    try {
        const cleanAutomationVars = () => {
            try {
                for (const key in document) {
                    if (key.includes('cdc_') || key.includes('dollar') || key.startsWith('$cdc')) {
                        delete document[key];
                    }
                }
            } catch(e) {}
            try {
                for (const key in window) {
                    if (key.includes('cdc_') || key.includes('dollar') || key.startsWith('$cdc')) {
                        delete window[key];
                    }
                }
            } catch(e) {}
        };
        cleanAutomationVars();
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', cleanAutomationVars);
        }
    } catch(e) {}

    // 2. Eliminar propiedad navigator.webdriver sin trampas detectables por DataDome
    try {
        delete Object.getPrototypeOf(navigator).webdriver;
        delete navigator.webdriver;
    } catch (e) {}

    // 3. Simular objeto window.chrome estándar de Google Chrome
    try {
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
                    RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' },
                    connect: () => {},
                    sendMessage: () => {}
                },
                loadTimes: () => ({}),
                csi: () => ({})
            };
        }
    } catch(e) {}

    // 4. Parchear permissions.query
    try {
        if (window.navigator && window.navigator.permissions) {
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters && parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : originalQuery(parameters)
            );
        }
    } catch (e) {}

    // 5. Parchear WebGL sólo si se detecta SwiftShader (CPU renderer)
    try {
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            const vendor = getParameter.apply(this, [37445]) || '';
            const renderer = getParameter.apply(this, [37446]) || '';
            if (renderer.toLowerCase().includes('swiftshader') || renderer.toLowerCase().includes('llvmpipe') || vendor.toLowerCase().includes('google inc.')) {
                if (parameter === 37445) return 'Google Inc. (NVIDIA)';
                if (parameter === 37446) return 'ANGLE (NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)';
            }
            return getParameter.apply(this, arguments);
        };
    } catch (e) {}

    // 6. Auto-remover de banners de cookies de forma segura
    try {
        if (window.top === window.self && !window.location.href.includes('payment.tidal.com') && !window.location.href.includes('subscription-order')) {
            const autoremoveCookies = () => {
                const ids = ['onetrust-consent-sdk', 'onetrust-banner-sdk'];
                ids.forEach(id => {
                    const el = document.getElementById(id);
                    if (el) el.remove();
                });
                document.querySelectorAll('.onetrust-pc-dark-filter, #onetrust-banner-sdk').forEach(el => {
                    try { el.remove(); } catch(e) {}
                });
            };

            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', autoremoveCookies);
            } else {
                autoremoveCookies();
            }
        }
    } catch (e) {}
})();
"""

valid_ng_list = []
valid_pe_list = []
CACHE_PROXIES_NG = []
CACHE_PROXIES_PE = []

try:
    from human_slider import generate_human_track
except ImportError:
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

CHROME_SILENT_ARGS = [
    "--credentials-enable-service=false",
    "--password-store=basic",
    "--disable-save-password-bubble",
    "--disable-single-click-autofill",
    "--disable-offer-store-unmasked-wallet-cards",
    "--disable-offer-save-autofill-addresses",
    "--no-default-browser-check",
    "--no-first-run",
    "--disable-blink-features=AutomationControlled",
    # Sin --test-type, Chrome pinta la barra "marca de línea de comandos no admitida" por culpa del
    # flag anterior. Esa barra roba alto a la ventana y delata la automatización por la diferencia
    # entre window.outerHeight e innerHeight, que es justo lo que miran los antirobot.
    "--test-type",
    "--disable-features=PasswordManager,CredentialManager,PasswordManagerLeakDetection,IsolateOrigins,site-per-process",
    "--disable-infobars",
    "--disable-notifications",
    "--disable-popup-blocking"
]

# El proveedor de proxies residenciales deniega los dominios de pasarela de pago con
# "503 Target host denied" (política antifraude). Como los campos de tarjeta de Tidal son iframes
# de Adyen, a través del proxy nunca cargaban y se veían como cajas grises no editables.
# Estos dominios salen por la conexión directa; el tráfico de Tidal sigue yendo por el proxy,
# que es el que determina el geo de la oferta.
DOMINIOS_PASARELA_PAGO = [
    # Pasarelas
    ".adyen.com", ".adyen.net",
    ".stripe.com", ".stripe.network",
    ".paypal.com", ".paypalobjects.com",
    ".braintreegateway.com", ".braintree-api.com",
    ".worldpay.com", ".checkout.com", ".klarna.com",
    ".cybersource.com", ".vantiv.com", ".globalpay.com",
    # 3D Secure: el reto del banco se abre después de enviar la tarjeta y el proxy
    # deniega estos dominios igual que los de la pasarela.
    ".cardinalcommerce.com", ".3dsecure.io", ".arcot.com",
    ".netcetera.com", ".modirum.com", ".gpayments.com", ".entersekt.com",
    ".securecode.com", ".verifiedbyvisa.com", ".safekey.americanexpress.com",
    ".mastercard.com", ".visa.com", ".americanexpress.com", ".aexp.com",
]

# Fichero opcional para añadir hosts sin tocar el código: si durante el checkout el navegador
# informa de un dominio bloqueado (p. ej. el 3D Secure de tu banco), se añade una línea aquí.
RUTA_DOMINIOS_SIN_PROXY = SCRIPT_DIR / "dominios_sin_proxy.txt"


def construir_bypass_pasarelas() -> str:
    """Lista de dominios que deben salir por conexión directa, no por el proxy."""
    dominios = list(DOMINIOS_PASARELA_PAGO)
    try:
        if RUTA_DOMINIOS_SIN_PROXY.exists():
            for linea in RUTA_DOMINIOS_SIN_PROXY.read_text(encoding="utf-8").splitlines():
                linea = linea.strip()
                if not linea or linea.startswith("#"):
                    continue
                # Aceptar tanto 'banco.com' como 'https://acs.banco.com/algo'
                linea = re.sub(r"^https?://", "", linea).split("/")[0].strip().lower()
                if linea and linea not in dominios:
                    dominios.append(linea)
    except Exception:
        pass
    return ",".join(dominios)

def reparar_perfil_corrupto(profile_dir):
    """Repara un perfil de Chrome corrupto eliminando archivos problemáticos y suprimiendo avisos de restauración/contraseñas."""
    profile_path = Path(profile_dir)
    if not profile_path.exists():
        profile_path.mkdir(parents=True, exist_ok=True)
    
    archivos_problematicos = [
        "SingletonLock", "SingletonCookie", "SingletonSocket",
        "lockfile", "LOCK",
        "DevToolsActivePort",
        "BrowserMetrics", "BrowserMetrics-spare.pma",
        "chrome_debug.log",
        "Crashpad",
        "ShaderCache",
        "GPUCache",
        "GrShaderCache",
        "GraphiteDawnCache",
    ]
    
    reparados = 0
    for nombre in archivos_problematicos:
        target = profile_path / nombre
        try:
            if target.is_file():
                target.unlink()
                reparados += 1
            elif target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                reparados += 1
        except Exception:
            pass
    
    for subdir in profile_path.iterdir():
        if subdir.is_dir() and (subdir.name.startswith("Default") or subdir.name.startswith("Profile")):
            for nombre in ["LOCK", "lockfile"]:
                target = subdir / nombre
                try:
                    if target.exists():
                        target.unlink()
                        reparados += 1
                except Exception:
                    pass

    # Forzar preferencias de salida limpia y desactivación del gestor de contraseñas para eliminar
    # las burbujas flotantes. Chrome lee estas preferencias de <perfil>/Default/Preferences; en un
    # perfil recién creado esa carpeta todavía no existe, así que hay que crearla o el archivo
    # acabaría sólo en la raíz, donde Chrome no lo mira, y la burbuja seguiría apareciendo.
    default_dir = profile_path / "Default"
    try:
        default_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    for d in (profile_path, default_dir):
        pref_file = d / "Preferences"
        try:
            data = {}
            if pref_file.exists():
                try:
                    data = json.loads(pref_file.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            if not isinstance(data, dict):
                data = {}

            profile_section = data.setdefault("profile", {})
            profile_section["exit_type"] = "Normal"
            profile_section["exited_cleanly"] = True
            profile_section["password_manager_enabled"] = False
            profile_section["password_manager_leak_detection"] = False

            data["credentials_enable_service"] = False
            data["credentials_enable_autosignin"] = False

            autofill_section = data.setdefault("autofill", {})
            autofill_section["profile_enabled"] = False
            autofill_section["credit_card_enabled"] = False

            pref_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

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

def es_error_navegacion_abortada(exc: BaseException) -> bool:
    """True si Playwright canceló la navegación (no implica proxy muerto).

    Tidal redirige account.tidal.com → login.tidal.com/authorize en cadena; Chromium a menudo
    reporta eso como net::ERR_ABORTED aunque la página destino cargue bien. Tratarlo como
    bloqueo de proxy quemaba IPs limpias en la opción 10.
    """
    t = str(exc).lower()
    return any(k in t for k in (
        "err_aborted",
        "navigation interrupted",
        "frame was detached",
        "navigating frame was detached",
        "execution context was destroyed",
    ))


def es_error_proxy_o_red(exc: BaseException) -> bool:
    """True solo para fallos reales de túnel/proxy/conectividad (sí conviene rotar IP)."""
    if es_error_navegacion_abortada(exc):
        return False
    t = str(exc).lower()
    return any(k in t for k in (
        "err_tunnel", "err_proxy", "err_socks", "err_connection_reset",
        "err_connection_closed", "err_connection_refused", "err_connection_timed_out",
        "err_timed_out", "err_address_unreachable", "err_name_not_resolved",
        "err_network_changed", "tunnel connection failed", "proxy",
        "econnrefused", "econnreset", "etimedout",
    ))


def url_es_pagina_marketing(url: str) -> bool:
    """True en pricing/landing pública (aún no hay login)."""
    u = (url or "").lower()
    if "account.tidal.com" in u or "login.tidal.com" in u or "listen.tidal.com" in u:
        return False
    return any(p in u for p in (
        "/pricing", "/try-now", "/plans", "/premium", "/campaigns",
    )) or u.rstrip("/").endswith("tidal.com") or u.rstrip("/").endswith("www.tidal.com")


def url_es_login_o_cuenta(url: str) -> bool:
    """True solo si ya estamos en el flujo de cuenta/login (no en marketing)."""
    u = (url or "").lower()
    if not u or "chrome-error://" in u:
        return False
    if url_es_pagina_marketing(u):
        return False
    return "account.tidal.com" in u or "login.tidal.com" in u or "listen.tidal.com" in u


def url_llegada_coincide_destino(url_actual: str, url_destino: str) -> bool:
    """Valida que ERR_ABORTED no nos haya dejado en la página de origen (p. ej. /pricing)."""
    actual = (url_actual or "").lower()
    destino = (url_destino or "").lower()
    if not actual or "chrome-error://" in actual:
        return False
    # Calentamiento a /pricing: basta con estar en tidal.com
    if "/pricing" in destino:
        return "tidal.com" in actual
    # Destino login/cuenta: quedarse en /pricing NO cuenta como éxito
    if "account.tidal.com" in destino or "login.tidal.com" in destino:
        return url_es_login_o_cuenta(actual)
    # Enlace de invitación familiar (ablink / family / accept / join):
    # quedarse en /pricing tras el calentamiento NO es llegada válida (opción 4).
    if any(k in destino for k in (
        "ablink.", "/family/", "/accept/", "/join/", "invite", "invit",
    )):
        if url_es_pagina_marketing(actual):
            return False
        return (
            url_es_login_o_cuenta(actual)
            or "ablink." in actual
            or "/accept/" in actual
            or "/join/" in actual
            or "family" in actual
        )
    # Otros destinos Tidal: no aceptar marketing genérico como llegada
    if "tidal.com" in destino or "ablink." in destino:
        if url_es_pagina_marketing(actual):
            return False
        return "tidal.com" in actual or "ablink." in actual
    return "tidal.com" in actual


def url_es_flujo_invitacion_familiar(url: str) -> bool:
    """True si la pestaña ya salió de marketing y está en login/aceptar/familia."""
    u = (url or "").lower()
    if not u or url_es_pagina_marketing(u):
        return False
    return (
        url_es_login_o_cuenta(u)
        or "ablink." in u
        or "/accept/" in u
        or "/join/" in u
        or ("family" in u and "tidal.com" in u)
    )


def navegar_tidal_tolerante(page, url: str, *, referer: str | None = None,
                            timeout_ms: int = 30000) -> None:
    """page.goto que tolera ERR_ABORTED solo si la pestaña llegó al destino real.

    Antes aceptaba cualquier URL con 'tidal.com', así que un aborto al ir a /login
    mientras la pestaña seguía en /pricing se daba por bueno y el login fallaba después.
    """
    kwargs = {"wait_until": "domcontentloaded", "timeout": timeout_ms}
    if referer:
        kwargs["referer"] = referer

    def _llegada_ok() -> bool:
        try:
            return url_llegada_coincide_destino(page.url or "", url)
        except Exception:
            return False

    try:
        page.goto(url, **kwargs)
        # goto sin excepción también puede quedar en marketing si Tidal revirtió la navegación
        if _llegada_ok():
            return
        raise RuntimeError(f"Navegación a {url} terminó en {(page.url or '')[:80]} (destino no alcanzado)")
    except Exception as e1:
        if not es_error_navegacion_abortada(e1) and "destino no alcanzado" not in str(e1).lower():
            raise
        time.sleep(1.0)
        if _llegada_ok():
            print(f"  [Navegación] ERR_ABORTED ignorado: llegada válida en {(page.url or '')[:80]}")
            return

        # Reintento suave: commit + esperar carga
        try:
            kwargs_commit = {"wait_until": "commit", "timeout": timeout_ms}
            if referer:
                kwargs_commit["referer"] = referer
            page.goto(url, **kwargs_commit)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except Exception:
                pass
            time.sleep(1.0)
            if _llegada_ok():
                return
        except Exception as e2:
            if es_error_navegacion_abortada(e2):
                time.sleep(1.5)
                if _llegada_ok():
                    print(f"  [Navegación] ERR_ABORTED recuperado: {(page.url or '')[:80]}")
                    return
            # Preferir el error original de aborto para el clasificador del caller
            raise e2 from e1

        raise RuntimeError(
            f"Tras ERR_ABORTED la pestaña sigue en {(page.url or '')[:80]} "
            f"(se esperaba llegar a {url})"
        )


def url_es_oauth_login_roto(url: str) -> bool:
    """True en login.tidal.com/authorize o /signin.

    Recargar esas URLs (sobre todo authorize?email=...) reproduce el Error
    'Algo salió mal' en bucle; hay que salir por pricing → account.tidal.com.
    """
    u = (url or "").lower()
    if "login.tidal.com" not in u:
        return False
    return "/authorize" in u or "/signin" in u


def es_pantalla_error_login_tidal(page) -> bool:
    """True en login.tidal.com con 'Algo salió mal...' (sin formulario).

    No es lo mismo que un captcha/IP restringida: suele aparecer al entrar en frío a
    /signin o al recargar authorize?email= /signin tras un OAuth interrumpido. Rotar IP
    y recargar esa URL lo reproduce en bucle (opción 8/10, log error.txt).
    """
    try:
        if not page or page.is_closed():
            return False
        url = (page.url or "").lower()
        if "login.tidal.com" not in url:
            return False
        for f in page.frames:
            try:
                if f.locator(
                    'input[type="email"], input[name="email"], #email, input[type="password"]'
                ).first.is_visible():
                    return False
            except Exception:
                pass
        for frame in page.frames:
            try:
                t = (frame.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''") or "")
                if not t:
                    continue
                if ("algo salió mal" in t or "something went wrong" in t) and (
                    "inténtalo" in t or "intentalo" in t or "try again" in t
                    or "atención al cliente" in t or "customer support" in t
                ):
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _formulario_login_visible(page) -> bool:
    try:
        if not page or page.is_closed():
            return False
        for f in page.frames:
            try:
                if f.locator(
                    'input[type="email"], input[name="email"], #email, '
                    'input[type="password"], input[name="code"], select[name*="day" i]'
                ).first.is_visible():
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _esperar_estado_login_estable(page, timeout_s: float = 8.0) -> str:
    """Tras rotar/navegar: 'ok' (formulario), 'bloqueo' (antibot/error) o 'indefinido'."""
    limite = time.time() + max(1.0, timeout_s)
    while time.time() < limite:
        page = pagina_vigente(page)
        if _formulario_login_visible(page):
            return "ok"
        if es_pantalla_error_login_tidal(page):
            return "bloqueo"
        time.sleep(0.4)
    if _formulario_login_visible(page):
        return "ok"
    if es_pantalla_error_login_tidal(page) or detectar_pantalla_antirobot(page):
        return "bloqueo"
    return "indefinido"


def detectar_pantalla_antirobot(page) -> bool:
    """Detecta si la página actual presenta un captcha o bloqueo real (Cloudflare, Datadome, Slider)."""
    try:
        if not page or page.is_closed():
            return False
            
        # Si algún campo de formulario ya está visible, la página NO está bloqueada
        if _formulario_login_visible(page):
            return False

        try:
            titulo = page.title()
            if re.search(r"403|request could not be satisfied|access denied|attention required|security|blocked|datadome", titulo, re.I):
                print(f"  [Anti-bot DEBUG] Detectado por TITULO: '{titulo}'")
                return True
        except Exception:
            pass

        # Error genérico de login: también bloquea el avance, pero se recupera distinto
        if es_pantalla_error_login_tidal(page):
            print("  [Anti-bot DEBUG] Detectado error genérico de login.tidal.com ('Algo salió mal')")
            return True

        patrones = [
            "nos aseguramos de que", "no a un robot", "desliza hacia la derecha",
            "making sure you are not a robot", "not a robot", "slide to right",
            "verify you are human", "confirmar que eres humano",
            "access denied", "error code 1020", "unusual activity",
            "bot detection", "acceso está restringido", "acceso restringido", "restringido temporalmente",
            "comportamiento del navegador nos ha intrigado",
        ]
        
        for frame in page.frames:
            try:
                body_text = frame.evaluate("() => document.body ? document.body.innerText : ''")
                if body_text:
                    body_text_lower = body_text.lower()
                    for pat in patrones:
                        if pat in body_text_lower:
                            print(f"  [Anti-bot DEBUG] Detectado bloqueo/error de IP por patrón: '{pat}'")
                            return True
                    
                    if re.search(r"403\s*ERROR|generated by cloudfront|request blocked|access denied|error code 1020|ray id", body_text, re.I):
                        print(f"  [Anti-bot DEBUG] Detectado bloqueo por body text en frame")
                        return True
            except Exception:
                continue
    except Exception:
        pass
    return False

def correos_iguales_exacto(c1: str, c2: str) -> bool:
    """Compara dos correos Tidal respetando los puntos de Gmail.

    En Tidal, s.oftcake78.8 y s.of.tcake7.88 son cuentas distintas aunque Gmail
    entregue el mismo buzón. Nunca usar son_correos_equivalentes para titulares.
    """
    if not c1 or not c2:
        return False
    return (c1 or "").strip().lower().rstrip(".") == (c2 or "").strip().lower().rstrip(".")


def son_correos_equivalentes(c1: str, c2: str) -> bool:
    """Compara dos correos ignorando los puntos del usuario en las direcciones de Gmail."""
    if not c1 or not c2:
        return False
    c1_clean = c1.strip().lower()
    c2_clean = c2.strip().lower()
    if c1_clean == c2_clean:
        return True
    if "@gmail.com" in c1_clean and "@gmail.com" in c2_clean:
        u1, d1 = c1_clean.split("@", 1)
        u2, d2 = c2_clean.split("@", 1)
        return u1.replace(".", "") == u2.replace(".", "")
    return False

def pagina_vigente(page):
    """Devuelve la página activa del manager cuando una rotación de proxy sustituyó la original.

    Sin esto, los ayudantes que reciben `page` por valor siguen consultando una página ya
    cerrada tras la rotación y agotan su timeout sin ver nunca la página nueva."""
    manager = getattr(page, "manager", None)
    if not manager:
        return page
    page_actual = getattr(manager, "page", None)
    if page_actual is None or page_actual is page:
        return page
    try:
        if not page.is_closed():
            return page
    except Exception:
        pass
    return page_actual

def _chequeo_reciente_limpio(page, clave: str, ventana_s: float) -> bool:
    """True si esta misma comprobación salió limpia sobre la misma URL hace menos de ventana_s."""
    try:
        url = page.url or ""
        marcas = getattr(page, "_marcas_chequeo", None)
        if marcas is None:
            marcas = {}
            page._marcas_chequeo = marcas
        url_previa, ts = marcas.get(clave, ("", 0.0))
        return url_previa == url and (time.time() - ts) < ventana_s
    except Exception:
        return False


def _registrar_chequeo_limpio(page, clave: str) -> None:
    try:
        marcas = getattr(page, "_marcas_chequeo", None)
        if marcas is None:
            marcas = {}
            page._marcas_chequeo = marcas
        marcas[clave] = (page.url or "", time.time())
    except Exception:
        pass


def esperar_carga_pagina(page, timeout_s: float = 2.5) -> None:
    """Espera a que la página termine de cargar, saliendo en cuanto está lista."""
    limite = time.time() + max(0.0, timeout_s)
    while time.time() < limite:
        try:
            if page.evaluate("() => document.readyState") == "complete":
                return
        except Exception:
            return
        time.sleep(0.25)


def manejar_bloqueos_e_intervencion(page, subtitulo: str = "") -> None:
    """Detecta captchas y bloqueos reales tras permitir que la página termine de cargar por completo."""
    page = pagina_vigente(page)
    # El flujo invoca esta función varias veces por página (y esperar_locator_en_frames la llama
    # además por su cuenta). Antes cada llamada costaba 2,5 s fijos aunque no hubiera bloqueo
    # alguno, lo que sumaba decenas de segundos por ventana. La ventana es corta a propósito:
    # DataDome puede servir su interstitial en la misma URL sin navegar.
    if _chequeo_reciente_limpio(page, "antibot", 1.5):
        return
    esperar_carga_pagina(page, 2.5)
    intentos_bucle = 0
    while True:
        page = pagina_vigente(page)
        if not detectar_pantalla_antirobot(page):
            _registrar_chequeo_limpio(page, "antibot")
            break
        intentos_bucle += 1
        print(f"\n[BLOQUEO DETECTADO] -> {subtitulo}")
        
        manager = getattr(page, "manager", None)

        # Error genérico /signin o authorize?email=: recuperar por pricing→account SIN quemar IP.
        # Recargar esa URL con otra IP era el bucle de g.etspooky2.189 / get.mushroom.3949.
        _url_act = ""
        try:
            _url_act = page.url or ""
        except Exception:
            pass
        if (
            (es_pantalla_error_login_tidal(page) or url_es_oauth_login_roto(_url_act))
            and manager and hasattr(manager, "recuperar_login_tras_error_tidal")
        ):
            n_rec = int(getattr(manager, "_recuperaciones_error_tidal", 0) or 0)
            if n_rec < 2:
                manager._recuperaciones_error_tidal = n_rec + 1
                print(f"  [Anti-bot] Error/authorize en login.tidal.com — recuperación "
                      f"{manager._recuperaciones_error_tidal}/2 vía pricing (sin rotar IP)...")
                try:
                    if manager.recuperar_login_tras_error_tidal():
                        page = pagina_vigente(page)
                        if _esperar_estado_login_estable(page, 8.0) == "ok":
                            print("  [Anti-bot] Login recuperado tras error genérico (sin quemar proxy).")
                            _registrar_chequeo_limpio(page, "antibot")
                            return
                except Exception as e_rec:
                    print(f"  [Anti-bot] [WARN] Recuperación sin rotar falló: {e_rec}")
        
        # Tope de rotaciones POR CUENTA (antes el contador era local → imprimía 1/4 en cada llamada)
        rotaciones_realizadas = int(getattr(manager, "_rotaciones_antibot", 0) or 0) if manager else 0
        if manager and getattr(manager, "use_proxy", False) and rotaciones_realizadas < 4:
            # Si estamos en authorize?email=, la rotación DEBE usar recuperar (no goto de esa URL).
            # ejecutar_rotacion_* de Register/AutoLogin ya lo hace vía url_es_oauth_login_roto.
            manager._rotaciones_antibot = rotaciones_realizadas + 1
            print(f"  [Auto-Proxy] [IP BLOQUEADA] Rotación de proxy "
                  f"({manager._rotaciones_antibot}/4)...")
            manager.ejecutar_rotacion_proxy_y_recargar()
            page = pagina_vigente(page)
            estado = _esperar_estado_login_estable(page, 10.0)
            if estado == "ok":
                print("  [Auto-Proxy] ¡Proxy rotado con éxito y bloqueo superado!")
                _registrar_chequeo_limpio(page, "antibot")
                return
            if estado == "indefinido":
                print("  [Auto-Proxy] [WARN] Tras rotar, la página no mostró formulario ni bloqueo claro.")
            continue

        # 2. En el intento 1, intentar resolver el captcha de arrastre si no hay proxy rotado
        if intentos_bucle == 1:
            print("  [Anti-bot] Intentando resolver captcha de arrastre de forma automática...")
            if resolver_slider_captcha_playwright(page):
                time.sleep(2.5)
                if not detectar_pantalla_antirobot(page):
                    print("  [Anti-bot] ¡Slider captcha resuelto automáticamente!")
                    return
            continue

        # 3. En el intento 2 o superior, intentar recargar / recuperar
        try:
            print("  [Anti-bot] Intentando recuperar la página para superar bloqueo...")
            if es_pantalla_error_login_tidal(page) and manager and hasattr(manager, "recuperar_login_tras_error_tidal"):
                manager.recuperar_login_tras_error_tidal()
            elif url_es_oauth_login_roto(getattr(page, "url", "") or "") and manager and hasattr(manager, "recuperar_login_tras_error_tidal"):
                # authorize?email= sin texto aún legible: igual no hay que hacer reload
                manager.recuperar_login_tras_error_tidal()
            elif url_es_oauth_login_roto(getattr(page, "url", "") or ""):
                print("  [Anti-bot] Evitando reload de authorize/signin (reproduce Error). Yendo a account.tidal.com/...")
                try:
                    navegar_tidal_tolerante(page, "https://tidal.com/pricing", timeout_ms=25000)
                    time.sleep(1.0)
                    navegar_tidal_tolerante(
                        page, "https://account.tidal.com/",
                        referer="https://tidal.com/pricing",
                        timeout_ms=30000,
                    )
                except Exception:
                    pass
            else:
                page.reload(timeout=15000)
            time.sleep(3.0)
            page = pagina_vigente(page)
            if _esperar_estado_login_estable(page, 6.0) == "ok":
                print("  [Anti-bot] ¡Página recuperada y bloqueo superado!")
                return
        except Exception:
            pass

        if intentos_bucle >= 3:
            if manager and getattr(manager, "headless", False) and hasattr(manager, "forzar_modo_visual"):
                print(f"\n[BLOQUEO PERSISTENTE] -> Transicionando a modo visual para intervención manual: {getattr(manager, 'client_email', 'cuenta')}.")
                try:
                    manager.forzar_modo_visual()
                    page = manager.page
                    time.sleep(2.5)
                    intentos_bucle = 0
                    continue
                except Exception as _e_visual:
                    print(f"  [Modo Headless] [WARN] No se pudo transicionar a modo visual: {_e_visual}")
            print(f"\n[BLOQUEO PERSISTENTE] -> No se pudo superar el anti-bot de forma automática para {getattr(manager, 'client_email', 'cuenta')}.")
            raise RuntimeError("Bloqueo/Captcha persistente no superado de forma automática.")

def rellenar_campo_humanizado(loc, valor: str) -> bool:
    """Escribe un valor en un localizador de forma robusta y directa para evitar fallos de latencia."""
    try:
        loc.click(timeout=4000)
        time.sleep(0.15)
        loc.focus()
        loc.fill(valor)
        time.sleep(0.25)
        
        # Validación de seguridad extra
        try:
            val_actual = loc.input_value()
            if val_actual != valor:
                print(f"    [Human Input] [WARN] Se detectó discrepancia al escribir. Corrigiendo...")
                loc.fill(valor)
                time.sleep(0.2)
        except Exception:
            pass
            
        return True
    except Exception as e:
        print(f"    [Human Input] Error al rellenar campo: {e}")
        try:
            loc.fill(valor)
            return True
        except Exception:
            return False

def _hay_banner_cookies(page) -> bool:
    """Comprobación barata de si queda algún banner de cookies VISIBLE en alguna trama."""
    sel = ('#onetrust-banner-sdk, #onetrust-consent-sdk, [id*="cookie" i], '
           '[class*="cookie" i], [class*="consent" i]')
    # Solo cuentan elementos visibles: el <style> anti-overlays que inyecta este script lleva
    # "cookie" en su id y, sin filtrar, haría creer que el banner sigue ahí para siempre.
    script = """(s) => {
        const ignorar = ['STYLE', 'LINK', 'SCRIPT', 'META'];
        return Array.from(document.querySelectorAll(s)).some(el => {
            if (el.id === 'anti-cookie-overlay-style') return false;
            if (ignorar.includes(el.tagName)) return false;
            return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        });
    }"""
    for frame in page.frames:
        try:
            if frame.evaluate(script, sel):
                return True
        except Exception:
            continue
    return False


def aceptar_cookies_con_espera(page, intentos: int = 3, pausa_s: float = 0.5) -> bool:
    """Busca y acepta banners de cookies habituales de forma optimizada en JS, y limpia el DOM."""
    # Igual que el chequeo antirobot: esta función se llamaba varias veces seguidas sobre la misma
    # página y repetía todo el barrido de tramas aunque no quedara ningún banner.
    if _chequeo_reciente_limpio(page, "cookies", 5.0):
        return True

    # 1. Intentar clickear botones de aceptación o rechazo
    pulsado = False
    for intento in range(max(1, intentos)):
        try:
            if page.is_closed():
                return False

            # Sin banner no hay nada que pulsar: evita gastar los reintentos y sus pausas
            if intento > 0 and not _hay_banner_cookies(page):
                break

            for frame in page.frames:
                try:
                    clicked = frame.evaluate("""() => {
                        const skipTexts = ['configur', 'preferenc', 'settings', 'manage', 'opciones', 'personalizar', 'details'];
                        const texts = ['aceptar todas', 'aceptar todo', 'aceptar', 'accept all', 'accept', 'ok', 'entendido', 'got it', 'rechazar', 'reject', 'confirmar mis preferencias', 'confirm preferences', 'guardar preferencias', 'save preferences'];
                        const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                        for (const btn of buttons) {
                            const txt = (btn.textContent || '').trim().toLowerCase();
                            // Omitir correos electrónicos o botones de configuración
                            if (txt.includes('@') || skipTexts.some(st => txt.includes(st))) {
                                continue;
                            }
                            if (texts.some(t => txt === t || (t.length > 3 && txt.includes(t)))) {
                                if (btn.offsetWidth || btn.offsetHeight || btn.getClientRects().length) {
                                    btn.click();
                                    return true;
                                }
                            }
                        }
                        const selectors = [
                            '#onetrust-accept-btn-handler',
                            '#onetrust-reject-all-handler',
                            '.onetrust-close-btn-handler',
                            '#onetrust-close-btn-container button',
                            '[id*="cookie" i] button',
                            'button[class*="accept" i]',
                            'button[class*="ok" i]'
                        ];
                        for (const sel of selectors) {
                            try {
                                const btn = document.querySelector(sel);
                                if (btn && (btn.offsetWidth || btn.offsetHeight || btn.getClientRects().length)) {
                                    // Omitir si es un botón de configuración
                                    const txt = (btn.textContent || '').trim().toLowerCase();
                                    if (skipTexts.some(st => txt.includes(st))) {
                                        continue;
                                    }
                                    btn.click();
                                    return true;
                                }
                            } catch(e) {}
                        }
                        return false;
                    }""")
                    if clicked:
                        print("  [Cookies] Banner de cookies aceptado/clickeado vía JS.")
                        time.sleep(pausa_s)
                        pulsado = True
                        break
                except Exception:
                    continue
            # El break interno solo salía del bucle de tramas: sin esto se repetían todos los
            # intentos restantes con sus pausas aunque el banner ya estuviera aceptado.
            if pulsado:
                break
            time.sleep(pausa_s)
        except Exception:
            pass


    # 2. Correr SIEMPRE el eliminador de DOM e inyectar CSS anti-overlays para garantizar pulsaciones limpias
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
                    if (style.position === 'fixed' || style.position === 'absolute' || style.zIndex > 100) {
                        el.style.setProperty('display', 'none', 'important');
                        el.style.setProperty('pointer-events', 'none', 'important');
                        el.remove();
                    }
                } catch(e) {}
            });

            if (!document.getElementById('anti-cookie-overlay-style')) {
                const st = document.createElement('style');
                st.id = 'anti-cookie-overlay-style';
                st.textContent = `
                    #onetrust-consent-sdk, #onetrust-banner-sdk, .onetrust-pc-dark-filter,
                    [id*="cookie" i], [class*="cookie-consent" i], [class*="cookiebanner" i],
                    .ot-sdk-container, .ot-cookie-policy {
                        display: none !important;
                        pointer-events: none !important;
                        visibility: hidden !important;
                    }
                `;
                (document.head || document.documentElement).appendChild(st);
            }
        }""")
        if pulsado:
            print("  [Cookies] Overlays de cookies removidos y deshabilitados del DOM.")
    except Exception:
        pass

    _registrar_chequeo_limpio(page, "cookies")
    return True

def esperar_visibilidad(loc, timeout_ms=15000) -> bool:
    try:
        loc.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False

def hacer_click_por_textos(page, textos: list) -> bool:
    for texto in textos:
        try:
            loc = page.get_by_text(texto, exact=True).first
            if esperar_visibilidad(loc, 4000):
                loc.click(force=True)
                return True
        except Exception:
            pass
        try:
            loc = page.get_by_text(texto, exact=False).first
            if esperar_visibilidad(loc, 1000):
                loc.click(force=True)
                return True
        except Exception:
            pass
    return False

def escribir_codigo_verificacion_inteligente(page, codigo: str) -> bool:
    """Ingresa un código de verificación (una caja o 6 cajas OTP). Exige lectura == código."""
    codigo = re.sub(r"\D", "", str(codigo or ""))
    if not codigo:
        return False

    for frame in page.frames:
        try:
            code_inputs = []
            for poll in range(10):
                inputs = frame.locator('input').all()
                code_inputs = []
                for ip in inputs:
                    try:
                        if not ip.is_visible():
                            continue
                        type_attr = (ip.get_attribute("type") or "").lower()
                        if type_attr in ["email", "checkbox", "radio", "submit", "button", "file", "hidden", "range", "color"]:
                            continue
                        if type_attr == "password" and (ip.get_attribute("maxlength") or "").strip() != "1":
                            continue
                        mode = (ip.get_attribute("inputmode") or "").lower()
                        name = (ip.get_attribute("name") or "").lower()
                        placeholder = (ip.get_attribute("placeholder") or "").lower()
                        autocomplete = (ip.get_attribute("autocomplete") or "").lower()
                        maxlength = (ip.get_attribute("maxlength") or "").strip()
                        aria = (ip.get_attribute("aria-label") or "").lower()

                        if (type_attr in ["", "text", "number", "tel", "password"] or
                            mode == "numeric" or
                            autocomplete == "one-time-code" or
                            maxlength == "1" or
                            "code" in name or
                            "code" in placeholder or
                            "código" in placeholder or
                            "codigo" in placeholder or
                            "digit" in aria or
                            "código" in aria or
                            "codigo" in aria):
                            code_inputs.append(ip)
                    except Exception:
                        pass
                if len(code_inputs) >= min(4, len(codigo)) or (code_inputs and len(codigo) <= 8 and len(code_inputs) == 1):
                    break
                time.sleep(0.35)

            if not code_inputs:
                continue

            def leer_cajas(objetivos):
                leido = ""
                for caja in objetivos:
                    try:
                        leido += (caja.input_value() or "").strip()
                    except Exception:
                        pass
                return leido

            def limpiar_cajas(objetivos):
                for caja in objetivos:
                    try:
                        caja.click(timeout=800)
                        caja.fill("")
                    except Exception:
                        try:
                            caja.press("Control+a")
                            caja.press("Backspace")
                        except Exception:
                            pass

            # Varias cajas (Tidal: 6 inputs maxlength=1)
            if len(code_inputs) >= 4:
                objetivos = code_inputs[:len(codigo)]
                if len(objetivos) < len(codigo):
                    continue

                # 1) JS + eventos input/change (React controlado)
                try:
                    ok_js = frame.evaluate(
                        """([digitos]) => {
                            const inputs = Array.from(document.querySelectorAll('input')).filter(el => {
                                if (!el.offsetParent && el.type !== 'hidden') {
                                    // visible check soft
                                }
                                const t = (el.type || '').toLowerCase();
                                if (['email','checkbox','radio','submit','button','file','hidden'].includes(t)) return false;
                                const max = el.getAttribute('maxlength') || '';
                                const mode = (el.inputMode || '').toLowerCase();
                                const ac = (el.autocomplete || '').toLowerCase();
                                return max === '1' || mode === 'numeric' || ac === 'one-time-code' || t === '' || t === 'text' || t === 'tel' || t === 'number';
                            }).filter(el => {
                                const r = el.getBoundingClientRect();
                                return r.width > 0 && r.height > 0;
                            });
                            if (inputs.length < digitos.length) return false;
                            const targets = inputs.slice(0, digitos.length);
                            targets.forEach((el, i) => {
                                el.focus();
                                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                                if (setter) setter.call(el, digitos[i]);
                                else el.value = digitos[i];
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: digitos[i] }));
                            });
                            return targets.every((el, i) => (el.value || '') === digitos[i]);
                        }""",
                        list(codigo),
                    )
                    if ok_js and leer_cajas(objetivos) == codigo:
                        return True
                except Exception:
                    pass

                # 2) Caja por caja con type()
                limpiar_cajas(objetivos)
                for idx, digit in enumerate(codigo):
                    try:
                        objetivos[idx].click(timeout=800)
                        objetivos[idx].fill("")
                        objetivos[idx].type(digit, delay=80)
                    except Exception:
                        try:
                            objetivos[idx].fill(digit)
                        except Exception:
                            pass
                    time.sleep(0.05)
                if leer_cajas(objetivos) == codigo:
                    return True

                # 3) Secuencia de teclado desde la primera caja
                limpiar_cajas(objetivos)
                try:
                    objetivos[0].click(timeout=800)
                except Exception:
                    pass
                time.sleep(0.2)
                for digit in codigo:
                    try:
                        page.keyboard.type(digit, delay=100)
                    except Exception:
                        break
                    time.sleep(0.08)
                if leer_cajas(objetivos) == codigo:
                    return True
                # Nunca tratar cajas vacías como éxito
                continue

            # Una sola caja
            target = code_inputs[0]
            try:
                target.click(timeout=800)
                target.fill("")
                target.type(codigo, delay=80)
            except Exception:
                try:
                    target.fill(codigo)
                except Exception:
                    continue
            try:
                if (target.input_value() or "").strip() == codigo:
                    return True
            except Exception:
                pass
            # Fallback JS
            try:
                ok = frame.evaluate(
                    """(code) => {
                        const el = document.querySelector('input[autocomplete="one-time-code"]')
                            || document.querySelector('input[name="code"]')
                            || document.querySelector('input[inputmode="numeric"]');
                        if (!el) return false;
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                        if (setter) setter.call(el, code);
                        else el.value = code;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        return (el.value || '') === code;
                    }""",
                    codigo,
                )
                if ok:
                    return True
            except Exception:
                pass
        except Exception:
            continue
    return False

def encontrar_locator_en_frames(page, selectors: list, label_regex=None, text_regex=None):
    """Busca en todos los frames el primer localizador visible.

    Si se pasa text_regex, SOLO se aceptan elementos cuyo texto coincida. Sin este filtro,
    selectores amplios como 'button' devolvían la flecha atrás del login de Tidal y el
    flujo volvía a la pantalla del correo justo después de enviar la contraseña.
    """
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel)
                cnt = loc.count()
                for idx in range(cnt):
                    btn = loc.nth(idx)
                    try:
                        if not btn.is_visible():
                            continue
                    except Exception:
                        continue
                    if text_regex is not None:
                        try:
                            txt = (btn.inner_text() or "").strip()
                        except Exception:
                            txt = ""
                        if not txt:
                            try:
                                txt = (btn.get_attribute("aria-label") or btn.get_attribute("title") or "").strip()
                            except Exception:
                                txt = ""
                        if not txt or not text_regex.search(txt):
                            continue
                    if label_regex is not None and text_regex is None:
                        # label_regex se aplica en la rama get_by_label más abajo; aquí no filtra
                        pass
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
                    el = loc.nth(idx)
                    if not el.is_visible():
                        continue
                    # Preferir controles clicables asociados al texto
                    try:
                        tag = el.evaluate("e => (e.closest('button, a, [role=\"button\"]') || e).tagName")
                        if tag:
                            clickable = el.locator("xpath=ancestor-or-self::button[1] | ancestor-or-self::a[1] | ancestor-or-self::*[@role='button'][1]")
                            if clickable.count() > 0 and clickable.first.is_visible():
                                return clickable.first
                    except Exception:
                        pass
                    return el
            except Exception:
                pass
    return None

def esperar_locator_en_frames(page, selectors: list, label_regex=None, text_regex=None, timeout_s=15.0):
    start_time = time.time()
    last_anti_bot_check = 0
    while time.time() - start_time < timeout_s:
        page = pagina_vigente(page)
        current_time = time.time()
        # Solo comprobar anti-bot si pasaron al menos 3.0s de carga inicial y han transcurrido 4.0s desde la última comprobación
        if current_time - start_time > 3.0 and current_time - last_anti_bot_check > 4.0:
            last_anti_bot_check = current_time
            if detectar_pantalla_antirobot(page):
                manejar_bloqueos_e_intervencion(page, "Bloqueo detectado durante la espera de elementos")
                start_time = time.time()
                continue
                
        # Limpieza activa de overlays de cookies para no obstruir los clics
        try:
            page.evaluate("""() => {
                const ids = ['onetrust-consent-sdk', 'onetrust-banner-sdk', 'onetrust-style', 'cookie-consent', 'cookiebanner'];
                ids.forEach(id => { const el = document.getElementById(id); if (el) el.remove(); });
            }""")
        except Exception:
            pass

        loc = encontrar_locator_en_frames(page, selectors, label_regex, text_regex)
        if loc:
            return loc
        time.sleep(0.5)
    return None

def cargar_proxies_desde_txt(filepath="proxies.txt", preferir_validos: bool = True):
    """Carga la configuración de proxies residenciales.

    Formatos aceptados por línea:
      user:pass@host:port
      http://user:pass@host:port
      host;port;user;pass
      host:port:user:pass
      host:port

    Si lista_proxies_*_validos.txt existe pero está vacío / no parsea nada, se cae a
    lista_proxies_*.txt (antes un validos vacío bloqueaba la lista fuente — opción 13).
    """
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
        raw_line = line_clean.strip().strip("\ufeff")
        if raw_line.lower().startswith("http://"):
            raw_line = raw_line[7:]
        elif raw_line.lower().startswith("https://"):
            raw_line = raw_line[8:]

        server, username, password = None, None, None
        if ";" in raw_line:
            # host;port;user;pass  (formato habitual de lista_proxies_pe.txt)
            parts = [p.strip() for p in raw_line.split(";")]
            if len(parts) >= 4 and parts[0] and parts[1]:
                host, port = parts[0], parts[1]
                username, password = parts[2], parts[3]
                server = f"http://{host}:{port}"
            elif len(parts) == 2 and parts[0] and parts[1]:
                server = f"http://{parts[0]}:{parts[1]}"
        elif "@" in raw_line:
            # user:pass@host:port  (formato habitual de lista_proxies_ng.txt)
            part_user_pass, part_host_port = raw_line.split("@", 1)
            part_host_port = part_host_port.strip()
            if not part_host_port:
                return None
            server = f"http://{part_host_port}"
            if ":" in part_user_pass:
                username, password = part_user_pass.split(":", 1)
                username = username.strip()
                password = password.strip()
        elif raw_line.count(":") >= 3:
            # host:port:user:pass
            parts = raw_line.split(":")
            host = parts[0].strip()
            port = parts[1].strip()
            username = parts[2].strip()
            password = ":".join(parts[3:]).strip()  # por si el pass tiene ':'
            if host and port:
                server = f"http://{host}:{port}"
        elif ":" in raw_line:
            parts = raw_line.split(":")
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                server = f"http://{parts[0].strip()}:{parts[1].strip()}"

        if server:
            if password and password.endswith("@"):
                password = password[:-1].strip()
            return {
                "server": server,
                "username": username or "",
                "password": password or "",
            }
        return None

    def _leer_archivo_proxies(path: Path) -> list[dict]:
        out = []
        if not path.exists():
            return out
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    p = parsear_linea_proxy(line.strip())
                    if p:
                        out.append(p)
        except Exception as e:
            print(f"  [Proxy Load] [WARN] Error al cargar {path.name}: {e}")
        return out

    def _cargar_region(etiqueta: str, path_validos: Path, path_fuente: Path) -> list[dict]:
        """Preferir validos solo si tienen entradas; si no, usar la lista fuente."""
        orden = []
        if preferir_validos:
            orden = [path_validos, path_fuente]
        else:
            orden = [path_fuente, path_validos]

        for path in orden:
            if not path.exists():
                continue
            lista = _leer_archivo_proxies(path)
            if lista:
                print(f"  [Proxy Load] Cargados {len(lista)} proxies de {etiqueta} desde {path.name}")
                return lista
            # Archivo existe pero vacío / no parseable → avisar y probar el siguiente
            print(f"  [Proxy Load] {path.name} existe pero no aportó proxies parseables "
                  f"(¿vacío o formato no reconocido?).")
        return []

    proxies_cfg["proxy_pe_list"] = _cargar_region(
        "Perú",
        SCRIPT_DIR / "lista_proxies_pe_validos.txt",
        SCRIPT_DIR / "lista_proxies_pe.txt",
    )
    proxies_cfg["proxy_ng_list"] = _cargar_region(
        "Nigeria",
        SCRIPT_DIR / "lista_proxies_ng_validos.txt",
        SCRIPT_DIR / "lista_proxies_ng.txt",
    )

    if not proxies_cfg["proxy_pe_list"] and not proxies_cfg["proxy_ng_list"]:
        proxies_txt = SCRIPT_DIR / filepath
        if not proxies_txt.exists():
            return None
        try:
            current_section = None
            with open(proxies_txt, "r", encoding="utf-8-sig") as f:
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
            print(f"  [Proxy Load] Cargados {len(proxies_cfg['proxy_pe_list'])} PE y "
                  f"{len(proxies_cfg['proxy_ng_list'])} NG proxies desde proxies.txt")
        except Exception as e:
            print(f"  [Proxy Load] [WARN] Error al cargar proxies.txt: {e}")

    if proxies_cfg["proxy_pe_list"] or proxies_cfg["proxy_ng_list"]:
        return proxies_cfg
    return None

def _clave_proxy_server(proxy: dict | None) -> str:
    if not proxy:
        return ""
    return (proxy.get("server") or "").replace("http://", "").replace("https://", "").strip().lower()


def fusionar_proxies_unicos(*listas: list[dict] | None) -> list[dict]:
    """Une listas de proxies sin duplicar por host:port (conserva el primero visto)."""
    out = []
    vistos = set()
    for lista in listas:
        for p in (lista or []):
            if not isinstance(p, dict):
                continue
            clave = _clave_proxy_server(p)
            if not clave or clave in vistos:
                continue
            vistos.add(clave)
            out.append(p)
    return out


def probar_y_seleccionar_mejor_proxy(proxy_list, region="Peru", cantidad_necesaria=5):
    """Prueba latencia y devuelve TODOS los válidos encontrados (no un recorte al tamaño del lote).

    Antes devolvía CACHE[:N] o validos[:N] y los callers sobrescribían el caché con eso:
    tras validar cientos en opción 13, un proceso de 10 ventanas dejaba el pool en ~10–40 IPs
    y las rotaciones antirobot lo vaciaban.
    """
    if not proxy_list:
        return []

    global CACHE_PROXIES_PE, CACHE_PROXIES_NG
    region_low = region.lower()
    es_pe = "peru" in region_low or region_low == "pe" or "pe" == region_low[:2]
    cache_actual = CACHE_PROXIES_PE if es_pe else CACHE_PROXIES_NG

    if cache_actual and len(cache_actual) >= max(1, cantidad_necesaria):
        print(f"  [Proxy Cache] Usando {len(cache_actual)} proxies de "
              f"{'PERÚ' if es_pe else 'NIGERIA'} pre-validados en memoria (lista completa).")
        return list(cache_actual)

    proxies_shuffled = list(proxy_list)
    random.shuffle(proxies_shuffled)
    # Probar un lote amplio: hace falta margen para rotaciones, no solo 1 por cuenta
    objetivo_min = max(cantidad_necesaria * 4, cantidad_necesaria + 20)
    limite = min(len(proxies_shuffled), max(150, objetivo_min * 8))
    proxies_a_probar = proxies_shuffled[:limite]

    print(f"  [Proxy Test] Probando hasta {len(proxies_a_probar)} proxies para [{region}] "
          f"(objetivo mínimo {objetivo_min} válidos para {cantidad_necesaria} cuenta(s) + rotaciones)...")

    def test_uno(proxy):
        server = proxy["server"]
        username = proxy["username"]
        password = proxy["password"]

        if password and password.endswith("@"):
            password = password[:-1].strip()

        server_clean = server.replace('http://', '').replace('https://', '')
        if username and password:
            proxy_url = f"http://{username}:{password}@{server_clean}"
        else:
            proxy_url = f"http://{server_clean}"

        formatted_proxy = {
            "http": proxy_url,
            "https": proxy_url,
        }

        start_time = time.time()
        for endpoint in ["https://api.ipify.org?format=json", "https://httpbin.org/ip", "https://www.google.com"]:
            try:
                r = requests.get(endpoint, proxies=formatted_proxy, timeout=5.0)
                latency = time.time() - start_time
                if r.status_code == 200:
                    ip_val = ""
                    try:
                        ip_val = r.json().get("ip") or r.json().get("origin")
                    except Exception:
                        ip_val = server_clean.split(":")[0]
                    return {
                        "success": True,
                        "proxy": {"server": f"http://{server_clean}", "username": username, "password": password},
                        "latency": latency,
                        "ip": ip_val
                    }
            except Exception:
                pass
        return {"success": False, "server": server}

    resultados = []
    workers = min(40, len(proxies_a_probar))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(test_uno, p): p for p in proxies_a_probar}
        for future in as_completed(futures):
            res = future.result()
            if res["success"]:
                print(f"    - Proxy [{region}] ({res['proxy']['server']})... [OK] Latencia: {res['latency']:.2f}s | IP: {res['ip']}")
                resultados.append(res)
                # Seguir un poco más del mínimo para acumular repuestos, sin parar en N*2 tan pronto
                if len(resultados) >= max(objetivo_min, cantidad_necesaria * 5):
                    break

    if resultados:
        resultados.sort(key=lambda x: x["latency"])
        validos = [r["proxy"] for r in resultados]
        # Fusionar con caché previo: nunca reducir la lista validada a un lote pequeño
        if es_pe:
            CACHE_PROXIES_PE = fusionar_proxies_unicos(CACHE_PROXIES_PE, validos)
            print(f"  [Proxy Test] [OK] Más rápido: {resultados[0]['proxy']['server']} "
                  f"(Latencia: {resultados[0]['latency']:.2f}s). Lote OK: {len(validos)} | "
                  f"Caché PE total: {len(CACHE_PROXIES_PE)}")
            return list(CACHE_PROXIES_PE)
        CACHE_PROXIES_NG = fusionar_proxies_unicos(CACHE_PROXIES_NG, validos)
        print(f"  [Proxy Test] [OK] Más rápido: {resultados[0]['proxy']['server']} "
              f"(Latencia: {resultados[0]['latency']:.2f}s). Lote OK: {len(validos)} | "
              f"Caché NG total: {len(CACHE_PROXIES_NG)}")
        return list(CACHE_PROXIES_NG)

    print(f"  [Proxy Test] [WARN] Ninguno de los proxies probados funcionó.")
    return list(cache_actual) if cache_actual else []


class ThreadSafeProxyPool:
    """Administrador thread-safe para asegurar que NINGÚN proxy se repita entre ventanas simultáneas y gestionar rotaciones en bloqueos antirobot."""
    def __init__(self, raw_proxies: list[dict] | None = None, etiqueta: str = "PE"):
        self.lock = threading.Lock()
        self.etiqueta = etiqueta
        self.all_proxies = []
        self.blocked_servers = set()
        self.in_use_servers = set()
        if raw_proxies:
            self.set_proxies(raw_proxies)

    def set_proxies(self, raw_proxies: list[dict]):
        with self.lock:
            # Conservar bloqueos conocidos al recargar, pero aceptar lista completa nueva
            self.all_proxies = list(raw_proxies or [])

    def estadisticas(self) -> dict:
        with self.lock:
            return {
                "total": len(self.all_proxies),
                "bloqueados": len(self.blocked_servers),
                "en_uso": len(self.in_use_servers),
                "libres": sum(
                    1 for p in self.all_proxies
                    if p.get("server") and p.get("server") not in self.blocked_servers
                    and p.get("server") not in self.in_use_servers
                ),
            }

    def reiniciar_bloqueos(self):
        """Libera marcas antirobot de la sesión (útil entre lotes / opciones)."""
        with self.lock:
            n = len(self.blocked_servers)
            self.blocked_servers.clear()
        if n:
            print(f"  [Proxy Pool {self.etiqueta}] Reiniciados {n} proxies antes marcados como bloqueados.")

    def obtener_proxy_unico(self, current_server: str | None = None, espera_s: float = 45.0) -> dict | None:
        """Obtiene un proxy que no use ninguna otra ventana simultánea ni esté bloqueado.

        Antes, si todos los limpios estaban ocupados, un segundo bucle devolvía uno YA en uso: dos
        ventanas salían por la misma IP y el antirobot las bloqueaba a la vez. Ahora se espera a que
        alguna lo libere y, si no ocurre, se devuelve None para que la cuenta aborte en vez de
        compartir IP.
        """
        limite = time.time() + max(0.0, espera_s)
        aviso_espera = False
        while True:
            with self.lock:
                if current_server:
                    self.in_use_servers.discard(current_server)
                    current_server = None

                for p in self.all_proxies:
                    serv = p.get("server", "")
                    if serv and serv not in self.blocked_servers and serv not in self.in_use_servers:
                        self.in_use_servers.add(serv)
                        return p

                total = len(self.all_proxies)
                bloqueados = len(self.blocked_servers)
                en_uso = len(self.in_use_servers)

            if time.time() >= limite:
                print(f"  [Proxy Pool {self.etiqueta}] {Color.FAIL}[WARN] Sin proxies libres: {total} en total, "
                      f"{bloqueados} bloqueados, {en_uso} en uso por otras ventanas.{Color.ENDC}")
                return None

            if not aviso_espera:
                aviso_espera = True
                print(f"  [Proxy Pool {self.etiqueta}] Esperando a que se libere un proxy limpio "
                      f"({total} en total, {bloqueados} bloqueados, {en_uso} en uso)...")
            time.sleep(1.0)

    def rotar_y_marcar_bloqueado(self, current_server: str | None) -> dict | None:
        """Marca un proxy como bloqueado por antirobot y entrega uno completamente limpio e inútil."""
        with self.lock:
            if current_server:
                self.blocked_servers.add(current_server)
                self.in_use_servers.discard(current_server)
                print(f"  [Proxy Pool {self.etiqueta}] {Color.WARNING}Proxy {current_server} marcado como BLOQUEADO por antirobot.{Color.ENDC}")

            for p in self.all_proxies:
                serv = p.get("server", "")
                if serv and serv not in self.blocked_servers and serv not in self.in_use_servers:
                    self.in_use_servers.add(serv)
                    print(f"  [Proxy Pool {self.etiqueta}] {Color.GREEN}Rotado exitosamente a nuevo proxy único limpio: {serv}{Color.ENDC}")
                    return p

            print(f"  [Proxy Pool {self.etiqueta}] {Color.FAIL}[WARN] No quedan más proxies limpios sin bloquear.{Color.ENDC}")
            return None

    def liberar_proxy(self, current_server: str | None):
        if not current_server:
            return
        with self.lock:
            self.in_use_servers.discard(current_server)

GLOBAL_PE_PROXY_POOL = ThreadSafeProxyPool(etiqueta="PE")
GLOBAL_NG_PROXY_POOL = ThreadSafeProxyPool(etiqueta="NG")
PROXIES_DISK_LOCK = threading.Lock()


def alimentar_pool_proxies_nigeria(proxies: list[dict] | None) -> None:
    """Sincroniza el pool NG global para que rotaciones y liberaciones no se pisen entre hilos."""
    global GLOBAL_NG_PROXY_POOL
    if proxies:
        GLOBAL_NG_PROXY_POOL.set_proxies(list(proxies))
        st = GLOBAL_NG_PROXY_POOL.estadisticas()
        print(f"  [Proxy Pool NG] Alimentado con {st['total']} proxies "
              f"({st['libres']} libres, {st['bloqueados']} bloqueados).")


def cargar_cache_proxies_validos_desde_disco(forzar: bool = False) -> tuple[list[dict], list[dict]]:
    """Carga *_validos.txt completos a memoria/pool. No recorta al tamaño del lote."""
    global CACHE_PROXIES_NG, CACHE_PROXIES_PE, GLOBAL_NG_PROXY_POOL, GLOBAL_PE_PROXY_POOL
    if not forzar and CACHE_PROXIES_NG and CACHE_PROXIES_PE:
        return list(CACHE_PROXIES_NG), list(CACHE_PROXIES_PE)

    cfg = cargar_proxies_desde_txt(preferir_validos=True)
    ng = (cfg or {}).get("proxy_ng_list") or []
    pe = (cfg or {}).get("proxy_pe_list") or []

    # Solo adoptar si vienen de archivos con datos; fusionar con caché en memoria
    if ng:
        CACHE_PROXIES_NG = fusionar_proxies_unicos(CACHE_PROXIES_NG, ng)
        GLOBAL_NG_PROXY_POOL.set_proxies(CACHE_PROXIES_NG)
    if pe:
        CACHE_PROXIES_PE = fusionar_proxies_unicos(CACHE_PROXIES_PE, pe)
        GLOBAL_PE_PROXY_POOL.set_proxies(CACHE_PROXIES_PE)

    if CACHE_PROXIES_NG or CACHE_PROXIES_PE:
        print(f"  {Color.GREEN}[Proxy Disk] Caché desde disco: "
              f"{len(CACHE_PROXIES_NG)} NG | {len(CACHE_PROXIES_PE)} PE (listas completas).{Color.ENDC}")
    return list(CACHE_PROXIES_NG), list(CACHE_PROXIES_PE)


def asegurar_proxies_peru(cantidad_necesaria: int = 5) -> list[dict]:
    """Carga proxies PE: primero lista completa validada en disco/caché; si falta, prueba ampliando."""
    global valid_pe_list, CACHE_PROXIES_PE, GLOBAL_PE_PROXY_POOL

    cargar_cache_proxies_validos_desde_disco()

    if CACHE_PROXIES_PE and len(CACHE_PROXIES_PE) >= max(1, cantidad_necesaria):
        valid_pe_list = list(CACHE_PROXIES_PE)
        GLOBAL_PE_PROXY_POOL.set_proxies(valid_pe_list)
        st = GLOBAL_PE_PROXY_POOL.estadisticas()
        print(f"  {Color.GREEN}[Proxy Caché] Pool PE con {st['total']} proxies verificados "
              f"({st['libres']} libres) para {cantidad_necesaria} cuenta(s).{Color.ENDC}")
        return valid_pe_list

    try:
        from config_migrar import proxies_cfg
    except ImportError:
        proxies_cfg = None

    if proxies_cfg and proxies_cfg.get("proxy_pe_list"):
        pe_list = proxies_cfg["proxy_pe_list"]
    else:
        proxies_cfg_local = cargar_proxies_desde_txt(preferir_validos=False)
        pe_list = proxies_cfg_local.get("proxy_pe_list", []) if proxies_cfg_local else []

    if pe_list:
        print(f"  {Color.CYAN}[Proxies PE] Probando y ampliando pool de Perú...{Color.ENDC}")
        objetivo = max(cantidad_necesaria * 4, cantidad_necesaria + 15)
        valid_pe_list = probar_y_seleccionar_mejor_proxy(pe_list, "Peru", objetivo)
        # Persistir lo encontrado para no perderlo al cerrar el script
        if valid_pe_list:
            guardar_proxies_validos_txt(SCRIPT_DIR / "lista_proxies_pe_validos.txt", valid_pe_list)
        GLOBAL_PE_PROXY_POOL.set_proxies(valid_pe_list)
        print(f"  {Color.GREEN}[Proxies PE] Pool listo con {len(valid_pe_list)} proxies para {cantidad_necesaria} cuenta(s) "
              f"(incluye repuestos para las rotaciones).{Color.ENDC}")
    else:
        print(f"  {Color.FAIL}[WARN] No se encontraron proxies de Perú en la configuración.{Color.ENDC}")
        valid_pe_list = []

    return valid_pe_list



def guardar_proxies_validos_txt(filepath: Path, proxy_list: list[dict]):
    """Guarda la lista de forma atómica (tmp + replace) bajo candado entre hilos."""
    if not proxy_list:
        return
    lines = []
    vistos = set()
    for p in proxy_list:
        server = (p.get("server") or "").replace("http://", "").replace("https://", "").strip()
        if not server:
            continue
        clave = server.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        user = p.get("username", "") or ""
        pwd = p.get("password", "") or ""
        if user and pwd:
            lines.append(f"http://{user}:{pwd}@{server}")
        else:
            lines.append(f"http://{server}")
    if not lines:
        return
    with PROXIES_DISK_LOCK:
        tmp_path = filepath.with_suffix(filepath.suffix + ".tmp")
        try:
            tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp_path, filepath)
        except Exception as e:
            print(f"  [Proxy Save] [WARN] No se pudo guardar {filepath.name}: {e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
            except Exception:
                pass


def validar_proxies_opcion13():
    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   OPCIÓN 13: VALIDACIÓN MASIVA DE PROXIES (NIGERIA Y PERÚ){Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")

    # Validar siempre contra las listas fuente (ng.txt / pe.txt), no contra *_validos
    # (que pueden estar vacíos o ser un subconjunto previo).
    proxies_cfg = cargar_proxies_desde_txt(preferir_validos=False)
    if not proxies_cfg:
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} No se encontraron listas de proxies en "
              f"'lista_proxies_ng.txt', 'lista_proxies_pe.txt' ni 'proxies.txt'.")
        print(f"{Color.WARNING}[Info]{Color.ENDC} Formatos aceptados: user:pass@host:port | "
              f"host;port;user;pass | host:port:user:pass")
        input("\n>>> Presiona Enter para volver al menú principal <<<")
        return

    ng_list = proxies_cfg.get("proxy_ng_list", [])
    pe_list = proxies_cfg.get("proxy_pe_list", [])

    print(f"  [Proxies Encontrados] Nigeria: {len(ng_list)} | Perú: {len(pe_list)}")
    if not ng_list and not pe_list:
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} Las listas de proxies están vacías o no pudieron ser parseadas.")
        input("\n>>> Presiona Enter para volver al menú principal <<<")
        return

    concurrencia_input = input("\nIngrese la concurrencia de prueba (hilos simultáneos, por defecto 50): ").strip()
    try:
        max_workers = int(concurrencia_input) if concurrencia_input else 50
    except ValueError:
        max_workers = 50

    global CACHE_PROXIES_NG, CACHE_PROXIES_PE

    def test_proxy_single(proxy):
        server = proxy["server"]
        username = proxy["username"]
        password = proxy["password"]

        if password and password.endswith("@"):
            password = password[:-1].strip()

        server_clean = server.replace('http://', '').replace('https://', '')
        if username and password:
            proxy_url = f"http://{username}:{password}@{server_clean}"
        else:
            proxy_url = f"http://{server_clean}"

        formatted_proxy = {
            "http": proxy_url,
            "https": proxy_url,
        }

        start_time = time.time()
        for endpoint in ["https://api.ipify.org?format=json", "https://httpbin.org/ip", "https://www.google.com"]:
            try:
                r = requests.get(endpoint, proxies=formatted_proxy, timeout=5.0)
                latency = time.time() - start_time
                if r.status_code == 200:
                    ip_val = ""
                    try:
                        ip_val = r.json().get("ip") or r.json().get("origin")
                    except Exception:
                        ip_val = server_clean.split(":")[0]
                    return {
                        "success": True,
                        "proxy": {"server": f"http://{server_clean}", "username": username, "password": password},
                        "latency": latency,
                        "ip": ip_val
                    }
            except Exception:
                pass
        return {"success": False, "server": server}

    def _flush_validos(region: str, resultados: list, path: Path, forzar: bool = False):
        """Guarda a disco de forma incremental para no perder el progreso si cierras el script."""
        if not resultados:
            return
        if not forzar and len(resultados) % 25 != 0:
            return
        ordenados = sorted(resultados, key=lambda x: x["latency"])
        proxies = [r["proxy"] for r in ordenados]
        guardar_proxies_validos_txt(path, proxies)
        print(f"  {Color.CYAN}[Guardado parcial {region}]{Color.ENDC} {len(proxies)} válidos → {path.name}")

    try:
        if ng_list:
            print(f"\n{Color.CYAN}{Color.BOLD}--- PROBANDO {len(ng_list)} PROXIES DE NIGERIA (Concurrencia: {max_workers}) ---{Color.ENDC}")
            print(f"  {Color.WARNING}[Info] Se guardan en disco cada 25 OK (y al terminar / Ctrl+C).{Color.ENDC}")
            validos_ng = []
            probados = 0
            ng_valid_path = SCRIPT_DIR / "lista_proxies_ng_validos.txt"
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(test_proxy_single, p): p for p in ng_list}
                try:
                    for future in as_completed(futures):
                        probados += 1
                        res = future.result()
                        if res["success"]:
                            validos_ng.append(res)
                            print(f"  {Color.GREEN}[NG OK]{Color.ENDC} ({probados}/{len(ng_list)}) Latencia: {res['latency']:.2f}s | IP: {res['ip']} | Proxy: {res['proxy']['server']}")
                            _flush_validos("NG", validos_ng, ng_valid_path)
                        if probados % 100 == 0:
                            print(f"  [Progreso NG] Procesados {probados}/{len(ng_list)} | Funcionales hasta ahora: {len(validos_ng)}")
                except KeyboardInterrupt:
                    print(f"\n  {Color.WARNING}[NG] Interrumpido por el usuario. Guardando {len(validos_ng)} válidos hallados...{Color.ENDC}")
                    for f in futures:
                        f.cancel()
                    _flush_validos("NG", validos_ng, ng_valid_path, forzar=True)
                    raise

            validos_ng.sort(key=lambda x: x["latency"])
            CACHE_PROXIES_NG = [r["proxy"] for r in validos_ng]
            if CACHE_PROXIES_NG:
                guardar_proxies_validos_txt(ng_valid_path, CACHE_PROXIES_NG)
                GLOBAL_NG_PROXY_POOL.set_proxies(CACHE_PROXIES_NG)
                print(f"  {Color.GREEN}[Guardado Disk]{Color.ENDC} Guardados {len(CACHE_PROXIES_NG)} proxies válidos de Nigeria en: {ng_valid_path.name}")
            print(f"\n{Color.GREEN}{Color.BOLD}>>> RESUMEN NIGERIA: {len(CACHE_PROXIES_NG)} proxies funcionales encontrados de {len(ng_list)} probados. <<<{Color.ENDC}")

        if pe_list:
            print(f"\n{Color.CYAN}{Color.BOLD}--- PROBANDO {len(pe_list)} PROXIES DE PERÚ (Concurrencia: {max_workers}) ---{Color.ENDC}")
            print(f"  {Color.WARNING}[Info] Se guardan en disco cada 25 OK (y al terminar / Ctrl+C).{Color.ENDC}")
            validos_pe = []
            probados_pe = 0
            pe_valid_path = SCRIPT_DIR / "lista_proxies_pe_validos.txt"
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(test_proxy_single, p): p for p in pe_list}
                try:
                    for future in as_completed(futures):
                        probados_pe += 1
                        res = future.result()
                        if res["success"]:
                            validos_pe.append(res)
                            print(f"  {Color.GREEN}[PE OK]{Color.ENDC} ({probados_pe}/{len(pe_list)}) Latencia: {res['latency']:.2f}s | IP: {res['ip']} | Proxy: {res['proxy']['server']}")
                            _flush_validos("PE", validos_pe, pe_valid_path)
                        if probados_pe % 100 == 0:
                            print(f"  [Progreso PE] Procesados {probados_pe}/{len(pe_list)} | Funcionales hasta ahora: {len(validos_pe)}")
                except KeyboardInterrupt:
                    print(f"\n  {Color.WARNING}[PE] Interrumpido por el usuario. Guardando {len(validos_pe)} válidos hallados...{Color.ENDC}")
                    for f in futures:
                        f.cancel()
                    _flush_validos("PE", validos_pe, pe_valid_path, forzar=True)
                    raise

            validos_pe.sort(key=lambda x: x["latency"])
            CACHE_PROXIES_PE = [r["proxy"] for r in validos_pe]
            if CACHE_PROXIES_PE:
                guardar_proxies_validos_txt(pe_valid_path, CACHE_PROXIES_PE)
                GLOBAL_PE_PROXY_POOL.set_proxies(CACHE_PROXIES_PE)
                print(f"  {Color.GREEN}[Guardado Disk]{Color.ENDC} Guardados {len(CACHE_PROXIES_PE)} proxies válidos de Perú en: {pe_valid_path.name}")
            print(f"\n{Color.GREEN}{Color.BOLD}>>> RESUMEN PERÚ: {len(CACHE_PROXIES_PE)} proxies funcionales encontrados de {len(pe_list)} probados. <<<{Color.ENDC}")

        print(f"\n{Color.GREEN}{Color.BOLD}>>> VALIDACIÓN COMPLETADA. Proxies funcionales guardados en disco y cargados en memoria. <<<{Color.ENDC}\n")
    except KeyboardInterrupt:
        print(f"\n{Color.WARNING}{Color.BOLD}>>> Validación interrumpida. Lo hallado hasta ahora ya está en *_validos.txt. <<<{Color.ENDC}\n")
    input(">>> Presiona Enter para volver al menú principal <<<")

def obtener_max_email_id(gmail_user="cakeseller1234@gmail.com", query_from="tidal") -> int:
    """Obtiene el UID IMAP más alto de correos cuyo remitente coincide con query_from.

    Siempre filtra por FROM (nunca ALL): sin eso, un correo de TuneMyMusic u otro flujo
    concurrente (opc. 11/14) inflaría la línea base. El UID es monotónico por buzón; con
    aliases del mismo Gmail varios hilos pueden subir el máximo, pero obtener_codigo_via_imap
    sigue filtrando por destinatario exacto.
    """
    user_real, app_pwd = obtener_credenciales_imap_reales(gmail_user)
    if not user_real or not app_pwd:
        return 0
    remitente = (query_from or "tidal").strip() or "tidal"
    criterio = f'(FROM "{remitente}")'
    try:
        with sesion_imap(user_real, app_pwd) as mail:
            status, messages = mail.uid("search", None, criterio)
            if status == "OK" and messages and messages[0]:
                ids = [int(x) for x in messages[0].split() if x.isdigit()]
                if ids:
                    return max(ids)
    except Exception as e:
        print(f"    [IMAP] [WARN] Error en obtener_max_email_id (FROM={remitente!r}): {e}")
    return 0

stdin_lock = threading.Lock()

class TidalRegisterManager:
    def __init__(self, client_email, client_pwd, proxy_ng_server=None, proxy_ng_user=None, proxy_ng_pass=None,
                 proxy_pe_server=None, proxy_pe_user=None, proxy_pe_pass=None, headless=False):
        self.client_email = client_email
        self.client_pwd = client_pwd
        self.use_proxy = proxy_ng_server is not None
        self.proxy_ng_server = proxy_ng_server
        self.proxy_ng_user = proxy_ng_user
        self.proxy_ng_pass = proxy_ng_pass
        self.proxy_pe_server = proxy_pe_server
        self.proxy_pe_user = proxy_pe_user
        self.proxy_pe_pass = proxy_pe_pass
        # El proceso arranca siempre en la fase de Nigeria; cambiar_a_proxy_peru() conmuta después a PE
        self.current_proxy_type = "NG"
        self.headless = headless
        self.playwright = None
        self.context = None
        self.page = None
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        self.start_step = 6
        self.cuenta_abortada = False
        # Hosts de pasarela cuyas peticiones falló el navegador: sirve para explicar en claro por qué
        # los campos de tarjeta aparecen vacíos en vez de dejar al operador mirando cajas grises.
        self.pagos_bloqueados = set()
        self._rotaciones_antibot = 0
        self._recuperaciones_error_tidal = 0
        # CSV fijado 1:1 en la opción 8 (asignar_csvs_a_cuentas); no re-resolver a otro archivo.
        self.csv_asignado = None
        
        email_safe = re.sub(r'[^a-zA-Z0-9]', '_', client_email)
        self.main_profile = Path(tempfile.gettempdir()) / f"tidal_reg_{email_safe}_{random.randint(1000, 9999)}"

    def recuperar_login_tras_error_tidal(self) -> bool:
        """Tras 'Algo salió mal' en authorize/signin: NO recargar esa URL.

        Recargar login.tidal.com/authorize?email=... con otra IP reproduce el Error
        (caso g.etspooky2.189). Siempre salir por pricing → account.tidal.com/.
        """
        try:
            self.page = pagina_vigente(self.page)
            if not self.page or self.page.is_closed():
                self.asegurar_navegador_abierto()
            print(f"  [Registro] [{self.client_email}] Recuperando flujo: "
                  f"tidal.com/pricing → account.tidal.com/ (sin recargar authorize)...")
            navegar_tidal_tolerante(self.page, "https://tidal.com/pricing", timeout_ms=30000)
            time.sleep(random.uniform(1.2, 2.2))
            aceptar_cookies_con_espera(self.page)
            time.sleep(0.3)
            try:
                navegar_tidal_tolerante(
                    self.page, "https://account.tidal.com/",
                    referer="https://tidal.com/pricing",
                    timeout_ms=30000,
                )
            except Exception:
                navegar_tidal_tolerante(
                    self.page, "https://account.tidal.com/login",
                    referer="https://tidal.com/pricing",
                    timeout_ms=30000,
                )
            time.sleep(2.0)
            self.page = pagina_vigente(self.page)
            if es_pantalla_error_login_tidal(self.page) or url_es_oauth_login_roto(self.page.url or ""):
                print(f"  [Registro] [{self.client_email}] Aún Error/authorize; reintento pricing→cuenta...")
                try:
                    navegar_tidal_tolerante(self.page, "https://tidal.com/pricing", timeout_ms=25000)
                    time.sleep(1.5)
                    aceptar_cookies_con_espera(self.page)
                    navegar_tidal_tolerante(
                        self.page, "https://account.tidal.com/",
                        referer="https://tidal.com/pricing",
                        timeout_ms=30000,
                    )
                    time.sleep(1.5)
                except Exception:
                    pass
            self.page = pagina_vigente(self.page)
            if es_pantalla_error_login_tidal(self.page):
                return False
            return _formulario_login_visible(self.page) or (
                url_es_login_o_cuenta(self.page.url or "")
                and not url_es_oauth_login_roto(self.page.url or "")
            )
        except Exception as e:
            print(f"  [Registro] [{self.client_email}] [WARN] recuperar_login_tras_error_tidal: {e}")
            return False

    def forzar_modo_visual(self):
        if not self.headless:
            return
        print(f"\n  [Modo Headless] Intervención requerida para {self.client_email}. Transicionando a modo visual...")
        current_url = "https://account.tidal.com/"
        try:
            if self.page and not self.page.is_closed():
                current_url = self.page.url
        except Exception:
            pass
        # Nunca reabrir authorize?email= /signin (reproduce 'Algo salió mal')
        if url_es_oauth_login_roto(current_url):
            current_url = "https://account.tidal.com/"
        self.headless = False
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.context = None
        self.page = None
        self.playwright = None
        
        # Esperar a que se liberen los bloqueos de archivos en Windows
        time.sleep(2.5)
        reparar_perfil_corrupto(self.main_profile)
        time.sleep(1.0)
        
        print("  [Modo Headless] Levantando navegador headed...")
        try:
            self.asegurar_navegador_abierto()
            if self.page and not self.page.is_closed():
                try:
                    if url_es_oauth_login_roto(current_url) or "login.tidal.com" in (current_url or "").lower():
                        self.recuperar_login_tras_error_tidal()
                    elif current_url and current_url.startswith("http"):
                        self.page.goto(current_url, wait_until="domcontentloaded", timeout=25000)
                        time.sleep(2.0)
                except Exception:
                    pass
                print("  [Modo Headless] Navegador headed abierto correctamente.")
        except Exception as e:
            print(f"  [Modo Headless] [ERROR] No se pudo abrir el navegador: {e}")

    def ejecutar_rotacion_proxy_y_recargar(self):
        # Rotar según la fase actual: durante el checkout el tráfico va por Perú y rotar a
        # Nigeria rompía tanto el geo del pago como la sesión del formulario.
        tipo_actual = self.current_proxy_type if self.current_proxy_type in ("NG", "PE") else "NG"
        pais = "Perú" if tipo_actual == "PE" else "Nigeria"
        print(f"\n  [Auto-Proxy] Bloqueo detectado en {self.client_email}. Rotando proxy de {pais}...")
        current_url = "https://account.tidal.com/"
        try:
            if self.page:
                current_url = self.page.url
        except Exception:
            pass
        url_rota = url_es_oauth_login_roto(current_url)
        self.rotar_proxy_contexto(tipo=tipo_actual)
        try:
            if self.context:
                self.context.clear_cookies(domain="tidal.com")
                self.context.clear_cookies(domain="login.tidal.com")
                self.context.clear_cookies(domain="account.tidal.com")
        except Exception:
            pass
        # NUNCA recargar authorize?email= /signin: es el bucle de "Algo salió mal".
        if url_rota or "login.tidal.com" in (current_url or "").lower():
            print(f"  [Auto-Proxy] [{self.client_email}] URL rota detectada "
                  f"({(current_url or '')[:90]}). Reabriendo vía pricing (no se recarga authorize)...")
            try:
                if not self.recuperar_login_tras_error_tidal():
                    navegar_tidal_tolerante(
                        self.page, "https://account.tidal.com/",
                        referer="https://tidal.com/pricing",
                        timeout_ms=30000,
                    )
                    time.sleep(2.0)
            except Exception as e:
                print(f"  [Auto-Proxy] [WARN] Error al reabrir cuenta tras rotación: {e}")
        elif current_url and current_url.startswith("http"):
            print(f"  [Auto-Proxy] Recargando página con el nuevo proxy en: {current_url}")
            try:
                self.page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2.0)
            except Exception as e:
                print(f"  [Auto-Proxy] [WARN] Error al recargar: {e}")

    def rotar_proxy_contexto(self, tipo="NG"):
        global valid_ng_list, valid_pe_list
        proxy_tipo = tipo if tipo in ("NG", "PE") else (self.current_proxy_type or "NG")
        print(f"\n  [Proxy Rotation] Rotando proxy de {'Perú' if proxy_tipo == 'PE' else 'Nigeria'} por uno nuevo...")
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.context = None
        self.page = None
        self.playwright = None
        
        # Esperar a que se liberen los bloqueos de archivos en Windows
        time.sleep(2.5)
        reparar_perfil_corrupto(self.main_profile)
        
        if proxy_tipo == "PE":
            # Antes: random.choice sin tocar el pool → dos hilos podían compartir IP y el
            # proxy quemado seguía marcado como "en uso" sin rotar de verdad.
            anterior = self.proxy_pe_server
            p_pe = GLOBAL_PE_PROXY_POOL.rotar_y_marcar_bloqueado(anterior)
            if not p_pe and valid_pe_list:
                candidatos = [p for p in valid_pe_list if p.get("server") != anterior]
                p_pe = random.choice(candidatos) if candidatos else None
            if p_pe:
                self.proxy_pe_server = p_pe.get("server")
                self.proxy_pe_user = p_pe.get("username")
                self.proxy_pe_pass = p_pe.get("password")
                self.current_proxy_type = "PE"
                self.use_proxy = True
                print(f"  [Proxy Rotation] Nuevo proxy PE configurado: {self.proxy_pe_server}")
            else:
                print("  [Proxy Rotation] No hay más proxies PE disponibles.")
                raise RuntimeError(f"Sin proxies PE limpios para rotar ({self.client_email}).")
        else:
            anterior = self.proxy_ng_server
            p_ng = GLOBAL_NG_PROXY_POOL.rotar_y_marcar_bloqueado(anterior)
            if not p_ng and valid_ng_list:
                candidatos = [p for p in valid_ng_list if p.get("server") != anterior]
                p_ng = random.choice(candidatos) if candidatos else None
            if p_ng:
                self.proxy_ng_server = p_ng.get("server")
                self.proxy_ng_user = p_ng.get("username")
                self.proxy_ng_pass = p_ng.get("password")
                self.current_proxy_type = "NG"
                self.use_proxy = True
                print(f"  [Proxy Rotation] Nuevo proxy NG configurado: {self.proxy_ng_server}")
            else:
                print("  [Proxy Rotation] No hay más proxies NG disponibles.")
                raise RuntimeError(f"Sin proxies NG limpios para rotar ({self.client_email}).")
        self.asegurar_navegador_abierto()

    def cambiar_a_proxy_peru(self):
        print(f"\n  [Proxy Switch] [{self.client_email}] Registro inicial finalizado.")
        print(f"  [Proxy Switch] [{self.client_email}] Cambiando a proxy de PERÚ para el proceso de pago...")
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.context = None
        self.page = None
        self.playwright = None

        time.sleep(2.5)
        reparar_perfil_corrupto(self.main_profile)

        # La fase NG ya terminó: devolver ese proxy al pool para que otro hilo pueda usarlo.
        # Antes el navegador se recreaba con PE y el NG quedaba "en uso" para siempre.
        if self.proxy_ng_server:
            try:
                GLOBAL_NG_PROXY_POOL.liberar_proxy(self.proxy_ng_server)
            except Exception:
                pass
            self.proxy_ng_server = None
            self.proxy_ng_user = None
            self.proxy_ng_pass = None

        global valid_pe_list
        if not self.proxy_pe_server:
            # Reservar en el pool: random.choice repetía IPs entre ventanas simultáneas
            p_pe = GLOBAL_PE_PROXY_POOL.obtener_proxy_unico()
            if not p_pe and valid_pe_list:
                p_pe = valid_pe_list[0]
            if p_pe:
                self.proxy_pe_server = p_pe.get("server")
                self.proxy_pe_user = p_pe.get("username")
                self.proxy_pe_pass = p_pe.get("password")

        self.current_proxy_type = "PE"
        if self.proxy_pe_server:
            self.use_proxy = True
            p_serv = self.proxy_pe_server
            if not p_serv.startswith("http"):
                p_serv = "http://" + p_serv
            print(f"  [Proxy Switch] [{self.client_email}] Conectado mediante proxy de PERÚ: {p_serv}")
            print(f"  [Proxy Switch] [{self.client_email}] Pasarelas de pago y 3D Secure excluidas del proxy "
                  f"(el proveedor las deniega y sin esto los campos de tarjeta salen vacíos).")
        else:
            # Nunca salir por la IP real: DataDome la marcaría y bloquearía todo el proceso a futuro
            raise RuntimeError(
                f"Sin proxy de Perú disponible para el pago de {self.client_email}. Se aborta antes de exponer tu IP real."
            )

        self.asegurar_navegador_abierto()

    def registrar_contador_datos(self, context):
        pass

    def input_concurrente(self, prompt):
        global stdin_lock
        with stdin_lock:
            print("\n" + "!" * 80)
            print(f"  [AVISO] PAUSA MANUAL REQUERIDA PARA: {self.client_email}")
            print("!" * 80)
            res = input(prompt)
            print("!" * 80 + "\n")
            return res

    def asegurar_navegador_abierto(self):
        try:
            if self.context and self.page and not self.page.is_closed():
                return
        except Exception:
            pass
            
        if not self.playwright:
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            
        reparar_perfil_corrupto(self.main_profile)
        launch_args = list(CHROME_SILENT_ARGS)
        proxy_dict = None
        if self.use_proxy:
            if self.current_proxy_type == "PE" and self.proxy_pe_server:
                p_serv = self.proxy_pe_server
                if p_serv and not p_serv.startswith("http"):
                    p_serv = "http://" + p_serv
                proxy_dict = {"server": p_serv}
                if self.proxy_pe_user:
                    proxy_dict["username"] = self.proxy_pe_user
                if self.proxy_pe_pass:
                    proxy_dict["password"] = self.proxy_pe_pass
                print(f"  [Proxy] Usando proxy de PERÚ: {p_serv}")
            elif self.proxy_ng_server:
                p_serv = self.proxy_ng_server
                if p_serv and not p_serv.startswith("http"):
                    p_serv = "http://" + p_serv
                proxy_dict = {"server": p_serv}
                if self.proxy_ng_user:
                    proxy_dict["username"] = self.proxy_ng_user
                if self.proxy_ng_pass:
                    proxy_dict["password"] = self.proxy_ng_pass
                print(f"  [Proxy] Usando proxy de NIGERIA: {p_serv}")

        if proxy_dict:
            # Sin esta exclusión el iframe de la tarjeta (Adyen) recibe ERR_TUNNEL_CONNECTION_FAILED
            # y el checkout muestra los campos de tarjeta como cajas grises imposibles de rellenar.
            proxy_dict["bypass"] = construir_bypass_pasarelas()

        launch_kwargs = {
            "user_data_dir": str(self.main_profile),
            "headless": self.headless,
            "args": launch_args,
            "ignore_default_args": ["--enable-automation", "--no-sandbox"],
            "viewport": {"width": 1280, "height": 800},
            "locale": "es-ES",
            "proxy": proxy_dict,
            "channel": "chrome",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
            
        try:
            self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:
            print(f"  [Navegador] [WARN] Falló el lanzamiento: {e}. Reparando y reintentando...")
            reparar_perfil_corrupto(self.main_profile)
            time.sleep(2.0)
            self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
            
        self.context.set_default_navigation_timeout(45000)
        self.context.set_default_timeout(35000)
        self.context.add_init_script(STEALTH_SCRIPT)
        self.vigilar_peticiones_de_pago()
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.client_email = self.client_email
        self.page.manager = self
        self.page.bring_to_front()

    def vigilar_peticiones_de_pago(self) -> None:
        """Anota los hosts de pasarela que el navegador no consigue cargar."""
        claves = ("adyen", "stripe", "paypal", "braintree", "cardinal", "worldpay",
                  "3dsecure", "klarna", "checkout.com")

        def on_failed(request):
            try:
                url = (request.url or "").lower()
                if any(k in url for k in claves):
                    self.pagos_bloqueados.add(url.split("/")[2] if "//" in url else url)
            except Exception:
                pass

        try:
            self.context.on("requestfailed", on_failed)
        except Exception:
            pass

    def cerrar_navegador(self, liberar_ng: bool = True, liberar_pe: bool = True):
        """Cierra Chrome y, opcionalmente, devuelve los proxies reservados a sus pools.

        Sin esto, al resetear el navegador (rotación / fin de registro) el proxy NG quedaba
        marcado como en uso y el pool se agotaba entre ventanas de la opción 8.
        liberar_pe=False cuando el PE aún se necesita para TuneMyMusic tras un registro OK.
        """
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        self.context = None
        self.page = None
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.playwright = None

        if liberar_ng and getattr(self, "proxy_ng_server", None):
            try:
                GLOBAL_NG_PROXY_POOL.liberar_proxy(self.proxy_ng_server)
            except Exception:
                pass
            self.proxy_ng_server = None
            self.proxy_ng_user = None
            self.proxy_ng_pass = None
        if liberar_pe and getattr(self, "proxy_pe_server", None):
            try:
                GLOBAL_PE_PROXY_POOL.liberar_proxy(self.proxy_pe_server)
            except Exception:
                pass
            self.proxy_pe_server = None
            self.proxy_pe_user = None
            self.proxy_pe_pass = None

    def run_registration(self, cerrar_navegador_al_final=True) -> bool:
        registro_exitoso = False
        try:
            self.asegurar_navegador_abierto()
            self.context.clear_cookies(domain="tidal.com")
            self.context.clear_cookies(domain="login.tidal.com")
            self.context.clear_cookies(domain="account.tidal.com")
            
            print(f"\n--- Iniciando registro para: {self.client_email} ---")
            
            # Bypass de reputación: entrar en frío a account.tidal.com/ a menudo acaba en
            # login.tidal.com/authorize?email=... con "Algo salió mal" (caso g.etspooky2.189).
            print(f"  [Registro] [{self.client_email}] Calentando reputación en tidal.com/pricing...")
            try:
                navegar_tidal_tolerante(self.page, "https://tidal.com/pricing", timeout_ms=30000)
                time.sleep(random.uniform(1.2, 2.2))
                aceptar_cookies_con_espera(self.page)
            except Exception as e:
                print(f"  [Registro] [{self.client_email}] [WARN] Pricing falló: {e}")

            print(f"  [Registro] [{self.client_email}] Cargando account.tidal.com/ con referer...")
            try:
                navegar_tidal_tolerante(
                    self.page, "https://account.tidal.com/",
                    referer="https://tidal.com/pricing",
                    timeout_ms=30000,
                )
            except Exception as e:
                print(f"  [Registro] [{self.client_email}] [WARN] Timeout en navegación inicial: {e}")
                # Si caímos en authorize?email= con Error, recuperar ya (no esperar a quemar 4 proxies)
                if es_pantalla_error_login_tidal(self.page) or url_es_oauth_login_roto(
                    getattr(self.page, "url", "") or ""
                ):
                    self.recuperar_login_tras_error_tidal()
                
            email_input = esperar_locator_en_frames(self.page, ['input[type="email"]', 'input[name="email"]'], timeout_s=25.0)
            if not email_input:
                # Si falló, rotamos el proxy una única vez y reintentamos por pricing (nunca authorize)
                if self.use_proxy:
                    print(f"  [Registro] [{self.client_email}] No se localizó el campo de correo. Rotando proxy y reintentando...")
                    self.ejecutar_rotacion_proxy_y_recargar()
                    time.sleep(2.0)
                    if not _formulario_login_visible(self.page):
                        try:
                            self.recuperar_login_tras_error_tidal()
                        except Exception:
                            pass
                    email_input = esperar_locator_en_frames(self.page, ['input[type="email"]', 'input[name="email"]'], timeout_s=25.0)
                    
            if not email_input:
                raise RuntimeError("No se localizó el campo de correo para iniciar el registro en Tidal.")
            
            aceptar_cookies_con_espera(self.page)
            manejar_bloqueos_e_intervencion(self.page, "Registro Tidal (Email)")
            
            # 1. Colocar correo a registrar
            print(f"  [Registro] [{self.client_email}] Ingresando correo electrónico...")
            
            email_input = esperar_locator_en_frames(self.page, ['input[type="email"]', 'input[name="email"]', '#email'], timeout_s=15.0)
            if email_input:
                try:
                    val_actual = email_input.input_value().strip()
                except Exception:
                    val_actual = ""

                if val_actual != self.client_email:
                    email_input.fill("")
                    email_input.fill(self.client_email)
                    time.sleep(0.3)
                    self.page.evaluate("""
                        () => {
                            const el = document.querySelector('input[type="email"]') || 
                                       document.querySelector('input[name="email"]') ||
                                       document.querySelector('#email');
                            if (el) {
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                        }
                    """)
                
                email_input.press("Enter")
                
                self.page.evaluate("""
                    () => {
                        const btn = document.querySelector('button[type="submit"]') || 
                                    document.querySelector('button[ui-test-id="check-user-continue-button"]') ||
                                    Array.from(document.querySelectorAll('button')).find(b => (b.textContent||'').toLowerCase().includes('continuar'));
                        if (btn) {
                            btn.removeAttribute('disabled');
                            btn.disabled = false;
                            btn.click();
                        }
                        const form = document.querySelector('form');
                        if (form) {
                            try { form.requestSubmit(); } catch(e){}
                        }
                    }
                """)
                
                btn_continue = esperar_locator_en_frames(
                    self.page,
                    ["button:has-text('Continuar')", "button:has-text('Continue')", "button[type='submit']"],
                    timeout_s=2.0
                )
                if btn_continue:
                    try:
                        btn_continue.click(timeout=1500, force=True)
                    except Exception:
                        pass

                time.sleep(2.0)
            
            # Verificar si pide contraseña (cuenta ya registrada)
            pwd_input_check = encontrar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]'])
            if pwd_input_check:
                print(f"  {Color.WARNING}[Registro] La cuenta {self.client_email} ya está registrada en TIDAL. Omitiendo...{Color.ENDC}")
                registro_exitoso = True
                return True

            print("  [Registro] Rellenando fecha de nacimiento (15/08/1995)...")
            time.sleep(1.0)
            
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
            


            print("  [Registro] Marcando checkbox de términos...")
            self.page.evaluate("""
                () => {
                    const checkboxes = document.querySelectorAll('input[type="checkbox"]');
                    checkboxes.forEach(cb => {
                        const parentText = cb.parentElement ? cb.parentElement.textContent || '' : '';
                        if (parentText.includes('Términos') || parentText.includes('Terms') || 
                            parentText.includes('Privacidad') || parentText.includes('Privacy')) {
                            if (!cb.checked) {
                                cb.click();
                                if (!cb.checked && cb.parentElement) {
                                    cb.parentElement.click();
                                }
                            }
                        }
                    });
                }
            """)
            time.sleep(1.0)
            
            # Serializar Suscríbete + IMAP + envío OTP por buzón Gmail (aliases con puntos).
            with _lock_registro_mismo_buzon(self.client_email):
                max_id_previo = obtener_max_email_id(self.client_email)

                def _pantalla_otp_registro() -> bool:
                    try:
                        self.page = pagina_vigente(self.page)
                        if not self.page or self.page.is_closed():
                            return False
                        return bool(self.page.evaluate("""() => {
                            const t = document.body ? document.body.innerText.toLowerCase() : '';
                            const frases = [
                                'verify your email', 'verifica tu correo', 'verifica tu email',
                                'verificar tu correo', 'verificar correo', 'confirma tu correo',
                                'confirm your email', 'finish creating your account',
                                'terminar de crear', '6-digit', '6 digit', '6-dígitos',
                                '6 dígitos', '6 digitos', 'resend code', 'reenviar código',
                                'reenviar codigo', 'email sent', 'correo enviado',
                                'check your inbox', 'revisa tu bandeja', 'we sent',
                                'te hemos enviado', "we've sent", 'código de 6', 'codigo de 6'
                            ];
                            if (frases.some(f => t.includes(f))) return true;
                            const n = document.querySelectorAll(
                                'input[maxlength="1"], input[autocomplete="one-time-code"]'
                            ).length;
                            return n >= 4;
                        }"""))
                    except Exception:
                        try:
                            return bool(encontrar_locator_en_frames(
                                self.page,
                                ['input[maxlength="1"]', 'input[autocomplete="one-time-code"]']
                            ))
                        except Exception:
                            return False

                def _sigue_en_formulario_registro() -> bool:
                    try:
                        return bool(encontrar_locator_en_frames(
                            self.page,
                            [
                                "button:has-text('Suscríbete')", "button:has-text('Subscribe')",
                                'select[name*="day" i]', 'select[name*="year" i]',
                                'input[name*="day" i]',
                            ]
                        ))
                    except Exception:
                        return False

                def _pulsar_suscribete() -> None:
                    print("  [Registro] Pulsando botón 'Suscríbete'...")
                    # Reafirmar términos (a veces el clic previo no quedó registrado)
                    try:
                        self.page.evaluate("""() => {
                            document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                                const parentText = cb.parentElement ? (cb.parentElement.textContent || '') : '';
                                if (/términos|terms|privacidad|privacy|acuerdo|agree/i.test(parentText)) {
                                    if (!cb.checked) {
                                        cb.click();
                                        if (!cb.checked && cb.parentElement) cb.parentElement.click();
                                    }
                                }
                            });
                        }""")
                    except Exception:
                        pass
                    btn_sub = esperar_locator_en_frames(
                        self.page,
                        [
                            "button:has-text('Suscríbete')", "button:has-text('Subscribe')",
                            "button:has-text('Create account')", "button:has-text('Crear cuenta')",
                            "button[type='submit']",
                        ],
                        timeout_s=5.0
                    )
                    if btn_sub:
                        try:
                            btn_sub.click(force=True)
                        except Exception:
                            try:
                                btn_sub.evaluate("b => b.click()")
                            except Exception:
                                pass
                    else:
                        try:
                            self.page.evaluate("""() => {
                                const btn = document.querySelector('button[type="submit"]') ||
                                    Array.from(document.querySelectorAll('button')).find(b => {
                                        const t = (b.textContent || '').toLowerCase();
                                        return t.includes('suscríbete') || t.includes('suscribete')
                                            || t.includes('subscribe') || t.includes('crear cuenta')
                                            || t.includes('create account');
                                    });
                                if (btn) { btn.disabled = false; btn.removeAttribute('disabled'); btn.click(); }
                            }""")
                        except Exception:
                            pass

                def _asegurar_otp_tras_suscribirse() -> bool:
                    """Tras Suscríbete: esperar OTP, recuperar authorize/antirobot o reintentar clic.

                    Antes se abortaba a los ~8s sin OTP aunque el proxy solo fuera lento o el
                    botón no hubiera registrado el clic → fallos falsos (g.etspooky381.9).
                    """
                    _pulsar_suscribete()
                    time.sleep(2.0)

                    for intento_rec in range(1, 6):
                        # Espera activa a la pantalla OTP
                        for _ in range(12):
                            if _pantalla_otp_registro():
                                return True
                            # Si ya llegó el correo, la UI puede ir retrasada: no abortar aún
                            time.sleep(1.0)

                        if _pantalla_otp_registro():
                            return True

                        # Diagnóstico de estado actual
                        try:
                            url_now = (self.page.url or "")[:120]
                            txt_snip = self.page.evaluate(
                                "() => (document.body && document.body.innerText || '').slice(0, 180)"
                            )
                        except Exception:
                            url_now, txt_snip = "?", ""
                        print(f"  [Registro] [{self.client_email}] Aún sin OTP tras Suscríbete "
                              f"(intento recuperación {intento_rec}/5). URL={url_now}")
                        if txt_snip:
                            print(f"  [Registro] [{self.client_email}] Texto visible: "
                                  f"{(txt_snip or '').replace(chr(10), ' ')[:120]!r}")

                        # Cuenta ya existente → no hace falta OTP de registro
                        try:
                            if encontrar_locator_en_frames(
                                self.page,
                                ['input[type="password"]', 'input[name="password"]']
                            ) and not _sigue_en_formulario_registro():
                                print(f"  {Color.WARNING}[Registro] [{self.client_email}] Apareció "
                                      f"login por contraseña (cuenta ya existente).{Color.ENDC}")
                                self._registro_cuenta_existente = True
                                return False
                        except Exception:
                            pass

                        # Antibot / Error authorize
                        try:
                            if detectar_pantalla_antirobot(self.page):
                                print(f"  [Registro] [{self.client_email}] Antibot tras Suscríbete; "
                                      f"intervención/rotación...")
                                manejar_bloqueos_e_intervencion(self.page, "Registro tras Suscríbete")
                        except Exception:
                            pass
                        try:
                            if es_pantalla_error_login_tidal(self.page) or url_es_oauth_login_roto(
                                getattr(self.page, "url", "") or ""
                            ):
                                print(f"  [Registro] [{self.client_email}] Authorize/Error tras "
                                      f"Suscríbete; recuperando flujo...")
                                if self.use_proxy:
                                    self.ejecutar_rotacion_proxy_y_recargar()
                                else:
                                    self.recuperar_login_tras_error_tidal()
                                # Tras recuperar estamos en login: hay que rehacer el formulario.
                                # Señalamos al caller para reiniciar run_registration externo no;
                                # aquí reintentamos email→fecha→suscribir en este mismo lock.
                                raise RuntimeError("__REINICIAR_FORMULARIO_REGISTRO__")
                        except RuntimeError:
                            raise
                        except Exception:
                            pass

                        # ¿Correo de sign-up ya en IMAP aunque la UI no cambió?
                        try:
                            codigo_previo = obtener_codigo_via_imap(
                                gmail_user=self.client_email,
                                required_keywords=[
                                    "registr", "bienven", "código", "codigo", "code",
                                    "verific", "sign-up", "signup", "sign up",
                                ],
                                query_exclude="cancel",
                                after_email_id=max_id_previo,
                                max_age_minutes=20,
                            )
                            if codigo_previo and not str(codigo_previo).startswith("http"):
                                print(f"  [Registro] [{self.client_email}] OTP ya llegó por IMAP "
                                      f"({codigo_previo}) aunque la UI no mostró Verify. Se continúa.")
                                # Forzar navegación a una URL de verificación no siempre existe;
                                # devolver True y dejar que el bucle IMAP/escritura lo use.
                                # Guardamos el código en atributo temporal.
                                self._otp_registro_prefetch = codigo_previo
                                return True
                        except Exception:
                            pass

                        # Seguir en el formulario: volver a pulsar Suscríbete
                        if _sigue_en_formulario_registro():
                            print(f"  [Registro] [{self.client_email}] Sigue el formulario; "
                                  f"reintentando Suscríbete...")
                            _pulsar_suscribete()
                            time.sleep(2.5)
                            continue

                        # Página rara: rotar proxy NG y recuperar pricing→account, luego abortar
                        # este helper con reinicio de formulario.
                        if self.use_proxy and intento_rec >= 3:
                            print(f"  [Registro] [{self.client_email}] Rotando proxy NG y "
                                  f"reiniciando formulario de registro...")
                            try:
                                self.ejecutar_rotacion_proxy_y_recargar()
                            except Exception:
                                pass
                            raise RuntimeError("__REINICIAR_FORMULARIO_REGISTRO__")

                        time.sleep(1.5)

                    return _pantalla_otp_registro()

                # Hasta 2 reinicios completos del formulario si authorize/proxy lo tumba
                self._otp_registro_prefetch = None
                self._registro_cuenta_existente = False
                otp_listo = False
                for _ciclo_form in range(1, 3):
                    try:
                        ok_otp = _asegurar_otp_tras_suscribirse()
                        if ok_otp:
                            otp_listo = True
                            break
                        if getattr(self, "_registro_cuenta_existente", False):
                            break
                        # False genérico: seguir al siguiente ciclo o fallar al salir
                    except RuntimeError as e_rein:
                        if "__REINICIAR_FORMULARIO_REGISTRO__" not in str(e_rein):
                            raise
                        print(f"  [Registro] [{self.client_email}] Reiniciando formulario "
                              f"(ciclo {_ciclo_form}/2) tras fallo post-Suscríbete...")
                        # Rehacer email + fecha + términos (mismo patrón que arriba)
                        email_input = esperar_locator_en_frames(
                            self.page,
                            ['input[type="email"]', 'input[name="email"]', '#email'],
                            timeout_s=20.0
                        )
                        if not email_input:
                            try:
                                self.recuperar_login_tras_error_tidal()
                            except Exception:
                                pass
                            email_input = esperar_locator_en_frames(
                                self.page,
                                ['input[type="email"]', 'input[name="email"]', '#email'],
                                timeout_s=20.0
                            )
                        if not email_input:
                            continue
                        try:
                            email_input.fill("")
                            email_input.fill(self.client_email)
                            email_input.press("Enter")
                        except Exception:
                            pass
                        time.sleep(1.5)
                        if encontrar_locator_en_frames(
                            self.page, ['input[type="password"]', 'input[name="password"]']
                        ):
                            self._registro_cuenta_existente = True
                            break
                        # Fecha + términos (JS compacto)
                        try:
                            self.page.evaluate("""() => {
                                const selects = document.querySelectorAll('select');
                                if (selects.length >= 3) {
                                    selects[0].value = '15';
                                    selects[0].dispatchEvent(new Event('change', {bubbles:true}));
                                    if (selects[1].options.length > 8) {
                                        selects[1].selectedIndex = selects[1].options.length === 13 ? 8 : 7;
                                        selects[1].dispatchEvent(new Event('change', {bubbles:true}));
                                    }
                                    selects[2].value = '1995';
                                    selects[2].dispatchEvent(new Event('change', {bubbles:true}));
                                }
                                document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                                    const p = cb.parentElement ? (cb.parentElement.textContent||'') : '';
                                    if (/términos|terms|privacidad|privacy/i.test(p) && !cb.checked) cb.click();
                                });
                            }""")
                        except Exception:
                            pass
                        time.sleep(0.8)
                        max_id_previo = obtener_max_email_id(self.client_email)

                if getattr(self, "_registro_cuenta_existente", False):
                    print(f"  {Color.WARNING}[Registro] La cuenta {self.client_email} ya está "
                          f"registrada en TIDAL. Omitiendo...{Color.ENDC}")
                    registro_exitoso = True
                    return True

                if not otp_listo and not _pantalla_otp_registro() and not getattr(self, "_otp_registro_prefetch", None):
                    raise RuntimeError(
                        f"Tras Suscríbete no apareció la verificación OTP para {self.client_email} "
                        f"tras varios reintentos/recuperaciones (proxy/authorize)."
                    )

                codigo_aceptado = False
                ultimo_error_codigo = ""
                for ronda in range(1, 4):
                    print(f"  [Registro] Buscando código de registro vía IMAP (ronda {ronda}/3)...")
                    codigo = getattr(self, "_otp_registro_prefetch", None)
                    self._otp_registro_prefetch = None
                    if not codigo:
                        for intento in range(1, 11):
                            print(f"  [Registro] Intento {intento}/10: Buscando correo...")
                            codigo = obtener_codigo_via_imap(
                                gmail_user=self.client_email,
                                required_keywords=[
                                    "registr", "bienven", "código", "codigo", "code",
                                    "verific", "sign-up", "signup", "sign up",
                                ],
                                query_exclude="cancel",
                                after_email_id=max_id_previo,
                                max_age_minutes=20,
                            )
                            if codigo:
                                break
                            # A mitad de intentos: Resend por si Tidal no envió el primer correo
                            if intento in (4, 7) and _pantalla_otp_registro():
                                try:
                                    btn_resend = esperar_locator_en_frames(
                                        self.page,
                                        [
                                            "button:has-text('Resend code')", "button:has-text('Resend')",
                                            "button:has-text('Reenviar código')", "button:has-text('Reenviar')",
                                            "a:has-text('Resend')", "a:has-text('Reenviar')",
                                        ],
                                        timeout_s=2.0,
                                    )
                                    if btn_resend:
                                        print(f"  [Registro] [{self.client_email}] Pulsando Resend code...")
                                        btn_resend.click(force=True)
                                        time.sleep(2.0)
                                        max_id_previo = obtener_max_email_id(self.client_email)
                                except Exception:
                                    pass
                            if intento < 10:
                                print("  [Registro] Correo no encontrado aún. Esperando 5 segundos...")
                                time.sleep(5.0)

                    if not codigo:
                        ultimo_error_codigo = (
                            "No se pudo extraer el código de verificación del correo de manera automática."
                        )
                        break

                    if codigo.startswith("http"):
                        reg_page = self.context.new_page()
                        reg_page.goto(codigo)
                        time.sleep(2.0)
                        reg_page.close()
                        codigo_aceptado = True
                        break

                    esperar_locator_en_frames(
                        self.page,
                        [
                            'input[autocomplete="one-time-code"]',
                            'input[maxlength="1"]',
                            'input[name="code"]',
                            'input[inputmode="numeric"]',
                            'input[type="text"]',
                        ],
                        timeout_s=12.0,
                    )
                    time.sleep(0.4)

                    print(f"  [Registro] [{self.client_email}] Escribiendo código OTP ({codigo})...")
                    if not escribir_codigo_verificacion_inteligente(self.page, codigo):
                        print(f"  [Registro] {Color.WARNING}[WARN] No se pudo rellenar las cajas OTP; "
                              f"reintentando...{Color.ENDC}")
                        ultimo_error_codigo = "No se pudieron rellenar las cajas del código OTP."
                        time.sleep(1.0)
                        continue

                    time.sleep(0.5)
                    try:
                        self.page.keyboard.press("Enter")
                        time.sleep(0.8)
                    except Exception:
                        pass

                    btn_confirm = esperar_locator_en_frames(
                        self.page,
                        [
                            "button[type='submit']",
                            "button:has-text('Continuar')", "button:has-text('Continue')",
                            "button:has-text('Confirmar')", "button:has-text('Confirm')",
                            "button:has-text('Verificar')", "button:has-text('Verify')"
                        ],
                        timeout_s=3.0
                    )
                    if btn_confirm:
                        try:
                            btn_confirm.click(force=True)
                        except Exception:
                            pass
                    else:
                        try:
                            self.page.evaluate("""() => {
                                const btn = document.querySelector('button[type="submit"]') ||
                                            Array.from(document.querySelectorAll('button')).find(b => {
                                                const t = (b.textContent || '').trim().toLowerCase();
                                                return t.includes('continuar') || t.includes('continue')
                                                    || t.includes('confirm') || t.includes('verificar')
                                                    || t.includes('verify');
                                            });
                                if (btn) btn.click();
                            }""")
                        except Exception:
                            pass

                    time.sleep(2.5)
                    try:
                        texto_err = self.page.evaluate(
                            "() => document.body ? document.body.innerText.toLowerCase() : ''"
                        )
                    except Exception:
                        texto_err = ""

                    sigue_en_otp = _pantalla_otp_registro()
                    if _texto_indica_codigo_invalido(texto_err) and sigue_en_otp:
                        print(f"  [Registro] {Color.WARNING}[WARN] [{self.client_email}] Tidal rechazó "
                              f"el código {codigo}. Solicitando uno nuevo...{Color.ENDC}")
                        ultimo_error_codigo = (
                            f"Tidal rechazó el código de verificación para {self.client_email}."
                        )
                        try:
                            btn_resend = esperar_locator_en_frames(
                                self.page,
                                [
                                    "button:has-text('Resend code')", "button:has-text('Resend')",
                                    "button:has-text('Reenviar código')", "button:has-text('Reenviar')",
                                    "a:has-text('Resend')", "a:has-text('Reenviar')",
                                ],
                                timeout_s=3.0,
                            )
                            if btn_resend:
                                btn_resend.click(force=True)
                                time.sleep(2.0)
                        except Exception:
                            pass
                        max_id_previo = obtener_max_email_id(self.client_email)
                        continue

                    codigo_aceptado = True
                    print("  [Registro] Código enviado. Esperando procesamiento de la cuenta...")
                    break

                if not codigo_aceptado:
                    raise RuntimeError(
                        ultimo_error_codigo
                        or f"No se pudo verificar el correo de registro para {self.client_email}."
                    )
            
            print("  [Registro] Esperando redirección automática al perfil o cuenta...")
            registro_exitoso = self._confirmar_registro_completado(timeout_s=60.0)

            if registro_exitoso:
                print(f"  {Color.GREEN}[OK] ¡Registro completado y verificado con éxito para {self.client_email}! Cerrando ventana automáticamente...{Color.ENDC}")
                try:
                    cookies_proxy = self.context.cookies()
                    self.cookies_tidal = [c for c in cookies_proxy if "tidal.com" in c.get("domain", "")]
                except Exception as ce:
                    self.cookies_tidal = []
                    print(f"  [Registro] [WARN] Error al extraer cookies de Tidal: {ce}")
                return True
            else:
                print(f"  {Color.FAIL}[Registro] [ERROR] No se logró verificar la redirección de cuenta para {self.client_email}.{Color.ENDC}")
                return False
                
        except Exception as e:
            print(f"  {Color.FAIL}[ERROR] Falló el registro para {self.client_email}: {e}{Color.ENDC}")
            return False
        finally:
            if cerrar_navegador_al_final:
                # Tras un registro OK la opción 8 reutiliza el PE en TuneMyMusic: no liberarlo aquí.
                # El NG sí se libera siempre; la fase Nigeria ya terminó.
                self.cerrar_navegador(liberar_ng=True, liberar_pe=not registro_exitoso)
            try:
                # Solo limpiar perfil temporal si NO fue exitoso y el navegador ya está cerrado:
                # borrarlo con Chrome abierto dejaba el perfil a medias y bloqueado en Windows.
                if cerrar_navegador_al_final and not registro_exitoso:
                    self.limpiar_perfil_temporal()
                    if not self.main_profile.exists():
                        print(f"  [Registro] Limpiado perfil temporal por fallo: {self.main_profile}")
            except Exception as ex:
                pass

    def limpiar_perfil_temporal(self) -> None:
        # Windows tarda un instante en liberar los ficheros del perfil tras cerrar Chrome
        for intento in range(3):
            try:
                if not self.main_profile.exists():
                    return
                shutil.rmtree(self.main_profile, ignore_errors=(intento == 2))
                return
            except Exception:
                time.sleep(1.5)

    def _url_indica_cuenta_activa(self, url: str) -> bool:
        u = (url or "").lower()
        if not u or "login.tidal.com" in u or "/authorize" in u:
            return False
        if "/login/tidal/return" in u or "/login/tidal/callback" in u:
            return False
        if "account.tidal.com" in u and "/login" not in u:
            return True
        if "listen.tidal.com" in u or "tidal.com/browse" in u:
            return True
        return False

    def _confirmar_registro_completado(self, timeout_s: float = 45.0) -> bool:
        """Confirma que el registro dejó sesión real, no solo un flash de redirección.

        Antes bastaba con ver account.tidal.com durante 20s. Si Tidal se quedaba en
        /authorize, listen.tidal.com o en un puente OAuth, el registro se daba por fallido
        aunque la cuenta ya existiera, y la opción 14 abortaba antes del checkout.
        """
        last_url_printed = ""
        start = time.time()
        intentos_perfil = 0
        while time.time() - start < timeout_s:
            try:
                self.page = pagina_vigente(self.page)
                if not self.page or self.page.is_closed():
                    return False
                curr_url = (self.page.url or "").lower()
                if curr_url != last_url_printed:
                    print(f"  [Registro] URL actual: {curr_url}")
                    last_url_printed = curr_url

                if self._url_indica_cuenta_activa(curr_url):
                    try:
                        texto = self.page.evaluate(
                            "() => document.body ? document.body.innerText.toLowerCase() : ''"
                        )
                    except Exception:
                        texto = ""
                    if any(x in texto for x in (
                        "perfil", "profile", "suscripción", "subscription",
                        "cuenta", "account", "family", "familiar", "plan"
                    )):
                        return True

                elapsed = time.time() - start
                # En /authorize a menudo basta con pulsar Continuar/Permitir del consentimiento OAuth
                if "/authorize" in curr_url:
                    try:
                        btn_auth = encontrar_locator_en_frames(
                            self.page,
                            [
                                "button:has-text('Continuar')", "button:has-text('Continue')",
                                "button:has-text('Permitir')", "button:has-text('Allow')",
                                "button:has-text('Aceptar')", "button:has-text('Accept')",
                                "button[type='submit']"
                            ],
                            text_regex=re.compile(
                                r"^(continuar|continue|permitir|allow|aceptar|accept|sí,\s*continuar|yes,\s*continue)$",
                                re.I
                            )
                        )
                        if btn_auth:
                            try:
                                btn_auth.click(timeout=2500, force=True)
                            except Exception:
                                try:
                                    btn_auth.evaluate("b => b.click()")
                                except Exception:
                                    pass
                            time.sleep(2.0)
                            continue
                    except Exception:
                        pass

                # Tras el código, Tidal a menudo se queda en /authorize o en tidal.com:
                # forzar la entrada al perfil varias veces en vez de esperar pasivamente.
                if elapsed > 2.0 and intentos_perfil < 6 and (
                    "/authorize" in curr_url
                    or "login.tidal.com" in curr_url
                    or curr_url.rstrip("/").endswith("tidal.com")
                    or "listen.tidal.com" in curr_url
                    or not self._url_indica_cuenta_activa(curr_url)
                ):
                    intentos_perfil += 1
                    print(f"  [Registro] [{self.client_email}] Forzando verificación en /profile "
                          f"(intento {intentos_perfil}/6)...")
                    try:
                        self.page.goto(
                            "https://account.tidal.com/profile",
                            wait_until="domcontentloaded",
                            timeout=15000
                        )
                        time.sleep(1.5)
                        aceptar_cookies_con_espera(self.page, intentos=1, pausa_s=0.2)
                        u2 = (self.page.url or "").lower()
                        if self._url_indica_cuenta_activa(u2) and "login.tidal.com" not in u2:
                            # Si hay formulario de login visible, la sesión no quedó
                            hay_login = False
                            try:
                                hay_login = bool(encontrar_locator_en_frames(
                                    self.page,
                                    ['input[type="email"]', 'input[name="email"]', '#email']
                                ))
                            except Exception:
                                pass
                            if not hay_login:
                                return True
                    except Exception as e_nav:
                        print(f"  [Registro] [{self.client_email}] [WARN] No se pudo abrir /profile: {e_nav}")
            except Exception:
                pass
            time.sleep(0.6)
        return False

    def run_register_and_upgrade_family(self) -> bool:
        reg_ok = self.run_registration(cerrar_navegador_al_final=False)
        if not reg_ok or not self.page or self.page.is_closed():
            print(f"  {Color.FAIL}[Familiar Auto] [{self.client_email}] Falló el registro inicial de la cuenta.{Color.ENDC}")
            self.cerrar_navegador()
            self.limpiar_perfil_temporal()
            return False

        # Anotar la contraseña en cuanto la cuenta existe: si el checkout o el upgrade fallan,
        # la cuenta ya está creada y sin esta línea quedaría inaccesible para las demás opciones.
        if guardar_credencial_cuenta(self.client_email, self.client_pwd):
            print(f"  [Cuentas] [{self.client_email}] Contraseña anotada en sesiones_imap_cuentas.txt.")

        try:
            # Registro completado con proxy NG. Cambiar a proxy de Perú para el checkout y upgrade.
            # Va dentro del try: si no hay proxy PE lanza RuntimeError y el finally cierra Chrome.
            self.cambiar_a_proxy_peru()

            # 1. Ir al enlace de la campaña de oferta, con reintentos y control de antirobot
            campaign_url = "https://offer.tidal.com/campaigns/63866cb3590780ce08a84457/products?geo=NG&campaignId=63866cb3590780ce08a84457"
            print(f"\n  [Campaña NG] [{self.client_email}] Cargando oferta de prueba: {campaign_url[:65]}...")
            if not self.abrir_campana_oferta(campaign_url):
                print(f"  {Color.FAIL}[Campaña NG] [{self.client_email}] No se pudo cargar la oferta de campaña.{Color.ENDC}")
                return False

            # 2. Presionar el botón "Continuar" de la oferta
            if not self.pulsar_continuar_campana():
                print(f"  {Color.WARNING}[Campaña NG] [{self.client_email}] No se encontró el botón 'Continuar'; "
                      f"continúa manualmente en la ventana de Chrome si es necesario.{Color.ENDC}")

            # 3. Esperar que el usuario ingrese los datos de pago en Checkout.
            # Margen amplio: con un lote de 10 ventanas el operador rellena los pagos uno a uno y
            # con 10 minutos la primera ventana caducaba antes de llegar su turno.
            if not self.esperar_confirmacion_pago(timeout_s=1500.0):
                print(f"  {Color.FAIL}[Checkout] [{self.client_email}] Tiempo de espera de pago agotado o ventana cerrada.{Color.ENDC}")
                return False

            print(f"  {Color.GREEN}[Checkout] [{self.client_email}] ¡Pago completado! Confirmación de activación detectada.{Color.ENDC}")
            # Tras el cobro Tidal tarda unos segundos en activar la suscripción; si se abre
            # /subscription/edit demasiado pronto el cambio a Family no aparece o no confirma.
            print(f"  [Plan Family] [{self.client_email}] Esperando a que la suscripción quede activa...")
            time.sleep(5.0)
            if not self._asegurar_sesion_cuenta_tras_pago():
                print(f"  {Color.FAIL}[Plan Family] [{self.client_email}] No hay sesión activa en account.tidal.com tras el pago.{Color.ENDC}")
                return False

            # 4. Cambiar el plan a Familiar y confirmar
            upgrade_ok = self.cambiar_plan_a_familiar()

            if not upgrade_ok:
                print(f"  {Color.FAIL}[Plan Family] [{self.client_email}] El plan no quedó como FAMILY. "
                      f"Revisa la cuenta antes de usarla como titular.{Color.ENDC}")
                return False

            print(f"\n  {Color.GREEN}{Color.BOLD}============================================================")
            print(f"  [OK] ¡PROCESO COMPLETADO! TITULAR FAMILIAR CREADO Y ACTUALIZADO:")
            print(f"  Correo: {self.client_email}")
            print(f"  Contraseña: {self.client_pwd}")
            print(f"  ============================================================{Color.ENDC}\n")

            self.registrar_titular_familiar()
            return True

        except Exception as ex_f:
            print(f"  {Color.FAIL}[ERROR] Excepción en proceso de cuenta familiar para {self.client_email}: {ex_f}{Color.ENDC}")
            return False
        finally:
            self.cerrar_navegador()
            # El perfil temporal ya no se necesita: sin esto cada cuenta dejaba cientos de MB en %TEMP%
            self.limpiar_perfil_temporal()

    def abrir_campana_oferta(self, campaign_url: str, intentos: int = 3) -> bool:
        """Carga la campaña de oferta NG tolerando cortes de proxy y pantallas de antirobot."""
        for intento in range(1, max(1, intentos) + 1):
            try:
                self.page = pagina_vigente(self.page)
                self.page.goto(campaign_url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(2.5)
                aceptar_cookies_con_espera(self.page)
                # Rota el proxy PE y recarga si DataDome/Cloudflare interceptan la oferta
                manejar_bloqueos_e_intervencion(self.page, f"Campaña NG ({self.client_email})")
                self.page = pagina_vigente(self.page)
                if "offer.tidal.com" in (self.page.url or "").lower():
                    return True
                # Redirección inesperada (p. ej. de vuelta a tidal.com): reintentar
                print(f"  [Campaña NG] [{self.client_email}] [WARN] Redirigido a {self.page.url[:70]}; reintentando...")
            except Exception as e:
                print(f"  [Campaña NG] [{self.client_email}] [WARN] Intento {intento}/{intentos} falló: {e}")
            if intento < intentos:
                time.sleep(random.uniform(2.0, 4.0))
        return False

    def pulsar_continuar_campana(self) -> bool:
        """Pulsa el 'Continuar' de la oferta anclando el texto exacto.

        'has-text' sin ancla también coincide con 'Continuar con Google', y el fallback anterior
        hacía clic en el primer <button> del DOM (normalmente el de cookies o el de navegación).
        """
        print(f"  [Campaña NG] [{self.client_email}] Presionando botón 'Continuar'...")
        self.page = pagina_vigente(self.page)
        btn_cont = esperar_locator_en_frames(
            self.page,
            [
                'button:text-is("Continuar")', 'button:text-is("Continue")',
                'button:text-is("Comenzar")', 'button:text-is("Get started")',
                'a:text-is("Continuar")', 'a:text-is("Continue")'
            ],
            text_regex=re.compile(r"^(continuar|continue|comenzar|get started|empezar)$", re.I),
            timeout_s=15.0
        )
        if not btn_cont:
            return False
        try:
            btn_cont.click(timeout=5000)
        except Exception:
            try:
                btn_cont.evaluate("b => b.click()")
            except Exception:
                return False
        time.sleep(3.0)
        return True

    def esperar_confirmacion_pago(self, timeout_s: float = 1500.0) -> bool:
        """Espera la confirmación real de activación tras el pago manual.

        Antes bastaba con encontrar 'welcome'/'bienvenido' en el DOM, palabras que ya aparecen en
        la propia página de la oferta y en el checkout: el flujo daba el pago por hecho al instante
        y seguía al upgrade sin suscripción activa.
        """
        print(f"\n  {Color.CYAN}{Color.BOLD}[Checkout] [{self.client_email}] 💳 Por favor completa los datos de pago en la ventana de Chrome...{Color.ENDC}")
        print(f"  {Color.CYAN}[Checkout] [{self.client_email}] Esperando la confirmación de activación de la cuenta...{Color.ENDC}")

        frases_activacion = [
            "tu cuenta ya está activada", "tu cuenta ya esta activada",
            "your account is now active", "your account is active",
            "you're all set", "youre all set", "ya está todo listo", "ya esta todo listo",
            "suscripción activada", "suscripcion activada", "subscription is active",
            "gracias por tu compra", "thank you for your purchase",
            "pago confirmado", "payment confirmed",
        ]
        aviso_bloqueo = False
        aviso_pasarela = False
        start_wait = time.time()
        while time.time() - start_wait < timeout_s:
            if self.pagos_bloqueados and not aviso_pasarela:
                aviso_pasarela = True
                hosts = ", ".join(sorted(self.pagos_bloqueados))
                print(f"  {Color.FAIL}[Checkout] [{self.client_email}] El navegador no pudo cargar la pasarela de pago "
                      f"({hosts}). Los campos de tarjeta quedarán vacíos y no se podrán rellenar.{Color.ENDC}")
                print(f"  {Color.WARNING}[Checkout] [{self.client_email}] Causa habitual: el proveedor de proxies "
                      f"deniega los dominios de pago. Añade esos hosts en "
                      f"'{RUTA_DOMINIOS_SIN_PROXY.name}' (uno por línea) y reintenta.{Color.ENDC}")
            try:
                # El checkout puede abrirse en otra pestaña: revisar todas las del contexto en vez
                # de solo self.page, que si no se quedaría mirando la página de la oferta.
                paginas = []
                try:
                    if self.context:
                        paginas = [p for p in self.context.pages if not p.is_closed()]
                except Exception:
                    paginas = []
                if not paginas:
                    if self.page and not self.page.is_closed():
                        paginas = [self.page]
                    else:
                        print(f"  {Color.FAIL}[Checkout] [{self.client_email}] La ventana se cerró antes de confirmar el pago.{Color.ENDC}")
                        return False

                for pg in paginas:
                    curr_url = (pg.url or "").lower()
                    if "/success" in curr_url or "/thank" in curr_url or "/confirmation" in curr_url:
                        self.page = pg
                        return True
                    # La página de la oferta no se examina por texto: su copy promocional puede
                    # contener las mismas palabras que la confirmación de pago.
                    if "/products" in curr_url or "/campaigns" in curr_url:
                        continue
                    text_dom = pg.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''")
                    if any(f in text_dom for f in frases_activacion):
                        self.page = pg
                        return True

                # No se rota proxy aquí: cambiar de IP a mitad del checkout invalida la sesión de
                # pago. Solo se avisa una vez para que el operador lo resuelva en pantalla.
                if not aviso_bloqueo and detectar_pantalla_antirobot(paginas[-1]):
                    aviso_bloqueo = True
                    print(f"  {Color.WARNING}[Checkout] [{self.client_email}] Se detectó una verificación antirobot "
                          f"en el checkout. Resuélvela en la ventana de Chrome para continuar.{Color.ENDC}")
            except Exception:
                pass
            time.sleep(2.0)
        return False

    def _asegurar_sesion_cuenta_tras_pago(self) -> bool:
        """Tras el checkout, garantiza que account.tidal.com sigue con sesión usable."""
        for intento in range(1, 4):
            try:
                self.page = pagina_vigente(self.page)
                # Preferir la pestaña que ya esté en account/offer; si no, usar la vigente
                try:
                    if self.context:
                        for pg in self.context.pages:
                            if pg.is_closed():
                                continue
                            u = (pg.url or "").lower()
                            if "account.tidal.com" in u or "offer.tidal.com" in u:
                                self.page = pg
                                break
                except Exception:
                    pass
                self.page.goto(
                    "https://account.tidal.com/subscription",
                    wait_until="domcontentloaded",
                    timeout=30000
                )
                time.sleep(2.0)
                aceptar_cookies_con_espera(self.page, intentos=1, pausa_s=0.2)
                manejar_bloqueos_e_intervencion(self.page, f"Sesión post-pago ({self.client_email})")
                self.page = pagina_vigente(self.page)
                url = (self.page.url or "").lower()
                if "login.tidal.com" in url or "/authorize" in url:
                    print(f"  [Plan Family] [{self.client_email}] [WARN] Sesión perdida tras el pago "
                          f"(intento {intento}/3). Reintentando abrir suscripción...")
                    time.sleep(2.0)
                    continue
                if "account.tidal.com" in url:
                    return True
            except Exception as e:
                print(f"  [Plan Family] [{self.client_email}] [WARN] No se pudo abrir suscripción: {e}")
            time.sleep(2.0)
        return False

    def _seleccionar_tarjeta_family(self) -> bool:
        """Marca la tarjeta/radio del plan Family en la página de cambio de plan."""
        self.page = pagina_vigente(self.page)
        try:
            pulsado = self.page.evaluate("""() => {
                const needles = [
                    'hasta 6 familiares', 'hasta seis miembros', 'up to 6', 'up to six',
                    'family plan', 'plan familiar', 'familiar', 'family'
                ];
                const skip = ['individual', 'estudiante', 'student', 'hi-fi plus', 'hifi plus'];
                const nodes = Array.from(document.querySelectorAll(
                    'button, label, [role="radio"], [role="option"], [role="button"], div, span, a, li'
                ));
                for (const el of nodes) {
                    const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                    if (!txt || txt.length > 180) continue;
                    if (skip.some(s => txt.includes(s)) && !txt.includes('family') && !txt.includes('familiar')) {
                        continue;
                    }
                    if (!needles.some(n => txt.includes(n))) continue;
                    // Preferir el contenedor clicable más cercano
                    const clickable = el.closest('button, label, [role="radio"], [role="option"], a, li') || el;
                    try { clickable.scrollIntoView({block: 'center'}); } catch(e) {}
                    try { clickable.click(); return true; } catch(e) {}
                }
                // Fallback: radio/input asociado a Family
                const inputs = Array.from(document.querySelectorAll('input[type="radio"], input[type="checkbox"]'));
                for (const inp of inputs) {
                    const val = ((inp.value || '') + ' ' + (inp.id || '') + ' ' + (inp.name || '')).toLowerCase();
                    const lab = inp.labels && inp.labels[0]
                        ? (inp.labels[0].innerText || '').toLowerCase()
                        : '';
                    if (val.includes('family') || lab.includes('family') || lab.includes('familiar')) {
                        try { inp.click(); return true; } catch(e) {}
                    }
                }
                return false;
            }""")
            if pulsado:
                print(f"  [Plan Family] [{self.client_email}] Tarjeta/opción Family seleccionada.")
                time.sleep(1.2)
                return True
        except Exception as e:
            print(f"  [Plan Family] [{self.client_email}] [WARN] Selección JS de Family: {e}")

        # Fallback Playwright por texto visible
        for texto in (
            "Hasta 6 familiares", "Hasta seis miembros", "Up to 6", "Family",
            "Familiar", "Upgrade to Family", "Actualizar a Family", "Plan familiar"
        ):
            try:
                loc = self.page.get_by_text(texto, exact=False).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(force=True, timeout=4000)
                    print(f"  [Plan Family] [{self.client_email}] Seleccionado por texto: '{texto}'.")
                    time.sleep(1.2)
                    return True
            except Exception:
                continue
        print(f"  [Plan Family] [{self.client_email}] [WARN] No se encontró la tarjeta Family "
              f"(puede venir preseleccionada por la URL).")
        return False

    def _pulsar_confirmar_cambio_plan(self) -> int:
        """Pulsa los botones de confirmación/upgrade del cambio de plan. Devuelve cuántos clics hizo.

        El fallo típico de la opción 14 era no encontrar el botón: text-is('Confirmar') exige
        coincidencia exacta y Tidal suele poner 'Confirmar cambio', 'Cambiar plan', 'Upgrade', etc.
        Sin ese clic, /family redirige a /profile porque el plan sigue siendo individual.
        """
        clics = 0
        patrones = re.compile(
            r"(confirmar(\s+cambio)?|confirm(\s+change)?|cambiar(\s+plan)?|change(\s+plan)?|"
            r"upgrade|actualizar|suscrib|subscribe|continuar|continue|aplicar|apply|guardar|save)",
            re.I
        )
        selectores = [
            'button:has-text("Confirmar")', 'button:has-text("Confirm")',
            'button:has-text("Cambiar")', 'button:has-text("Change")',
            'button:has-text("Upgrade")', 'button:has-text("Actualizar")',
            'button:has-text("Suscribir")', 'button:has-text("Subscribe")',
            'button:has-text("Continuar")', 'button:has-text("Continue")',
            'button[type="submit"]',
            'a:has-text("Confirmar")', 'a:has-text("Upgrade")', 'a:has-text("Cambiar")',
        ]
        for num_click in range(1, 4):
            self.page = pagina_vigente(self.page)
            try:
                self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            time.sleep(0.6)

            btn = esperar_locator_en_frames(
                self.page,
                selectores,
                text_regex=patrones,
                timeout_s=8.0
            )
            etiqueta = ""
            if btn:
                try:
                    etiqueta = (btn.inner_text(timeout=1000) or "").strip()
                except Exception:
                    etiqueta = ""
                # has-text('Continuar') también pilla 'Continuar con Google'; hay que descartarlo
                if re.search(r"(google|apple|facebook|cookie|rechazar|reject|cancel|atrás|back)", etiqueta, re.I):
                    btn = None
                    etiqueta = ""

            if not btn:
                # Último recurso: JS busca cualquier CTA visible de confirmación/upgrade
                try:
                    js_ok = self.page.evaluate("""() => {
                        const re = /(confirmar|confirm|cambiar|change|upgrade|actualizar|suscrib|subscribe|continuar|continue|aplicar|apply)/i;
                        const skip = /(cancel|atrás|back|google|apple|facebook|cookie|reject)/i;
                        const btns = Array.from(document.querySelectorAll('button, a[role="button"], [role="button"], input[type="submit"]'));
                        for (const b of btns) {
                            const t = (b.innerText || b.value || b.textContent || '').trim();
                            if (!t || t.length > 60) continue;
                            if (skip.test(t) || !re.test(t)) continue;
                            const style = window.getComputedStyle(b);
                            if (style.display === 'none' || style.visibility === 'hidden') continue;
                            if (b.disabled || b.getAttribute('aria-disabled') === 'true') {
                                b.removeAttribute('disabled');
                                b.disabled = false;
                            }
                            try { b.scrollIntoView({block:'center'}); b.click(); return t; } catch(e) {}
                        }
                        return '';
                    }""")
                except Exception:
                    js_ok = ""
                if not js_ok:
                    if clics == 0:
                        print(f"  [Plan Family] [{self.client_email}] [WARN] No apareció botón de "
                              f"confirmación/upgrade en el paso {num_click}.")
                    break
                print(f"  [Plan Family] [{self.client_email}] Presionando '{js_ok}' (clic {num_click}/3, vía JS)...")
                clics += 1
                time.sleep(2.8)
                continue

            print(f"  [Plan Family] [{self.client_email}] Presionando '{(etiqueta or 'Confirmar')[:40]}' (clic {num_click}/3)...")
            try:
                btn.click(timeout=5000, force=True)
            except Exception:
                try:
                    btn.evaluate("b => b.click()")
                except Exception:
                    pass
            clics += 1
            time.sleep(2.8)

            # Si tras el clic pide otro cobro/3DS, dejar margen al operador
            try:
                url_ahora = (self.page.url or "").lower()
                if any(k in url_ahora for k in ("checkout", "payment", "adyen", "stripe", "3ds", "secure")):
                    print(f"  {Color.CYAN}[Plan Family] [{self.client_email}] Se abrió un paso de pago "
                          f"extra para el upgrade. Complétalo en Chrome si aparece...{Color.ENDC}")
                    self.esperar_confirmacion_pago(timeout_s=300.0)
            except Exception:
                pass
        return clics

    def plan_familiar_confirmado(self) -> bool:
        """Confirma que la cuenta quedó realmente como titular de un plan Family.

        Prueba principal: /family accesible con gestión de miembros.
        Prueba secundaria: la página de suscripción declara el plan Family/Familiar.
        """
        try:
            self.page = pagina_vigente(self.page)
            self.page.goto("https://account.tidal.com/family", wait_until="domcontentloaded", timeout=30000)
            time.sleep(2.5)
            aceptar_cookies_con_espera(self.page, intentos=1, pausa_s=0.2)
            self.page = pagina_vigente(self.page)
            url = (self.page.url or "").lower()
            if "/family" in url:
                texto = self.page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''")
                senales_familia = [
                    "invitar", "invite", "miembros", "members", "familiar", "family",
                    "añadir miembro", "add member", "add a member", "invitar a un miembro"
                ]
                if any(s in texto for s in senales_familia):
                    return True
                # /family abrió pero aún sin texto de gestión: a veces carga en dos fases
                time.sleep(2.0)
                texto = self.page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''")
                if any(s in texto for s in senales_familia):
                    return True

            print(f"  [Plan Family] [{self.client_email}] [WARN] Tidal no permitió abrir el panel familiar ({url[:80]}).")

            # Secundaria: mirar el nombre del plan en /subscription
            self.page.goto("https://account.tidal.com/subscription", wait_until="domcontentloaded", timeout=25000)
            time.sleep(2.0)
            texto_sub = self.page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''")
            if any(s in texto_sub for s in (
                "plan familiar", "family plan", "tidal family", "familiar (",
                "family (", "hasta 6", "up to 6", "6 familiares", "6 members"
            )):
                print(f"  [Plan Family] [{self.client_email}] Suscripción declara plan Family aunque "
                      f"/family aún no abrió. Se acepta como confirmado.")
                return True
        except Exception as e:
            print(f"  [Plan Family] [{self.client_email}] [WARN] No se pudo verificar la suscripción: {e}")
        return False

    def cambiar_plan_a_familiar(self) -> bool:
        """Selecciona el plan Family, confirma el cambio y verifica el resultado en /family."""
        rutas = [
            "https://account.tidal.com/subscription/edit?select=FAMILY",
            "https://account.tidal.com/subscription/edit",
            "https://account.tidal.com/family",
        ]
        for intento in range(1, 4):
            try:
                print(f"\n  [Plan Family] [{self.client_email}] Redirigiendo a cambio de plan (intento {intento}/3)...")
                self.page = pagina_vigente(self.page)
                destino = rutas[(intento - 1) % len(rutas)]
                self.page.goto(destino, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2.5)
                aceptar_cookies_con_espera(self.page)
                manejar_bloqueos_e_intervencion(self.page, f"Plan Family ({self.client_email})")
                self.page = pagina_vigente(self.page)

                url = (self.page.url or "").lower()
                if "login.tidal.com" in url or "/authorize" in url:
                    print(f"  [Plan Family] [{self.client_email}] [WARN] Redirigido al login; reintentando sesión...")
                    if not self._asegurar_sesion_cuenta_tras_pago():
                        continue
                    self.page.goto(destino, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(2.0)

                # Si ya estamos en /family con gestión, el upgrade ya estaba hecho
                if "/family" in (self.page.url or "").lower():
                    if self.plan_familiar_confirmado():
                        return True
                    # En /family a veces hay CTA "Upgrade to Family" / "Actualizar a Family"
                    try:
                        for t in ("Upgrade to Family", "Actualizar a Family", "Upgrade",
                                  "Cambiar a Family", "Get Family"):
                            loc = self.page.get_by_text(t, exact=False).first
                            if loc.count() > 0 and loc.is_visible():
                                loc.click(force=True, timeout=4000)
                                print(f"  [Plan Family] [{self.client_email}] Pulsado CTA '{t}' en /family.")
                                time.sleep(2.0)
                                break
                    except Exception:
                        pass

                print(f"  [Plan Family] [{self.client_email}] Seleccionando plan 'Family hasta 6 familiares'...")
                self._seleccionar_tarjeta_family()

                clics = self._pulsar_confirmar_cambio_plan()
                if clics == 0:
                    print(f"  [Plan Family] [{self.client_email}] [WARN] No se confirmó el cambio "
                          f"(sin botón de confirmación). Reintentando...")
                    time.sleep(random.uniform(2.0, 3.5))
                    continue

                # Dar tiempo a que Tidal propague el cambio antes de mirar /family
                print(f"  [Plan Family] [{self.client_email}] Verificando actualización del plan...")
                time.sleep(4.0)
                if self.plan_familiar_confirmado():
                    return True
            except Exception as e_up:
                print(f"  [Plan Family] [{self.client_email}] [WARN] Intento {intento}/3 falló: {e_up}")
            time.sleep(random.uniform(2.0, 4.0))
        return False

    def registrar_titular_familiar(self) -> None:
        """Añade la cuenta al fichero de titulares usando la misma ruta que se leyó y con lock.

        Antes se calculaba una ruta distinta a la cargada y se escribía sin sincronizar, así que
        con varias ventanas terminando a la vez unos titulares sobrescribían a otros.
        """
        try:
            with TITULARES_FILE_LOCK:
                titulares_existentes, path_tit = cargar_titulares_familiares()
                if any(correos_iguales_exacto(t["correo"], self.client_email) for t in titulares_existentes):
                    return
                titulares_existentes.append({
                    "correo": self.client_email,
                    "usados": 0,
                    "estado": "disponible",
                    "miembros": []
                })
                guardar_titulares_familiares(titulares_existentes, path_tit)
            print(f"  [Titulares] [{self.client_email}] Registrado automáticamente en {path_tit.name} como Titular disponible.")
        except Exception as e:
            print(f"  {Color.WARNING}[Titulares] [{self.client_email}] [WARN] No se pudo registrar como titular: {e}{Color.ENDC}")

    def run_tmm_transfer(self, event_subir_csv: threading.Event) -> None:
        """Abre una nueva ventana para TuneMyMusic con proxy de Perú y sube el archivo CSV."""
        try:
            from playwright.sync_api import sync_playwright

            def _lanzar_contexto_tmm(pw):
                """Lanza Chrome TMM con el proxy PE actual (o uno nuevo del pool)."""
                if not self.proxy_pe_server:
                    p_pe = GLOBAL_PE_PROXY_POOL.obtener_proxy_unico(espera_s=60.0)
                    if p_pe:
                        self.proxy_pe_server = p_pe.get("server")
                        self.proxy_pe_user = p_pe.get("username")
                        self.proxy_pe_pass = p_pe.get("password")

                proxy_dict = None
                if self.proxy_pe_server:
                    p_serv = self.proxy_pe_server
                    if not p_serv.startswith("http"):
                        p_serv = "http://" + p_serv
                    proxy_dict = {"server": p_serv}
                    if self.proxy_pe_user:
                        proxy_dict["username"] = self.proxy_pe_user
                    if self.proxy_pe_pass:
                        proxy_dict["password"] = self.proxy_pe_pass
                    self.current_proxy_type = "PE"
                    self.use_proxy = True
                    print(f"  [TuneMyMusic] [{self.client_email}] Usando proxy de PERÚ: {p_serv}")
                else:
                    # No exponer IP real a DataDome vía TMM
                    raise RuntimeError(
                        f"Sin proxy de Perú para TuneMyMusic ({self.client_email}). "
                        f"Se omite la transferencia antes de exponer tu IP real."
                    )

                launch_args = [
                    "--credentials-enable-service=false",
                    "--password-store=basic",
                    "--disable-autofill",
                    "--disable-save-password-bubble",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                ]
                launch_kwargs = {
                    "user_data_dir": str(self.main_profile),
                    "headless": self.headless,
                    "args": launch_args,
                    "ignore_default_args": ["--enable-automation", "--no-sandbox"],
                    "viewport": {"width": 1280, "height": 800},
                    "locale": "es-ES",
                    "proxy": proxy_dict,
                    "channel": "chrome",
                }
                reparar_perfil_corrupto(self.main_profile)
                ctx = pw.chromium.launch_persistent_context(**launch_kwargs)
                ctx.set_default_navigation_timeout(60000)
                ctx.set_default_timeout(45000)
                ctx.add_init_script(STEALTH_SCRIPT)
                try:
                    valid_cookies = cargar_cookies_tmm()
                    if valid_cookies:
                        ctx.add_cookies(valid_cookies)
                        print(f"  [TuneMyMusic] [{self.client_email}] Sesión precargada desde 'tmm_cookies.json'.")
                except Exception as e:
                    print(f"  [TuneMyMusic] [WARN] Error al cargar cookies de TuneMyMusic: {e}")
                if getattr(self, "cookies_tidal", None):
                    try:
                        ctx.add_cookies(self.cookies_tidal)
                    except Exception:
                        pass
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.client_email = self.client_email
                page.manager = self
                page.bring_to_front()
                return ctx, page

            def _cerrar_contexto_tmm():
                try:
                    if self.context:
                        self.context.close()
                except Exception:
                    pass
                self.context = None
                self.page = None

            def _rotar_proxy_pe_tmm(razon: str) -> bool:
                print(f"  {Color.WARNING}[TuneMyMusic] [{self.client_email}] {razon}. "
                      f"Rotando proxy PE...{Color.ENDC}")
                _cerrar_contexto_tmm()
                anterior = self.proxy_pe_server
                p_pe = GLOBAL_PE_PROXY_POOL.rotar_y_marcar_bloqueado(anterior)
                if not p_pe:
                    print(f"  {Color.FAIL}[TuneMyMusic] [{self.client_email}] No quedan proxies PE limpios.{Color.ENDC}")
                    return False
                self.proxy_pe_server = p_pe.get("server")
                self.proxy_pe_user = p_pe.get("username")
                self.proxy_pe_pass = p_pe.get("password")
                print(f"  [TuneMyMusic] [{self.client_email}] Nuevo proxy PE: {self.proxy_pe_server}")
                time.sleep(1.5)
                return True

            self.playwright = sync_playwright().start()
            self.context, self.page = _lanzar_contexto_tmm(self.playwright)

            print(f"  [TuneMyMusic] [{self.client_email}] Cargando tunemymusic.com/es/transfer...")
            nav_ok = False
            _max_tmm = 4
            for intento_tmm in range(1, _max_tmm + 1):
                try:
                    self.page.goto(
                        "https://www.tunemymusic.com/es/transfer",
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    nav_ok = True
                    break
                except Exception as e_nav:
                    print(f"  {Color.WARNING}[TuneMyMusic] [{self.client_email}] Fallo de carga "
                          f"({intento_tmm}/{_max_tmm}): {e_nav}{Color.ENDC}")
                    if intento_tmm >= _max_tmm:
                        raise
                    # ERR_TUNNEL / timeout: rotar IP y relanzar Chrome (antes reintentaba el mismo proxy muerto)
                    if es_error_proxy_o_red(e_nav) or "timeout" in str(e_nav).lower():
                        if not _rotar_proxy_pe_tmm("Túnel/proxy caído al abrir TuneMyMusic"):
                            raise
                        self.context, self.page = _lanzar_contexto_tmm(self.playwright)
                    else:
                        time.sleep(2.5)

            if not nav_ok:
                raise RuntimeError("No se pudo cargar TuneMyMusic tras varios intentos con rotación de proxy.")

            print(f"  [TuneMyMusic] [{self.client_email}] Ventana abierta con la sesión de Tidal inyectada.")
            print(f"  [TuneMyMusic] [{self.client_email}] Esperando que prepares la pantalla de selección de archivo...")
            
            # Esperar a que el usuario presione Enter en el hilo principal
            event_subir_csv.wait()

            transfer_ok = False
            mantener_abierta = False

            # CSV fijado 1:1 a esta cuenta (lote opción 8). No re-resolver a otro archivo.
            csv_path = obtener_csv_para_subida(self, reintentar_s=0.0)
            if not csv_path:
                print(f"  {Color.WARNING}[TuneMyMusic] [{self.client_email}] CSV aún no visible en 'descargas/'. "
                      f"Esperando hasta 90s (solo archivos que coincidan con esta cuenta)...{Color.ENDC}")
                csv_path = obtener_csv_para_subida(self, reintentar_s=90.0)

            if not csv_path:
                print(f"  {Color.FAIL}[TuneMyMusic] [{self.client_email}] ERROR: No se encontró CSV válido en "
                      f"'descargas/' para esta cuenta ({self.client_email}.csv). "
                      f"La ventana se mantiene abierta para subida manual "
                      f"(hasta 15 min o hasta que la cierres).{Color.ENDC}")
                mantener_abierta = True
            else:
                # Tras ENTER, cada ventana puede seguir sin el input listo (usuario preparando 10 pantallas).
                # Antes: 30s → error → cierre inmediato (error.txt: Timeout input[type=file]).
                print(f"  [TuneMyMusic] [{self.client_email}] CSV emparejado: {csv_path.name} "
                      f"→ cuenta {self.client_email}")
                print(f"  [TuneMyMusic] [{self.client_email}] Esperando el selector de archivo (hasta 5 min) "
                      f"para subir {csv_path.name}...")
                file_input = None
                limite_input = time.time() + 300
                ultimo_aviso = 0.0
                while time.time() < limite_input:
                    try:
                        if self.page.is_closed():
                            break
                        loc = self.page.locator('input[type="file"]').first
                        if loc.count() > 0:
                            try:
                                loc.wait_for(state="attached", timeout=2500)
                                file_input = loc
                                break
                            except Exception:
                                pass
                    except Exception:
                        pass
                    ahora = time.time()
                    if ahora - ultimo_aviso > 30:
                        restante = int(limite_input - ahora)
                        print(f"  [TuneMyMusic] [{self.client_email}] Aún sin input de archivo… "
                              f"({restante}s). Deja la pantalla de CSV lista en TuneMyMusic.")
                        ultimo_aviso = ahora
                    time.sleep(2.0)

                if not file_input:
                    print(f"  {Color.FAIL}[TuneMyMusic] [{self.client_email}] No apareció input[type=file]. "
                          f"Ventana abierta para subida manual (15 min).{Color.ENDC}")
                    mantener_abierta = True
                else:
                    if not csv_pertenece_a_cuenta(csv_path, self.client_email) or not csv_parece_valido(csv_path):
                        print(f"  {Color.FAIL}[TuneMyMusic] [{self.client_email}] Abortada subida: "
                              f"'{csv_path.name}' ya no coincide con la cuenta.{Color.ENDC}")
                        mantener_abierta = True
                    else:
                        print(f"  [TuneMyMusic] [{self.client_email}] Subiendo archivo CSV: {csv_path.name}...")
                        try:
                            file_input.set_input_files(str(csv_path.resolve()))
                            print(f"  [TuneMyMusic] [{self.client_email}] Archivo CSV subido con éxito "
                                  f"({csv_path.name} → {self.client_email}).")
                        except Exception as e:
                            print(f"  {Color.FAIL}[TuneMyMusic] [{self.client_email}] ERROR al subir el archivo CSV: {e}. "
                                  f"Ventana abierta para reintento manual (15 min).{Color.ENDC}")
                            mantener_abierta = True

            # Monitorear transferencia (también si la subida fue manual: el usuario puede completar solo)
            if not self.page or self.page.is_closed():
                print(f"  {Color.WARNING}[TuneMyMusic] [{self.client_email}] Ventana ya cerrada; se omite monitoreo.{Color.ENDC}")
            else:
                print(f"  [TuneMyMusic] [{self.client_email}] Monitoreando el progreso de la transferencia...")
                t_start = time.time()
                # Si quedó en modo manual, dar más margen; si ya subió, 10 min bastan
                timeout_mon = 900 if mantener_abierta else 600
                completado = False
                while time.time() - t_start < timeout_mon:
                    try:
                        if self.page.is_closed():
                            break

                        has_completed = self.page.evaluate("""() => {
                            const text = document.body ? document.body.innerText : '';
                            return text.includes('Transferencia completada')
                                || text.includes('¡Transferencia completada!')
                                || text.includes('Transfer completed');
                        }""")
                        if has_completed:
                            completado = True
                            transfer_ok = True
                            print(f"  {Color.GREEN}[TuneMyMusic] [{self.client_email}] ¡Transferencia completada con éxito! "
                                  f"Esperando 8s antes de cerrar...{Color.ENDC}")
                            time.sleep(8.0)
                            break
                    except Exception:
                        pass
                    time.sleep(2.0)

                if not completado and mantener_abierta:
                    print(f"  {Color.WARNING}[TuneMyMusic] [{self.client_email}] Sin confirmación de transferencia; "
                          f"cerrando tras el plazo de revisión manual.{Color.ENDC}")
                elif not completado:
                    print(f"  {Color.WARNING}[TuneMyMusic] [{self.client_email}] El monitoreo ha excedido el tiempo de espera. "
                          f"Se mantiene la ventana 3 min más por si aún carga...{Color.ENDC}")
                    extra = time.time() + 180
                    while time.time() < extra:
                        try:
                            if self.page.is_closed():
                                break
                            if self.page.evaluate("""() => {
                                const text = document.body ? document.body.innerText : '';
                                return text.includes('Transferencia completada')
                                    || text.includes('¡Transferencia completada!')
                                    || text.includes('Transfer completed');
                            }"""):
                                transfer_ok = True
                                print(f"  {Color.GREEN}[TuneMyMusic] [{self.client_email}] Transferencia confirmada en tiempo extra. "
                                      f"Cerrando en 8s...{Color.ENDC}")
                                time.sleep(8.0)
                                break
                        except Exception:
                            break
                        time.sleep(2.0)

            self.tmm_transfer_ok = transfer_ok
                
        except Exception as e:
            print(f"  {Color.FAIL}[TuneMyMusic] [{self.client_email}] ERROR inesperado: {e}{Color.ENDC}")
        finally:
            # Guardar cookies de TuneMyMusic antes de cerrar
            try:
                if self.context and guardar_cookies_tmm(self.context.cookies()):
                    print(f"  [TuneMyMusic] [{self.client_email}] Cookies actualizadas en 'tmm_cookies.json'.")
            except Exception as e:
                print(f"  [TuneMyMusic] [WARN] Error al guardar cookies de TuneMyMusic: {e}")

            # Cerrar Chrome SIN liberar PE todavía: liberar_proxy va justo después para no
            # dejar el proxy huérfano si close() lanza excepción.
            try:
                if self.context:
                    self.context.close()
            except Exception:
                pass
            self.context = None
            self.page = None
            try:
                if self.playwright:
                    self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

            try:
                GLOBAL_PE_PROXY_POOL.liberar_proxy(self.proxy_pe_server)
            except Exception:
                pass
            self.proxy_pe_server = None
            self.proxy_pe_user = None
            self.proxy_pe_pass = None

            # Usar el limpiador con reintentos: el rmtree async anterior a veces no borraba
            # el perfil en Windows porque Chrome aún no había soltado los ficheros.
            try:
                self.limpiar_perfil_temporal()
            except Exception:
                pass


class TidalResetPasswordManager:
    def __init__(self, client_email, target_pwd, proxy_pe_server=None, proxy_pe_user=None, proxy_pe_pass=None, headless=False, barreras=None, thread_index=1):
        self.client_email = client_email
        self.target_pwd = target_pwd
        self.use_proxy = proxy_pe_server is not None
        self.proxy_pe_server = proxy_pe_server
        self.proxy_pe_user = proxy_pe_user
        self.proxy_pe_pass = proxy_pe_pass
        self.headless = headless
        self.barreras = barreras or {}
        self.thread_index = thread_index
        self.playwright = None
        self.context = None
        self.page = None
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        self.start_step = 8
        self.cuenta_abortada = False
        
        email_safe = re.sub(r'[^a-zA-Z0-9]', '_', client_email)
        self.main_profile = Path(tempfile.gettempdir()) / f"tidal_reset_{email_safe}_{random.randint(1000, 9999)}"

    def esperar_barrera(self, nombre):
        barrera = self.barreras.get(nombre)
        if not barrera:
            return
        try:
            print(f"  [{self.client_email}] Esperando sincronización de paso: {nombre}...")
            barrera.wait(timeout=180)
        except TimeoutError:
            print(f"  [{self.client_email}] [WARN] Timeout en sincronización '{nombre}'. Se deserta el cupo y se continúa.")
            if hasattr(barrera, "desertar"):
                try:
                    barrera.desertar()
                except Exception:
                    pass
        except threading.BrokenBarrierError:
            # Solo aplica a threading.Barrier legado: no tratar como éxito silencioso del lote
            print(f"  [{self.client_email}] {Color.WARNING}[WARN] Barrera '{nombre}' rota (otro hilo hizo abort). "
                  f"Continuando sin sincronización — el lote puede desfasarse.{Color.ENDC}")
        except Exception as e:
            print(f"  [{self.client_email}] [WARN] Error en barrera '{nombre}': {e}")

    def desertar_barreras(self):
        """Libera el cupo de este hilo en cada barrera sin romperlas para el resto del lote."""
        for _name, b in self.barreras.items():
            try:
                if hasattr(b, "desertar"):
                    b.desertar()
            except Exception:
                pass

    def abortar_barreras(self):
        """Compat: ya no se usa Barrier.abort() (rompía el lote entero). Delega en desertar."""
        self.desertar_barreras()

    def cerrar_navegador(self, liberar_proxy=True):
        # liberar_proxy=False cuando se va a reabrir el navegador con el mismo proxy de Perú,
        # para que ningún otro hilo pueda tomarlo mientras la cuenta sigue en proceso.
        if liberar_proxy and getattr(self, "proxy_pe_server", None):
            GLOBAL_PE_PROXY_POOL.liberar_proxy(self.proxy_pe_server)
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        self.context = None
        self.page = None

    def forzar_modo_visual(self):
        if not self.headless:
            return
        print(f"\n  [Modo Headless] Intervención requerida para {self.client_email}. Transicionando a modo visual...")
        current_url = "https://account.tidal.com/"
        try:
            if self.page and not self.page.is_closed():
                current_url = self.page.url
        except Exception:
            pass
        self.headless = False
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.context = None
        self.page = None
        self.playwright = None
        
        # Esperar a que se liberen los bloqueos de archivos en Windows
        time.sleep(2.5)
        reparar_perfil_corrupto(self.main_profile)
        time.sleep(1.0)
        
        print("  [Modo Headless] Levantando navegador headed...")
        try:
            self.asegurar_navegador_abierto()
            if self.page and not self.page.is_closed():
                if current_url and current_url.startswith("http"):
                    try:
                        self.page.goto(current_url, wait_until="domcontentloaded", timeout=25000)
                        time.sleep(2.0)
                    except Exception:
                        pass
                print("  [Modo Headless] Navegador headed abierto correctamente.")
        except Exception as e:
            print(f"  [Modo Headless] [ERROR] No se pudo abrir el navegador: {e}")

    def ejecutar_rotacion_proxy_y_recargar(self):
        print(f"\n  [Auto-Proxy] Bloqueo detectado en {self.client_email}. Rotando proxy de Nigeria...")
        current_url = "https://account.tidal.com/"
        try:
            if self.page:
                current_url = self.page.url
        except Exception:
            pass
        self.rotar_proxy_contexto()
        try:
            if self.context:
                self.context.clear_cookies(domain="tidal.com")
                self.context.clear_cookies(domain="login.tidal.com")
                self.context.clear_cookies(domain="account.tidal.com")
        except Exception:
            pass
        if current_url and current_url.startswith("http"):
            print(f"  [Auto-Proxy] Recargando página con el nuevo proxy en: {current_url}")
            try:
                self.page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2.0)
            except Exception as e:
                print(f"  [Auto-Proxy] [WARN] Error al recargar: {e}")

    def rotar_proxy_contexto(self, tipo="PE"):
        global GLOBAL_PE_PROXY_POOL
        print(f"\n  [Proxy Rotation PE] Rotando proxy de Perú por bloqueo antirobot...")
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.context = None
        self.page = None
        self.playwright = None
        
        time.sleep(2.0)
        reparar_perfil_corrupto(self.main_profile)
        
        p_pe = GLOBAL_PE_PROXY_POOL.rotar_y_marcar_bloqueado(self.proxy_pe_server)
        nuevo = (p_pe or {}).get("server") if p_pe else None
        if nuevo:
            self.proxy_pe_server = nuevo
            self.proxy_pe_user = p_pe.get("username")
            self.proxy_pe_pass = p_pe.get("password")
            self.use_proxy = True
            print(f"  [Proxy Rotation PE] Nuevo proxy PE configurado: {self.proxy_pe_server}")
        else:
            # No poner proxy_pe_server=None: la siguiente asegurar_navegador_abierto tumbaría el lote
            raise RuntimeError(
                f"No quedan proxies de Perú limpios para {self.client_email}. Se aborta la cuenta en vez de usar tu IP real."
            )

        self.asegurar_navegador_abierto()

    def registrar_contador_datos(self, context):
        pass

    def input_concurrente(self, prompt):
        global stdin_lock
        with stdin_lock:
            print("\n" + "!" * 80)
            print(f"  [AVISO] PAUSA MANUAL REQUERIDA PARA: {self.client_email}")
            print("!" * 80)
            res = input(prompt)
            print("!" * 80 + "\n")
            return res

    def _garantizar_proxy_pe(self) -> None:
        """Asegura proxy PE válido antes de abrir Chrome. Sin esto, asegurar_navegador_abierto
        lanza RuntimeError y puede tumbar la sincronización del lote entero."""
        if self.proxy_pe_server:
            self.use_proxy = True
            return
        p_pe = GLOBAL_PE_PROXY_POOL.obtener_proxy_unico(espera_s=60.0)
        serv = (p_pe or {}).get("server") if p_pe else None
        if not serv:
            raise RuntimeError(
                f"Sin proxy de Perú disponible para {self.client_email}. "
                f"Se aborta la cuenta antes de exponer tu IP real."
            )
        self.proxy_pe_server = serv
        self.proxy_pe_user = p_pe.get("username") or ""
        self.proxy_pe_pass = p_pe.get("password") or ""
        self.use_proxy = True
        print(f"  [Proxy PE] [{self.client_email}] Proxy PE reasignado desde el pool: {serv}")

    def asegurar_navegador_abierto(self):
        try:
            if self.context and self.page and not self.page.is_closed():
                return
        except Exception:
            pass

        self._garantizar_proxy_pe()
            
        if not self.playwright:
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            
        launch_args = list(CHROME_SILENT_ARGS)
        p_serv = self.proxy_pe_server
        if p_serv and not p_serv.startswith("http"):
            p_serv = "http://" + p_serv
        proxy_dict = {"server": p_serv}
        if self.proxy_pe_user:
            proxy_dict["username"] = self.proxy_pe_user
        if self.proxy_pe_pass:
            proxy_dict["password"] = self.proxy_pe_pass
        print(f"  [Proxy PE] [{self.client_email}] Usando proxy de PERÚ para el restablecimiento: {p_serv}")
            
        launch_kwargs = {
            "user_data_dir": str(self.main_profile),
            "headless": self.headless,
            "args": launch_args,
            "ignore_default_args": ["--enable-automation", "--no-sandbox"],
            "viewport": {"width": 1280, "height": 800},
            "locale": "es-ES",
            "proxy": proxy_dict,
            "channel": "chrome"
        }
            
        try:
            self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:
            print(f"  [Navegador] [WARN] Falló el lanzamiento: {e}. Reparando y reintentando...")
            reparar_perfil_corrupto(self.main_profile)
            time.sleep(2.0)
            self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
            
        self.context.set_default_navigation_timeout(60000)
        self.context.set_default_timeout(45000)
        self.context.add_init_script(STEALTH_SCRIPT)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.client_email = self.client_email
        self.page.manager = self
        self.page.bring_to_front()

    def _limpiar_overlays_reset(self) -> None:
        """Cookies + restos de overlay que interceptan el click en Continuar."""
        try:
            aceptar_cookies_con_espera(self.page)
        except Exception:
            pass
        try:
            self.page.evaluate("""
                () => {
                    const selectors = [
                        '#onetrust-consent-sdk', '.ot-sdk-container', '#onetrust-banner-sdk',
                        '[id*="onetrust" i]', '.ot-cookie-policy',
                        '[class*="cookie-banner" i]', '[id*="cookie-banner" i]',
                        '[class*="consent" i][class*="banner" i]'
                    ];
                    selectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => { try { el.remove(); } catch(e) {} });
                    });
                }
            """)
        except Exception:
            pass

    def _elemento_interactuable_reset(self, locator) -> bool:
        """True si el locator es visible y no está tapado por un overlay (cookies/DataDome)."""
        if not locator:
            return False
        try:
            if not locator.is_visible():
                return False
        except Exception:
            return False
        try:
            return bool(locator.evaluate("""(el) => {
                const r = el.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) return false;
                const x = r.left + r.width / 2;
                const y = r.top + r.height / 2;
                const top = document.elementFromPoint(x, y);
                if (!top) return false;
                return el === top || el.contains(top) || top.contains(el);
            }"""))
        except Exception:
            # Si no se puede evaluar cobertura, confiar solo en is_visible
            return True

    def _formulario_reset_sigue_activo(self) -> bool:
        """True si el formulario de solicitud (email + Continuar) sigue usable tras quitar overlays."""
        self._limpiar_overlays_reset()
        email = esperar_locator_en_frames(
            self.page,
            ['input[type="email"]', 'input[name="email"]', 'input[placeholder*="email" i]',
             'input[placeholder*="usuario" i]', 'input[type="text"]'],
            timeout_s=1.5
        )
        btn = esperar_locator_en_frames(
            self.page,
            ["button:has-text('Continuar')", "button:has-text('Continue')", "button[type='submit']"],
            timeout_s=1.5
        )
        return self._elemento_interactuable_reset(email) and self._elemento_interactuable_reset(btn)

    def _reset_solicitud_confirmada_ui(self, url_antes: str, email_antes_visible: bool) -> bool:
        """True solo con evidencia de avance real — no copy estático de /resetpass.

        El DOM de Tidal ya trae frases tipo 'revisa tu correo' / 'check your email' antes
        del submit; matchear body.innerText produce falsos positivos masivos.
        Tampoco basta is_hidden() del input: el banner OneTrust lo tapa y miente.
        """
        self._limpiar_overlays_reset()
        if detectar_pantalla_antirobot(self.page):
            return False

        url_ahora = (self.page.url or "").lower()
        url_prev = (url_antes or "").lower()
        # Cambio de ruta inequívoco fuera del formulario de solicitud
        if url_ahora and url_ahora != url_prev:
            if any(k in url_ahora for k in ("/sent", "check-email", "link-sent", "email-sent", "/verify")):
                return True
            if "resetpass" not in url_ahora and "login.tidal.com" in url_ahora:
                # Redirigió fuera de resetpass (p. ej. confirmación en otra ruta)
                if not self._formulario_reset_sigue_activo():
                    return True

        # Misma URL (SPA): el formulario debe haber desaparecido de verdad
        if email_antes_visible and not self._formulario_reset_sigue_activo():
            # Confirmar que no es solo un overlay: tras limpiar cookies, sin email+Continuar
            return True
        return False

    def _pulsar_continuar_reset(self, btn_continue) -> bool:
        """Click actionable en Continuar: limpia overlays, evita force=True salvo último recurso.

        force=True atraviesa iframes/overlays de DataDome: el evento no llega a React/Tidal.
        """
        self._limpiar_overlays_reset()
        try:
            manejar_bloqueos_e_intervencion(self.page, "Restablecer Contraseña (antes de Continuar)")
        except RuntimeError:
            raise
        except Exception:
            pass
        self.page = pagina_vigente(self.page)
        self._limpiar_overlays_reset()

        btn = esperar_locator_en_frames(
            self.page,
            ["button:has-text('Continuar')", "button:has-text('Continue')", "button[type='submit']"],
            timeout_s=4.0
        ) or btn_continue
        if not btn:
            return False

        # 1) Click normal (Playwright comprueba visibilidad/cobertura)
        try:
            btn.scroll_into_view_if_needed(timeout=2000)
            btn.click(timeout=3500)
            return True
        except Exception as e1:
            print(f"  [Reset Pass] [{self.client_email}] Click normal falló ({e1}). Reintentando tras limpiar overlay...")

        self._limpiar_overlays_reset()
        try:
            if detectar_pantalla_antirobot(self.page):
                manejar_bloqueos_e_intervencion(self.page, "Restablecer Contraseña (DataDome)")
                self.page = pagina_vigente(self.page)
        except RuntimeError:
            raise
        except Exception:
            pass

        btn = esperar_locator_en_frames(
            self.page,
            ["button:has-text('Continuar')", "button:has-text('Continue')", "button[type='submit']"],
            timeout_s=3.0
        ) or btn
        try:
            btn.click(timeout=3500)
            return True
        except Exception as e2:
            print(f"  [Reset Pass] [{self.client_email}] Click aún bloqueado ({e2}). Último recurso: Enter en el campo.")

        # 2) Enter en el input (sin force sobre el botón tapado)
        try:
            email = esperar_locator_en_frames(
                self.page,
                ['input[type="email"]', 'input[name="email"]', 'input[placeholder*="email" i]'],
                timeout_s=2.0
            )
            if email:
                email.focus()
                email.press("Enter")
                return True
        except Exception:
            pass
        return False

    def _abrir_enlace_reinicio(self, enlace_reset: str) -> bool:
        """Abre el magic link del correo con calentamiento, reintentos y rotación de proxy.

        Un solo page.goto al token fallaba con Timeout 45000ms en proxies PE lentos/colgados
        (p. ej. get.mushroom2.0.48, getmush.ro.om1849) y abortaba la cuenta sin reintento,
        aunque la fase previa de /resetpass sí rotaba ante fallos de red.
        """
        _max = 4
        for intento in range(1, _max + 1):
            try:
                self._garantizar_proxy_pe()
                self.asegurar_navegador_abierto()

                print(
                    f"  [Bypass] [{self.client_email}] Calentando reputación antes del enlace "
                    f"de reinicio (intento {intento}/{_max})..."
                )
                try:
                    navegar_tidal_tolerante(self.page, "https://tidal.com/pricing", timeout_ms=35000)
                    time.sleep(random.uniform(1.0, 2.0))
                    aceptar_cookies_con_espera(self.page)
                except Exception as e_warm:
                    # Túnel/timeout en el calentamiento: rotar ya; otros avisos se ignoran
                    if es_error_proxy_o_red(e_warm) or "timeout" in str(e_warm).lower():
                        raise
                    print(f"  [Bypass] [{self.client_email}] [WARN] Calentamiento falló: {e_warm}")

                print(f"  [Reset Pass] Abriendo enlace de reinicio: {enlace_reset[:70]}...")
                navegar_tidal_tolerante(
                    self.page,
                    enlace_reset,
                    referer="https://tidal.com/pricing",
                    timeout_ms=60000,
                )
                manejar_bloqueos_e_intervencion(self.page, "Restablecer Contraseña (Enlace)")
                self.page = pagina_vigente(self.page)
                time.sleep(2.0)

                pwd = esperar_locator_en_frames(
                    self.page,
                    [
                        'input[name="newPassword"]',
                        'input[type="password"]',
                        'input[name="password"]',
                        'input[name="confirmNewPassword"]',
                    ],
                    timeout_s=18.0,
                )
                if pwd:
                    return True

                print(
                    f"  [Reset Pass] {Color.WARNING}[WARN] [{self.client_email}] Enlace abierto "
                    f"pero sin formulario de contraseña (intento {intento}/{_max}).{Color.ENDC}"
                )
                if intento >= _max:
                    return False
                self.rotar_proxy_contexto()
            except Exception as e:
                print(
                    f"  [Reset Pass] {Color.WARNING}[WARN] [{self.client_email}] Fallo al abrir "
                    f"enlace (intento {intento}/{_max}): {e}{Color.ENDC}"
                )
                reintentable = (
                    es_error_proxy_o_red(e)
                    or "timeout" in str(e).lower()
                    or es_error_navegacion_abortada(e)
                )
                if not reintentable or intento >= _max:
                    raise
                if es_error_proxy_o_red(e) or "timeout" in str(e).lower():
                    self.rotar_proxy_contexto()
                else:
                    time.sleep(2.0)
        return False

    def run_password_reset(self) -> bool:
        try:
            self.asegurar_navegador_abierto()
            try:
                self.context.clear_cookies(domain="tidal.com")
                self.context.clear_cookies(domain="login.tidal.com")
                self.context.clear_cookies(domain="account.tidal.com")
            except Exception:
                pass
            
            print(f"  [Navegador] [{self.client_email}] Abriendo ventana de Chrome y cargando página de restablecimiento...")
            
            # Baseline IMAP se toma JUSTO antes del Continuar (más abajo). Tomarlo aquí, antes del
            # calentamiento/carga/escritura, permite que el correo de reset llegue en el intervalo
            # y quede por debajo del baseline → descartado por after_email_id.
            reset_baseline_id = 0
            
            # 2. Navegar a la página de restablecimiento con reintentos y rotación de proxy autónoma.
            #    Igual que la opción 10: primero se "calienta" la reputación de la IP visitando una
            #    página pública (tidal.com/pricing) y luego se entra a /resetpass con referer orgánico.
            #    Ir en frío a /resetpass con un proxy de datacenter dispara el antirobot casi siempre.
            _max_intentos_nav = 3
            nav_reset_ok = False
            for _intento_nav in range(1, _max_intentos_nav + 1):
                try:
                    print(f"  [Bypass] [{self.client_email}] Calentando reputación en tidal.com/pricing (intento {_intento_nav}/{_max_intentos_nav})...")
                    self.page.goto("https://tidal.com/pricing", wait_until="domcontentloaded", timeout=30000)
                    manejar_bloqueos_e_intervencion(self.page, "Restablecer Contraseña (Calentamiento)")
                    time.sleep(random.uniform(2.0, 3.5))
                    aceptar_cookies_con_espera(self.page)
                    time.sleep(random.uniform(0.5, 1.0))

                    print(f"  [Bypass] [{self.client_email}] Entrando a /resetpass con referer orgánico...")
                    self.page.goto("https://login.tidal.com/resetpass", wait_until="domcontentloaded", timeout=30000, referer="https://tidal.com/pricing")
                    aceptar_cookies_con_espera(self.page)
                    nav_reset_ok = True
                    break
                except Exception as _nav_err:
                    err_txt = str(_nav_err).lower()
                    print(f"  [Reset Pass] {Color.WARNING}[WARN] [{self.client_email}] Intento {_intento_nav}/{_max_intentos_nav} de navegación falló ({_nav_err})...{Color.ENDC}")
                    if "tunnel" in err_txt or "proxy" in err_txt or "connection" in err_txt or "err_" in err_txt or "failed" in err_txt:
                        # Nunca random.choice(valid_pe_list): puede devolver el proxy de otro hilo.
                        p_pe = GLOBAL_PE_PROXY_POOL.rotar_y_marcar_bloqueado(self.proxy_pe_server)
                        nuevo = (p_pe or {}).get("server") if p_pe else None
                        if not nuevo:
                            # Dejar proxy_pe_server intacto si la rotación no entregó nada usable;
                            # no poner None (eso tumba asegurar_navegador_abierto del resto del flujo).
                            raise RuntimeError(
                                f"No quedan proxies PE limpios para {self.client_email} tras fallo de red."
                            )
                        self.proxy_pe_server = nuevo
                        self.proxy_pe_user = p_pe.get("username", "")
                        self.proxy_pe_pass = p_pe.get("password", "")
                        self.use_proxy = True
                        print(f"  [Proxy PE] [{self.client_email}] Rotado vía pool a: {self.proxy_pe_server}")
                        try:
                            if self.context:
                                self.context.close()
                        except Exception:
                            pass
                        self.context = None
                        self.page = None
                        time.sleep(1.5)
                        self.asegurar_navegador_abierto()
                    else:
                        time.sleep(2.0)
            
            if not nav_reset_ok:
                raise RuntimeError(f"No se pudo cargar la página de restablecimiento para {self.client_email} tras 3 intentos.")

            # Esperar a que el campo de correo esté renderizado antes de sincronizar
            esperar_locator_en_frames(
                self.page, 
                ['input[type="email"]', 'input[type="text"]', 'input[placeholder*="email" i]', 'input[placeholder*="usuario" i]'], 
                timeout_s=15.0
            )
            time.sleep(1.0)
            print(f"  [Navegador] {Color.GREEN}[Cargado] Ventana de restablecimiento lista para: {self.client_email}{Color.ENDC}")

            # Sincronización inicial: Esperar a que TODAS las ventanas estén cargadas
            self.esperar_barrera("inicio")
            
            print(f"\n--- Iniciando restablecimiento de contraseña para: {self.client_email} ---")
            
            manejar_bloqueos_e_intervencion(self.page, "Restablecer Contraseña (Email)")
            
            # 3. Colocar correo a restablecer
            email_input = esperar_locator_en_frames(
                self.page, 
                ['input[type="email"]', 'input[type="text"]', 'input[placeholder*="email" i]', 'input[placeholder*="usuario" i]'], 
                timeout_s=15.0
            )
            if not email_input:
                raise RuntimeError("No se localizó el campo de correo para iniciar restablecimiento.")
                
            # Escribir instantáneamente sin retrasos por latencia y disparar los eventos de React mediante JS
            email_input.fill(self.client_email)
            self.page.evaluate("""
                () => {
                    const el = document.querySelector('input[type="email"]') || 
                               document.querySelector('input[type="text"]') || 
                               document.querySelector('input[placeholder*="email" i]') ||
                               document.querySelector('input[placeholder*="usuario" i]');
                    if (el) {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }
            """)
            email_input.press("Tab")
            time.sleep(0.5)
            
            # Cookies ANTES de cualquier verificación de avance (si el banner tapa el input,
            # is_hidden() mentiría y daríamos el submit por bueno).
            self._limpiar_overlays_reset()
            time.sleep(0.3)

            # Baseline justo antes del submit: minimiza la ventana donde el correo de reset
            # llega a Gmail y queda ≤ baseline (antes se tomaba antes del calentamiento).
            reset_baseline_id = obtener_max_email_id(self.client_email, "tidal")
            print(f"  [Reset Pass] [{self.client_email}] Baseline IMAP antes de Continuar: {reset_baseline_id}")
            
            btn_continue = esperar_locator_en_frames(
                self.page,
                ["button:has-text('Continuar')", "button:has-text('Continue')", "button[type='submit']"],
                timeout_s=8.0
            )
            if not btn_continue:
                raise RuntimeError("No se encontró el botón 'Continuar' en resetpass.")

            url_antes_submit = self.page.url or ""
            email_antes_visible = self._elemento_interactuable_reset(email_input)
            if not email_antes_visible:
                # Re-localizar tras limpiar cookies
                email_input = esperar_locator_en_frames(
                    self.page,
                    ['input[type="email"]', 'input[name="email"]', 'input[placeholder*="email" i]',
                     'input[placeholder*="usuario" i]', 'input[type="text"]'],
                    timeout_s=5.0
                )
                if not email_input or not self._elemento_interactuable_reset(email_input):
                    raise RuntimeError(
                        "El campo de correo no es interactuable (posible overlay de cookies/DataDome)."
                    )
                email_antes_visible = True
                
            print("  [Reset Pass] Pulsando botón 'Continuar'...")
            click_exitoso = False
            for intento_click in range(1, 4):  # máx 3 intentos, 1 submit actionable cada uno
                if self._reset_solicitud_confirmada_ui(url_antes_submit, email_antes_visible):
                    click_exitoso = True
                    break

                if not self._pulsar_continuar_reset(btn_continue):
                    print(f"  [Reset Pass] [WARN] No se pudo pulsar Continuar (intento {intento_click}/3).")

                # Esperar propagación UI antes de decidir reintento
                for _ in range(10):
                    time.sleep(0.45)
                    if self._reset_solicitud_confirmada_ui(url_antes_submit, email_antes_visible):
                        click_exitoso = True
                        break
                if click_exitoso:
                    break
                print(f"  [Reset Pass] [WARN] Continuar no avanzó el formulario (intento {intento_click}/3).")

            if not click_exitoso:
                raise RuntimeError(
                    "No se confirmó el envío del formulario de restablecimiento "
                    "(el formulario sigue activo o hay overlay/DataDome). "
                    "No se cerrará el navegador ni se hará polling IMAP."
                )

            manejar_bloqueos_e_intervencion(self.page, "Restablecer Contraseña (Envío)")
            time.sleep(1.0)
            
            # Solo aquí: UI confirmó avance → mensaje fiel y cierre para abrir el enlace limpio
            print(f"  [Reset Pass] [{self.client_email}] Solicitud de restablecimiento enviada (UI confirmada). Cerrando la ventana actual...")
            self.cerrar_navegador(liberar_proxy=False)
            time.sleep(1.5)
            
            # Sincronización tras el envío de correo de restablecimiento
            self.esperar_barrera("post_solicitud")
            
            # 5. Esperar el enlace del Gmail via IMAP (5 min: cola del semáforo + aliases mismo buzón)
            print("  [Reset Pass] Buscando enlace de restablecimiento enviado por correo...")
            enlace_reset = None
            _max_intentos_imap = 30  # 30 * ~10s ≈ 5 min
            for intento in range(1, _max_intentos_imap + 1):
                print(f"  [Reset Pass] Intento {intento}/{_max_intentos_imap}: Buscando correo de cambio de contraseña...")
                enlace_reset = obtener_codigo_via_imap(
                    gmail_user=self.client_email,
                    required_keywords=["resetting your tidal password", "restablecer tu contraseña de tidal", "reset your password", "link to reset your password"],
                    query_exclude="invited to a tidal family",
                    after_email_id=reset_baseline_id,
                    max_age_minutes=15,
                    solo_link=True
                )
                if enlace_reset and enlace_reset.startswith("http"):
                    break
                if intento < _max_intentos_imap:
                    time.sleep(8.0 + random.uniform(0.0, 3.0))
            
            if not enlace_reset or not enlace_reset.startswith("http"):
                raise RuntimeError("No se pudo extraer el enlace de reinicio automáticamente por IMAP.")
                
            # --- PARTE 2: Abrir el enlace de restablecimiento con el mismo proxy de Perú ---
            print(f"  [Reset Pass] [{self.client_email}] Abriendo nuevo navegador con el proxy de PERÚ...")
            if not self._abrir_enlace_reinicio(enlace_reset):
                raise RuntimeError(
                    "No se cargó el formulario de restablecimiento de contraseña tras abrir el enlace del correo."
                )

            # Sincronización tras cargar la página del reset de contraseña
            self.esperar_barrera("post_link")

            # 7. Colocar contraseña
            pwd_new1 = self.page.locator('input[name="newPassword"], input[type="password"], input[name="password"]').first
            if esperar_visibilidad(pwd_new1, 20000):
                rellenar_campo_humanizado(pwd_new1, self.target_pwd)
                
                try:
                    pwd_new2 = self.page.locator('input[name="confirmNewPassword"], input[id*="confirm" i]').first
                    if pwd_new2.count() > 0 and pwd_new2.is_visible():
                        rellenar_campo_humanizado(pwd_new2, self.target_pwd)
                except Exception:
                    pass
                time.sleep(1.0)
                
                # 8. Presionar continuar para guardar la contraseña
                btn_submit = self.page.locator("button[type='submit']").or_(self.page.locator("button:has-text('Restablecer contraseña')")).or_(self.page.locator("button:has-text('Guardar')")).or_(self.page.locator("button:has-text('Reset password')")).first
                if esperar_visibilidad(btn_submit, 8000):
                    try:
                        btn_submit.evaluate("el => el.click()")
                    except Exception:
                        btn_submit.click(force=True)
                else:
                    self.page.keyboard.press("Enter")
                    
                time.sleep(6.0)
                print(f"  {Color.GREEN}[OK] Contraseña restablecida con éxito para {self.client_email}.{Color.ENDC}")
                
                # Sincronización final antes de terminar
                self.esperar_barrera("final")
                return True
            else:
                raise RuntimeError("No se cargó el formulario de restablecimiento de contraseña en la página.")
                
        except Exception as e:
            self.desertar_barreras()
            print(f"  {Color.FAIL}[ERROR] Falló el restablecimiento para {self.client_email}: {e}{Color.ENDC}")
            return False
        finally:
            try:
                GLOBAL_PE_PROXY_POOL.liberar_proxy(getattr(self, "proxy_pe_server", None))
            except Exception:
                pass
            try:
                if self.context:
                    self.context.close()
            except Exception:
                pass
            try:
                if self.playwright:
                    self.playwright.stop()
            except Exception:
                pass
            try:
                if self.main_profile.exists():
                    prof = self.main_profile
                    def _rm_async(p):
                        time.sleep(1.0)
                        try:
                            if p.exists():
                                shutil.rmtree(p, ignore_errors=True)
                                print(f"  [Reset Pass] Limpiado perfil temporal: {p}")
                        except Exception:
                            pass
                    threading.Thread(target=_rm_async, args=(prof,), daemon=True).start()
            except Exception as ex:
                pass

def cargar_titulares_familiares() -> tuple[list[dict], Path]:
    # Intentar cargar de "perfiles/familiar_titular.txt" primero (resuelto con SCRIPT_DIR)
    path1 = SCRIPT_DIR / "perfiles" / "familiar_titular.txt"
    path2 = SCRIPT_DIR / "titular_familiar.txt"
    
    def has_valid_lines(p: Path) -> bool:
        if not p.exists():
            return False
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return True
        except Exception:
            pass
        return False

    path = path1
    if not has_valid_lines(path1) and has_valid_lines(path2):
        path = path2
        print(f"  [Inviter] Cargando cuentas titulares desde: {path.name}")
    else:
        print(f"  [Inviter] Cargando cuentas titulares desde: {path}")

    titulares = []
    if not path.exists():
        # Crear plantilla si no existe ninguna
        lines = [
            "# Cuentas Familiares Titulares de Tidal (Nigeria)",
            "# Formato por bloques (opción 11):",
            "# TITULAR",
            "# correo_titular, miembros_actuales, estado, [miembros_detalles]",
            "# MIEMBROS:",
            "# miembro1@gmail.com",
            "#",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        return [], path

    titulares, _ = parsear_titular_familiar_txt_opcion11(path)
    return titulares, path

def guardar_titulares_familiares(titulares: list[dict], path: Path):
    """Guarda en formato por bloques TITULAR / MIEMBROS, preservando el plan de invitaciones."""
    # Conservar miembros_invitar del archivo si el dict en memoria no lo trae.
    # Clave EXACTA (con puntos): no usar clean_email/son_correos_equivalentes.
    if path.exists():
        try:
            existentes, _ = parsear_titular_familiar_txt_opcion11(path)
            by_key = {
                (t.get("correo") or "").strip().lower(): t for t in existentes
            }
            for t in titulares:
                key = (t.get("correo") or "").strip().lower()
                if not t.get("miembros_invitar") and key in by_key:
                    t["miembros_invitar"] = list(by_key[key].get("miembros_invitar") or [])
        except Exception:
            pass

    lines = [
        "# Cuentas Familiares Titulares de Tidal (Nigeria)",
        "# Formato por bloques:",
        "# TITULAR",
        "# correo_titular, miembros_actuales, estado, [miembros_detalles]",
        "# MIEMBROS:",
        "# miembro1@gmail.com",
        "#",
    ]
    for t in titulares:
        lines.append("TITULAR")
        lines.append(f"{t['correo']}, {t['usados']}, {t['estado']}, {t.get('miembros', [])}")
        lines.append("MIEMBROS:")
        for m in (t.get("miembros_invitar") or []):
            lines.append(str(m))
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
def _pw_error_types():
    from playwright.sync_api import Error as PlaywrightError
    return (PlaywrightError,)

def _frames_visibles(page):
    try:
        return list(page.frames)
    except _pw_error_types():
        return []

def hacer_clic_humanizado(page, loc) -> bool:
    """Mueve el mouse simulando trayectorias humanas antes de hacer clic en un elemento."""
    import random
    from playwright.sync_api import Error as PWErr
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        loc.scroll_into_view_if_needed(timeout=2000)
        box = loc.bounding_box()
        if not box:
            loc.click(timeout=5000, force=True)
            return True

        target_x = box["x"] + box["width"] * random.uniform(0.2, 0.8)
        target_y = box["y"] + box["height"] * random.uniform(0.2, 0.8)

        pasos = random.randint(4, 8)
        page.mouse.move(target_x, target_y, steps=pasos)
        time.sleep(random.uniform(0.08, 0.18))
        page.mouse.click(target_x, target_y)
        return True
    except (PlaywrightTimeout, PWErr, Exception):
        try:
            loc.click(timeout=5000, force=True)
            return True
        except Exception:
            return False

def _normalizar_correo_invitacion(email: str) -> str:
    """Normaliza correo para comparar miembros en el plan familiar de Tidal.

    NO se quitan los puntos de Gmail: en Tidal getmu.shroom03.03 y getmushr.o.om0303
    son cuentas distintas. Quitar puntos hacía que el segundo se saltara como 'ya figura'
    (opción 9 / invitador, log getmushr.o.om0303).
    """
    return (email or "").strip().lower().rstrip(".")


def _miembro_presente_en_pagina_familia(page, email_objetivo: str) -> bool:
    """True si el correo EXACTO (conservando puntos) aparece en la página Family."""
    objetivo = _normalizar_correo_invitacion(email_objetivo)
    if not objetivo:
        return False
    try:
        return bool(page.evaluate("""(objetivo) => {
            const body = document.body ? document.body.innerText : '';
            const emailRegex = /[a-zA-Z0-9._%+*\\-]+@[a-zA-Z0-9.*\\-]+\\.[a-zA-Z0-9.*\\-]+/g;
            const matches = body.match(emailRegex) || [];
            const norm = (e) => (e || '').trim().toLowerCase().replace(/[\\s.,]+$/, '');
            const target = norm(objetivo);
            return matches.some(m => norm(m) === target);
        }""", email_objetivo))
    except Exception:
        return False


def _alias_gmail_hermano_en_plan(page, email_objetivo: str) -> str | None:
    """Si en /family ya hay otro alias con puntos del mismo buzón Gmail, lo devuelve.

    No implica que el objetivo esté invitado: Tidal trata cada variante con puntos
    como cuenta distinta; solo sirve para avisar antes de invitar.
    """
    objetivo = _normalizar_correo_invitacion(email_objetivo)
    if not objetivo or ("@gmail.com" not in objetivo and "@googlemail.com" not in objetivo):
        return None
    try:
        return page.evaluate("""(objetivo) => {
            const body = document.body ? document.body.innerText : '';
            const emailRegex = /[a-zA-Z0-9._%+\\-]+@[a-zA-Z0-9.\\-]+\\.[a-zA-Z]{2,}/g;
            const matches = body.match(emailRegex) || [];
            const stripDots = (e) => {
                e = (e || '').trim().toLowerCase().replace(/[\\s.,]+$/, '');
                const i = e.indexOf('@');
                if (i < 0) return e;
                let local = e.slice(0, i), dom = e.slice(i + 1);
                if (dom === 'gmail.com' || dom === 'googlemail.com') local = local.replace(/\\./g, '');
                return local + '@' + dom;
            };
            const exact = (e) => (e || '').trim().toLowerCase().replace(/[\\s.,]+$/, '');
            const target = stripDots(objetivo);
            const targetExact = exact(objetivo);
            for (const m of matches) {
                const mClean = exact(m);
                if (!mClean || mClean === targetExact) continue;
                if (stripDots(m) === target) return mClean;
            }
            return null;
        }""", email_objetivo) or None
    except Exception:
        return None


def _recargar_pagina_familia(page) -> bool:
    """Recarga account.tidal.com/family como pide el propio mensaje de error de Tidal."""
    try:
        page.goto("https://account.tidal.com/family", wait_until="domcontentloaded", timeout=30000)
        time.sleep(2.0)
        aceptar_cookies_con_espera(page)
        # Esperar contenido útil
        limite = time.time() + 12.0
        while time.time() < limite:
            try:
                txt = page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''")
                if "familia" in txt or "family" in txt or "miembro" in txt or "member" in txt:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return True
    except Exception as e:
        print(f"    [Invitar] [WARN] No se pudo recargar /family: {e}")
        return False


def _cerrar_alerta_error_familia(page) -> None:
    """Intenta cerrar toasts/modales de error para no contaminar el siguiente intento."""
    try:
        page.evaluate("""() => {
            const closeKws = ['cerrar', 'close', 'ok', 'aceptar', 'dismiss'];
            const nodes = Array.from(document.querySelectorAll('button, [role="button"], [aria-label]'));
            for (const el of nodes) {
                const t = ((el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).trim().toLowerCase();
                if (!t) continue;
                if (t === '×' || t === 'x' || closeKws.some(k => t === k || t.includes(k))) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 && r.width < 120 && r.height < 120) {
                        try { el.click(); } catch (e) {}
                    }
                }
            }
        }""")
    except Exception:
        pass


def invitar_miembro_plan_familiar_tid(
    page,
    email_objetivo: str,
    *,
    pausa_s: float = 0.45,
) -> str:
    """
    En account.tidal.com/family: localiza la sección de invitar,
    rellena el correo del miembro y envía la invitación.

    Devuelve: 'ok' | 'ya_miembro' | 'reintentar' | 'fallo'
    - ok: invitación enviada (campo vacío o mensaje de éxito)
    - ya_miembro: Tidal dice que ya está / pertenece (cuenta como éxito)
    - reintentar: error inesperado / pide recargar (suele ser falso negativo)
    - fallo: error duro o no se pudo operar la UI
    """
    from playwright.sync_api import Error as PWErr

    email_objetivo = (email_objetivo or "").strip()
    if not email_objetivo:
        return "fallo"

    try:
        if page.is_closed():
            return "fallo"
    except PWErr:
        return "fallo"

    try:
        aceptar_cookies_con_espera(page, intentos=1, pausa_s=0.1)
    except Exception:
        pass

    # Si el miembro ya está en la lista, no volver a invitar (evita el "error inesperado")
    if _miembro_presente_en_pagina_familia(page, email_objetivo):
        print(f"    [Invitar] El correo {email_objetivo} ya figura en el plan familiar.")
        return "ya_miembro"

    selectores_input = [
        'input[type="email"]',
        'input[placeholder*="Correo electrónico" i]',
        'input[placeholder*="email" i]',
        'input[placeholder*="Email" i]',
        'input[id*="email" i]',
        'input[name*="email" i]',
    ]

    nombres_boton_abrir = (
        re.compile(r"invitar\s+a\s+un\s+familiar", re.I),
        re.compile(r"invite\s+a\s+family\s+member", re.I),
        re.compile(r"añadir\s+familiar", re.I),
        re.compile(r"add\s+family\s+member", re.I),
        re.compile(r"invitar\s+miembro", re.I),
        re.compile(r"invite\s+member", re.I),
        re.compile(r"agregar\s+miembro", re.I),
        re.compile(r"añadir\s+a\s+un\s+familiar", re.I),
    )

    # 1) Encontrar el input. Si no está visible, buscar el botón para abrir el formulario
    target_frame = None
    input_loc = None

    for frame in _frames_visibles(page):
        for sel in selectores_input:
            try:
                loc = frame.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=400):
                    input_loc = loc
                    target_frame = frame
                    break
            except Exception:
                continue
        if input_loc:
            break

    if not input_loc:
        for frame in _frames_visibles(page):
            for rx in nombres_boton_abrir:
                try:
                    btn = frame.get_by_role("button", name=rx).first
                    if btn.count() > 0 and btn.is_visible(timeout=400):
                        hacer_clic_humanizado(page, btn)
                        time.sleep(pausa_s + 0.2)
                        break
                    lnk = frame.get_by_role("link", name=rx).first
                    if lnk.count() > 0 and lnk.is_visible(timeout=400):
                        hacer_clic_humanizado(page, lnk)
                        time.sleep(pausa_s + 0.2)
                        break
                except Exception:
                    continue

            for sel in selectores_input:
                try:
                    loc = frame.locator(sel).first
                    if loc.count() > 0 and loc.is_visible(timeout=600):
                        input_loc = loc
                        target_frame = frame
                        break
                except Exception:
                    continue
            if input_loc:
                break

    if not input_loc or not target_frame:
        print("    [Invitar] Error: No se encontró el campo de correo para invitar.")
        return "fallo"

    # 2) Rellenar el correo
    print(f"    [Invitar] Escribiendo correo: {email_objetivo}")
    try:
        input_loc.focus()
        input_loc.fill("")
        input_loc.fill(email_objetivo)
        try:
            input_loc.dispatch_event("input")
            input_loc.dispatch_event("change")
        except Exception:
            pass
    except Exception:
        if not rellenar_campo_humanizado(input_loc, email_objetivo):
            print("    [Invitar] Error al escribir el correo.")
            return "fallo"

    time.sleep(pausa_s + 0.2)

    # 3) Encontrar el botón "Invitar" / "Invite" (texto exacto para no pulsar "Invitar a un familiar")
    nombres_boton_invitar = (
        re.compile(r"^\s*invitar\s*$", re.I),
        re.compile(r"^\s*invite\s*$", re.I),
        re.compile(r"^\s*enviar\s*$", re.I),
        re.compile(r"^\s*send\s*$", re.I),
    )
    selectores_boton_invitar = [
        "form button[type='submit']",
        "button[type='submit']",
        'button:text-is("Invitar")',
        'button:text-is("Invite")',
        'button:text-is("Enviar")',
        'button:text-is("Send")',
        "input[type='submit'][value*='Invitar' i]",
        "input[type='submit'][value*='Invite' i]",
    ]

    button_loc = None
    for rx in nombres_boton_invitar:
        try:
            loc = target_frame.get_by_role("button", name=rx).first
            if loc.count() > 0 and loc.is_visible(timeout=800):
                button_loc = loc
                break
        except Exception:
            continue

    if not button_loc:
        for sel in selectores_boton_invitar:
            try:
                loc = target_frame.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=500):
                    # Evitar el botón grande de abrir formulario
                    try:
                        txt = (loc.inner_text() or "").strip().lower()
                    except Exception:
                        txt = ""
                    # Evitar el botón grande "Invitar a un familiar" / "Add family member"
                    if len(txt) > 12 and ("familiar" in txt or "family member" in txt or "add member" in txt):
                        continue
                    button_loc = loc
                    break
            except Exception:
                continue

    if not button_loc:
        print("    [Invitar] Error: No se encontró el botón de enviar invitación.")
        return "fallo"

    try:
        button_loc.wait_for_element_state("enabled", timeout=3000)
    except Exception:
        pass

    # 4) Pulsar el botón
    print("    [Invitar] Pulsando botón de enviar...")
    if not hacer_clic_humanizado(page, button_loc):
        try:
            button_loc.click(force=True)
        except Exception:
            print("    [Invitar] Error al hacer clic en el botón de enviar.")
            return "fallo"

    time.sleep(pausa_s + 0.8)

    # 5) Clasificar resultado. El regex genérico r"error" era demasiado amplio y
    # trataba el "error inesperado" (que Tidal muestra aunque a veces sí añade al miembro)
    # como fallo definitivo sin recargar.
    alertas_exito = (
        re.compile(r"invitaci[oó]n\s+enviada", re.I),
        re.compile(r"invitation\s+sent", re.I),
        re.compile(r"enviada\s+con\s+[eé]xito", re.I),
        re.compile(r"invite\s+sent", re.I),
    )
    alertas_ya_miembro = (
        re.compile(r"ya\s+(est[aá]|pertenece|forma\s+parte)", re.I),
        re.compile(r"already\s+(in|a\s+member|belongs)", re.I),
        re.compile(r"already\s+invited", re.I),
        re.compile(r"ya\s+invitad", re.I),
    )
    alertas_reintentar = (
        re.compile(r"error\s+inesperado", re.I),
        re.compile(r"unexpected\s+error", re.I),
        re.compile(r"no\s+hemos\s+podido\s+a[nñ]adir", re.I),
        re.compile(r"couldn'?t\s+add", re.I),
        re.compile(r"vuelve\s+a\s+cargar", re.I),
        re.compile(r"reload\s+the\s+page", re.I),
        re.compile(r"int[eé]ntalo\s+de\s+nuevo", re.I),
        re.compile(r"try\s+again", re.I),
    )
    alertas_fallo_duro = (
        re.compile(r"l[ií]mite", re.I),
        re.compile(r"\blimit\b", re.I),
        re.compile(r"inv[aá]lid", re.I),
        re.compile(r"no\s+v[aá]lid", re.I),
        re.compile(r"plan\s+lleno", re.I),
        re.compile(r"family\s+is\s+full", re.I),
        re.compile(r"5\s*(?:de|of)\s*5", re.I),
    )

    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        try:
            val = input_loc.input_value(timeout=300)
            if not val or val.strip() == "":
                print("    [Invitar] Confirmado: El campo se ha vaciado.")
                return "ok"
        except Exception:
            # El formulario se desmontó: suele significar éxito
            if _miembro_presente_en_pagina_familia(page, email_objetivo):
                return "ok"
            return "ok"

        for frame in _frames_visibles(page):
            for rx in alertas_exito:
                try:
                    loc = frame.get_by_text(rx)
                    if loc.count() > 0 and loc.first.is_visible(timeout=150):
                        print(f"    [Invitar] Mensaje de éxito detectado: '{loc.first.inner_text()}'")
                        return "ok"
                except Exception:
                    continue

            for rx in alertas_ya_miembro:
                try:
                    loc = frame.get_by_text(rx)
                    if loc.count() > 0 and loc.first.is_visible(timeout=150):
                        print(f"    [Invitar] Ya estaba en el plan: '{loc.first.inner_text()}'")
                        return "ya_miembro"
                except Exception:
                    continue

            for rx in alertas_reintentar:
                try:
                    loc = frame.get_by_text(rx)
                    if loc.count() > 0 and loc.first.is_visible(timeout=150):
                        msg = loc.first.inner_text()
                        print(f"    [Invitar] ⚠️ Error transitorio de Tidal: '{msg}'")
                        return "reintentar"
                except Exception:
                    continue

            for rx in alertas_fallo_duro:
                try:
                    loc = frame.get_by_text(rx)
                    if loc.count() > 0 and loc.first.is_visible(timeout=150):
                        print(f"    [Invitar] ⚠️ Error definitivo: '{loc.first.inner_text()}'")
                        return "fallo"
                except Exception:
                    continue
        time.sleep(0.25)

    # Sin mensaje claro: comprobar si el miembro ya aparece (falso negativo frecuente)
    if _miembro_presente_en_pagina_familia(page, email_objetivo):
        print("    [Invitar] Sin toast claro, pero el correo ya figura en el plan.")
        return "ok"
    return "reintentar"


def invitar_miembro_plan_familiar_con_reintentos(
    page,
    email_objetivo: str,
    intentos: int = 3,
    pausa_s: float = 0.6,
) -> bool:
    """Envía la invitación con recarga de /family ante el error inesperado de Tidal.

    El propio mensaje de Tidal pide recargar; además, a menudo el miembro SÍ queda
    añadido aunque salga el toast de error — por eso se verifica la lista tras cada intento.
    """
    for intento in range(1, max(1, intentos) + 1):
        if intento > 1:
            print(f"    [Invitar] Reintento {intento}/{intentos} tras recargar la página Family...")
            _cerrar_alerta_error_familia(page)
            _recargar_pagina_familia(page)
            time.sleep(random.uniform(1.0, 2.0))

        # Verificación previa: si ya está, éxito
        if _miembro_presente_en_pagina_familia(page, email_objetivo):
            print(f"    [Invitar] {Color.GREEN}[OK] {email_objetivo} ya está en el plan familiar.{Color.ENDC}")
            return True

        resultado = invitar_miembro_plan_familiar_tid(page, email_objetivo, pausa_s=pausa_s)
        if resultado in ("ok", "ya_miembro"):
            time.sleep(1.0)
            if _miembro_presente_en_pagina_familia(page, email_objetivo):
                return True
            # Campo vacío / éxito toast pero lista aún no pintó: recargar y comprobar EXACTO
            _recargar_pagina_familia(page)
            if _miembro_presente_en_pagina_familia(page, email_objetivo):
                return True
            if resultado == "ya_miembro":
                # Toast engañoso frecuente con alias Gmail: Tidal dice "ya está" porque
                # getmu.shroom03.03 ya está, pero getmushr.o.om0303 (distintos puntos) no.
                hermano = _alias_gmail_hermano_en_plan(page, email_objetivo)
                if hermano:
                    print(f"    [Invitar] {Color.WARNING}[WARN] Toast 'ya miembro', pero en el plan "
                          f"está '{hermano}' (mismo buzón Gmail), no '{email_objetivo}'. "
                          f"Se sigue intentando invitar el alias exacto.{Color.ENDC}")
                else:
                    print(f"    [Invitar] {Color.WARNING}[WARN] Toast 'ya miembro' sin el correo "
                          f"exacto en la lista. Se reintenta.{Color.ENDC}")
                # No contar como éxito; dejar que el bucle reintente
            elif resultado == "ok":
                # Invitación pendiente a veces tarda en listar: confiar en éxito de UI
                return True

        if resultado == "fallo":
            print(f"    [Invitar] Fallo definitivo en intento {intento}/{intentos}.")
            # Última comprobación por si el error duro era engañoso
            _recargar_pagina_familia(page)
            if _miembro_presente_en_pagina_familia(page, email_objetivo):
                print(f"    [Invitar] {Color.GREEN}[OK] Pese al error, {email_objetivo} sí quedó en el plan.{Color.ENDC}")
                return True
            return False

        # resultado == "reintentar"
        print(f"    [Invitar] Tidal pidió reintentar (intento {intento}/{intentos}). Comprobando si el miembro entró de todos modos...")
        time.sleep(random.uniform(1.2, 2.2))
        _cerrar_alerta_error_familia(page)
        _recargar_pagina_familia(page)
        if _miembro_presente_en_pagina_familia(page, email_objetivo):
            print(f"    [Invitar] {Color.GREEN}[OK] Falso error: {email_objetivo} ya está en el plan tras recargar.{Color.ENDC}")
            return True
        time.sleep(pausa_s)

    # Última verificación global
    _recargar_pagina_familia(page)
    if _miembro_presente_en_pagina_familia(page, email_objetivo):
        print(f"    [Invitar] {Color.GREEN}[OK] {email_objetivo} confirmado en el plan tras los reintentos.{Color.ENDC}")
        return True
    return False

class TidalFamilyInviter:
    def __init__(self, queue_miembros: queue.Queue, client_email: str = "titular_familiar",
                 perfil_dir: Path | None = None):
        self.queue_miembros = queue_miembros
        self.playwright = None
        self.context = None
        self.page = None
        self.perfil_dir = Path(perfil_dir) if perfil_dir else (SCRIPT_DIR / "perfiles" / "familiar_titular")
        self.perfil_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        # manejar_bloqueos_e_intervencion() consulta estos atributos en page.manager para poder
        # rotar el proxy en vez de rendirse ante el antirobot.
        self.client_email = client_email or "titular_familiar"
        self.headless = False
        self.use_proxy = False
        self.proxy_pe_server = None
        self.proxy_pe_user = None
        self.proxy_pe_pass = None
        self._rotaciones_antibot = 0
        self._recuperaciones_error_tidal = 0

    def cerrar_recursos(self):
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        self.context = None
        self.page = None
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.playwright = None
        try:
            GLOBAL_PE_PROXY_POOL.liberar_proxy(self.proxy_pe_server)
        except Exception:
            pass
        self.proxy_pe_server = None

    def abrir_navegador(self):
        global GLOBAL_PE_PROXY_POOL

        if not self.proxy_pe_server:
            # Reservar el proxy en el pool evita que el invitador comparta IP con los hilos de
            # restablecimiento, que es lo que disparaba el antirobot en la ventana del titular.
            # Sin fallback a random.choice: escogía un proxy sin reservarlo en el pool y podía
            # repetir la IP de otra ventana, que es justo lo que dispara el antirobot.
            p_pe = GLOBAL_PE_PROXY_POOL.obtener_proxy_unico()
            if not p_pe:
                # Sin este corte el invitador abría Tidal por la IP real y la dejaba marcada por DataDome
                raise RuntimeError("Sin proxies de Perú disponibles para el invitador familiar. Se aborta antes de exponer tu IP real.")
            self.proxy_pe_server = p_pe.get("server", "")
            self.proxy_pe_user = p_pe.get("username", "")
            self.proxy_pe_pass = p_pe.get("password", "")
        self.use_proxy = True

        reparar_perfil_corrupto(self.perfil_dir)

        p_serv = self.proxy_pe_server
        if p_serv and not p_serv.startswith("http"):
            p_serv = "http://" + p_serv

        launch_kwargs = {
            "user_data_dir": str(self.perfil_dir),
            "headless": self.headless,
            # El invitador usaba una lista propia de flags sin las medidas antidetección del resto
            # del script, así que su ventana era la más fácil de marcar por DataDome.
            "args": list(CHROME_SILENT_ARGS),
            "ignore_default_args": ["--enable-automation", "--no-sandbox"],
            "viewport": {"width": 1280, "height": 800},
            "locale": "es-ES",
            "channel": "chrome",
            "proxy": {
                "server": p_serv,
                "username": self.proxy_pe_user or "",
                "password": self.proxy_pe_pass or ""
            }
        }
        print(f"  [Inviter Titular] Conectando mediante proxy de Perú: {p_serv}")

        from playwright.sync_api import sync_playwright
        if not self.playwright:
            self.playwright = sync_playwright().start()
        try:
            self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:
            print(f"  [Inviter Titular] [WARN] Falló el lanzamiento: {e}. Reparando perfil y reintentando...")
            reparar_perfil_corrupto(self.perfil_dir)
            time.sleep(2.0)
            self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        self.context.set_default_navigation_timeout(45000)
        self.context.set_default_timeout(35000)
        self.context.add_init_script(STEALTH_SCRIPT)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.manager = self
        self.page.client_email = self.client_email
        self.page.bring_to_front()

    def rotar_proxy_contexto(self, tipo="PE"):
        global GLOBAL_PE_PROXY_POOL
        print("\n  [Proxy Rotation PE] Rotando proxy del invitador familiar por bloqueo antirobot...")
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.context = None
        self.page = None
        self.playwright = None
        time.sleep(2.0)

        # El perfil persistente guarda las cookies de bloqueo de DataDome; reutilizarlo arrastraría
        # el bloqueo al proxy nuevo y la rotación no serviría de nada.
        try:
            shutil.rmtree(self.perfil_dir, ignore_errors=True)
        except Exception:
            pass
        self.perfil_dir.mkdir(parents=True, exist_ok=True)

        p_pe = GLOBAL_PE_PROXY_POOL.rotar_y_marcar_bloqueado(self.proxy_pe_server)
        if not p_pe:
            raise RuntimeError("No quedan proxies de Perú limpios para el invitador familiar. Se aborta en vez de usar tu IP real.")
        self.proxy_pe_server = p_pe.get("server", "")
        self.proxy_pe_user = p_pe.get("username", "")
        self.proxy_pe_pass = p_pe.get("password", "")
        print(f"  [Proxy Rotation PE] Nuevo proxy PE configurado para el titular: {self.proxy_pe_server}")

        self.abrir_navegador()

    def recuperar_login_tras_error_tidal(self) -> bool:
        """Tras 'Algo salió mal' en /signin: NO recargar esa URL; pasar por pricing→account/login."""
        try:
            self.page = pagina_vigente(self.page)
            if not self.page or self.page.is_closed():
                self.abrir_navegador()
            print(f"  [Inviter] [{self.client_email}] Recuperando flujo: tidal.com/pricing → account.tidal.com/login...")
            navegar_tidal_tolerante(self.page, "https://tidal.com/pricing", timeout_ms=30000)
            time.sleep(random.uniform(1.5, 2.5))
            aceptar_cookies_con_espera(self.page)
            time.sleep(0.4)
            navegar_tidal_tolerante(
                self.page, "https://account.tidal.com/login",
                referer="https://tidal.com/pricing",
                timeout_ms=30000
            )
            time.sleep(2.0)
            self.page = pagina_vigente(self.page)
            if es_pantalla_error_login_tidal(self.page):
                return False
            return bool(encontrar_locator_en_frames(
                self.page,
                ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]', '#email']
            )) or (
                url_es_login_o_cuenta(self.page.url or "")
                and not es_pantalla_error_login_tidal(self.page)
            )
        except Exception as e:
            print(f"  [Inviter] [{self.client_email}] [WARN] recuperar_login_tras_error_tidal: {e}")
            return False

    def ejecutar_rotacion_proxy_y_recargar(self):
        self.rotar_proxy_contexto()
        # NUNCA recargar login.tidal.com/signin (reproduce 'Algo salió mal').
        print(f"  [Auto-Proxy PE] [{self.client_email}] Reabriendo login vía pricing (no se recarga /signin)...")
        try:
            if not self.recuperar_login_tras_error_tidal():
                navegar_tidal_tolerante(
                    self.page, "https://account.tidal.com/login",
                    referer="https://tidal.com/pricing",
                    timeout_ms=30000
                )
                time.sleep(2.0)
        except Exception as e:
            print(f"  [Auto-Proxy] [WARN] Error al reabrir login tras rotar el proxy del titular: {e}")

    def logout_titular(self):
        try:
            if self.context:
                try:
                    self.context.clear_cookies()
                except Exception:
                    pass
            if self.page and not self.page.is_closed():
                try:
                    self.page.evaluate("""() => {
                        try { localStorage.clear(); } catch(e) {}
                        try { sessionStorage.clear(); } catch(e) {}
                    }""")
                except Exception:
                    pass
            if self.context:
                try:
                    self.context.clear_cookies()
                except Exception:
                    pass
            print("  [Inviter] Sesión del titular cerrada y almacenamiento/cookies limpiadas con éxito (sin URL de logout).")
        except Exception as e:
            print(f"  [Inviter] [WARN] Error al limpiar sesión del titular: {e}")

    def sincronizar_y_validar_cupos_titular(self, titular, titulares, path) -> bool:
        """Lee los miembros reales en la página de Tidal y actualiza titular_familiar.txt.
        Si la cuenta está llena, cierra sesión y retorna False."""
        try:
            curr_url = self.page.url.lower()
            if "family" not in curr_url or "/login" in curr_url:
                try:
                    self.page.goto("https://account.tidal.com/family", wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
                
            # Esperar a que la página de familia cargue completamente (desaparición de spinner y presencia de textos)
            start_wait = time.time()
            loaded = False
            while time.time() - start_wait < 12.0:
                try:
                    loaded = self.page.evaluate("""() => {
                        const isFamilyUrl = window.location.href.toLowerCase().includes('family');
                        if (!isFamilyUrl) return false;
                        
                        const text = document.body ? document.body.innerText.toLowerCase() : '';
                        const hasContent = text.includes('familia') || text.includes('family') || text.includes('miembro') || text.includes('member');
                        const spinner = document.querySelector('[class*="spinner" i], [class*="loading" i]');
                        return hasContent && !spinner;
                    }""")
                    if loaded:
                        break
                except Exception:
                    pass
                time.sleep(0.5)
                
            if not loaded:
                curr_url = self.page.url.lower()
                if "family" not in curr_url:
                    print(f"  [Inviter] [WARN] No se cargó la página de familia (URL: {curr_url}). Omitiendo sincronización preventiva.")
                    return True
                
            aceptar_cookies_con_espera(self.page)
            
            # Evaluar estado de miembros reales, textos de cupo y presencia de botón "Agregar"
            resultado = self.page.evaluate("""(titularCorreo) => {
                const bodyText = document.body ? document.body.innerText : '';
                
                // Regex tolerante a asteriscos para correos enmascarados
                const emailRegex = /[a-zA-Z0-9._%+*\\-]+@[a-zA-Z0-9.*\\-]+\\.[a-zA-Z0-9.*\\-]+/g;
                const rawMatches = bodyText.match(emailRegex) || [];
                
                const cleanedSet = new Set();
                rawMatches.forEach(email => {
                    let clean = email.trim().replace(/[\\s.,]+$/, '').toLowerCase();
                    if (clean && clean.includes('@') && clean !== titularCorreo.toLowerCase()) {
                        cleanedSet.add(clean);
                    }
                });
                const foundEmails = Array.from(cleanedSet);
                
                // Texto de plan lleno
                const isFullText = /\\b5\\s*(?:de|of)\\s*5\\b/i.test(bodyText);
                
                // Botón agregar/invitar
                const addKws = [
                    'invitar a un familiar', 'invitar familiar', 'invitar miembro', 
                    'agregar miembro', 'agregar familiar', 'añadir a un familiar', 
                    'añadir familiar', 'add family member', 'add member', 'add family', 
                    'invite family member', 'invite member', 'invite family', 'add email'
                ];
                const buttons = Array.from(document.querySelectorAll('button, a, div, span, p, [role="button"]'));
                const hasAddButton = buttons.some(el => {
                    const t = (el.textContent || '').trim().toLowerCase();
                    return addKws.some(kw => t.includes(kw));
                });
                
                return {
                    miembros: foundEmails,
                    isFullText: isFullText,
                    hasAddButton: hasAddButton
                };
            }""", titular["correo"])
            
            raw_miembros = resultado.get("miembros", [])
            miembros_reales = []
            titular_l = (titular.get("correo") or "").strip().lower()
            for m in raw_miembros:
                m_clean = m.strip().rstrip('.').lower()
                # Exacto con puntos: no colapsar aliases Gmail del titular
                if m_clean and m_clean != titular_l and m_clean not in miembros_reales:
                    if not correos_iguales_exacto(m_clean, titular_l):
                        miembros_reales.append(m_clean)

            is_full_text = resultado.get("isFullText", False)
            has_add_button = resultado.get("hasAddButton", False)
            
            print(f"  [Inviter] Miembros detectados en Tidal para {titular['correo']}: {miembros_reales}")
            print(f"  [Inviter] ¿Texto de cupo lleno?: {is_full_text}, ¿Tiene botón de agregar?: {has_add_button}")
            
            # Se considera lleno únicamente si no hay botón de agregar Y (tiene 5 o más correos o el texto lo afirma)
            esta_lleno = (len(miembros_reales) >= 5 or is_full_text) and not has_add_button
            
            if esta_lleno:
                titular["estado"] = "lleno"
                # Rellenar con placeholders si faltan de la extracción
                while len(miembros_reales) < 5:
                    miembros_reales.append(f"miembro_ocupado_{len(miembros_reales)+1}@tidal.com")
                titular["miembros"] = miembros_reales
                titular["usados"] = 5
                
                print(f"  [Inviter] {Color.WARNING}¡La cuenta {titular['correo']} está llena (5/5)! Cerrando sesión...{Color.ENDC}")
                guardar_titulares_familiares(titulares, path)
                self.logout_titular()
                return False
            else:
                titular["estado"] = "disponible"
                titular["miembros"] = miembros_reales
                titular["usados"] = len(miembros_reales)
                guardar_titulares_familiares(titulares, path)
                return True
        except Exception as e:
            print(f"  [Inviter] [WARN] Error al sincronizar cupos: {e}")
            return True

    @staticmethod
    def _normalizar_correo(email: str) -> str:
        """Normaliza para comparar titulares: minúsculas, SIN quitar puntos de Gmail.

        s.oftcake78.8 y s.of.tcake7.88 son titulares distintos en Tidal.
        """
        return (email or "").strip().lower().rstrip(".")

    def _sesion_titular_activa(self, titular) -> bool:
        """Comprueba contra Tidal que la sesión abierta pertenece al titular esperado."""
        try:
            curr_url = self.page.url.lower()
            if "account.tidal.com" in curr_url and "/login" not in curr_url and "family" in curr_url:
                # En /family hace falta verificar el correo del perfil: no asumir por URL
                pass

            self.page.goto("https://account.tidal.com/profile", wait_until="domcontentloaded", timeout=30000)
            manejar_bloqueos_e_intervencion(self.page, f"Invitador Titular ({titular['correo']})")
            self.page = pagina_vigente(self.page)
            aceptar_cookies_con_espera(self.page)

            # Con proxy el perfil puede tardar varios segundos en pintar el correo; el chequeo
            # instantáneo anterior daba "sesión incorrecta" antes de que la página cargara.
            limite = time.time() + 15.0
            email_detectado = ""
            while time.time() < limite:
                curr_url = self.page.url.lower()
                if "login" in curr_url or "authorize" in curr_url:
                    return False
                email_detectado = self.page.evaluate("""() => {
                    // Preferir el campo de correo del perfil (no el primer email del body,
                    // que puede ser un miembro del plan familiar).
                    const el = document.querySelector('input[type="email"], input[name="email"], #email');
                    if (el && el.value) return el.value.trim().toLowerCase();
                    const labels = Array.from(document.querySelectorAll('label, span, p, div, dt, dd'));
                    for (const n of labels) {
                        const t = (n.textContent || '').trim().toLowerCase();
                        if (t === 'correo electrónico' || t === 'email' || t === 'e-mail') {
                            const sib = n.parentElement;
                            if (sib) {
                                const m = (sib.innerText || '').match(
                                    /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}/
                                );
                                if (m) return m[0].trim().toLowerCase();
                            }
                        }
                    }
                    return '';
                }""")
                if email_detectado:
                    break
                time.sleep(1.0)

            if not email_detectado:
                return False

            print(f"  [Inviter] Sesión activa detectada en Chrome para: {email_detectado}")
            # EXACTO con puntos: no usar son_correos_equivalentes / quitar puntos
            if correos_iguales_exacto(email_detectado, titular["correo"]):
                print(f"  [Inviter] {Color.GREEN}Sesión confirmada para el titular correcto: {titular['correo']}{Color.ENDC}")
                return True

            print(f"  [Inviter] Sesión de cuenta incorrecta ({email_detectado} ≠ {titular['correo']}). "
                  f"Cerrando sesión...")
            self.logout_titular()
        except Exception as e:
            print(f"  [Inviter] [WARN] Error al verificar sesión activa: {e}")
        return False

    def _esperar_fin_de_login(self, titular, timeout_s: float = 75.0) -> bool:
        """Espera a que Tidal termine de establecer la sesión tras enviar el código.

        La espera fija de 4 s no alcanzaba a través de un proxy de Perú: se navegaba a /family
        antes de tener sesión y Tidal rebotaba al login, dando el proceso por fallido.
        """
        limite = time.time() + timeout_s
        continuaciones = 0
        while time.time() < limite:
            self.page = pagina_vigente(self.page)
            try:
                url = self.page.url.lower()
            except Exception:
                time.sleep(1.5)
                continue

            if "account.tidal.com" in url and "/login" not in url:
                return True
            if "listen.tidal.com" in url or "my.tidal.com" in url:
                return True

            try:
                if detectar_pantalla_antirobot(self.page):
                    manejar_bloqueos_e_intervencion(self.page, f"Invitador Titular ({titular['correo']})")
                    self.page = pagina_vigente(self.page)
                    time.sleep(1.5)
                    continue
            except RuntimeError:
                raise
            except Exception:
                pass

            try:
                estado = self.page.evaluate("""() => {
                    const texto = document.body ? document.body.innerText.toLowerCase() : '';
                    const errorKws = ['código no válido', 'codigo no valido', 'invalid code', 'incorrect code',
                                      'código incorrecto', 'ha caducado', 'expired', 'try again'];
                    return {
                        error: errorKws.some(k => texto.includes(k)),
                        continuar: /\\b(continuar|continue|permitir|allow|aceptar y continuar)\\b/i.test(texto)
                    };
                }""")
            except Exception:
                estado = {}

            if estado.get("error"):
                print(f"  [Inviter] {Color.WARNING}Tidal rechazó el código de inicio de sesión. Se pedirá uno nuevo.{Color.ENDC}")
                return False

            if estado.get("continuar") and continuaciones < 2:
                continuaciones += 1
                btn = esperar_locator_en_frames(
                    self.page,
                    ["button:has-text('Continuar')", "button:has-text('Continue')",
                     "button:has-text('Permitir')", "button:has-text('Allow')", "button[type='submit']"],
                    timeout_s=4.0
                )
                if btn:
                    try:
                        btn.click()
                        time.sleep(2.0)
                        continue
                    except Exception:
                        pass

            time.sleep(1.5)

        try:
            print(f"  [Inviter] [WARN] El login del titular no llegó a completarse (URL actual: {self.page.url}).")
        except Exception:
            pass
        return False

    def _abrir_panel_familia(self, titular) -> bool:
        for intento in range(1, 4):
            try:
                self.page.goto("https://account.tidal.com/family", wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"  [Inviter] [WARN] Intento {intento}/3 de abrir el panel familiar falló: {e}")
                time.sleep(3.0)
                continue

            manejar_bloqueos_e_intervencion(self.page, f"Invitador Titular ({titular['correo']})")
            self.page = pagina_vigente(self.page)
            aceptar_cookies_con_espera(self.page)

            limite = time.time() + 15.0
            while time.time() < limite:
                try:
                    url = self.page.url.lower()
                except Exception:
                    break
                if "login" in url or "authorize" in url:
                    break
                if "/family" in url:
                    print(f"  [Inviter] {Color.GREEN}Sesión iniciada con éxito para titular: {titular['correo']}{Color.ENDC}")
                    return True
                time.sleep(1.0)
            time.sleep(2.0)
        return False

    def _login_titular_una_vez(self, titular) -> bool:
        """Login titular con bypass de reputación, antibot y reintento ante túnel muerto.

        En frío a account.tidal.com/ (sin pricing) DataDome suele tapar el formulario.
        Ante ERR_TUNNEL se rota proxy; tras el código se espera sesión real antes de /family.
        """
        max_intentos_nav = 4
        ultimo_error = None
        for intento_nav in range(1, max_intentos_nav + 1):
            try:
                if self._login_titular_flujo(titular):
                    return True
                # Flujo llegó al final sin sesión: reintentar con IP limpia si queda cupo
                if intento_nav < max_intentos_nav:
                    print(f"  [Inviter] [{titular['correo']}] Login incompleto "
                          f"(intento {intento_nav}/{max_intentos_nav}). Rotando proxy PE...")
                    self.logout_titular()
                    self.rotar_proxy_contexto()
                    continue
            except RuntimeError as e:
                # Antibot persistente / sin proxies: no insistir
                print(f"  [Inviter] [{titular['correo']}] Login abortado: {e}")
                return False
            except Exception as e:
                ultimo_error = e
                if es_error_proxy_o_red(e) and intento_nav < max_intentos_nav:
                    print(f"  [Inviter] [{titular['correo']}] Fallo de túnel/proxy "
                          f"({e}). Rotando IP ({intento_nav}/{max_intentos_nav})...")
                    try:
                        self.rotar_proxy_contexto()
                    except RuntimeError as e_rot:
                        print(f"  [Inviter] [{titular['correo']}] Sin proxies limpios: {e_rot}")
                        return False
                    continue
                if es_error_navegacion_abortada(e) and intento_nav < max_intentos_nav:
                    print(f"  [Inviter] [{titular['correo']}] Navegación abortada; "
                          f"reintentando mismo proxy ({intento_nav}/{max_intentos_nav})...")
                    time.sleep(1.5)
                    continue
                print(f"  [Inviter] [{titular['correo']}] Excepción en login: {e}")
                if intento_nav < max_intentos_nav:
                    time.sleep(2.0)
                    continue
                return False
        if ultimo_error:
            print(f"  [Inviter] [{titular['correo']}] Login agotó reintentos: {ultimo_error}")
        return False

    def _cambiar_a_modo_codigo_si_hay_password(self, titular) -> None:
        """Si Tidal muestra contraseña, intentar pasar a 'iniciar sesión sin contraseña'."""
        pwd_input = encontrar_locator_en_frames(
            self.page, ['input[type="password"]', 'input[name="password"]']
        )
        if not pwd_input:
            return
        print(f"  [Inviter] [{titular['correo']}] Pantalla de contraseña detectada. "
              f"Cambiando a inicio por código...")
        btn_code_mode = esperar_locator_en_frames(
            self.page,
            ["a:has-text('contraseña')", "button:has-text('contraseña')",
             "a:has-text('código')", "button:has-text('código')",
             "a:has-text('code')", "button:has-text('code')",
             "text='Inicia sesión sin contraseña'", "text='Sign in without password'",
             "text='Inicia sesión con un código'", "text='Sign in with a code'"],
            timeout_s=6.0
        )
        if btn_code_mode:
            try:
                btn_code_mode.click()
            except Exception:
                try:
                    btn_code_mode.evaluate("el => el.click()")
                except Exception:
                    pass
            time.sleep(2.5)
            self.page = pagina_vigente(self.page)

    def _login_titular_flujo(self, titular) -> bool:
        # Calentar la reputación de la IP antes de tocar el login
        print(f"  [Bypass] [{titular['correo']}] Calentando reputación en tidal.com/pricing...")
        navegar_tidal_tolerante(self.page, "https://tidal.com/pricing", timeout_ms=30000)
        manejar_bloqueos_e_intervencion(self.page, "Invitador Titular (Calentamiento)")
        self.page = pagina_vigente(self.page)
        time.sleep(random.uniform(2.0, 3.5))
        aceptar_cookies_con_espera(self.page)
        time.sleep(random.uniform(0.5, 1.0))

        print(f"  [Bypass] [{titular['correo']}] Entrando a account.tidal.com/login con referer...")
        navegar_tidal_tolerante(
            self.page, "https://account.tidal.com/login",
            referer="https://tidal.com/pricing",
            timeout_ms=30000
        )
        manejar_bloqueos_e_intervencion(self.page, f"Invitador Titular ({titular['correo']})")
        self.page = pagina_vigente(self.page)
        aceptar_cookies_con_espera(self.page)

        if es_pantalla_error_login_tidal(self.page):
            print(f"  [Inviter] [{titular['correo']}] Pantalla 'Algo salió mal'. Recuperando vía pricing...")
            if not self.recuperar_login_tras_error_tidal():
                return False
            manejar_bloqueos_e_intervencion(self.page, f"Invitador Titular ({titular['correo']})")
            self.page = pagina_vigente(self.page)

        email_selectors = ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]', '#email']
        email_input = esperar_locator_en_frames(self.page, email_selectors, timeout_s=20.0)
        if not email_input:
            print(f"  [Inviter] [WARN] No apareció el campo de correo para {titular['correo']}.")
            return False

        # Baseline ANTES de pedir el código (evitar usar un código viejo del buzón)
        base_id = obtener_max_email_id(titular["correo"], "tidal")

        rellenar_campo_humanizado(email_input, titular["correo"])
        time.sleep(0.5)

        btn_continue = esperar_locator_en_frames(
            self.page,
            ["button:has-text('Continuar')", "button:has-text('Continue')", "button[type='submit']"],
            timeout_s=10.0
        )
        if btn_continue:
            try:
                btn_continue.click()
            except Exception:
                try:
                    btn_continue.evaluate("el => el.click()")
                except Exception:
                    pass
        else:
            try:
                email_input.press("Enter")
            except Exception:
                print("  [Inviter] [WARN] No apareció el botón 'Continuar' en el login del titular.")
                return False

        time.sleep(2.0)
        manejar_bloqueos_e_intervencion(self.page, f"Invitador Titular post-email ({titular['correo']})")
        self.page = pagina_vigente(self.page)
        self._cambiar_a_modo_codigo_si_hay_password(titular)

        print(f"  [Inviter] [{titular['correo']}] Esperando enlace/código de inicio de sesión vía IMAP...")
        code_or_link = None
        for intento in range(1, 15):
            print(f"  [Inviter] [{titular['correo']}] Intento {intento}/14: Buscando correo de inicio de sesión...")
            code_or_link = obtener_codigo_via_imap(
                gmail_user=titular["correo"],
                required_keywords=["code", "código", "verific", "login", "link", "acceso", "entrar", "inici"],
                query_exclude="invited to a tidal family",
                after_email_id=base_id,
                solo_link=False
            )
            if code_or_link:
                break
            time.sleep(8.0)

        if not code_or_link:
            print(f"  [Inviter] ERROR: No se recibió ningún código/enlace por IMAP para {titular['correo']}.")
            return False

        if code_or_link.startswith("http"):
            print(f"  [Inviter] [{titular['correo']}] Enlace mágico detectado. Abriendo: {code_or_link[:70]}...")
            navegar_tidal_tolerante(self.page, code_or_link, timeout_ms=45000)
            self.page = pagina_vigente(self.page)
            time.sleep(3.0)
        else:
            print(f"  [Inviter] [{titular['correo']}] Código de 6 dígitos detectado: {code_or_link}...")
            escrito = False
            for reintento in range(1, 4):
                self.page = pagina_vigente(self.page)
                if escribir_codigo_verificacion_inteligente(self.page, code_or_link):
                    escrito = True
                    break
                print(f"  [Inviter] [WARN] No se pudo escribir el código (intento {reintento}/3). Reintentando...")
                time.sleep(2.5)
            if not escrito:
                print(f"  [Inviter] [WARN] La pantalla de código no aceptó el valor para {titular['correo']}.")
                return False

            time.sleep(1.0)
            btn_login = esperar_locator_en_frames(
                self.page,
                ["button[type='submit']", "button:has-text('Iniciar sesión')", "button:has-text('Log in')",
                 "button:has-text('Continuar')", "button:has-text('Continue')"],
                timeout_s=8.0
            )
            if btn_login:
                try:
                    btn_login.click()
                except Exception:
                    pass

        if not self._esperar_fin_de_login(titular):
            return False

        return self._abrir_panel_familia(titular)

    def asegurar_login_titular(self, titular) -> bool:
        if self._sesion_titular_activa(titular):
            return True

        print(f"  [Inviter] No se detectó sesión activa o es incorrecta. Iniciando sesión en titular: {titular['correo']}...")
        for intento_login in range(1, 4):
            if intento_login > 1:
                print(f"  [Inviter] {Color.WARNING}Reintentando inicio de sesión del titular ({intento_login}/3)...{Color.ENDC}")
                self.logout_titular()
                time.sleep(random.uniform(3.0, 6.0))
            try:
                if self._login_titular_una_vez(titular):
                    return True
            except RuntimeError as e:
                # Bloqueo antirobot insuperable o sin proxies limpios: no tiene sentido insistir.
                print(f"  [Inviter] {Color.FAIL}Login del titular abortado: {e}{Color.ENDC}")
                return False
            except Exception as e:
                print(f"  [Inviter] Excepción al iniciar sesión (intento {intento_login}/3): {e}")
        return False

    def enviar_invitacion_familiar(self, titular, miembro_correo) -> bool:
        try:
            curr_url = self.page.url.lower()
            if "family" not in curr_url or "/login" in curr_url:
                if not _recargar_pagina_familia(self.page):
                    curr_url = self.page.url.lower()
                    if "family" not in curr_url:
                        print(f"  [Inviter] ERROR al navegar a /family")
                        return False
            else:
                aceptar_cookies_con_espera(self.page)

            # Si ya está en el plan (p. ej. un "error inesperado" previo sí lo añadió), no reinvitar.
            # Comparación EXACTA (con puntos): getmu.shroom03.03 ≠ getmushr.o.om0303 en Tidal.
            if _miembro_presente_en_pagina_familia(self.page, miembro_correo):
                print(f"  {Color.GREEN}[Inviter] [OK] {miembro_correo} ya figura en el plan familiar (sin reinvitar).{Color.ENDC}")
                return True

            hermano = _alias_gmail_hermano_en_plan(self.page, miembro_correo)
            if hermano:
                print(f"  {Color.WARNING}[Inviter] [WARN] En el plan ya está '{hermano}' "
                      f"(mismo buzón Gmail que {miembro_correo}, distintos puntos). "
                      f"Se invita de todos modos: en Tidal son cuentas distintas.{Color.ENDC}")

            if invitar_miembro_plan_familiar_con_reintentos(self.page, miembro_correo, intentos=3, pausa_s=0.7):
                print(f"  {Color.GREEN}[Inviter] [OK] Invitación enviada / confirmada para {miembro_correo}.{Color.ENDC}")
                return True

            # Último recurso: recargar y mirar la lista otra vez (falso negativo típico de Tidal)
            print(f"  [Inviter] Verificación final en /family para {miembro_correo}...")
            _recargar_pagina_familia(self.page)
            if _miembro_presente_en_pagina_familia(self.page, miembro_correo):
                print(f"  {Color.GREEN}[Inviter] [OK] {miembro_correo} confirmado en el plan tras verificación final.{Color.ENDC}")
                return True

            print(f"  [Inviter] [WARN] No se pudo enviar la invitación a {miembro_correo}.")
        except Exception as e:
            print(f"  [Inviter] ERROR al enviar invitación para {miembro_correo}: {e}")
            try:
                _recargar_pagina_familia(self.page)
                if _miembro_presente_en_pagina_familia(self.page, miembro_correo):
                    print(f"  {Color.GREEN}[Inviter] [OK] Pese a la excepción, {miembro_correo} está en el plan.{Color.ENDC}")
                    return True
            except Exception:
                pass
        return False

    def run_inviter(self):
        try:
            titulares, path = cargar_titulares_familiares()
            if not titulares:
                print(f"  {Color.FAIL}[Inviter] ERROR: No se encontraron titulares en perfiles/familiar_titular.txt ni titular_familiar.txt{Color.ENDC}")
                return
                
            idx_titular = -1
            for i, t in enumerate(titulares):
                if t["estado"] == "disponible" and t["usados"] < 5:
                    idx_titular = i
                    break
                    
            if idx_titular == -1:
                print(f"  {Color.FAIL}[Inviter] ERROR: No hay titulares con cupos libres en {path.name}{Color.ENDC}")
                return
                
            titular = titulares[idx_titular]
            self.abrir_navegador()
            print(f"  [Inviter] {Color.CYAN}Iniciando Paso 9 con login limpio del perfil de Chrome para la cuenta titular ({titular['correo']})...{Color.ENDC}")
            self.logout_titular()
            
            while True:
                miembro_correo = self.queue_miembros.get()
                if miembro_correo is None:
                    self.queue_miembros.task_done()
                    break
                    
                print(f"\n  [Inviter] Procesando invitación para: {miembro_correo}...")
                
                logeado = self.asegurar_login_titular(titular)
                if not logeado:
                    print(f"  {Color.FAIL}[Inviter] ERROR al logearse en titular: {titular['correo']}{Color.ENDC}")
                    self.queue_miembros.task_done()
                    continue
                    
                # Sincronizar y validar cupos reales en Tidal
                tiene_cupos = self.sincronizar_y_validar_cupos_titular(titular, titulares, path)
                if not tiene_cupos:
                    # Buscar el siguiente titular disponible
                    idx_titular = -1
                    for i, t in enumerate(titulares):
                        if t["estado"] == "disponible" and t["usados"] < 5:
                            idx_titular = i
                            break
                    if idx_titular != -1:
                        titular = titulares[idx_titular]
                        print(f"  [Inviter] Cambiando al siguiente titular disponible: {titular['correo']}")
                        self.queue_miembros.put(miembro_correo)
                        self.queue_miembros.task_done()
                        continue
                    else:
                        print(f"  {Color.FAIL}[Inviter] ADVERTENCIA: No quedan más titulares disponibles en la lista.{Color.ENDC}")
                        self.queue_miembros.task_done()
                        break
                    
                invitado_ok = self.enviar_invitacion_familiar(titular, miembro_correo)
                if invitado_ok:
                    miembro_clean = miembro_correo.strip().rstrip('.').lower()
                    miembros_unicos = []
                    for m in titular.get("miembros", []):
                        m_c = m.strip().rstrip('.').lower()
                        if m_c and m_c not in miembros_unicos and m_c != titular["correo"].strip().lower():
                            miembros_unicos.append(m_c)
                    if miembro_clean not in miembros_unicos:
                        miembros_unicos.append(miembro_clean)

                    titular["miembros"] = miembros_unicos
                    titular["usados"] = len(titular["miembros"])

                    esta_lleno_real = False
                    if titular["usados"] >= 5:
                        time.sleep(1.0)
                        esta_lleno_real = self.page.evaluate("""() => {
                            const bodyText = document.body ? document.body.innerText : '';
                            if (/\\b5\\s*(?:de|of)\\s*5\\b/i.test(bodyText)) return true;

                            const addKws = ['invitar a un familiar', 'invitar familiar', 'invitar miembro', 'agregar miembro', 'add family member', 'add member'];
                            const buttons = Array.from(document.querySelectorAll('button, a, div, span, p, [role="button"]'));
                            const hasAddBtn = buttons.some(el => {
                                const t = (el.textContent || '').trim().toLowerCase();
                                return addKws.some(kw => t.includes(kw));
                            });
                            return !hasAddBtn;
                        }""")

                    if esta_lleno_real:
                        titular["estado"] = "lleno"
                        print(f"  [Inviter] {Color.WARNING}El plan familiar de {titular['correo']} se ha llenado (5/5). Cerrando sesión...{Color.ENDC}")
                        self.logout_titular()

                        idx_titular = -1
                        for i, t in enumerate(titulares):
                            if t["estado"] == "disponible" and t["usados"] < 5:
                                idx_titular = i
                                break
                        if idx_titular != -1:
                            titular = titulares[idx_titular]
                            print(f"  [Inviter] Cambiando al siguiente titular disponible: {titular['correo']}")
                        else:
                            print(f"  {Color.FAIL}[Inviter] ADVERTENCIA: No quedan más titulares disponibles en la lista.{Color.ENDC}")
                            guardar_titulares_familiares(titulares, path)
                            self.queue_miembros.task_done()
                            break

                    guardar_titulares_familiares(titulares, path)
                else:
                    # No descartar del todo: a veces Tidal añade al miembro segundos después.
                    # Se deja constancia; el siguiente ciclo de sincronización lo detectará.
                    print(f"  [Inviter] {Color.WARNING}Invitación no confirmada para {miembro_correo}; se continúa con el siguiente.{Color.ENDC}")

                # Pausa entre invitaciones: invitaciones seguidas disparan el "error inesperado" de Tidal
                time.sleep(random.uniform(2.0, 3.5))

                self.queue_miembros.task_done()
                
        except Exception as ex:
            print(f"  {Color.FAIL}[Inviter] ERROR crítico en hilo inviter: {ex}{Color.ENDC}")
        finally:
            self.cerrar_recursos()
            # Cerrar la conexión IMAP reutilizable del hilo para no dejarla abierta contra Gmail
            cerrar_sesion_imap_hilo()
            print("  [Inviter] Hilo de invitación finalizado y ventana de Chrome cerrada.")


def restablecer_contrasenas_tidal(correos=None):
    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   RESTABLECIMIENTO AUTOMÁTICO DE CONTRASEÑAS TIDAL{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"{Color.FAIL}[Error]{Color.ENDC} Playwright no está instalado. Ejecute 'pip install playwright' e instale los navegadores con 'playwright install'.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    path_cuentas = SCRIPT_DIR / "sesiones_imap_cuentas.txt"
    if not path_cuentas.exists():
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} El archivo 'sesiones_imap_cuentas.txt' no existe en la carpeta actual.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return
        
    cuentas_map = cargar_mapa_cuentas_sesiones()
    if not cuentas_map:
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} No se encontraron cuentas válidas en 'sesiones_imap_cuentas.txt' (formato: correo contraseña).")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    cuentas_map = filtrar_cuentas_por_correos_activos(cuentas_map, correos)
    if cuentas_map is None:
        input(">>> Presiona Enter para volver al menú principal <<<")
        return
        
    correos_lista = list(cuentas_map.keys())
    print(f"\nSe procesarán {len(correos_lista)} cuenta(s) (filtradas por correos activos del menú).")

    headless_opt = input("\n¿Deseas ejecutar el navegador en segundo plano (headless)? (s/n, por defecto 'n'): ").strip().lower()
    headless = headless_opt in ("s", "si", "yes", "y")

    success_count = 0
    fail_count = 0

    num_cuentas = len(correos_lista)
    print(f"\n{Color.CYAN}[Proxies PE] Habilitando proxies de Perú obligatorios para restablecimiento (Opción 9)...{Color.ENDC}")
    valid_pe_list = asegurar_proxies_peru(cantidad_necesaria=num_cuentas)
    if not valid_pe_list:
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} No hay proxies de Perú válidos y esta opción los exige (solicitud y enlace de restablecimiento). "
              f"Valida la lista con la opción 13 antes de continuar.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    inviter = None
    inviter_thread = None
    if len(correos_lista) >= 6:
        print(f"\n{Color.WARNING}[Info] Se detectaron {len(correos_lista)} correos (>= 6). Se omite el paso de invitación al plan familiar.{Color.ENDC}")
    else:
        path_titular = SCRIPT_DIR / "titular_familiar.txt"
        if not path_titular.exists():
            path_titular = SCRIPT_DIR / "perfiles" / "familiar_titular.txt"

        _, miembros_anotados = parsear_titular_familiar_txt_opcion11(path_titular)
        miembros_a_invitar = miembros_anotados if miembros_anotados else correos_lista

        print(f"  [Paso 9] Miembros a invitar al plan familiar ({len(miembros_a_invitar)}): {miembros_a_invitar}")

        family_invite_queue = queue.Queue()
        for correo in miembros_a_invitar:
            family_invite_queue.put(correo)
        family_invite_queue.put(None)

        inviter = TidalFamilyInviter(family_invite_queue)
        inviter_thread = threading.Thread(target=inviter.run_inviter, daemon=True)
        inviter_thread.start()

    batch_size = 20
    total_cuentas = len(correos_lista)
    print(f"\n{Color.CYAN}{Color.BOLD}Iniciando restablecimiento de {total_cuentas} cuentas de forma simultánea (en lotes de {batch_size})...{Color.ENDC}\n")
    
    for b_start in range(0, total_cuentas, batch_size):
        lote_correos = correos_lista[b_start : b_start + batch_size]
        num_cuentas_lote = len(lote_correos)
        
        barreras_lote = {
            "inicio": BarreraTolerante(num_cuentas_lote),
            "post_solicitud": BarreraTolerante(num_cuentas_lote),
            "post_link": BarreraTolerante(num_cuentas_lote),
            "final": BarreraTolerante(num_cuentas_lote)
        }
        
        workers = num_cuentas_lote
        if total_cuentas > batch_size:
            print(f"\n{Color.CYAN}{Color.BOLD}--- Procesando Lote ({b_start + 1} a {b_start + num_cuentas_lote} de {total_cuentas}) ---{Color.ENDC}")

        def restablecer_un_correo(idx_rel, correo):
            if idx_rel > 1:
                # Escalonar arranque sin alargar tanto el lote (antes 0.35s → ~6.6s el último)
                time.sleep((idx_rel - 1) * 0.15)
            idx_abs = b_start + idx_rel
            contrasena = cuentas_map[correo]
            
            p_pe = GLOBAL_PE_PROXY_POOL.obtener_proxy_unico()
            p_pe_server = p_pe.get("server") if p_pe else None
            p_pe_user = p_pe.get("username") if p_pe else None
            p_pe_pass = p_pe.get("password") if p_pe else None
                
            manager = TidalResetPasswordManager(
                client_email=correo,
                target_pwd=contrasena,
                proxy_pe_server=p_pe_server,
                proxy_pe_user=p_pe_user,
                proxy_pe_pass=p_pe_pass,
                headless=headless,
                barreras=barreras_lote,
                thread_index=idx_abs
            )
            
            print(f"\n{Color.CYAN}{Color.BOLD}[Restablecimiento Concurrente] Iniciando proceso para: {correo}{Color.ENDC}")
            try:
                exito = manager.run_password_reset()
            finally:
                # Cerrar la conexión IMAP reutilizable del hilo para no dejarla abierta contra Gmail
                cerrar_sesion_imap_hilo()
            return correo, exito

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(restablecer_un_correo, idx_rel, correo): correo for idx_rel, correo in enumerate(lote_correos, 1)}
            for future in as_completed(futures):
                correo = futures[future]
                try:
                    c, exito = future.result()
                    if exito:
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    print(f"  {Color.FAIL}[ERROR] Excepción inesperada procesando {correo}: {e}{Color.ENDC}")
                    fail_count += 1

    if inviter_thread and inviter_thread.is_alive():
        print(f"\n{Color.CYAN}Esperando a que finalice el proceso de invitación familiar...{Color.ENDC}")
        inviter_thread.join(timeout=15.0)
    
    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   RESUMEN DEL RESTABLECIMIENTO{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f" Cuentas procesadas con éxito: {Color.GREEN}{success_count}{Color.ENDC}")
    print(f" Cuentas fallidas: {Color.FAIL}{fail_count}{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}\n")
    print(f"{Color.GREEN}{Color.BOLD}>>> Proceso finalizado. Regresando al menú principal...{Color.ENDC}\n")


TIEMPO_REVISION_MANUAL_S = 900

PATRON_MODO_CONTRASENA = r"(?:inicia|iniciar|usar|use|sign\s*in|log\s*in|entrar)[^\n]{0,24}(?:contrase|password)"


class TidalAutoLoginManager:
    def __init__(self, client_email, target_pwd, proxy_pe_server=None, proxy_pe_user=None, proxy_pe_pass=None, headless=False, barreras=None, thread_index=1,
                 mantener_ventana_si_falla=False):
        self.client_email = client_email
        self.target_pwd = target_pwd
        self.proxy_pe_server = proxy_pe_server
        self.proxy_pe_user = proxy_pe_user
        self.proxy_pe_pass = proxy_pe_pass
        self.use_proxy = proxy_pe_server is not None
        self.headless = headless
        self.barreras = barreras or {}
        self.thread_index = thread_index
        self.mantener_ventana_si_falla = mantener_ventana_si_falla
        self.playwright = None
        self.context = None
        self.page = None
        self.tmm_page = None

        self.download_completed = False
        self.sin_playlists = False
        self.login_ok = False
        self.export_ok = False
        self.eliminacion_ok = False
        self._rotaciones_antibot = 0
        self._recuperaciones_error_tidal = 0
        
        email_safe = re.sub(r'[^a-zA-Z0-9]', '_', client_email)
        self.main_profile = Path(tempfile.gettempdir()) / f"tidal_chrome_profile_{email_safe}_{random.randint(1000, 9999)}"

    def recuperar_login_tras_error_tidal(self) -> bool:
        """Tras 'Algo salió mal' en /signin: NO recargar esa URL; pasar por pricing→account/login.

        Recargar login.tidal.com/signin con otra IP reproduce el Error (ver error.txt).
        """
        try:
            self.page = pagina_vigente(self.page)
            if not self.page or self.page.is_closed():
                self.asegurar_navegador_abierto()
            print(f"  [Login] [{self.client_email}] Recuperando flujo: tidal.com/pricing → account.tidal.com/login...")
            navegar_tidal_tolerante(self.page, "https://tidal.com/pricing", timeout_ms=30000)
            time.sleep(random.uniform(1.5, 2.5))
            aceptar_cookies_con_espera(self.page)
            time.sleep(0.4)
            try:
                navegar_tidal_tolerante(
                    self.page, "https://account.tidal.com/login",
                    referer="https://tidal.com/pricing",
                    timeout_ms=30000
                )
            except Exception:
                if url_es_pagina_marketing(self.page.url if self.page else ""):
                    if not self._entrar_login_desde_pricing():
                        return False
                else:
                    raise
            time.sleep(2.0)
            self.page = pagina_vigente(self.page)
            if es_pantalla_error_login_tidal(self.page):
                print(f"  [Login] [{self.client_email}] Aún Error en login; reintentando CTA desde pricing...")
                try:
                    navegar_tidal_tolerante(self.page, "https://tidal.com/pricing", timeout_ms=25000)
                    time.sleep(1.5)
                    aceptar_cookies_con_espera(self.page)
                except Exception:
                    pass
                if not self._entrar_login_desde_pricing():
                    return False
                time.sleep(1.5)
            return _formulario_login_visible(self.page) or (
                url_es_login_o_cuenta(self.page.url or "")
                and not es_pantalla_error_login_tidal(self.page)
            )
        except Exception as e:
            print(f"  [Login] [{self.client_email}] [WARN] recuperar_login_tras_error_tidal: {e}")
            return False

    def ejecutar_rotacion_proxy_y_recargar(self):
        print(f"\n  [Auto-Proxy PE] Bloqueo detectado en {self.client_email}. Rotando proxy de Perú...")
        self.rotar_proxy_contexto()
        try:
            if self.context:
                self.context.clear_cookies(domain="tidal.com")
                self.context.clear_cookies(domain="login.tidal.com")
                self.context.clear_cookies(domain="account.tidal.com")
        except Exception:
            pass
        # NUNCA recargar login.tidal.com/signin (reproduce 'Algo salió mal').
        # Siempre reabrir el login por el bypass de reputación.
        print(f"  [Auto-Proxy PE] [{self.client_email}] Reabriendo login vía pricing (no se recarga /signin)...")
        try:
            if not self.recuperar_login_tras_error_tidal():
                # Último recurso: account.tidal.com/login directo con referer
                navegar_tidal_tolerante(
                    self.page, "https://account.tidal.com/login",
                    referer="https://tidal.com/pricing",
                    timeout_ms=30000
                )
                time.sleep(2.0)
        except Exception as e:
            print(f"  [Auto-Proxy PE] [WARN] Error al reabrir login tras rotación: {e}")

    def rotar_proxy_contexto(self, tipo="PE"):
        global GLOBAL_PE_PROXY_POOL
        print(f"\n  [Proxy Rotation PE] Rotando proxy de Perú por bloqueo antirobot...")
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.context = None
        self.page = None
        self.playwright = None

        time.sleep(2.0)
        # Eliminar perfil contaminado y crear uno nuevo limpio
        old_profile = self.main_profile
        email_safe = re.sub(r'[^a-zA-Z0-9]', '_', self.client_email)
        self.main_profile = Path(tempfile.gettempdir()) / f"tidal_chrome_profile_{email_safe}_{random.randint(1000, 9999)}"
        try:
            import shutil
            if old_profile.exists():
                shutil.rmtree(old_profile, ignore_errors=True)
        except Exception:
            pass
        reparar_perfil_corrupto(self.main_profile)

        p_pe = GLOBAL_PE_PROXY_POOL.rotar_y_marcar_bloqueado(self.proxy_pe_server)
        if p_pe:
            self.proxy_pe_server = p_pe.get("server")
            self.proxy_pe_user = p_pe.get("username")
            self.proxy_pe_pass = p_pe.get("password")
            self.use_proxy = True
            print(f"  [Proxy Rotation PE] Nuevo proxy PE configurado: {self.proxy_pe_server}")
        else:
            # Antes se relanzaba sin proxy: eso sacaba el tráfico por la IP real y DataDome la marcaba
            raise RuntimeError(
                f"No quedan proxies de Perú limpios para {self.client_email}. Se aborta la cuenta en vez de usar tu IP real."
            )

        self.asegurar_navegador_abierto()

    def handle_download(self, download):
        try:
            suggested_filename = ""
            try:
                suggested_filename = (download.suggested_filename or "").lower()
            except Exception:
                pass
            print(f"  [Descarga] [{self.client_email}] Detectada descarga: {suggested_filename or '(sin nombre)'}")

            dest_file = guardar_csv_descarga_playwright(download, self.client_email)
            if not dest_file:
                print(f"  [Descarga] {Color.FAIL}[ERROR] [{self.client_email}] CSV NO guardado "
                      f"(no se marcará como exportado).{Color.ENDC}")
                return

            print(f"  [Descarga] {Color.GREEN}[OK] Archivo guardado con éxito en: {dest_file} "
                  f"({dest_file.stat().st_size} bytes){Color.ENDC}")

            # Guardar cookies de TuneMyMusic antes de cerrar
            try:
                if self.context and guardar_cookies_tmm(self.context.cookies()):
                    print(f"  [TuneMyMusic] [{self.client_email}] Cookies actualizadas en 'tmm_cookies.json'.")
            except Exception as ce:
                print(f"  [TuneMyMusic] [WARN] Falló al guardar cookies para {self.client_email}: {ce}")

            print(f"  [Descarga] [{self.client_email}] Marcando descarga como completada...")
            self.download_completed = True
            self.export_ok = True
        except Exception as ex:
            print(f"  [Descarga] [ERROR] Falló al procesar descarga para {self.client_email}: {ex}")
            # No marcar completed: la opción 10 no debe eliminar sin CSV en disco
            self.download_completed = False

    def abortar_barreras(self):
        for name, b in self.barreras.items():
            try:
                b.abort()
            except Exception:
                pass

    def esperar_barrera(self, nombre):
        barrera = self.barreras.get(nombre)
        if barrera:
            try:
                print(f"  [{self.client_email}] Esperando sincronización de paso: {nombre}...")
                barrera.wait(timeout=180)
            except threading.BrokenBarrierError:
                print(f"  [{self.client_email}] [WARN] Sincronización '{nombre}' continuada tras interrupción en otro hilo.")
            except Exception as e:
                print(f"  [{self.client_email}] [WARN] Error en barrera '{nombre}': {e}")

    def buscar_modo_contrasena(self, accion: str):
        """Localiza el control 'Inicia sesión con contraseña' de la pantalla del código.

        Tidal lo pinta como texto suelto dentro de un contenedor que no es <a> ni <button>, por lo
        que los selectores por etiqueta no lo alcanzan. Acciones: 'existe', 'coords' o 'click'."""
        js = """(args) => {
            const re = new RegExp(args.patron, 'i');
            const visible = (el) => {
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') < 0.1) return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const buscar = (root) => {
                const cand = Array.from(root.querySelectorAll('a, button, [role="button"], span, div, p, li, label'))
                    .filter(el => visible(el))
                    .filter(el => {
                        const t = (el.textContent || '').trim();
                        return t.length > 0 && t.length < 60 && re.test(t);
                    });
                if (cand.length) {
                    cand.sort((a, b) => (a.textContent || '').trim().length - (b.textContent || '').trim().length);
                    return cand[0];
                }
                if (!args.soloTop) {
                    for (const fr of root.querySelectorAll('iframe, frame')) {
                        try {
                            const doc = fr.contentDocument || fr.contentWindow.document;
                            if (doc) {
                                const encontrado = buscar(doc);
                                if (encontrado) return encontrado;
                            }
                        } catch(e) {}
                    }
                }
                return null;
            };
            const el = buscar(document);
            if (!el) return null;
            const objetivo = el.closest('a, button, [role="button"]') || el;
            if (args.accion === 'existe') return true;
            if (args.accion === 'click') { objetivo.click(); return true; }
            el.scrollIntoView({block: 'center'});
            const r = objetivo.getBoundingClientRect();
            return {x: r.x + r.width / 2, y: r.y + r.height / 2};
        }"""
        try:
            return self.page.evaluate(js, {
                "patron": PATRON_MODO_CONTRASENA,
                "accion": accion,
                "soloTop": accion == "coords"
            })
        except Exception:
            return None

    def hay_control_modo_contrasena(self) -> bool:
        return bool(self.buscar_modo_contrasena("existe"))

    def clic_modo_contrasena(self) -> bool:
        """Pulsa el control con un clic real de ratón y, si no es posible, por JS."""
        coords = self.buscar_modo_contrasena("coords")
        if coords:
            try:
                self.page.mouse.click(coords["x"], coords["y"])
                return True
            except Exception:
                pass
        return bool(self.buscar_modo_contrasena("click"))

    def hay_pantalla_codigo_login(self) -> bool:
        """Detecta la pantalla 'Revisa tu correo electrónico' con las cajas del código de acceso."""
        try:
            return bool(self.page.evaluate("""() => {
                const txt = document.body ? document.body.innerText.toLowerCase() : '';
                const frases = ['revisa tu correo', 'check your email', 'te hemos enviado un código',
                                'te hemos enviado un codigo', "we've sent", 'we have sent',
                                'reenviar código', 'reenviar codigo', 'resend code'];
                if (frases.some(f => txt.includes(f))) return true;
                return document.querySelectorAll('input[maxlength="1"], input[autocomplete="one-time-code"]').length >= 4;
            }"""))
        except Exception:
            return False

    def iniciar_sesion_con_codigo_email(self, base_email_id: int = 0) -> bool:
        """Último recurso cuando Tidal no ofrece la opción de contraseña: usar el código de acceso."""
        print(f"  [Login] [{self.client_email}] Recuperando el código de acceso desde el buzón...")
        codigo = None
        for intento in range(1, 13):  # 12 intentos * 10s = 2 minutos
            codigo = obtener_codigo_via_imap(
                gmail_user=self.client_email,
                required_keywords=["código", "code", "inici"],
                query_exclude="cancel",
                after_email_id=base_email_id
            )
            if codigo:
                break
            if intento < 12:
                print(f"  [Login] [{self.client_email}] Intento {intento}/12: el código de acceso aún no ha llegado...")
                time.sleep(10.0)

        if not codigo:
            print(f"  [Login] {Color.FAIL}[ERROR] [{self.client_email}] No llegó el código de acceso al buzón.{Color.ENDC}")
            return False

        print(f"  [Login] [{self.client_email}] {Color.GREEN}Código de acceso obtenido: {codigo}{Color.ENDC}")
        if not escribir_codigo_verificacion_inteligente(self.page, codigo):
            print(f"  [Login] {Color.FAIL}[ERROR] [{self.client_email}] No se pudo escribir el código en la pantalla.{Color.ENDC}")
            return False

        time.sleep(1.5)
        btn_entrar = esperar_locator_en_frames(
            self.page,
            ["button[type='submit']", "button:has-text('Inicia Sesión')", "button:has-text('Inicia sesión')",
             "button:has-text('Iniciar sesión')", "button:has-text('Log in')",
             "button:has-text('Continuar')", "button:has-text('Continue')"],
            timeout_s=8.0
        )
        if btn_entrar:
            try:
                btn_entrar.click(timeout=3000, force=True)
            except Exception:
                pass

        if self.esperar_establecimiento_sesion(40.0):
            print(f"  [Login] {Color.GREEN}[OK] [{self.client_email}] Sesión iniciada con el código del correo.{Color.ENDC}")
            return True

        print(f"  [Login] {Color.WARNING}[WARN] [{self.client_email}] El código se ingresó pero la sesión no avanzó.{Color.ENDC}")
        return False

    def hay_campo_codigo(self):
        return encontrar_locator_en_frames(
            self.page,
            ['input[autocomplete="one-time-code"]', 'input[name="code"]',
             'input[placeholder*="código" i]', 'input[placeholder*="codigo" i]', 'input[placeholder*="code" i]',
             'input[inputmode="numeric"]', 'input[maxlength="1"]']
        )

    def recorrer_asistente_eliminacion(self, max_pasos: int = 6) -> bool:
        """Recorre el asistente de confirmación de Tidal hasta la pantalla del código.

        Es este recorrido (checkbox 'He leído lo anterior' + 'Continuar') el que hace que Tidal
        envíe el código de verificación; abrir la URL de verificación por sí sola no lo dispara."""
        for paso_num in range(1, max_pasos + 1):
            if self.hay_campo_codigo():
                print(f"  [Eliminación] [{self.client_email}] Pantalla del código alcanzada en el paso {paso_num}.")
                return True

            for intento_cb in range(1, 4):
                try:
                    checkbox_clicked = self.page.evaluate(r"""
                        () => {
                            const regex = /he\s+le[íi]do\s+lo\s+anterior/i;
                            const buscarYClicar = (root) => {
                                const elms = Array.from(root.querySelectorAll('label, span, input, p, div'));
                                const matches = elms.filter(el => {
                                    const text = (el.textContent || '').trim();
                                    const isVisible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                                    return regex.test(text) && isVisible;
                                });
                                if (matches.length === 0) {
                                    for (const frame of root.querySelectorAll('iframe, frame')) {
                                        try {
                                            const doc = frame.contentDocument || frame.contentWindow.document;
                                            if (doc && buscarYClicar(doc)) return true;
                                        } catch(e) {}
                                    }
                                    return false;
                                }
                                matches.sort((a, b) => a.textContent.length - b.textContent.length);
                                const bestEl = matches[0];
                                const wrapper = bestEl.closest('.form-checkbox-wrapper') || bestEl.closest('label')?.parentElement || bestEl.parentElement;
                                const cbEl = wrapper ? wrapper.querySelector('input[type="checkbox"], [role="checkbox"], .form-checkbox, .form-checked-icon') : null;
                                (cbEl || bestEl).click();
                                return true;
                            };
                            return buscarYClicar(document);
                        }
                    """)
                except Exception as e_cb:
                    print(f"  [Eliminación] [{self.client_email}] [WARN] Error al buscar el checkbox: {e_cb}")
                    checkbox_clicked = False

                if not checkbox_clicked:
                    break

                print(f"  [Eliminación] [{self.client_email}] Checkbox 'He leído lo anterior' marcado en el paso {paso_num}.")
                time.sleep(1.5)
                try:
                    boton_habilitado = self.page.evaluate(r"""
                        () => {
                            const textos = ['Continuar', 'Continue', 'Siguiente', 'Next', 'Confirmar', 'Confirm', 'Eliminar', 'Delete', 'Eliminar cuenta', 'Delete account'];
                            const buscar = (root) => {
                                for (const el of root.querySelectorAll('button, a, [role="button"]')) {
                                    const text = (el.textContent || '').trim();
                                    const isVisible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                                    if (textos.includes(text) && isVisible) {
                                        return !el.disabled && !el.hasAttribute('disabled');
                                    }
                                }
                                for (const frame of root.querySelectorAll('iframe, frame')) {
                                    try {
                                        const doc = frame.contentDocument || frame.contentWindow.document;
                                        if (doc && buscar(doc)) return true;
                                    } catch(e) {}
                                }
                                return false;
                            };
                            return buscar(document);
                        }
                    """)
                except Exception:
                    boton_habilitado = False

                if boton_habilitado:
                    break
                print(f"  [Eliminación] [{self.client_email}] [WARN] El botón sigue deshabilitado (intento {intento_cb}/3). Recargando...")
                if intento_cb < 3:
                    try:
                        self.page.reload(wait_until="domcontentloaded")
                        time.sleep(3.0)
                        aceptar_cookies_con_espera(self.page)
                    except Exception:
                        pass

            try:
                btn_clicked = self.page.evaluate("""
                    () => {
                        const textos = ['Continuar', 'Continue', 'Siguiente', 'Next', 'Confirmar', 'Confirm', 'Eliminar cuenta', 'Delete account', 'Eliminar', 'Delete'];
                        const buscar = (root) => {
                            const isVisible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
                            for (const el of root.querySelectorAll('button, a, [role="button"]')) {
                                const text = (el.textContent || '').trim();
                                if (textos.includes(text) && isVisible(el) && !el.disabled) {
                                    el.click();
                                    return true;
                                }
                            }
                            for (const frame of root.querySelectorAll('iframe, frame')) {
                                try {
                                    const doc = frame.contentDocument || frame.contentWindow.document;
                                    if (doc && buscar(doc)) return true;
                                } catch(e) {}
                            }
                            return false;
                        };
                        return buscar(document);
                    }
                """)
            except Exception as e_btn:
                print(f"  [Eliminación] [{self.client_email}] [WARN] Error al pulsar 'Continuar': {e_btn}")
                btn_clicked = False

            if btn_clicked:
                print(f"  [Eliminación] [{self.client_email}] 'Continuar' pulsado en el paso {paso_num} del asistente.")
                time.sleep(3.0)
                continue

            btn_fallback = esperar_locator_en_frames(
                self.page,
                ["button:has-text('Continuar')", "button:has-text('Continue')",
                 "a:has-text('Continuar')", "button[type='submit']"],
                timeout_s=4.0
            )
            if btn_fallback:
                try:
                    btn_fallback.click(force=True)
                    time.sleep(3.0)
                    continue
                except Exception:
                    pass
            break

        return bool(self.hay_campo_codigo())

    def verificar_destino_del_codigo(self, target_email_clean: str) -> bool:
        """Confirma que Tidal muestra el correo de la cuenta como destino del código de eliminación."""
        try:
            del_body = self.page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''")
        except Exception:
            return True

        candidatos = [
            c for c in re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', del_body)
            if not c.endswith("tidal.com")
        ]
        if not candidatos:
            print(f"  [Eliminación] [{self.client_email}] [Info] La pantalla no muestra el correo de destino; se continúa sin esa comprobación.")
            return True

        correo_destino = candidatos[0]
        if not son_correos_equivalentes(correo_destino, target_email_clean):
            print(f"  [Eliminación] {Color.FAIL}[ALERTA] Tidal envía el código a '{correo_destino}', que NO coincide con '{self.client_email}'.{Color.ENDC}")
            return False
        print(f"  [Eliminación] {Color.GREEN}[OK] Confirmado: Tidal enviará el código a '{correo_destino}' (coincide con la cuenta IMAP).{Color.ENDC}")
        return True

    def forzar_reenvio_codigo(self) -> bool:
        """Pulsa 'Reenviar' si el botón está libre (sin cuenta atrás), señal de que no llegó nada."""
        try:
            btn_resend = encontrar_locator_en_frames(
                self.page,
                ["button:has-text('Reenviar')", "a:has-text('Reenviar')", "span:has-text('Reenviar')",
                 "button:has-text('Resend')", "a:has-text('Resend')", "span:has-text('Resend')"]
            )
            if not btn_resend:
                return False
            texto_boton = btn_resend.inner_text()
            if any(ch.isdigit() for ch in texto_boton):
                return False
            print(f"  [Eliminación] [{self.client_email}] Botón de reenvío libre ('{texto_boton.strip()}'). Forzando envío del código...")
            btn_resend.click()
            time.sleep(3.0)
            return True
        except Exception as e:
            print(f"  [Eliminación] [{self.client_email}] [WARN] No se pudo forzar el reenvío: {e}")
            return False

    def finalizar_sin_exito(self, motivo: str) -> bool:
        """Libera el navegador desde el MISMO hilo que lo creó.

        La API sync de Playwright está ligada al hilo creador, así que el cierre no puede
        delegarse al hilo principal: fallaría en silencio y dejaría Chrome y el driver vivos."""
        if self.mantener_ventana_si_falla:
            minutos = max(1, int(TIEMPO_REVISION_MANUAL_S / 60))
            print(f"  [Navegador] [{self.client_email}] {motivo} La ventana queda abierta para revisión manual "
                  f"(hasta {minutos} min o hasta que la cierres).")
            limite = time.time() + TIEMPO_REVISION_MANUAL_S
            while time.time() < limite:
                try:
                    if not self.page or self.page.is_closed():
                        break
                except Exception:
                    break
                time.sleep(1.0)
        else:
            print(f"  [Navegador] [{self.client_email}] {motivo} Cerrando la ventana y liberando recursos.")
        self.cerrar_recursos()
        return False

    def asegurar_navegador_abierto(self):
        try:
            if self.context and self.page and not self.page.is_closed():
                return
        except Exception:
            pass
            
        if not self.playwright:
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            
        reparar_perfil_corrupto(self.main_profile)
        launch_args = list(CHROME_SILENT_ARGS)
        
        launch_kwargs = {
            "user_data_dir": str(self.main_profile),
            "headless": self.headless,
            "args": launch_args,
            "ignore_default_args": ["--enable-automation", "--no-sandbox"],
            "viewport": {"width": 1280, "height": 800},
            "locale": "es-ES",
            "channel": "chrome",
            "accept_downloads": True
        }

        if self.proxy_pe_server:
            p_serv = self.proxy_pe_server
            if not p_serv.startswith("http://") and not p_serv.startswith("https://") and not p_serv.startswith("socks5://"):
                p_serv = "http://" + p_serv
            proxy_dict = {"server": p_serv}
            if self.proxy_pe_user:
                proxy_dict["username"] = self.proxy_pe_user
            if self.proxy_pe_pass:
                proxy_dict["password"] = self.proxy_pe_pass
            launch_kwargs["proxy"] = proxy_dict
            print(f"  [Proxy PE] [{self.client_email}] Conectando obligatoriamente con proxy de Perú: {p_serv}")
        else:
            # Nunca salir por la IP real: DataDome la marcaría y bloquearía todo el proceso a futuro
            raise RuntimeError(
                f"Sin proxy de Perú disponible para {self.client_email}. Se aborta la cuenta antes de exponer tu IP real."
            )
        
        try:
            self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:
            print(f"  [Navegador] [WARN] Falló el lanzamiento: {e}. Reparando y reintentando...")
            reparar_perfil_corrupto(self.main_profile)
            time.sleep(2.0)
            self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
            
        self.context.set_default_navigation_timeout(60000)
        self.context.set_default_timeout(45000)
        self.context.add_init_script(STEALTH_SCRIPT)
        
        # Limpiar cookies de Tidal para garantizar login desde cero sin arrastrar sesiones de cuentas previas
        try:
            cookies = self.context.cookies()
            non_tidal = [c for c in cookies if "tidal.com" not in c.get("domain", "").lower()]
            self.context.clear_cookies()
            if non_tidal:
                self.context.add_cookies(non_tidal)
            print(f"  [Perfil Limpio] [{self.client_email}] Sesiones previas de Tidal limpiadas.")
        except Exception:
            pass

        # Cargar cookies de TuneMyMusic si existen
        try:
            valid_cookies = cargar_cookies_tmm()
            if valid_cookies:
                self.context.add_cookies(valid_cookies)
                print(f"  [TuneMyMusic] [{self.client_email}] Sesión precargada desde 'tmm_cookies.json'.")
        except Exception as e:
            print(f"  [TuneMyMusic] [WARN] Error al cargar cookies: {e}")
                
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.client_email = self.client_email
        self.page.manager = self
        self.page.bring_to_front()
        
        # Registrar los escuchadores de descarga a nivel de página
        self.page.on("download", self.handle_download)
        self.context.on("page", lambda p: p.on("download", self.handle_download))

    def hay_formulario_login_visible(self) -> bool:
        """True solo en el flujo OAuth de login (no en páginas de cuenta que también tienen inputs)."""
        try:
            self.page = pagina_vigente(self.page)
            url = self.page.url.lower()
            # La URL de retorno OAuth (/login/tidal/return) NO es pantalla de login: es el puente
            # post-contraseña. Tratarla como formulario hacía fallar todo el inicio de sesión.
            if "/login/tidal/return" in url or "/login/tidal/callback" in url:
                return False
            if "login.tidal.com" in url or "/authorize" in url:
                return True
            if "account.tidal.com" in url and "/login" in url and "/tidal/return" not in url:
                # Solo si realmente hay campos de acceso (no un redirect vacío)
                if encontrar_locator_en_frames(
                    self.page,
                    ['input[type="email"]', 'input[name="email"]', '#email',
                     'input[type="password"]', 'input[name="password"]']
                ):
                    return True
        except Exception:
            pass
        return False

    def es_sesion_activa(self) -> bool:
        """Sesión real en account.tidal.com (fuera del OAuth de login.tidal.com)."""
        try:
            self.page = pagina_vigente(self.page)
            url = self.page.url.lower()
            # Puente OAuth tras contraseña: todavía no es sesión consolidada, pero tampoco es fallo
            if "/login/tidal/return" in url or "/login/tidal/callback" in url:
                return False
            if "login.tidal.com" in url or "/authorize" in url:
                return False
            if self.hay_formulario_login_visible():
                return False
            # Rutas de cuenta autenticada
            if "account.tidal.com" in url and any(
                kw in url for kw in ["/profile", "/settings", "/subscription", "/family",
                                     "/overview", "/payment", "/gift", "/account"]
            ):
                return True
            if "account.tidal.com" in url and "/login" not in url:
                body_txt = self.page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''")
                senales_cuenta = ["cerrar sesión", "sign out", "log out", "detalles de inicio de sesión",
                                  "login details", "editar perfil", "edit profile", "plan familiar",
                                  "family plan", "mi cuenta", "my account", "suscripción", "subscription"]
                senales_login = ["regístrate o inicia", "registrate o inicia", "introduce tu email",
                                 "introduce tu correo", "correo electrónico o nombre", "sign up or log in",
                                 "continuar con google", "continue with google"]
                if any(s in body_txt for s in senales_login):
                    return False
                if any(s in body_txt for s in senales_cuenta):
                    return True
                # account.tidal.com/ sin formulario de login tras OAuth = sesión casi siempre válida
                if not encontrar_locator_en_frames(
                    self.page,
                    ['input[type="email"]', 'input[name="email"]', '#email', 'input[type="password"]']
                ):
                    return True
        except Exception:
            pass
        return False

    def confirmar_sesion_en_perfil(self, timeout_s: float = 25.0) -> bool:
        """Prueba definitiva: abrir /profile y comprobar que Tidal NO redirige a login.tidal.com.

        Es el mismo criterio que usa migrar_cuentas_tidal.py y evita los falsos negativos del
        puente OAuth /login/tidal/return."""
        try:
            self.page = pagina_vigente(self.page)
            self.page.goto("https://account.tidal.com/profile", wait_until="domcontentloaded", timeout=30000)
            time.sleep(2.0)
            aceptar_cookies_con_espera(self.page)
            limite = time.time() + timeout_s
            while time.time() < limite:
                self.page = pagina_vigente(self.page)
                url = self.page.url.lower()
                if "login.tidal.com" in url or "/authorize" in url:
                    return False
                if self.hay_formulario_login_visible():
                    return False
                if "account.tidal.com" in url and "/login" not in url:
                    # Esperar un momento a que termine el return OAuth si aún está redirigiendo
                    if "/login/tidal/return" in url:
                        time.sleep(1.0)
                        continue
                    return True
                time.sleep(0.8)
        except Exception as e:
            print(f"  [Login] [{self.client_email}] [WARN] No se pudo confirmar sesión en perfil: {e}")
        return False

    def confirmar_cuenta_eliminada(self, timeout_s: float = 25.0) -> bool:
        """Prueba definitiva post-borrado: /profile debe redirigir a login.tidal.com/authorize.

        Misma navegación que confirmar_sesion_en_perfil, criterio invertido. NO basta con ver
        'login' tras ir a account.tidal.com/ (eso puede ser OAuth normal con sesión viva).

        Con proxy PE lento el goto a /profile a menudo hace Timeout aunque Tidal ya redirigió
        a /authorize (cuenta borrada). En ese caso hay que mirar la URL actual: si no, se marca
        fallo, se intenta un botón fantasma y Chrome puede quedar colgado a mitad de navegación.
        """
        def _url_indica_borrado() -> bool:
            try:
                self.page = pagina_vigente(self.page)
                url = (self.page.url or "").lower()
                if "login.tidal.com" in url or "/authorize" in url:
                    return True
                if self.hay_formulario_login_visible():
                    return True
            except Exception:
                pass
            return False

        try:
            self.page = pagina_vigente(self.page)
            print(f"  [Eliminación] [{self.client_email}] Verificando borrado en account.tidal.com/profile...")
            try:
                # commit tolera mejor la cadena profile→authorize tras el borrado
                self.page.goto(
                    "https://account.tidal.com/profile",
                    wait_until="commit",
                    timeout=45000,
                )
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=20000)
                except Exception:
                    pass
            except Exception as e_nav:
                if _url_indica_borrado():
                    print(
                        f"  [Eliminación] [{self.client_email}] Timeout/aborto en /profile pero la "
                        f"pestaña ya está en login/authorize. Cuenta eliminada confirmada."
                    )
                    return True
                print(f"  [Eliminación] [{self.client_email}] [WARN] Navegación a /profile falló: {e_nav}")
                # Reintento corto: a veces el primer goto queda colgado en el proxy
                try:
                    self.page.goto(
                        "https://account.tidal.com/profile",
                        wait_until="commit",
                        timeout=25000,
                    )
                    time.sleep(2.0)
                except Exception as e2:
                    if _url_indica_borrado():
                        print(
                            f"  [Eliminación] [{self.client_email}] Tras reintento, URL en "
                            f"login/authorize. Cuenta eliminada confirmada."
                        )
                        return True
                    print(f"  [Eliminación] [{self.client_email}] [WARN] No se pudo verificar borrado en perfil: {e2}")
                    return False

            time.sleep(1.5)
            aceptar_cookies_con_espera(self.page)
            if _url_indica_borrado():
                print(f"  [Eliminación] [{self.client_email}] /profile → login/authorize. Cuenta eliminada confirmada.")
                return True

            limite = time.time() + timeout_s
            while time.time() < limite:
                self.page = pagina_vigente(self.page)
                url = (self.page.url or "").lower()
                if "login.tidal.com" in url or "/authorize" in url:
                    print(f"  [Eliminación] [{self.client_email}] /profile → login/authorize. Cuenta eliminada confirmada.")
                    return True
                if self.hay_formulario_login_visible():
                    print(f"  [Eliminación] [{self.client_email}] Formulario de login visible tras /profile. Cuenta eliminada confirmada.")
                    return True
                if "account.tidal.com" in url and "/login" not in url:
                    if "/login/tidal/return" in url:
                        time.sleep(1.0)
                        continue
                    print(f"  [Eliminación] [{self.client_email}] /profile sigue en cuenta ({url[:80]}). Borrado NO confirmado.")
                    return False
                time.sleep(0.8)
            # Timeout del bucle: última mirada a la URL (proxy lento puede haber llegado tarde)
            if _url_indica_borrado():
                print(f"  [Eliminación] [{self.client_email}] Llegó a login/authorize al final de la espera. Cuenta eliminada confirmada.")
                return True
        except Exception as e:
            if _url_indica_borrado():
                print(
                    f"  [Eliminación] [{self.client_email}] Excepción al verificar, pero URL en "
                    f"login/authorize. Cuenta eliminada confirmada."
                )
                return True
            print(f"  [Eliminación] [{self.client_email}] [WARN] No se pudo verificar borrado en perfil: {e}")
        return False

    def _texto_exito_eliminacion_visible(self) -> bool:
        """True si algún frame muestra mensaje de éxito y ya no hay input de código."""
        for frame in self.page.frames:
            try:
                content = frame.evaluate("() => document.body.innerText").lower()
                if not any(kw in content for kw in (
                    "eliminada", "eliminado", "deleted", "correctamente", "exitosamente",
                    "success", "confirmada", "su cuenta ha sido",
                )):
                    continue
                for selector in (
                    'input[name="code"]',
                    'input[placeholder*="code" i]',
                    'input[placeholder*="código" i]',
                ):
                    try:
                        if frame.locator(selector).first.is_visible():
                            break
                    except Exception:
                        pass
                else:
                    return True
            except Exception:
                pass
        return False

    def _error_codigo_eliminacion_visible(self) -> bool:
        for frame in self.page.frames:
            try:
                error_loc = (
                    frame.locator("text=incorrecto")
                    .or_(frame.locator("text=inválido"))
                    .or_(frame.locator("text=invalid"))
                    .or_(frame.locator("text=incorrect"))
                    .first
                )
                if error_loc and error_loc.is_visible():
                    return True
            except Exception:
                pass
        return False

    def esperar_y_confirmar_eliminacion(self, timeout_s: float = 15.0) -> bool:
        """Tras pulsar confirmar: espera señal UI y confirma borrado real vía /profile.

        No trata 'login' en la URL del asistente/OAuth como éxito (falso positivo habitual).
        """
        self.page = pagina_vigente(self.page)
        senal_ui = False
        limite = time.time() + timeout_s
        while time.time() < limite:
            try:
                self.page = pagina_vigente(self.page)
                url = (self.page.url or "").lower()
                # Solo señales inequívocas en UI; salir de account-deletion puede ser OAuth, no borrado
                if "deleted" in url or "account-deleted" in url:
                    senal_ui = True
                    break
                if self._texto_exito_eliminacion_visible():
                    senal_ui = True
                    break
                if self._error_codigo_eliminacion_visible():
                    print(f"  [Eliminación] [{self.client_email}] Código rechazado en el asistente.")
                    return False
            except Exception:
                pass
            time.sleep(0.5)

        if senal_ui:
            print(f"  [Eliminación] [{self.client_email}] Señal de éxito en UI; confirmando en /profile...")
        else:
            print(f"  [Eliminación] [{self.client_email}] Sin señal UI clara; confirmando borrado en /profile...")

        return self.confirmar_cuenta_eliminada(20.0)

    def esperar_redireccion_login_o_sesion(self, timeout_s: float = 12.0) -> None:
        """Tras abrir account.tidal.com/, espera a que Tidal decida: login OAuth o sesión real."""
        limite = time.time() + timeout_s
        while time.time() < limite:
            try:
                self.page = pagina_vigente(self.page)
                if self.hay_formulario_login_visible():
                    return
                if self.es_sesion_activa():
                    time.sleep(1.0)
                    if self.es_sesion_activa() and not self.hay_formulario_login_visible():
                        return
            except Exception:
                pass
            time.sleep(0.5)

    def esperar_establecimiento_sesion(self, timeout_s: float = 40.0) -> bool:
        """Tras enviar contraseña/código: espera salir de login.tidal.com y confirma en /profile.

        No pulsa botones genéricos durante la espera: en la pantalla de contraseña el primer
        <button> visible es la flecha atrás junto al correo, y pulsarla devolvía al paso 1.
        """
        limite = time.time() + timeout_s
        reintentos_login_btn = 0
        while time.time() < limite:
            self.page = pagina_vigente(self.page)
            try:
                if detectar_pantalla_antirobot(self.page):
                    # No rotar proxy a mitad del submit de contraseña: eso aborta el OAuth.
                    # Solo intentar el slider si hay captcha real sin campos de login.
                    if not encontrar_locator_en_frames(self.page, ['input[type="password"]', 'input[type="email"]']):
                        manejar_bloqueos_e_intervencion(self.page, f"Login Tidal ({self.client_email})")
                        self.page = pagina_vigente(self.page)
            except RuntimeError:
                raise
            except Exception:
                pass

            url = ""
            try:
                url = self.page.url.lower()
            except Exception:
                pass

            # Consentimiento OAuth real (texto específico; nunca 'button' genérico)
            btn_consent = encontrar_locator_en_frames(
                self.page,
                [
                    "button:has-text('Sí, continuar')", "button:has-text('Si, continuar')",
                    "button:has-text('Yes, continue')", "button:has-text('Permitir')",
                    "button:has-text('Allow')", "button:has-text('Aceptar y continuar')"
                ],
                text_regex=re.compile(r"^(sí,\s*continuar|si,\s*continuar|yes,\s*continue|permitir|allow|aceptar y continuar)$", re.I)
            )
            if btn_consent:
                try:
                    btn_consent.click(timeout=2500)
                    time.sleep(2.0)
                    continue
                except Exception:
                    pass

            if self.es_sesion_activa():
                return True

            # Ya salimos del dominio de login o estamos en el puente return → confirmar en perfil
            if "/login/tidal/return" in url or "/login/tidal/callback" in url:
                time.sleep(1.5)
                break
            if "account.tidal.com" in url and "login.tidal.com" not in url and "/authorize" not in url:
                break

            # Tras captcha/Turnstile el botón puede volver a "Inicia Sesión": reenviar una vez
            if "login.tidal.com" in url and reintentos_login_btn < 2:
                pwd_still = encontrar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]'])
                btn_login = encontrar_locator_en_frames(
                    self.page,
                    [
                        "button[type='submit']",
                        "button:has-text('Inicia Sesión')", "button:has-text('Inicia sesión')",
                        "button:has-text('Iniciar sesión')", "button:has-text('Log in')"
                    ],
                    text_regex=re.compile(r"^(inicia\s*sesión|iniciar\s*sesión|log\s*in|continuar|continue)$", re.I)
                )
                if pwd_still and btn_login:
                    try:
                        if not btn_login.is_disabled():
                            # Comprobar que hay valor en el password antes de re-pulsar
                            try:
                                val = pwd_still.input_value()
                            except Exception:
                                val = "x"
                            if val:
                                print(f"  [Login] [{self.client_email}] Re-pulsando 'Inicia Sesión' tras verificación antirobot...")
                                btn_login.click(timeout=2500, force=True)
                                reintentos_login_btn += 1
                                time.sleep(2.0)
                                continue
                    except Exception:
                        pass

            time.sleep(1.0)

        if self.es_sesion_activa():
            return True
        # Solo forzar /profile cuando ya no estamos a mitad de un submit OAuth reciente
        try:
            url_now = self.page.url.lower()
        except Exception:
            url_now = ""
        if "login.tidal.com" in url_now and encontrar_locator_en_frames(self.page, ['input[type="password"]']):
            # Seguir en contraseña = el submit no terminó; no abortar navegando al perfil
            return False
        return self.confirmar_sesion_en_perfil(18.0)

    def pulsar_continuar_login(self) -> bool:
        """Espera a que Continuar se habilite; si React lo deja disabled, lo fuerza por JS."""
        # Texto exacto con :text-is: has-text('Continuar') también matchea 'Continuar con Google',
        # y :has-text(/regex/) no existe en Playwright (lanza error de selector y no encuentra nada).
        selectores = [
            'button:text-is("Continuar")', 'button:text-is("Continue")', "button[type='submit']"
        ]
        btn = esperar_locator_en_frames(
            self.page, selectores,
            text_regex=re.compile(r"^(continuar|continue)$", re.I),
            timeout_s=8.0
        )
        if not btn:
            return False
        limite = time.time() + 6.0
        while time.time() < limite:
            try:
                if not btn.is_disabled():
                    break
            except Exception:
                break
            time.sleep(0.3)
        try:
            if not btn.is_disabled():
                btn.click(timeout=3000)
                return True
        except Exception:
            pass
        # Fallback: React a veces deja el botón disabled aunque el correo ya es válido
        try:
            btn.evaluate("""el => {
                el.removeAttribute('disabled');
                el.disabled = false;
                el.setAttribute('aria-disabled', 'false');
                el.click();
            }""")
            return True
        except Exception:
            try:
                btn.click(timeout=2000, force=True)
                return True
            except Exception:
                return False

    def escribir_correo_login(self, email_input) -> None:
        """Escribe el correo de forma que React habilite el botón Continuar."""
        try:
            email_input.click(timeout=3000)
            email_input.fill("")
            time.sleep(0.1)
        except Exception:
            pass
        escrito = False
        # 1) Tecleo caracter a caracter (mejor para React controlado)
        try:
            email_input.press_sequentially(self.client_email, delay=25)
            escrito = True
        except Exception:
            pass
        if not escrito:
            try:
                email_input.type(self.client_email, delay=25)
                escrito = True
            except Exception:
                pass
        if not escrito:
            rellenar_campo_humanizado(email_input, self.client_email)
        time.sleep(0.35)
        try:
            email_input.dispatch_event("input")
            email_input.dispatch_event("change")
            email_input.dispatch_event("blur")
        except Exception:
            pass
        # Disparar InputEvent nativo por si Playwright no lo propaga al estado de React
        try:
            email_input.evaluate("""(el, val) => {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""", self.client_email)
        except Exception:
            pass

    def rehacer_login_credenciales(self) -> bool:
        """Re-login completo cuando Tidal expulsó la sesión al ir al perfil."""
        print(f"  [Login] [{self.client_email}] Rehaciendo inicio de sesión (sesión perdida o falsa)...")
        try:
            try:
                self.context.clear_cookies(domain="tidal.com")
                self.context.clear_cookies(domain="login.tidal.com")
                self.context.clear_cookies(domain="account.tidal.com")
            except Exception:
                pass
            self.page.goto("https://account.tidal.com/", wait_until="domcontentloaded", timeout=30000, referer="https://tidal.com/pricing")
            manejar_bloqueos_e_intervencion(self.page, "Re-login Tidal")
            self.page = pagina_vigente(self.page)
            aceptar_cookies_con_espera(self.page)
            self.esperar_redireccion_login_o_sesion(10.0)
            if self.es_sesion_activa() or self.confirmar_sesion_en_perfil(12.0):
                return True

            email_selectors = ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]', '#email']
            email_input = esperar_locator_en_frames(self.page, email_selectors, timeout_s=15.0)
            if not email_input:
                return False

            self.escribir_correo_login(email_input)
            if not self.pulsar_continuar_login():
                try:
                    email_input.press("Enter")
                except Exception:
                    pass
            time.sleep(2.0)

            pwd_input = esperar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]'], timeout_s=6.0)
            if not pwd_input and (self.hay_pantalla_codigo_login() or self.hay_control_modo_contrasena()):
                for _ in range(3):
                    if self.hay_control_modo_contrasena():
                        self.clic_modo_contrasena()
                    pwd_input = esperar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]'], timeout_s=6.0)
                    if pwd_input:
                        break

            if not pwd_input:
                pwd_input = esperar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]'], timeout_s=10.0)
            if not pwd_input:
                if self.hay_pantalla_codigo_login():
                    return self.iniciar_sesion_con_codigo_email(obtener_max_email_id(self.client_email, "tidal"))
                return False

            try:
                pwd_input.fill("")
                time.sleep(0.15)
                pwd_input.fill(self.target_pwd)
            except Exception:
                rellenar_campo_humanizado(pwd_input, self.target_pwd)
            time.sleep(0.4)

            btn_login = esperar_locator_en_frames(
                self.page,
                ["button[type='submit']", "button:has-text('Iniciar sesión')", "button:has-text('Log in')",
                 "button:has-text('Continuar')", "button:has-text('Continue')"],
                timeout_s=8.0
            )
            if btn_login:
                try:
                    btn_login.click(timeout=2500, force=True)
                except Exception:
                    pass
            else:
                try:
                    pwd_input.press("Enter")
                except Exception:
                    pass

            return self.esperar_establecimiento_sesion(40.0)
        except Exception as e:
            print(f"  [Login] {Color.WARNING}[WARN] [{self.client_email}] Falló el re-login: {e}{Color.ENDC}")
            return False

    def _entrar_login_desde_pricing(self) -> bool:
        """Desde tidal.com/pricing, entra al login pulsando 'Iniciar sesión' o por goto directo.

        Cuando page.goto(account.../login) aborta y deja la pestaña en /pricing, este camino
        orgánico (clic en el CTA visible) suele completar la redirección a login.tidal.com.
        """
        self.page = pagina_vigente(self.page)
        if not self.page or self.page.is_closed():
            return False

        # 1) Clic en el botón/enlace visible de la cabecera
        for sel, regex in (
            ('a:has-text("Iniciar sesión")', r"iniciar\s*sesión|log\s*in|sign\s*in"),
            ('button:has-text("Iniciar sesión")', r"iniciar\s*sesión|log\s*in|sign\s*in"),
            ('a:has-text("Log in")', r"log\s*in|sign\s*in|iniciar"),
            ('a[href*="login"]', r".*"),
            ('a[href*="signin"]', r".*"),
        ):
            try:
                loc = esperar_locator_en_frames(
                    self.page, [sel],
                    text_regex=re.compile(regex, re.I) if regex != r".*" else None,
                    timeout_s=3.0
                )
                if loc:
                    try:
                        loc.click(timeout=4000)
                    except Exception:
                        loc.evaluate("el => el.click()")
                    time.sleep(2.5)
                    if url_es_login_o_cuenta(self.page.url or "") and not es_pantalla_error_login_tidal(self.page):
                        print(f"  [Bypass] [{self.client_email}] Entró al login vía CTA de pricing.")
                        return True
                    if es_pantalla_error_login_tidal(self.page):
                        print(f"  [Bypass] [{self.client_email}] CTA de pricing llevó a Error genérico.")
                        break
            except Exception:
                continue

        # 2) Fallback JS por si el selector no alcanzó el enlace de la barra
        try:
            pulsado = self.page.evaluate("""() => {
                const re = /(iniciar\\s*sesi[oó]n|log\\s*in|sign\\s*in)/i;
                const nodes = Array.from(document.querySelectorAll('a, button, [role="button"]'));
                for (const el of nodes) {
                    const t = (el.innerText || el.textContent || '').trim();
                    const href = (el.getAttribute('href') || '').toLowerCase();
                    if (re.test(t) || href.includes('login') || href.includes('signin')) {
                        try { el.click(); return true; } catch(e) {}
                    }
                }
                return false;
            }""")
            if pulsado:
                time.sleep(2.5)
                if es_pantalla_error_login_tidal(self.page):
                    print(f"  [Bypass] [{self.client_email}] JS desde pricing llevó a Error genérico.")
                elif url_es_login_o_cuenta(self.page.url or ""):
                    print(f"  [Bypass] [{self.client_email}] Entró al login vía JS desde pricing.")
                    return True
        except Exception:
            pass

        # 3) Preferir account.tidal.com/login (cadena OAuth) — /signin en frío provoca 'Algo salió mal'
        try:
            navegar_tidal_tolerante(
                self.page, "https://account.tidal.com/login",
                referer="https://tidal.com/pricing",
                timeout_ms=30000
            )
            time.sleep(1.5)
            if es_pantalla_error_login_tidal(self.page):
                print(f"  [Bypass] [{self.client_email}] account/login devolvió Error genérico.")
                return False
            if url_es_login_o_cuenta(self.page.url or "") and not es_pantalla_error_login_tidal(self.page):
                print(f"  [Bypass] [{self.client_email}] Entró a account.tidal.com/login con referer.")
                return True
        except Exception:
            pass

        # 4) Último recurso: /signin, pero NO contar éxito si aparece la pantalla Error
        try:
            navegar_tidal_tolerante(
                self.page, "https://login.tidal.com/signin",
                referer="https://tidal.com/pricing",
                timeout_ms=30000
            )
            time.sleep(1.5)
            if es_pantalla_error_login_tidal(self.page):
                print(f"  [Bypass] [{self.client_email}] /signin mostró 'Algo salió mal' — no cuenta como login.")
                return False
            if url_es_login_o_cuenta(self.page.url or ""):
                print(f"  [Bypass] [{self.client_email}] Entró a login.tidal.com/signin.")
                return True
        except Exception:
            pass
        return (
            url_es_login_o_cuenta(self.page.url or "")
            and not es_pantalla_error_login_tidal(self.page)
        )

    def run_auto_login(self) -> bool:
        try:
            self.asegurar_navegador_abierto()
            
            # 1. Abrir navegador y cargar página de login en Tidal con bypass de reputación
            print(f"  [Navegador] [{self.client_email}] Abriendo ventana de Chrome mediante proxy de Perú...")
            
            nav_exitoso = False
            # Misma cuota: cada intento duro puede rotar. Los ERR_ABORTED no consumen rotación
            # ni agotan el cupón antes de que los fallos de red reales puedan rotar.
            _max_intentos_nav = 5
            _max_rotaciones_proxy = _max_intentos_nav  # antes era 3 con 5 intentos → intentos finales sin IP
            rotaciones_proxy = 0
            for intento_nav in range(1, _max_intentos_nav + 1):
                try:
                    # Bypass de reputación: visitar tidal.com/pricing primero para acumular confianza
                    print(f"  [Bypass] [{self.client_email}] Cargando tidal.com/pricing para acumular reputación (intento {intento_nav}/{_max_intentos_nav})...")
                    navegar_tidal_tolerante(self.page, "https://tidal.com/pricing", timeout_ms=30000)
                    manejar_bloqueos_e_intervencion(self.page, "Bypass Precios")
                    time.sleep(random.uniform(2.0, 3.5))
                    aceptar_cookies_con_espera(self.page)
                    time.sleep(random.uniform(0.5, 1.0))

                    # Entrar al login. Si goto aborta y deja la pestaña en /pricing, se intenta
                    # el botón "Iniciar sesión" de la propia página (camino orgánico).
                    print(f"  [Bypass] [{self.client_email}] Redirigiendo a account.tidal.com/login con referer...")
                    try:
                        navegar_tidal_tolerante(
                            self.page, "https://account.tidal.com/login",
                            referer="https://tidal.com/pricing",
                            timeout_ms=30000
                        )
                    except Exception as e_login_nav:
                        if not url_es_pagina_marketing(self.page.url if self.page else ""):
                            raise
                        print(f"  [Bypass] [{self.client_email}] goto /login falló ({e_login_nav}). "
                              f"Pulsando 'Iniciar sesión' en la página de precios...")
                        if not self._entrar_login_desde_pricing():
                            raise

                    # Si tras todo seguimos en marketing, el bypass no sirvió
                    if url_es_pagina_marketing(self.page.url if self.page else ""):
                        raise RuntimeError(
                            f"Tras el bypass la pestaña sigue en marketing: {(self.page.url or '')[:80]}"
                        )

                    manejar_bloqueos_e_intervencion(self.page, "Login Tidal")
                    # Esperar a que Tidal redirija a /authorize o confirme sesión real (evita falso INSTANTÁNEO)
                    self.esperar_redireccion_login_o_sesion(12.0)
                    nav_exitoso = True
                    break
                except Exception as e_nav:
                    print(f"  [Login] {Color.WARNING}[WARN] [{self.client_email}] Intento {intento_nav}/{_max_intentos_nav} de navegación con proxy falló ({e_nav})...{Color.ENDC}")

                    # ERR_ABORTED / redirect: reintentar con el MISMO proxy. Solo rotar ante
                    # fallo real de túnel/conectividad.
                    if es_error_navegacion_abortada(e_nav) and not es_error_proxy_o_red(e_nav):
                        try:
                            actual = (self.page.url or "").lower() if self.page and not self.page.is_closed() else ""
                        except Exception:
                            actual = ""
                        if url_es_login_o_cuenta(actual):
                            print(f"  [Login] [{self.client_email}] Navegación abortada pero la pestaña "
                                  f"quedó en login/cuenta ({actual[:70]}). Se continúa sin rotar proxy.")
                            try:
                                manejar_bloqueos_e_intervencion(self.page, "Login Tidal")
                                self.esperar_redireccion_login_o_sesion(12.0)
                                nav_exitoso = True
                                break
                            except Exception:
                                pass
                        if intento_nav < _max_intentos_nav:
                            print(f"  [Login] [{self.client_email}] No se alcanzó el login "
                                  f"(URL actual: {actual[:70] or 'desconocida'}). "
                                  f"Reintentando con el mismo proxy...")
                            time.sleep(random.uniform(1.5, 2.5))
                            continue
                        raise RuntimeError(
                            f"Agotados los reintentos suaves (ERR_ABORTED) para {self.client_email} "
                            f"sin alcanzar el login."
                        )

                    if not es_error_proxy_o_red(e_nav) and intento_nav < _max_intentos_nav:
                        print(f"  [Login] [{self.client_email}] Fallo no atribuible al proxy. "
                              f"Reintentando sin rotar IP...")
                        time.sleep(random.uniform(1.5, 2.5))
                        continue

                    if not es_error_proxy_o_red(e_nav):
                        raise RuntimeError(
                            f"Fallo de navegación no recuperable para {self.client_email}: {e_nav}"
                        )

                    if rotaciones_proxy >= _max_rotaciones_proxy:
                        raise RuntimeError(
                            f"Agotadas las {_max_rotaciones_proxy} rotaciones de proxy de Perú para "
                            f"{self.client_email} tras fallos de red/túnel."
                        )

                    global GLOBAL_PE_PROXY_POOL
                    proxy_quemado = self.proxy_pe_server
                    p_pe = GLOBAL_PE_PROXY_POOL.rotar_y_marcar_bloqueado(proxy_quemado)
                    nuevo_serv = p_pe.get("server") if p_pe else None
                    if not nuevo_serv or nuevo_serv == proxy_quemado:
                        raise RuntimeError(
                            f"No queda ningún proxy de Perú limpio para {self.client_email} "
                            f"tras descartar {proxy_quemado}. Se aborta la cuenta."
                        )
                    rotaciones_proxy += 1
                    print(f"  [Login] [{self.client_email}] Rotación de proxy PE "
                          f"{rotaciones_proxy}/{_max_rotaciones_proxy} tras fallo de red/túnel.")
                    self.proxy_pe_server = nuevo_serv
                    self.proxy_pe_user = p_pe.get("username")
                    self.proxy_pe_pass = p_pe.get("password")

                    # Re-inicializar con perfil nuevo limpio y el nuevo proxy
                    try:
                        if self.context:
                            self.context.close()
                    except Exception:
                        pass
                    try:
                        if self.playwright:
                            self.playwright.stop()
                    except Exception:
                        pass
                    self.context = None
                    self.page = None
                    self.playwright = None
                    # Generar perfil nuevo para no arrastrar banderas de DataDome
                    old_prof = self.main_profile
                    email_safe = re.sub(r'[^a-zA-Z0-9]', '_', self.client_email)
                    self.main_profile = Path(tempfile.gettempdir()) / f"tidal_chrome_profile_{email_safe}_{random.randint(1000, 9999)}"
                    try:
                        import shutil
                        if old_prof.exists():
                            shutil.rmtree(old_prof, ignore_errors=True)
                    except Exception:
                        pass
                    time.sleep(1.5)
                    self.asegurar_navegador_abierto()

            if not nav_exitoso:
                raise RuntimeError(f"Imposible conectar a la página de login de Tidal para {self.client_email} mediante los proxies de Perú.")

            # Cierre de seguridad: si el bypass dejó la ventana en /pricing, no seguir al login
            if url_es_pagina_marketing(self.page.url if self.page else ""):
                print(f"  [Bypass] [{self.client_email}] Aún en página de precios. Forzando entrada al login...")
                if not self._entrar_login_desde_pricing():
                    raise RuntimeError(
                        f"La ventana quedó en {(self.page.url or '')[:80]} sin alcanzar el login de Tidal."
                    )

            aceptar_cookies_con_espera(self.page)

            if not self.es_sesion_activa():
                # Esperar formulario (máx 8s) únicamente si la sesión NO está iniciada
                esperar_locator_en_frames(
                    self.page, 
                    ['input[type="email"]', 'input[type="password"]', '#email', 'input[name="email"]'], 
                    timeout_s=8.0
                )
            
            print(f"  [Navegador] {Color.GREEN}[Cargado] Ventana cargada y lista para: {self.client_email}{Color.ENDC}")
            
            # Sincronización inicial: Esperar a que TODAS las ventanas de Chrome estén completamente cargadas
            self.esperar_barrera("inicio")
            
            print(f"\n--- Procesando inicio de sesión para: {self.client_email} ---")

            # Tras la barrera otra ventana puede haber tocado el DOM; revalidar que no volvimos a pricing
            if url_es_pagina_marketing(self.page.url if self.page else ""):
                print(f"  [Login] [{self.client_email}] Volvió a pricing tras la sincronización. Reentrando al login...")
                if not self._entrar_login_desde_pricing():
                    raise RuntimeError(
                        f"No se pudo abrir el login: la ventana sigue en {(self.page.url or '')[:80]}"
                    )
            sesion_ya_ok = False
            if self.es_sesion_activa():
                # Confirmar de verdad en /profile: un flash de account.tidal.com/ no basta
                if self.confirmar_sesion_en_perfil(15.0):
                    print(f"  [Login] {Color.GREEN}[INSTANTÁNEO] Sesión ya iniciada y confirmada para: {self.client_email}{Color.ENDC}")
                    sesion_ya_ok = True
                else:
                    # confirmar_sesion_en_perfil navega a /profile y suele dejar la pestaña en
                    # authorize/login vacío: sin reabrir el flujo no hay campo de contraseña
                    # (caso s.im.plypretty803 — error inmediato sin reintento).
                    print(f"  [Login] {Color.WARNING}[WARN] [{self.client_email}] Falsa sesión detectada. "
                          f"Reabriendo login limpio vía pricing...{Color.ENDC}")
                    try:
                        if not self.recuperar_login_tras_error_tidal():
                            navegar_tidal_tolerante(
                                self.page, "https://account.tidal.com/login",
                                referer="https://tidal.com/pricing",
                                timeout_ms=30000,
                            )
                            time.sleep(1.5)
                    except Exception as e_fs:
                        print(f"  [Login] [{self.client_email}] [WARN] Reapertura tras falsa sesión: {e_fs}")
                    aceptar_cookies_con_espera(self.page)
                    manejar_bloqueos_e_intervencion(self.page, "Login tras falsa sesión")

            if not sesion_ya_ok:
                # Paso 1: Ingreso de correo con verificación de avance
                paso_correo_ok = False
                pantalla_codigo = False
                base_login_id = 0
                email_selectors = ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]', '#email']

                def estado_login(timeout_s=0.0):
                    """Estado de la pantalla: 'sesion', 'password', 'modo_password', 'codigo' o None."""
                    limite_estado = time.time() + timeout_s
                    while True:
                        if self.es_sesion_activa():
                            return "sesion"
                        if encontrar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]']):
                            return "password"
                        # Un campo de correo visible significa que seguimos en el primer paso
                        if not encontrar_locator_en_frames(self.page, email_selectors):
                            if self.hay_control_modo_contrasena():
                                return "modo_password"
                            if self.hay_pantalla_codigo_login():
                                return "codigo"
                        if time.time() >= limite_estado:
                            return None
                        time.sleep(0.5)

                def _recuperar_campo_password(motivo: str) -> object | None:
                    """Reintentos reales cuando no hay input password (código / pricing / antibot)."""
                    nonlocal base_login_id
                    print(f"  [Login] [{self.client_email}] Recuperando formulario de contraseña ({motivo})...")

                    # 1) Pantalla de código → 'Inicia sesión con contraseña'
                    if self.hay_pantalla_codigo_login() or self.hay_control_modo_contrasena():
                        for intento_modo in range(1, 4):
                            if not self.hay_control_modo_contrasena():
                                break
                            print(f"  [Login] [{self.client_email}] Pantalla de código. Pulsando modo contraseña "
                                  f"(recuperación {intento_modo}/3)...")
                            self.clic_modo_contrasena()
                            loc = esperar_locator_en_frames(
                                self.page, ['input[type="password"]', 'input[name="password"]'], timeout_s=8.0
                            )
                            if loc:
                                print(f"  [Login] {Color.GREEN}[OK] [{self.client_email}] Formulario de contraseña abierto.{Color.ENDC}")
                                return loc
                            time.sleep(1.0)

                    # 2) Error genérico /authorize → pricing → login
                    if es_pantalla_error_login_tidal(self.page) or url_es_oauth_login_roto(self.page.url or ""):
                        try:
                            self.recuperar_login_tras_error_tidal()
                        except Exception:
                            pass

                    # 3) Reabrir login limpio y reenviar correo
                    try:
                        if not _formulario_login_visible(self.page):
                            navegar_tidal_tolerante(self.page, "https://tidal.com/pricing", timeout_ms=25000)
                            time.sleep(1.0)
                            aceptar_cookies_con_espera(self.page)
                            if not self._entrar_login_desde_pricing():
                                navegar_tidal_tolerante(
                                    self.page, "https://account.tidal.com/login",
                                    referer="https://tidal.com/pricing",
                                    timeout_ms=30000,
                                )
                        manejar_bloqueos_e_intervencion(self.page, "Recuperación login (password)")
                        email_input = esperar_locator_en_frames(self.page, email_selectors, timeout_s=12.0)
                        if email_input:
                            if not base_login_id:
                                base_login_id = obtener_max_email_id(self.client_email, "tidal")
                            print(f"  [Login] [{self.client_email}] Reenviando correo tras recuperación...")
                            self.escribir_correo_login(email_input)
                            if not self.pulsar_continuar_login():
                                try:
                                    email_input.press("Enter")
                                except Exception:
                                    pass
                            estado = estado_login(20.0)
                            if estado == "password":
                                return encontrar_locator_en_frames(
                                    self.page, ['input[type="password"]', 'input[name="password"]']
                                )
                            if estado in ("codigo", "modo_password"):
                                for _ in range(3):
                                    if self.hay_control_modo_contrasena():
                                        self.clic_modo_contrasena()
                                    loc = esperar_locator_en_frames(
                                        self.page, ['input[type="password"]', 'input[name="password"]'], timeout_s=8.0
                                    )
                                    if loc:
                                        return loc
                                    time.sleep(0.8)
                    except Exception as e_rec:
                        print(f"  [Login] [{self.client_email}] [WARN] Recuperación password falló: {e_rec}")

                    return encontrar_locator_en_frames(
                        self.page, ['input[type="password"]', 'input[name="password"]']
                    )

                for intento_email in range(1, 4):
                    estado = estado_login()
                    if estado:
                        paso_correo_ok = True
                        pantalla_codigo = estado in ("codigo", "modo_password")
                        break

                    email_input = esperar_locator_en_frames(self.page, email_selectors, label_regex=re.compile(r"correo|email", re.I), timeout_s=15.0)
                    if email_input:
                        # Línea base del buzón antes de que Tidal pueda enviar el código de acceso
                        if not base_login_id:
                            base_login_id = obtener_max_email_id(self.client_email, "tidal")

                        print(f"  [Login] [{self.client_email}] Ingresando correo (intento {intento_email}/3)...")
                        self.escribir_correo_login(email_input)

                        if not self.pulsar_continuar_login():
                            try:
                                email_input.press("Enter")
                            except Exception:
                                pass

                        # Tidal tarda en pintar la pantalla siguiente (y en enviar el código):
                        # esperar el avance real en vez de decidir a los 2 segundos.
                        estado = estado_login(20.0)
                        if estado:
                            paso_correo_ok = True
                            pantalla_codigo = estado in ("codigo", "modo_password")
                            break
                        print(f"  [Login] {Color.WARNING}[WARN] [{self.client_email}] Sin avance tras enviar el correo (intento {intento_email}/3).{Color.ENDC}")

                curr_url_check = self.page.url.lower()
                if not paso_correo_ok and not self.es_sesion_activa() and not ("/profile" in curr_url_check or "/settings" in curr_url_check):
                    # Último intento: reapertura limpia antes de abortar el correo
                    print(f"  [Login] [{self.client_email}] Sin avance de correo. Intentando reapertura vía pricing...")
                    try:
                        self.recuperar_login_tras_error_tidal()
                        email_input = esperar_locator_en_frames(self.page, email_selectors, timeout_s=12.0)
                        if email_input:
                            if not base_login_id:
                                base_login_id = obtener_max_email_id(self.client_email, "tidal")
                            self.escribir_correo_login(email_input)
                            if not self.pulsar_continuar_login():
                                email_input.press("Enter")
                            estado = estado_login(20.0)
                            if estado:
                                paso_correo_ok = True
                                pantalla_codigo = estado in ("codigo", "modo_password")
                    except Exception:
                        pass
                    if not paso_correo_ok and not self.es_sesion_activa():
                        raise RuntimeError(f"No se pudo avanzar de la pantalla de correo para {self.client_email}.")

                manejar_bloqueos_e_intervencion(self.page, "Login Tidal (Contraseña)")

                pwd_input = encontrar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]'])
                login_por_codigo = False

                # Tidal manda a la pantalla del código: hay que cambiar a modo contraseña
                if not pwd_input and (pantalla_codigo or self.hay_pantalla_codigo_login() or self.hay_control_modo_contrasena()):
                    for intento_modo in range(1, 4):
                        if not self.hay_control_modo_contrasena():
                            print(f"  [Login] {Color.WARNING}[WARN] [{self.client_email}] La pantalla del código no ofrece la opción de contraseña.{Color.ENDC}")
                            break
                        print(f"  [Login] [{self.client_email}] Pantalla de código detectada. Pulsando 'Inicia sesión con contraseña' (intento {intento_modo}/3)...")
                        self.clic_modo_contrasena()
                        pwd_input = esperar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]'], timeout_s=8.0)
                        if pwd_input:
                            print(f"  [Login] {Color.GREEN}[OK] [{self.client_email}] Formulario de contraseña abierto.{Color.ENDC}")
                            break
                        time.sleep(1.0)

                    # Si no hay forma de usar la contraseña, entrar con el código que Tidal ya envió
                    if not pwd_input and self.hay_pantalla_codigo_login():
                        print(f"  [Login] [{self.client_email}] Sin acceso al formulario de contraseña. Usando el código de acceso del correo...")
                        login_por_codigo = self.iniciar_sesion_con_codigo_email(base_login_id)

                # Si no se encuentra el campo de contraseña, verificar si la pantalla sigue pidiendo correo
                if not pwd_input and not login_por_codigo:
                    email_back = encontrar_locator_en_frames(self.page, email_selectors)
                    if email_back:
                        print(f"  [Login] {Color.WARNING}[WARN] [{self.client_email}] Re-intentando envío de correo (desfase detectado)...{Color.ENDC}")
                        try:
                            self.escribir_correo_login(email_back)
                            if not self.pulsar_continuar_login():
                                email_back.press("Enter")
                        except Exception:
                            pass
                        time.sleep(2.5)
                        pwd_input = esperar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]'], timeout_s=12.0)
                        if not pwd_input and (self.hay_pantalla_codigo_login() or self.hay_control_modo_contrasena()):
                            self.clic_modo_contrasena()
                            pwd_input = esperar_locator_en_frames(
                                self.page, ['input[type="password"]', 'input[name="password"]'], timeout_s=8.0
                            )
                
                login_exitoso = login_por_codigo
                for intento_pwd in range(1, 5):
                    if login_exitoso:
                        break
                    # Solo confirmar en /profile si YA salimos del flujo de login (no navegar
                    # ahí mientras aún estamos en la pantalla de contraseña).
                    if self.es_sesion_activa():
                        if self.confirmar_sesion_en_perfil(12.0):
                            login_exitoso = True
                            break
                    pwd_input = esperar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]'], timeout_s=12.0)
                    if not pwd_input:
                        if self.es_sesion_activa() and self.confirmar_sesion_en_perfil(10.0):
                            login_exitoso = True
                            break
                        # Antes: al 2º intento lanzaba RuntimeError sin recuperar (log s.im.plypretty803)
                        if intento_pwd < 4:
                            print(f"  [Login] {Color.WARNING}[WARN] [{self.client_email}] Sin campo contraseña "
                                  f"(intento {intento_pwd}/4). Recuperando...{Color.ENDC}")
                            pwd_input = _recuperar_campo_password(f"intento {intento_pwd}/4")
                            if pwd_input:
                                pass  # continuar al fill abajo
                            elif self.hay_pantalla_codigo_login():
                                print(f"  [Login] [{self.client_email}] Fallback a código de acceso del correo...")
                                if not base_login_id:
                                    base_login_id = obtener_max_email_id(self.client_email, "tidal")
                                if self.iniciar_sesion_con_codigo_email(base_login_id):
                                    login_exitoso = True
                                    break
                                continue
                            else:
                                # Rotar PE si hay antibot / error genérico
                                if detectar_pantalla_antirobot(self.page) or es_pantalla_error_login_tidal(self.page):
                                    try:
                                        self.ejecutar_rotacion_proxy_y_recargar()
                                    except Exception as e_rot:
                                        print(f"  [Login] [{self.client_email}] [WARN] Rotación PE: {e_rot}")
                                continue
                        raise RuntimeError(
                            "No se localizó el campo de contraseña tras varios reintentos de recuperación."
                        )

                    try:
                        pwd_input.click(timeout=3000)
                        pwd_input.fill("")
                        time.sleep(0.15)
                        pwd_input.fill(self.target_pwd)
                    except Exception:
                        rellenar_campo_humanizado(pwd_input, self.target_pwd)
                    time.sleep(0.4)
                    try:
                        pwd_input.dispatch_event("input")
                        pwd_input.dispatch_event("change")
                    except Exception:
                        pass
                    # Forzar que React habilite "Inicia Sesión" (igual que Continuar con el correo)
                    try:
                        pwd_input.evaluate("""(el, val) => {
                            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                            setter.call(el, val);
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }""", self.target_pwd)
                    except Exception:
                        pass
                    time.sleep(0.5)

                    btn_login = esperar_locator_en_frames(
                        self.page,
                        [
                            "button[type='submit']",
                            'button:text-is("Iniciar sesión")', 'button:text-is("Inicia Sesión")',
                            'button:text-is("Inicia sesión")', 'button:text-is("Log in")',
                            'button:text-is("Continuar")', 'button:text-is("Continue")',
                            "button:has-text('Iniciar sesión')", "button:has-text('Inicia Sesión')",
                            "button:has-text('Log in')"
                        ],
                        timeout_s=10.0
                    )
                    # Esperar a que el botón deje de estar disabled (React lo habilita al escribir)
                    if btn_login:
                        limite_btn = time.time() + 6.0
                        while time.time() < limite_btn:
                            try:
                                if not btn_login.is_disabled():
                                    break
                            except Exception:
                                break
                            time.sleep(0.3)
                        try:
                            if btn_login.is_disabled():
                                btn_login.evaluate("""el => {
                                    el.removeAttribute('disabled');
                                    el.disabled = false;
                                    el.click();
                                }""")
                            else:
                                btn_login.click(timeout=2500)
                        except Exception:
                            try:
                                btn_login.click(timeout=2000, force=True)
                            except Exception:
                                try:
                                    pwd_input.press("Enter")
                                except Exception:
                                    pass
                    else:
                        try:
                            pwd_input.press("Enter")
                        except Exception:
                            pass
                    time.sleep(2.5)

                    # Detectar contraseña incorrecta YA, antes de esperar 40s a una sesión que no llegará
                    error_pwd = False
                    try:
                        for frame in self.page.frames:
                            try:
                                body_text = (frame.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''") or "")
                                if any(k in body_text for k in ["incorrecto", "incorrect", "invalid"]):
                                    if any(k in body_text for k in ["contraseña", "password", "usuario", "username", "correo"]):
                                        error_pwd = True
                                        break
                            except Exception:
                                pass
                        if not error_pwd:
                            for err_sel in [
                                "text='Contraseña incorrecta'", "text='Invalid password'", "text='Wrong password'",
                                "[data-test='form-error']", ".error-message", "[role='alert']"
                            ]:
                                try:
                                    loc_err = self.page.locator(err_sel).first
                                    if loc_err.count() > 0 and loc_err.is_visible():
                                        txt = loc_err.inner_text().lower()
                                        if any(kw in txt for kw in ["contraseña", "password", "incorrect", "invalid"]):
                                            error_pwd = True
                                            break
                                except Exception:
                                    pass
                    except Exception:
                        pass

                    if error_pwd:
                        print(f"  [Login] {Color.WARNING}[WARN] [{self.client_email}] Intento {intento_pwd}/2 de contraseña falló. Reintentando...{Color.ENDC}")
                        time.sleep(1.0)
                        continue

                    # Esperar consolidación OAuth + confirmación en /profile (no marcar éxito por un flash)
                    if self.esperar_establecimiento_sesion(35.0):
                        login_exitoso = True
                        print(f"  [Login] {Color.GREEN}[OK] [{self.client_email}] Sesión consolidada tras contraseña.{Color.ENDC}")
                        break

                if not login_exitoso:
                    print(f"\n  {Color.FAIL}{Color.BOLD}✖ [LOGIN] NO SE PUDO INICIAR SESIÓN PARA: {self.client_email}{Color.ENDC}\n")
                    self.abortar_barreras()
                    return self.finalizar_sin_exito("No se consolidó la sesión tras el login.")

                # Consentimiento residual (si quedó tras el wait) — NUNCA selectores genéricos 'button'
                btn_consent = encontrar_locator_en_frames(
                    self.page,
                    [
                        "button:has-text('Sí, continuar')", "button:has-text('Si, continuar')",
                        "button:has-text('Yes, continue')", "button:has-text('Permitir')",
                        "button:has-text('Allow')"
                    ],
                    text_regex=re.compile(r"^(sí,\s*continuar|si,\s*continuar|yes,\s*continue|permitir|allow)$", re.I)
                )
                if btn_consent:
                    try:
                        btn_consent.click()
                        time.sleep(2.0)
                    except Exception:
                        pass
                    self.esperar_establecimiento_sesion(15.0)
                
            manejar_bloqueos_e_intervencion(self.page, "Login Tidal")
            if not self.es_sesion_activa() and not self.confirmar_sesion_en_perfil(15.0):
                # Último intento de recuperar sesión antes de tocar el perfil
                if not self.rehacer_login_credenciales():
                    print(f"\n  {Color.FAIL}{Color.BOLD}✖ [LOGIN] NO SE CONSOLIDÓ LA SESIÓN PARA: {self.client_email}{Color.ENDC}\n")
                    self.abortar_barreras()
                    return self.finalizar_sin_exito("Sesión no consolidada tras el login.")
            self.login_ok = True
            print(f"  [Login] {Color.GREEN}[OK] [{self.client_email}] Inicio de sesión listo.{Color.ENDC}")
            
            # 2. Comprobar y cambiar correo si no coincide en el perfil
            target_email_clean = self.client_email.strip().lower()
            correo_perfil_correcto = False
            print(f"  [Verificación Email] [{self.client_email}] Navegando al perfil para verificar/editar información...")
            _max_verif = 4
            for intento_verif in range(1, _max_verif + 1):
                try:
                    print(f"  [Verificación Email] Intento {intento_verif}/{_max_verif}: Navegando a la página de EDICIÓN del perfil...")

                    # SIEMPRE ir a /profile/edit para ver el correo REAL registrado en el input
                    # (el sidebar/barra lateral muestra el correo de LOGIN, no el registrado)
                    navegar_tidal_tolerante(
                        self.page,
                        "https://account.tidal.com/profile/edit",
                        timeout_ms=60000,
                    )
                    # manejar_bloqueos_e_intervencion ya espera a que la página acabe de cargar
                    manejar_bloqueos_e_intervencion(self.page, "Edición Perfil Tidal")
                    aceptar_cookies_con_espera(self.page)
                    self.page = pagina_vigente(self.page)

                    # Verificar que seguimos con sesión activa
                    url_perfil = self.page.url.lower()
                    if (("/login/tidal/return" in url_perfil or "/login/tidal/callback" in url_perfil)
                            and "login.tidal.com" not in url_perfil):
                        # Todavía en el puente OAuth: esperar a que termine
                        time.sleep(2.0)
                        url_perfil = self.page.url.lower()
                    if ("login.tidal.com" in url_perfil or "/authorize" in url_perfil
                            or (("/login" in url_perfil and "account.tidal.com" in url_perfil
                                 and "/login/tidal/return" not in url_perfil
                                 and "/login/tidal/callback" not in url_perfil))
                            or self.hay_formulario_login_visible()):
                        print(f"  [Verificación Email] {Color.WARNING}[WARN] Tidal redirigió al login. Rehaciendo sesión...{Color.ENDC}")
                        if not self.rehacer_login_credenciales():
                            print(f"  [Verificación Email] {Color.FAIL}[ERROR] No se pudo recuperar la sesión en el reintento {intento_verif}/{_max_verif}.{Color.ENDC}")
                            continue
                        # Tras re-login, volver a intentar /profile/edit en el siguiente ciclo
                        continue

                    # Buscar el campo de correo en el formulario de edición
                    email_input = esperar_locator_en_frames(
                        self.page,
                        ['input[type="email"]', 'input[name="email"]', 'input[id*="email" i]', 'input[placeholder*="correo" i]', 'input[placeholder*="email" i]'],
                        timeout_s=10.0
                    )

                    if not email_input:
                        print(f"  [Verificación Email] {Color.WARNING}[WARN] No se encontró el campo de correo en la edición de perfil. Reintentando...{Color.ENDC}")
                        continue

                    # Leer el correo REAL registrado desde el input
                    current_email_value = ""
                    try:
                        current_email_value = email_input.input_value().strip().lower()
                    except Exception:
                        pass

                    print(f"  [Verificación Email] Correo REAL registrado en perfil: '{current_email_value}' (Objetivo: '{target_email_clean}')")

                    if current_email_value and son_correos_equivalentes(current_email_value, target_email_clean):
                        print(f"  [Verificación Email] {Color.GREEN}[OK] El correo registrado ya coincide con el de acceso: {self.client_email}{Color.ENDC}")
                        correo_perfil_correcto = True
                        break

                    # El correo NO coincide — hay que cambiarlo
                    print(f"  [Verificación Email] {Color.WARNING}[ACTUALIZANDO] Reemplazando correo registrado '{current_email_value}' por '{self.client_email}'...{Color.ENDC}")
                    try:
                        email_input.click(timeout=3000)
                        self.page.keyboard.press("Control+A")
                        self.page.keyboard.press("Backspace")
                        time.sleep(0.2)
                    except Exception:
                        pass

                    rellenar_campo_humanizado(email_input, self.client_email)
                    time.sleep(0.3)

                    try:
                        email_input.dispatch_event("input")
                        email_input.dispatch_event("change")
                        email_input.dispatch_event("blur")
                    except Exception:
                        pass
                    time.sleep(0.5)

                    # Pulsar Guardar
                    btn_guardar = esperar_locator_en_frames(
                        self.page,
                        [
                            "button[type='submit']",
                            "button:has-text('Guardar cambios')", "button:has-text('Save changes')",
                            "button:has-text('Guardar')", "button:has-text('Save')",
                            "button:has-text('Continuar')", "button:has-text('Continue')",
                            "input[type='submit']"
                        ],
                        timeout_s=8.0
                    )
                    btn_clicked = False
                    if btn_guardar:
                        try:
                            btn_guardar.click(timeout=3000, force=True)
                            btn_clicked = True
                        except Exception:
                            pass

                    if not btn_clicked:
                        try:
                            email_input.press("Enter")
                        except Exception:
                            pass

                    time.sleep(1.5)

                    # Confirmación de contraseña si es requerida por TIDAL
                    pwd_confirm = esperar_locator_en_frames(self.page, ['input[type="password"]'], timeout_s=6.0)
                    if pwd_confirm:
                        print("  [Verificación Email] Confirmando contraseña requerida para guardar cambios...")
                        try:
                            pwd_confirm.fill("")
                            time.sleep(0.2)
                            pwd_confirm.fill(self.target_pwd)
                            pwd_confirm.dispatch_event("input")
                            pwd_confirm.dispatch_event("change")
                        except Exception:
                            rellenar_campo_humanizado(pwd_confirm, self.target_pwd)
                        time.sleep(0.5)

                        btn_confirm = esperar_locator_en_frames(
                            self.page,
                            ["button[type='submit']", "button:has-text('Guardar')", "button:has-text('Confirmar')", "button:has-text('Save')", "button:has-text('Confirm')"],
                            timeout_s=6.0
                        )
                        if btn_confirm:
                            try:
                                btn_confirm.click(timeout=3000, force=True)
                            except Exception:
                                pass
                        try:
                            pwd_confirm.press("Enter")
                        except Exception:
                            pass
                        time.sleep(3.0)

                    # Verificar volviendo a cargar la edición del perfil
                    print("  [Verificación Email] Verificando si el correo fue guardado...")
                    navegar_tidal_tolerante(
                        self.page,
                        "https://account.tidal.com/profile/edit",
                        timeout_ms=45000,
                    )
                    time.sleep(2.5)

                    email_post = esperar_locator_en_frames(self.page, ['input[type="email"]', 'input[name="email"]'], timeout_s=8.0)
                    val_post = ""
                    if email_post:
                        try:
                            val_post = email_post.input_value().strip().lower()
                        except Exception:
                            pass

                    if val_post and son_correos_equivalentes(val_post, target_email_clean):
                        print(f"  [Verificación Email] {Color.GREEN}[ÉXITO] Correo actualizado y verificado correctamente: {self.client_email}{Color.ENDC}")
                        correo_perfil_correcto = True
                        break
                    else:
                        print(f"  [Verificación Email] {Color.WARNING}[WARN] El correo aún no coincide tras guardar (leído: '{val_post}'). Reintentando...{Color.ENDC}")

                except Exception as e_edit:
                    print(f"  [Verificación Email] [WARN] Error durante verificación/edición de perfil "
                          f"({intento_verif}/{_max_verif}): {e_edit}")
                    # Timeout/túnel: rotar PE y rehacer login (antes un solo fallo abortaba y seguía a TMM)
                    if es_error_proxy_o_red(e_edit) or "timeout" in str(e_edit).lower():
                        if intento_verif >= _max_verif:
                            break
                        print(f"  [Verificación Email] [{self.client_email}] Timeout/proxy en /profile/edit. "
                              f"Rotando PE y rehaciendo sesión...")
                        try:
                            self.ejecutar_rotacion_proxy_y_recargar()
                        except Exception as e_rot:
                            print(f"  [Verificación Email] [WARN] Rotación PE falló: {e_rot}")
                        if not self.rehacer_login_credenciales():
                            print(f"  [Verificación Email] {Color.FAIL}[ERROR] No se pudo recuperar la sesión "
                                  f"tras rotación ({intento_verif}/{_max_verif}).{Color.ENDC}")
                        continue
                    time.sleep(2.0)

            if not correo_perfil_correcto:
                print(f"  [Verificación Email] {Color.FAIL}[ERROR CRÍTICO] No se logró actualizar el correo "
                      f"registrado a '{self.client_email}'.{Color.ENDC}")
                print(f"  [Eliminación] [{self.client_email}] [ABORTADO] El correo del perfil no pudo "
                      f"actualizarse a {self.client_email}; no se abrirá TuneMyMusic ni se eliminará la cuenta.")
                self.abortar_barreras()
                return self.finalizar_sin_exito(
                    "Perfil/correo no verificado (sesión Tidal no usable). Se omite TuneMyMusic."
                )

            # La eliminación se aborda al final, después de exportar el CSV: entrar antes en el
            # asistente dispararía el código de verificación mucho antes de poder usarlo.
            print(f"  [Eliminación] [{self.client_email}] Pendiente: se ejecutará al terminar la exportación en TuneMyMusic.")

            # Gate final: no abrir TMM si la pestaña de Tidal ya no tiene sesión (caso del log:
            # timeout en /profile/edit dejó authorize vacío y aun así se abría TuneMyMusic).
            if not self.es_sesion_activa() and not self.confirmar_sesion_en_perfil(15.0):
                print(f"  [Login] {Color.WARNING}[WARN] [{self.client_email}] Sesión perdida antes de "
                      f"TuneMyMusic. Intentando recuperar...{Color.ENDC}")
                if not self.rehacer_login_credenciales():
                    print(f"  [TuneMyMusic] [{self.client_email}] [ABORTADO] Sin sesión Tidal activa; "
                          f"no se abrirá TuneMyMusic.")
                    self.abortar_barreras()
                    return self.finalizar_sin_exito("Sin sesión Tidal antes de TuneMyMusic.")

            # 3. Abrir TuneMyMusic en una pestaña aparte con tolerancia a fallos de red/proxy
            print(f"  [TuneMyMusic] [{self.client_email}] Abriendo TuneMyMusic en una nueva pestaña...")
            try:
                self.tmm_page = self.context.new_page()
                self.tmm_page.on("download", self.handle_download)
                
                tmm_loaded = False
                for tmm_try in range(1, 4):
                    try:
                        self.tmm_page.goto("https://www.tunemymusic.com/es/transfer", wait_until="domcontentloaded", timeout=45000)
                        tmm_loaded = True
                        break
                    except Exception as e_tmm:
                        print(f"  [TuneMyMusic] {Color.WARNING}[WARN] [{self.client_email}] Intento {tmm_try}/3 a TuneMyMusic tuvo fallo/retardo de proxy ({e_tmm}). Reintentando...{Color.ENDC}")
                        time.sleep(2.0)
                        
                if not tmm_loaded:
                    print(f"  [TuneMyMusic] {Color.WARNING}[WARN] [{self.client_email}] No se pudo cargar automáticamente la portada de TuneMyMusic por red/proxy, pero la ventana permanecerá abierta.{Color.ENDC}")
            except Exception as ex_tmm_init:
                print(f"  [TuneMyMusic] {Color.WARNING}[WARN] [{self.client_email}] Error al iniciar pestaña TuneMyMusic: {ex_tmm_init}{Color.ENDC}")
            
            # Mantener el hilo de Playwright vivo para procesar eventos de descargas y cierres,
            # y detectar si TuneMyMusic muestra aviso de "No se encontraron listas de reproducción"
            print(f"  [TuneMyMusic] [{self.client_email}] Listo para transferencias. Esperando descargas o aviso de cuenta vacía...")
            self.sin_playlists = False
            # Frases completas: un "sin listas" suelto no basta, porque este aviso habilita
            # la eliminación de la cuenta y un falso positivo la borraría sin exportar nada.
            frases_cuenta_vacia = [
                "no se encontraron listas de reproducción", "no se encontraron listas",
                "no se encontraron canciones", "no playlists found", "no playlist found",
                "no tracks found", "no music found"
            ]
            inicio_espera_tmm = time.time()
            # Snapshot del CSV exacto de ESTA cuenta (no alias): un archivo viejo no cuenta como éxito
            csv_exacto = DESCARGAS_DIR / f"{self.client_email}.csv"
            prev_mtime = 0.0
            prev_size = -1
            if csv_parece_valido(csv_exacto):
                try:
                    st0 = csv_exacto.stat()
                    prev_mtime = st0.st_mtime
                    prev_size = st0.st_size
                    print(f"  [Descarga] [{self.client_email}] Ya existía CSV previo "
                          f"({csv_exacto.name}, {prev_size} bytes); si hay descarga se exigirá uno nuevo. "
                          f"Si TuneMyMusic indica cuenta vacía, no se exige CSV.")
                except Exception:
                    pass
            confirmaciones_vacio = 0

            def _csv_nuevo_en_disco():
                """Solo el fichero exacto email.csv, y solo si es nuevo o cambió en esta sesión."""
                if not csv_parece_valido(csv_exacto):
                    return None
                try:
                    st = csv_exacto.stat()
                except Exception:
                    return None
                if prev_size < 0:
                    return csv_exacto
                if st.st_mtime > prev_mtime + 0.5 or st.st_size != prev_size:
                    return csv_exacto
                if st.st_mtime >= inicio_espera_tmm - 1.0:
                    return csv_exacto
                return None

            try:
                while not self.download_completed and not self.sin_playlists and self.context and self.tmm_page and not self.tmm_page.is_closed():
                    try:
                        # Respaldo: evento download perdido pero CSV NUEVO ya en disco
                        if not self.download_completed:
                            csv_en_disco = _csv_nuevo_en_disco()
                            if csv_en_disco:
                                print(f"  [Descarga] [{self.client_email}] CSV nuevo detectado en disco: "
                                      f"{csv_en_disco.name} ({csv_en_disco.stat().st_size} bytes).")
                                self.download_completed = True
                                self.export_ok = True
                                break

                        # Solo texto realmente visible: el SPA de TuneMyMusic mantiene avisos ocultos
                        # en el DOM que dispararían la detección antes de empezar la transferencia.
                        texto_visible = self.tmm_page.evaluate("""() => {
                            const visible = (el) => {
                                const st = window.getComputedStyle(el);
                                if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') < 0.1) return false;
                                const r = el.getBoundingClientRect();
                                return r.width > 0 && r.height > 0;
                            };
                            const partes = [];
                            for (const el of document.querySelectorAll('div, span, p, h1, h2, h3, li, [role="alert"]')) {
                                if (el.children.length === 0 && visible(el)) {
                                    partes.push((el.textContent || '').trim().toLowerCase());
                                }
                            }
                            return partes.join(' | ');
                        }""")
                        if any(f in texto_visible for f in frases_cuenta_vacia):
                            confirmaciones_vacio += 1
                        else:
                            confirmaciones_vacio = 0

                        # Exigir el aviso estable (4 lecturas) y pasado el arranque de la página
                        if confirmaciones_vacio >= 4 and (time.time() - inicio_espera_tmm) > 20.0:
                            print(f"  {Color.WARNING}[TuneMyMusic] [{self.client_email}] La cuenta no contiene playlists/canciones ('No se encontraron listas de reproducción').{Color.ENDC}")
                            self.sin_playlists = True
                            break
                    except Exception:
                        pass
                    self.tmm_page.wait_for_timeout(500)
            except Exception:
                pass

            # Verificación de CSV en disco — SOLO si hubo descarga.
            # Cuentas vacías (sin_playlists): TuneMyMusic no ofrece CSV; eso ya es éxito
            # válido y NO debe anularse por falta de fichero (comportamiento original).
            if self.sin_playlists:
                self.export_ok = False  # no hubo export, pero la cuenta vacía está confirmada
                print(f"  [TuneMyMusic] [{self.client_email}] Cuenta vacía confirmada: no se exige CSV; "
                      f"se puede continuar con la eliminación.")
            else:
                csv_nuevo = _csv_nuevo_en_disco()
                if self.download_completed and not csv_parece_valido(csv_exacto):
                    print(f"  {Color.FAIL}[Descarga] [{self.client_email}] Flag de descarga OK pero el CSV no está "
                          f"en 'descargas/' o está vacío. Se anula el éxito de exportación.{Color.ENDC}")
                    self.download_completed = False
                    self.export_ok = False
                elif self.download_completed and not csv_nuevo:
                    print(f"  {Color.FAIL}[Descarga] [{self.client_email}] El CSV en disco es anterior a esta "
                          f"exportación (no se actualizó). Se anula el éxito.{Color.ENDC}")
                    self.download_completed = False
                    self.export_ok = False
                elif csv_nuevo:
                    self.download_completed = True
                    self.export_ok = True
                    print(f"  [Descarga] [{self.client_email}] CSV verificado en disco: {csv_nuevo} "
                          f"({csv_nuevo.stat().st_size} bytes).")
                else:
                    self.export_ok = False

            # Retardo corto para asegurar el guardado correcto antes del desmantelamiento
            if self.download_completed or self.sin_playlists:
                time.sleep(2.5)
                
            # Guardar cookies de TuneMyMusic como respaldo antes de finalizar el hilo
            try:
                if self.context:
                    guardar_cookies_tmm(self.context.cookies())
            except Exception:
                pass

            # --- Proceso de eliminación de cuenta ---
            # Éxito de fase TMM = CSV descargado OK  O  cuenta vacía (sin playlists, sin CSV).
            exito_eliminacion = False
            if self.sin_playlists:
                print(f"\n  [Eliminación] [{self.client_email}] Cuenta sin playlists/CSV: se procede a eliminar "
                      f"(no aplica exigir archivo en 'descargas/').")
            if not (self.download_completed or self.sin_playlists):
                print(f"\n  [Eliminación] [{self.client_email}] [OMITIDA] No se descargó el CSV ni se confirmó que la cuenta esté vacía.")
            elif self.download_completed and not self.sin_playlists and not resolver_csv_cuenta(self.client_email):
                print(f"\n  [Eliminación] [{self.client_email}] [OMITIDA] Había señal de descarga pero el CSV "
                      f"no está en 'descargas/'; no se elimina la cuenta.")
            elif not correo_perfil_correcto:
                print(f"\n  [Eliminación] [{self.client_email}] [OMITIDA] El correo del perfil no coincide con la cuenta IMAP.")
            else:
                print(f"\n  [Eliminación] [{self.client_email}] Iniciando eliminación de cuenta Tidal...")
                try:
                    # Asegurar enfoque de la página de Tidal
                    try:
                        self.page.bring_to_front()
                    except Exception:
                        pass

                    # La línea base del buzón se toma ANTES de recorrer el asistente, que es lo que
                    # dispara el envío: tomarla después descartaría el código ya recibido.
                    base_del_id = obtener_max_email_id(self.client_email, "tidal")
                    print(f"  [Eliminación] [{self.client_email}] ID de correo de Tidal más reciente antes de disparar el envío: {base_del_id}")

                    print(f"  [Eliminación] [{self.client_email}] Recorriendo el asistente de confirmación de Tidal...")
                    self.page.goto("https://account.tidal.com/account-deletion", wait_until="domcontentloaded", timeout=35000)
                    time.sleep(2.0)
                    aceptar_cookies_con_espera(self.page)
                    manejar_bloqueos_e_intervencion(self.page, "Eliminación de Cuenta")

                    # Tidal a veces devuelve al perfil: desde ahí hay que entrar por el enlace
                    if "account-deletion" not in self.page.url:
                        print(f"  [Eliminación] [{self.client_email}] Redirigido fuera del asistente. Buscando el enlace 'Eliminar cuenta'...")
                        btn_entrada = encontrar_locator_en_frames(
                            self.page,
                            ["a:has-text('Eliminar cuenta')", "button:has-text('Eliminar cuenta')",
                             "a:has-text('Delete account')", "button:has-text('Delete account')"]
                        )
                        if btn_entrada:
                            btn_entrada.click()
                            time.sleep(3.0)
                        else:
                            self.page.goto("https://account.tidal.com/account-deletion", wait_until="domcontentloaded", timeout=25000)
                            time.sleep(2.5)
                        if "account-deletion" not in self.page.url:
                            raise RuntimeError("Tidal no permitió abrir el asistente de eliminación de cuenta.")

                    if not self.recorrer_asistente_eliminacion():
                        raise RuntimeError("No se alcanzó la pantalla del código del asistente de eliminación.")

                    if not self.verificar_destino_del_codigo(target_email_clean):
                        raise RuntimeError("Tidal enviaría el código a un correo distinto al de la cuenta IMAP.")

                    codigo_eliminacion = None
                    print(f"  [Eliminación] [{self.client_email}] Buscando código de eliminación en el correo...")
                    for intento in range(1, 19): # 18 intentos * 10s = 180s = 3 minutos
                        if intento in (2, 8):
                            self.forzar_reenvio_codigo()
                        print(f"  [Eliminación] [{self.client_email}] Intento {intento}/18: Buscando correo de eliminación...")
                        codigo_eliminacion = obtener_codigo_via_imap(
                            gmail_user=self.client_email,
                            required_keywords=["elimin", "desactiv", "delete", "code", "codigo"],
                            after_email_id=base_del_id
                        )
                        if codigo_eliminacion:
                            break
                        if intento < 18:
                            time.sleep(10.0)
                            
                    if not codigo_eliminacion:
                        print(f"  {Color.FAIL}[Eliminación] [{self.client_email}] No se pudo obtener el código de eliminación vía IMAP.{Color.ENDC}")
                    else:
                        print(f"  [Eliminación] [{self.client_email}] {Color.GREEN}Código de eliminación obtenido: {codigo_eliminacion}{Color.ENDC}")
                        
                        # Escribir código
                        if escribir_codigo_verificacion_inteligente(self.page, codigo_eliminacion):
                            print(f"  [Eliminación] [{self.client_email}] Código ingresado correctamente.")
                            time.sleep(2.0)
                            
                            # Click en el botón de confirmación
                            btn_confirmar = esperar_locator_en_frames(
                                self.page,
                                [
                                    "button[type='submit']",
                                    "button:has-text('Eliminar cuenta')", "button:has-text('Delete account')",
                                    "button:has-text('Confirmar')", "button:has-text('Confirm')",
                                    "button:has-text('Eliminar')", "button:has-text('Delete')"
                                ],
                                timeout_s=15.0
                            )
                            if btn_confirmar:
                                print(f"  [Eliminación] [{self.client_email}] Pulsando botón para confirmar la eliminación...")
                                btn_confirmar.click()

                                if self.esperar_y_confirmar_eliminacion(20.0):
                                    print(f"  [Eliminación] {Color.GREEN}[OK] Cuenta {self.client_email} eliminada correctamente.{Color.ENDC}")
                                    exito_eliminacion = True
                                else:
                                    # Tras timeout de /profile la pestaña puede estar ya en /authorize
                                    # (cuenta borrada) sin que confirmar_cuenta_eliminada lo viera a tiempo.
                                    url_post = ""
                                    try:
                                        url_post = (pagina_vigente(self.page).url or "").lower()
                                    except Exception:
                                        pass
                                    if "login.tidal.com" in url_post or "/authorize" in url_post:
                                        print(f"  [Eliminación] {Color.GREEN}[OK] Cuenta {self.client_email} "
                                              f"eliminada (pestaña ya en login/authorize).{Color.ENDC}")
                                        exito_eliminacion = True
                                    else:
                                        print(f"  [Eliminación] {Color.WARNING}[WARN] Primera confirmación no verificó borrado. Probando botón secundario...{Color.ENDC}")
                                        btn_final = esperar_locator_en_frames(
                                            self.page,
                                            [
                                                "button:has-text('Eliminar cuenta')", "button:has-text('Delete account')",
                                                "button:has-text('Confirmar')", "button:has-text('Confirm')"
                                            ],
                                            timeout_s=5.0
                                        )
                                        if btn_final:
                                            print(f"  [Eliminación] [{self.client_email}] Pulsando botón de confirmación final...")
                                            btn_final.click()
                                            if self.esperar_y_confirmar_eliminacion(20.0):
                                                print(f"  [Eliminación] {Color.GREEN}[OK] Cuenta {self.client_email} eliminada correctamente (confirmación secundaria).{Color.ENDC}")
                                                exito_eliminacion = True
                                            else:
                                                try:
                                                    url_post2 = (pagina_vigente(self.page).url or "").lower()
                                                except Exception:
                                                    url_post2 = ""
                                                if "login.tidal.com" in url_post2 or "/authorize" in url_post2:
                                                    print(f"  [Eliminación] {Color.GREEN}[OK] Cuenta {self.client_email} "
                                                          f"eliminada (authorize tras confirmación secundaria).{Color.ENDC}")
                                                    exito_eliminacion = True
                            else:
                                print(f"  {Color.FAIL}[Eliminación] [{self.client_email}] No se encontró el botón de confirmación de eliminación.{Color.ENDC}")
                        else:
                            print(f"  {Color.FAIL}[Eliminación] [{self.client_email}] No se pudo ingresar el código de verificación.{Color.ENDC}")
                except Exception as ex_el:
                    print(f"  {Color.FAIL}[Eliminación] [ERROR] Ocurrió un error al intentar eliminar la cuenta de {self.client_email}: {ex_el}{Color.ENDC}")

            self.eliminacion_ok = exito_eliminacion
            if exito_eliminacion:
                self.cerrar_recursos()
                return True
            return self.finalizar_sin_exito("No se completó la eliminación de la cuenta.")
            
        except Exception as e:
            print(f"  {Color.FAIL}[ERROR] Excepción general en el proceso para {self.client_email}: {e}{Color.ENDC}")
            self.abortar_barreras()
            return self.finalizar_sin_exito("El proceso terminó con una excepción.")

    def cerrar_recursos(self):
        """Cierra Chrome de forma agresiva: páginas → contexto → playwright.

        Tras un goto a /profile que hace timeout (cuenta ya borrada), el context.close()
        simple a veces falla en silencio y deja la ventana en login/authorize abierta
        (caso get.mushroom0.5.84).
        """
        if getattr(self, "proxy_pe_server", None):
            try:
                GLOBAL_PE_PROXY_POOL.liberar_proxy(self.proxy_pe_server)
            except Exception:
                pass

        # 1) Cerrar pestañas primero (Tidal + TuneMyMusic) para desbloquear el contexto
        pages = []
        try:
            if self.context:
                pages = list(self.context.pages)
        except Exception:
            pass
        for p in pages:
            try:
                if not p.is_closed():
                    p.close()
            except Exception:
                pass

        # 2) Cerrar contexto con reintentos (navegación a medias bloquea el close)
        for intento in range(1, 4):
            try:
                if self.context:
                    self.context.close()
                break
            except Exception as e:
                print(f"  [Navegador] [{getattr(self, 'client_email', '?')}] [WARN] "
                      f"Cerrar Chrome intento {intento}/3: {e}")
                time.sleep(0.6 * intento)

        self.context = None
        self.page = None
        self.tmm_page = None

        try:
            if self.playwright:
                self.playwright.stop()
        except Exception as e:
            print(f"  [Navegador] [{getattr(self, 'client_email', '?')}] [WARN] "
                  f"playwright.stop() falló: {e}")
        self.playwright = None

        prof_dir = getattr(self, "main_profile", None)
        if prof_dir and Path(prof_dir).exists():
            def _rm_async(p_dir):
                time.sleep(2.0)
                try:
                    shutil.rmtree(p_dir, ignore_errors=True)
                except Exception:
                    pass
            threading.Thread(target=_rm_async, args=(Path(prof_dir),), daemon=True).start()


def iniciar_sesion_automatico_tidal(correos):
    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   INICIO DE SESIÓN AUTOMÁTICO DE CUENTAS TIDAL / TMM{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"{Color.FAIL}[Error]{Color.ENDC} Playwright no está instalado. Ejecute 'pip install playwright' e instale los navegadores con 'playwright install'.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    path_cuentas = SCRIPT_DIR / "sesiones_imap_cuentas.txt"
    if not path_cuentas.exists():
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} El archivo 'sesiones_imap_cuentas.txt' no existe en la carpeta actual.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return
        
    cuentas_map = cargar_mapa_cuentas_sesiones()
    if not cuentas_map:
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} No se encontraron cuentas válidas en 'sesiones_imap_cuentas.txt' (formato: correo contraseña).")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    cuentas_map = filtrar_cuentas_por_correos_activos(cuentas_map, correos)
    if cuentas_map is None:
        input(">>> Presiona Enter para volver al menú principal <<<")
        return
        
    correos_lista = list(cuentas_map.keys())
    print(f"\nSe procesarán {len(correos_lista)} cuenta(s) (filtradas por correos activos del menú).")

    headless_opt = input("\n¿Deseas ejecutar el navegador en segundo plano (headless)? (s/n, por defecto 'n'): ").strip().lower()
    headless = headless_opt in ("s", "si", "yes", "y")

    revision_opt = input("¿Mantener abiertas las ventanas con error para revisión manual? (s/n, por defecto 'n'): ").strip().lower()
    mantener_ventanas = revision_opt in ("s", "si", "sí", "yes", "y")

    success_count = 0
    fail_count = 0
    managers = []

    num_cuentas = len(correos_lista)
    print(f"\n{Color.CYAN}[Proxies PE] Habilitando proxies de Perú obligatorios para inicio de sesión (Opción 10)...{Color.ENDC}")
    valid_pe_list = asegurar_proxies_peru(cantidad_necesaria=num_cuentas)
    if not valid_pe_list:
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} No hay proxies de Perú válidos y esta opción los exige. Valida la lista con la opción 13 antes de continuar.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    GLOBAL_PE_PROXY_POOL.reiniciar_bloqueos()
    st_pe = GLOBAL_PE_PROXY_POOL.estadisticas()
    print(f"  [Proxy Pool PE] Disponibles: {st_pe['total']} total | {st_pe['libres']} libres "
          f"(lote máximo 10 ventanas; rotaciones usan el resto).")

    # Con menos proxies que cuentas, las cuentas sin proxy libre se omiten al llegar su turno
    if len(valid_pe_list) < min(num_cuentas, 10):
        print(f"\n{Color.WARNING}[Proxies PE]{Color.ENDC} Solo hay {len(valid_pe_list)} proxies de Perú válidos para {num_cuentas} cuentas "
              f"(se usan hasta 10 ventanas por lote). Las cuentas sin proxy libre se omitirán.")
        resp_proxy = input("¿Continuar de todas formas? (s/n, por defecto 'n'): ").strip().lower()
        if resp_proxy not in ("s", "si", "sí", "yes", "y"):
            print(f"{Color.CYAN}Proceso cancelado. Amplía la lista de proxies de Perú con la opción 13.{Color.ENDC}")
            input(">>> Presiona Enter para volver al menú principal <<<")
            return

    batch_size = 10
    total_cuentas = len(correos_lista)
    print(f"\n{Color.CYAN}{Color.BOLD}Iniciando sesión de {total_cuentas} cuentas en bloques de máximo {batch_size} ventanas de Chrome simultáneas...{Color.ENDC}\n")
    
    for b_start in range(0, total_cuentas, batch_size):
        # Entre lotes: liberar marcas antirobot para reutilizar IPs que solo fallaron temporalmente
        if b_start > 0:
            GLOBAL_PE_PROXY_POOL.reiniciar_bloqueos()
        lote_correos = correos_lista[b_start : b_start + batch_size]
        num_cuentas_lote = len(lote_correos)
        
        # Sólo se sincroniza la apertura de ventanas. Las fases siguientes son independientes por
        # cuenta, así que encadenarlas dejaba a todas las ventanas paradas esperando a la más lenta.
        barreras_lote = {
            "inicio": threading.Barrier(num_cuentas_lote)
        }
        
        workers = num_cuentas_lote
        if total_cuentas > batch_size:
            print(f"\n{Color.CYAN}{Color.BOLD}--- Procesando Lote ({b_start + 1} a {b_start + num_cuentas_lote} de {total_cuentas}) ---{Color.ENDC}")

        def login_un_correo(idx_rel, correo):
            if idx_rel > 1:
                time.sleep((idx_rel - 1) * 1.5)
            idx_abs = b_start + idx_rel
            contrasena = cuentas_map[correo]
            
            p_pe = GLOBAL_PE_PROXY_POOL.obtener_proxy_unico()
            if not p_pe:
                print(f"  {Color.FAIL}[Proxy PE] [{correo}] No queda ningún proxy de Perú disponible; se omite la cuenta.{Color.ENDC}")
                # Liberar a los demás hilos del lote: esta cuenta ya no llegará a las barreras
                for b in barreras_lote.values():
                    try:
                        b.abort()
                    except Exception:
                        pass
                return correo, False

            p_pe_server = p_pe.get("server")
            p_pe_user = p_pe.get("username")
            p_pe_pass = p_pe.get("password")

            manager = TidalAutoLoginManager(
                client_email=correo,
                target_pwd=contrasena,
                proxy_pe_server=p_pe_server,
                proxy_pe_user=p_pe_user,
                proxy_pe_pass=p_pe_pass,
                headless=headless,
                barreras=barreras_lote,
                thread_index=idx_abs,
                mantener_ventana_si_falla=mantener_ventanas
            )
            managers.append(manager)
            print(f"\n{Color.CYAN}{Color.BOLD}[Login Automático Concurrente] Iniciando proceso para: {correo}{Color.ENDC}")
            try:
                exito = manager.run_auto_login()
            finally:
                # Cerrar la conexión IMAP reutilizable del hilo para no dejarla abierta contra Gmail
                cerrar_sesion_imap_hilo()
            return correo, exito

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(login_un_correo, idx_rel, correo): correo for idx_rel, correo in enumerate(lote_correos, 1)}
            for future in as_completed(futures):
                correo = futures[future]
                try:
                    c, exito = future.result()
                    if exito:
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    print(f"  {Color.FAIL}[ERROR] Excepción al procesar {correo}: {e}{Color.ENDC}")
                    fail_count += 1

    total_login = sum(1 for m in managers if getattr(m, "login_ok", False))
    total_export = sum(1 for m in managers if getattr(m, "export_ok", False))
    total_vacias = sum(1 for m in managers if getattr(m, "sin_playlists", False))
    total_eliminadas = sum(1 for m in managers if getattr(m, "eliminacion_ok", False))

    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"   RESUMEN DEL INICIO DE SESIÓN AUTOMÁTICO")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f" Cuentas procesadas: {total_cuentas}")
    print(f" Inicios de sesión correctos: {total_login}")
    print(f" CSV exportados desde TuneMyMusic: {total_export}")
    print(f" Cuentas detectadas sin playlists: {total_vacias}")
    print(f" Cuentas eliminadas: {total_eliminadas}")
    print(f" Procesos completos (login + exportación + eliminación): {success_count}")
    print(f" Procesos incompletos: {fail_count}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}\n")

    if fail_count and mantener_ventanas:
        print(f"{Color.WARNING}Las ventanas de las cuentas incompletas ya se cerraron tras el plazo de revisión manual.{Color.ENDC}")

    print(f"\n{Color.GREEN}{Color.BOLD}>>> Proceso finalizado. Regresando al menú principal...{Color.ENDC}\n")


def registrar_cuentas_tidal(correos):
    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   REGISTRO AUTOMÁTICO DE CUENTAS TIDAL (NIGERIA){Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"{Color.FAIL}[Error]{Color.ENDC} Playwright no está instalado. Ejecute 'pip install playwright' e instale los navegadores con 'playwright install'.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    global valid_ng_list, CACHE_PROXIES_NG
    valid_ng_list = []

    # Cargar *_validos.txt completos (no un recorte al nº de correos)
    cargar_cache_proxies_validos_desde_disco()
    
    if CACHE_PROXIES_NG:
        valid_ng_list = list(CACHE_PROXIES_NG)
        print(f"\n{Color.GREEN}[Proxy Caché] Usando {len(valid_ng_list)} proxies de NIGERIA previamente verificados.{Color.ENDC}")
    else:
        proxies_cfg = cargar_proxies_desde_txt(preferir_validos=False)
        if proxies_cfg and proxies_cfg.get("proxy_ng_list"):
            ng_list = proxies_cfg["proxy_ng_list"]
            print(f"\nSe encontraron {len(ng_list)} proxies para NIGERIA.")
            # Pedir margen para rotaciones; la función fusiona al caché completo (no lo reduce a len(correos))
            valid_ng_list = probar_y_seleccionar_mejor_proxy(
                ng_list, "NIGERIA", max(len(correos) * 4, len(correos) + 15)
            )
            if valid_ng_list:
                guardar_proxies_validos_txt(SCRIPT_DIR / "lista_proxies_ng_validos.txt", valid_ng_list)
        else:
            print(f"\n{Color.WARNING}[Proxy]{Color.ENDC} No se encontraron proxies de Nigeria en 'lista_proxies_ng.txt' ni en 'proxies.txt'.")

    # Alimentar el pool NG con la lista COMPLETA
    alimentar_pool_proxies_nigeria(valid_ng_list)
    GLOBAL_NG_PROXY_POOL.reiniciar_bloqueos()
    GLOBAL_PE_PROXY_POOL.reiniciar_bloqueos()
        
    use_proxy = False
    
    if valid_ng_list:
        use_proxy = True
    else:
        print(f"\n{Color.WARNING}[WARN]{Color.ENDC} No hay proxies de Nigeria válidos disponibles.")
        confirm = input("¿Deseas continuar con tu IP local/VPN actual? (s/n, por defecto 'n'): ").strip().lower()
        if confirm not in ("s", "si", "yes", "y"):
            print("Operación cancelada.")
            return

    print(f"\n{Color.CYAN}[Proxies PE] Habilitando proxies de Perú para el pago y para TuneMyMusic...{Color.ENDC}")
    proxies_pe = asegurar_proxies_peru(cantidad_necesaria=len(correos))
    if not proxies_pe:
        print(f"\n{Color.WARNING}[WARN]{Color.ENDC} No hay proxies de Perú válidos: el pago y TuneMyMusic usarían tu IP actual.")
        confirm_pe = input("¿Deseas continuar sin proxy de Perú? (s/n, por defecto 'n'): ").strip().lower()
        if confirm_pe not in ("s", "si", "sí", "yes", "y"):
            print("Operación cancelada. Valida la lista de proxies de Perú con la opción 13.")
            return

    headless_opt = input("\n¿Deseas ejecutar el navegador en segundo plano (headless)? (s/n, por defecto 'n'): ").strip().lower()
    headless = headless_opt in ("s", "si", "yes", "y")
    
    success_count = 0
    fail_count = 0
    successful_managers = []
    
    def registrar_un_correo(idx, correo):
        p_ng_server = p_ng_user = p_ng_pass = None
        p_pe_server = p_pe_user = p_pe_pass = None
        manager = None
        exito = False
        try:
            if use_proxy and valid_ng_list:
                p_ng = GLOBAL_NG_PROXY_POOL.obtener_proxy_unico()
                if not p_ng:
                    print(f"  {Color.FAIL}[Proxy NG] [{correo}] Sin proxy de Nigeria libre; se omite la cuenta.{Color.ENDC}")
                    return correo, False, None
                p_ng_server = p_ng.get("server")
                p_ng_user = p_ng.get("username")
                p_ng_pass = p_ng.get("password")

            # Reservar PE desde el inicio (pago / TuneMyMusic). Si __init__ o el registro
            # fallan, el finally lo devuelve al pool para no dejarlo huérfano.
            if proxies_pe:
                p_pe = GLOBAL_PE_PROXY_POOL.obtener_proxy_unico()
                if p_pe:
                    p_pe_server = p_pe.get("server")
                    p_pe_user = p_pe.get("username")
                    p_pe_pass = p_pe.get("password")

            manager = TidalRegisterManager(
                client_email=correo,
                client_pwd="",
                proxy_ng_server=p_ng_server,
                proxy_ng_user=p_ng_user,
                proxy_ng_pass=p_ng_pass,
                proxy_pe_server=p_pe_server,
                proxy_pe_user=p_pe_user,
                proxy_pe_pass=p_pe_pass,
                headless=headless
            )

            print(f"\n{Color.CYAN}{Color.BOLD}[Registro Concurrente] Iniciando proceso para: {correo}{Color.ENDC}")
            exito = manager.run_registration()
            return correo, exito, manager
        except Exception:
            # Asegurar cierre de Chrome si el fallo ocurrió a mitad del proceso
            if manager is not None:
                try:
                    manager.cerrar_navegador(liberar_ng=False, liberar_pe=False)
                except Exception:
                    pass
            raise
        finally:
            cerrar_sesion_imap_hilo()
            # Si el registro no tuvo éxito, devolver ambos proxies. Si tuvo éxito, NG ya lo
            # liberó run_registration y el PE se conserva en el manager para TuneMyMusic.
            if not exito:
                try:
                    GLOBAL_NG_PROXY_POOL.liberar_proxy(p_ng_server)
                except Exception:
                    pass
                try:
                    GLOBAL_PE_PROXY_POOL.liberar_proxy(p_pe_server)
                except Exception:
                    pass
                if manager is not None:
                    manager.proxy_ng_server = None
                    manager.proxy_pe_server = None

    # Ejecutar de manera simultánea
    workers = min(10, len(correos))
    print(f"\n{Color.CYAN}{Color.BOLD}Iniciando registro de {len(correos)} cuentas de forma simultánea (usando {workers} hilos)...{Color.ENDC}\n")
    
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(registrar_un_correo, idx, correo): correo for idx, correo in enumerate(correos, 1)}
        for future in as_completed(futures):
            correo = futures[future]
            try:
                c, exito, manager = future.result()
                if exito and manager is not None:
                    success_count += 1
                    successful_managers.append(manager)
                else:
                    fail_count += 1
            except Exception as e:
                print(f"  {Color.FAIL}[ERROR] Excepción inesperada procesando {correo}: {e}{Color.ENDC}")
                fail_count += 1
            
    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   RESUMEN DEL REGISTRO{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f" Cuentas procesadas con éxito: {Color.GREEN}{success_count}{Color.ENDC}")
    print(f" Cuentas fallidas: {Color.FAIL}{fail_count}{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}\n")
    
    if successful_managers:
        print(f"\n{Color.CYAN}{Color.BOLD}Iniciando transferencia en TuneMyMusic para las {len(successful_managers)} cuentas exitosas...{Color.ENDC}")

        # Pre-chequeo exclusivo 1:1: avisar qué CSV faltan ANTES de abrir TuneMyMusic
        DESCARGAS_DIR.mkdir(parents=True, exist_ok=True)
        asignaciones = asignar_csvs_a_cuentas([mgr.client_email for mgr in successful_managers])
        con_csv = []
        sin_csv = []
        for mgr in successful_managers:
            ruta = asignaciones.get(mgr.client_email)
            mgr.csv_asignado = ruta
            if ruta and csv_pertenece_a_cuenta(ruta, mgr.client_email):
                con_csv.append((mgr.client_email, ruta.name, ruta.stat().st_size))
            else:
                mgr.csv_asignado = None
                sin_csv.append(mgr.client_email)

        print(f"\n{Color.CYAN}[CSV] Emparejados {len(con_csv)}/{len(successful_managers)} "
              f"(1 archivo ↔ 1 cuenta Tidal) en 'descargas/'.{Color.ENDC}")
        for email, nombre, nbytes in con_csv:
            marca = "" if nombre.lower() == f"{email.lower()}.csv" else " [alias]"
            print(f"  {Color.GREEN}✓{Color.ENDC} {email} → {nombre} ({nbytes} bytes){marca}")
        if sin_csv:
            print(f"\n{Color.WARNING}[CSV] Sin archivo exclusivo (o ambiguo) para:{Color.ENDC}")
            for email in sin_csv:
                print(f"  {Color.FAIL}✗{Color.ENDC} {email}  (espera: descargas/{email}.csv)")
            print(f"{Color.WARNING}Esas ventanas se abrirán igual y permanecerán abiertas para subida manual "
                  f"si al pulsar ENTER aún no hay CSV con el nombre exacto de la cuenta.{Color.ENDC}")
            cont = input("¿Continuar con TuneMyMusic de todas formas? (s/n, por defecto 's'): ").strip().lower()
            if cont in ("n", "no"):
                print(f"{Color.CYAN}Transferencia TuneMyMusic cancelada. Regresa cuando los CSV estén en 'descargas/'.{Color.ENDC}")
                print(f"\n{Color.GREEN}{Color.BOLD}>>> Proceso finalizado. Regresando al menú principal...{Color.ENDC}\n")
                return
        
        event_subir_csv = threading.Event()
        
        def transferir_un_correo(mgr):
            mgr.run_tmm_transfer(event_subir_csv)
            
        tmm_threads = []
        for mgr in successful_managers:
            t = threading.Thread(target=transferir_un_correo, args=(mgr,), daemon=True)
            t.start()
            tmm_threads.append(t)
            
        print(f"\n{Color.CYAN}Esperando a que las ventanas de TuneMyMusic se abran...{Color.ENDC}")
        time.sleep(0.5)
        
        input(f"\n{Color.BOLD}>>> Prepara el selector de archivo CSV en CADA TuneMyMusic "
              f"(todas las ventanas) y luego presiona ENTER.\n"
              f"    Cada ventana subirá SOLO su CSV emparejado (nombre = correo de la cuenta).\n"
              f"    Tras ENTER cada ventana esperará hasta 5 min a que el input esté listo "
              f"(ya no se cierra a los 30s). <<<{Color.ENDC}")
        
        # Desencadenar subida en todos los hilos
        event_subir_csv.set()
        
        print(f"\n{Color.CYAN}Subida disparada. Cada ventana espera su propio input; "
              f"si falla, permanece abierta para corrección manual.{Color.ENDC}")
        for t in tmm_threads:
            t.join()
            
        print(f"\n{Color.GREEN}{Color.BOLD}>>> Proceso de transferencia TuneMyMusic finalizado para todas las cuentas exitosas.{Color.ENDC}")
        
    print(f"\n{Color.GREEN}{Color.BOLD}>>> Proceso finalizado. Regresando al menú principal...{Color.ENDC}\n")


def parsear_titular_familiar_txt_opcion11(path: Path) -> tuple[list[dict], list[str]]:
    """Lee titular_familiar.txt.

    Formato preferido (bloques, define titular ↔ miembros a invitar):
        TITULAR
        correo@..., 0, disponible, []
        MIEMBROS:
        miembro1@...
        miembro2@...

    Formato antiguo (compatibilidad): líneas titular,... y una sola sección MIEMBROS:
    global al final (se reparte por cupos en orden).

    Cada dict titular incluye:
      correo, usados, estado, miembros (ya invitados / detalles),
      miembros_invitar (lista ordenada del bloque MIEMBROS de ese titular).
    El segundo valor de retorno es la lista plana de todos los miembros_invitar.
    """
    titulares: list[dict] = []
    miembros_planos: list[str] = []
    if not path.exists():
        return titulares, miembros_planos

    lines = path.read_text(encoding="utf-8").splitlines()

    def _parse_linea_titular(line_clean: str) -> dict | None:
        if "," not in line_clean or "@" not in line_clean:
            return None
        parts = [p.strip() for p in line_clean.split(",")]
        if not parts or "@" not in parts[0]:
            return None
        correo_t = re.sub(r'^[\s\.]+|[\s\.]+$', '', parts[0])
        if "@" not in correo_t:
            return None
        usados = 0
        if len(parts) >= 2:
            try:
                match = re.search(r'\d+', parts[1])
                usados = int(match.group()) if match else 0
            except Exception:
                usados = 0
        estado = parts[2].strip().lower() if len(parts) >= 3 else "disponible"
        miembros_detalles: list[str] = []
        lista_match = re.search(r'\[(.*?)\]', line_clean)
        if lista_match:
            inside = lista_match.group(1)
            emails_in_list = re.findall(
                r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', inside
            )
            if emails_in_list:
                miembros_detalles = emails_in_list
            else:
                try:
                    import ast
                    miembros_detalles = ast.literal_eval(lista_match.group())
                    if not isinstance(miembros_detalles, list):
                        miembros_detalles = []
                except Exception:
                    miembros_detalles = []
        usados = max(usados, len(miembros_detalles))
        if usados >= 5 or "lleno" in estado:
            estado = "lleno"
        return {
            "correo": correo_t,
            "usados": usados,
            "estado": estado,
            "miembros": list(miembros_detalles),
            "miembros_invitar": [],
        }

    # Detectar formato por bloques (línea TITULAR)
    tiene_bloques = any(
        ln.strip().upper() == "TITULAR" or ln.strip().upper().startswith("TITULAR")
        for ln in lines if ln.strip() and not ln.strip().startswith("#")
    )

    if tiene_bloques:
        actual: dict | None = None
        en_miembros = False
        for line in lines:
            line_clean = line.strip()
            if not line_clean or line_clean.startswith("#"):
                continue
            upper = line_clean.upper()
            if upper == "TITULAR" or upper.startswith("TITULAR"):
                if actual:
                    titulares.append(actual)
                actual = None
                en_miembros = False
                continue
            if upper.startswith("MIEMBROS"):
                en_miembros = True
                continue
            if en_miembros:
                if "@" in line_clean and "," not in line_clean:
                    correo_m = re.sub(r'^[\s\.]+|[\s\.]+$', '', line_clean)
                    if correo_m and "@" in correo_m and actual is not None:
                        if correo_m not in actual["miembros_invitar"]:
                            actual["miembros_invitar"].append(correo_m)
                        miembros_planos.append(correo_m)
                elif "," in line_clean and "@" in line_clean:
                    if actual:
                        titulares.append(actual)
                    actual = _parse_linea_titular(line_clean)
                    en_miembros = False
                continue
            parsed = _parse_linea_titular(line_clean)
            if parsed:
                if actual:
                    titulares.append(actual)
                actual = parsed
                en_miembros = False
        if actual:
            titulares.append(actual)
        return titulares, miembros_planos

    # --- Formato antiguo ---
    in_miembros_section = False
    for line in lines:
        line_clean = line.strip()
        if not line_clean or line_clean.startswith("#"):
            continue
        if "MIEMBROS:" in line_clean.upper():
            in_miembros_section = True
            continue
        if in_miembros_section:
            if "@" in line_clean:
                correo_m = re.sub(r'^[\s\.]+|[\s\.]+$', '', line_clean)
                if correo_m:
                    miembros_planos.append(correo_m)
        else:
            parsed = _parse_linea_titular(line_clean)
            if parsed:
                titulares.append(parsed)
    return titulares, miembros_planos


def invitar_al_plan_familiar_opcion11():
    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   OPCIÓN 11: INVITAR AL PLAN FAMILIAR AUTOMÁTICAMENTE (IMAP CODE){Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    
    path = SCRIPT_DIR / "titular_familiar.txt"
    if not path.exists():
        path1 = SCRIPT_DIR / "perfiles" / "familiar_titular.txt"
        if path1.exists():
            path = path1
            
    titulares, miembros_planos = parsear_titular_familiar_txt_opcion11(path)
    if not titulares:
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} No se encontraron cuentas titulares válidas en {path.name}.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    # ¿Hay bloques con MIEMBROS por titular?
    hay_asignacion_por_bloque = any(t.get("miembros_invitar") for t in titulares)

    if hay_asignacion_por_bloque:
        print(f"\n  [Opción 11] Formato por bloques TITULAR/MIEMBROS en {path.name}.")
        print(f"  [Opción 11] Titulares cargados ({len(titulares)}):")
        for t in titulares:
            print(f"    • {t['correo']} → {len(t.get('miembros_invitar') or [])} miembro(s) "
                  f"(usados={t['usados']}, estado={t['estado']})")
            for m in (t.get("miembros_invitar") or []):
                print(f"        - {m}")
    else:
        miembros = list(miembros_planos)
        if not miembros:
            print(f"\n{Color.WARNING}[Info]{Color.ENDC} No se encontraron miembros en {path.name}.")
            miembros = ingresar_correos()
        print(f"\n  [Opción 11] Titulares cargados ({len(titulares)}): {[t['correo'] for t in titulares]}")
        print(f"  [Opción 11] Total de miembros a invitar ({len(miembros)}): {miembros}")

    print(f"\n{Color.CYAN}[Proxies PE] Habilitando proxies de Perú obligatorios para invitaciones familiares (Opción 11)...{Color.ENDC}")
    valid_pe_list = asegurar_proxies_peru(cantidad_necesaria=len(titulares))
    if not valid_pe_list:
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} No hay proxies de Perú válidos y esta opción los exige.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    def clean_email_loc(email):
        if not email:
            return ""
        email = email.strip().lower()
        parts = email.split("@")
        if len(parts) == 2:
            local = parts[0].replace(".", "")
            return f"{local}@{parts[1]}"
        return email

    def _ya_invitado(titular: dict, miembro: str) -> bool:
        for m in titular.get("miembros") or []:
            # Exacto con puntos: en Tidal cada variante con puntos es otra cuenta
            if correos_iguales_exacto(m, miembro):
                return True
        return False

    titulares_trabajos = []

    if hay_asignacion_por_bloque:
        for idx_t, titular in enumerate(titulares, 1):
            cupos_disponibles = 5 - int(titular.get("usados") or 0)
            if cupos_disponibles <= 0 or titular.get("estado") == "lleno":
                print(f"  [Opción 11] Titular {titular['correo']} ya se encuentra lleno (5/5). Omitiendo...")
                continue
            plan = list(titular.get("miembros_invitar") or [])
            pendientes = [m for m in plan if not _ya_invitado(titular, m)]
            miembros_titular = pendientes[:cupos_disponibles]
            if not miembros_titular:
                print(f"  [Opción 11] Titular {titular['correo']}: sin miembros pendientes en su bloque MIEMBROS.")
                continue
            titulares_trabajos.append({
                "idx_t": idx_t,
                "titular": titular,
                "miembros_titular": miembros_titular,
            })
    else:
        idx_miembro = 0
        total_miembros = len(miembros)
        for idx_t, titular in enumerate(titulares, 1):
            if idx_miembro >= total_miembros:
                break
            cupos_disponibles = 5 - int(titular.get("usados") or 0)
            if cupos_disponibles <= 0:
                print(f"  [Opción 11] Titular {titular['correo']} ya se encuentra lleno (5/5). Omitiendo...")
                continue
            miembros_titular = miembros[idx_miembro: idx_miembro + cupos_disponibles]
            idx_miembro += len(miembros_titular)
            titular["miembros_invitar"] = list(miembros_titular)
            titulares_trabajos.append({
                "idx_t": idx_t,
                "titular": titular,
                "miembros_titular": miembros_titular,
            })

    if not titulares_trabajos:
        print(f"\n{Color.WARNING}[Opción 11] No hay titulares con cupos/miembros pendientes para procesar.{Color.ENDC}")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    lock_guardar = threading.Lock()

    def procesar_un_titular(trabajo):
        idx_t = trabajo["idx_t"]
        titular = trabajo["titular"]
        miembros_titular = trabajo["miembros_titular"]
        if idx_t > 1:
            time.sleep((idx_t - 1) * 0.35)

        print(f"\n{Color.CYAN}{Color.BOLD}" + "-" * 60 + f"{Color.ENDC}")
        print(f"{Color.CYAN}{Color.BOLD} PROCESANDO TITULAR ({idx_t}/{len(titulares)}): {titular['correo']}{Color.ENDC}")
        print(f"  Miembros asignados a este titular ({len(miembros_titular)}): {miembros_titular}")
        print(f"{Color.CYAN}{Color.BOLD}" + "-" * 60 + f"{Color.ENDC}")

        temp_dir = Path(tempfile.mkdtemp(prefix=f"tidal_inviter_{clean_email_loc(titular['correo'])}_"))
        inviter = TidalFamilyInviter(
            queue.Queue(),
            client_email=titular["correo"],
            perfil_dir=temp_dir,
        )

        try:
            inviter.abrir_navegador()
            print(f"  [Opción 11] Usando proxy de Perú único para {titular['correo']}: {inviter.proxy_pe_server}")

            if not inviter.asegurar_login_titular(titular):
                print(f"  {Color.FAIL}[Opción 11] ERROR: No se pudo iniciar sesión en el titular {titular['correo']}.{Color.ENDC}")
                return

            print(f"  {Color.GREEN}[Opción 11] Sesión iniciada con éxito en la vista de familia para: {titular['correo']}{Color.ENDC}")

            for idx_m, miembro_c in enumerate(miembros_titular):
                print(f"  [Opción 11] [{titular['correo']}] Invitando a {miembro_c}...")
                if inviter.enviar_invitacion_familiar(titular, miembro_c):
                    with lock_guardar:
                        if miembro_c not in titular["miembros"]:
                            titular["miembros"].append(miembro_c)
                        titular["usados"] = len(titular["miembros"])
                        if titular["usados"] >= 5:
                            titular["estado"] = "lleno"
                        guardar_titulares_familiares(titulares, path)
                    print(f"    {Color.GREEN}[OK] [{titular['correo']}] Invitación enviada a {miembro_c}.{Color.ENDC}")
                else:
                    print(f"    {Color.WARNING}[WARN] [{titular['correo']}] No se pudo enviar la invitación a {miembro_c}.{Color.ENDC}")

                if idx_m < len(miembros_titular) - 1:
                    time.sleep(random.uniform(2.0, 3.5))

            print(f"  [Opción 11] Completadas las invitaciones para el titular {titular['correo']}. Cerrando ventana...")
        except Exception as ex:
            print(f"  {Color.FAIL}[Opción 11] Error en titular {titular['correo']}: {ex}{Color.ENDC}")
        finally:
            try:
                inviter.cerrar_recursos()
            except Exception:
                pass
            cerrar_sesion_imap_hilo()

            def _rm_async(p_dir):
                time.sleep(2.0)
                try:
                    shutil.rmtree(p_dir, ignore_errors=True)
                except Exception:
                    pass

            threading.Thread(target=_rm_async, args=(temp_dir,), daemon=True).start()

    print(f"\n{Color.CYAN}{Color.BOLD}Iniciando {len(titulares_trabajos)} cuentas titulares en SIMULTÁNEO en ventanas separadas...{Color.ENDC}\n")
    with ThreadPoolExecutor(max_workers=len(titulares_trabajos)) as executor:
        futures = [executor.submit(procesar_un_titular, t_trabajo) for t_trabajo in titulares_trabajos]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"  {Color.FAIL}[Opción 11] Excepción en hilo de titular: {e}{Color.ENDC}")

    print(f"\n{Color.GREEN}{Color.BOLD}=== OPCIÓN 11 FINALIZADA EXITOSAMENTE ==={Color.ENDC}\n")
    input(">>> Presiona Enter para volver al menú principal <<<")


def ingresar_correos():
    correos = []
    while not correos:
        primer = input(f"{Color.BOLD}Introduce el correo de TIDAL del cliente:{Color.ENDC} ").strip()
        if primer:
            # Limpiar posibles puntos o espacios al principio o final del correo introducido
            primer = re.sub(r'^[\s\.]+|[\s\.]+$', '', primer)
            if "@" not in primer:
                print(f"{Color.FAIL}[Error]{Color.ENDC} Formato de correo inválido.")
                continue
            correos.append(primer)
            
            # Bucle para leer más correos con >>
            while True:
                siguiente = input(">> ").strip()
                if not siguiente:
                    break
                # Limpiar posibles puntos o espacios
                siguiente = re.sub(r'^[\s\.]+|[\s\.]+$', '', siguiente)
                if "@" not in siguiente:
                    print(f"{Color.FAIL}[Error]{Color.ENDC} Formato de correo inválido. Se omite.")
                    continue
                correos.append(siguiente)
    return correos


def tiene_contrasena_imap_registrada(gmail_user_solicitado: str) -> bool:
    """Verifica si la cuenta especificada tiene su propia contraseña IMAP/App Password en passwords.txt."""
    pwd_file = SCRIPT_DIR / "passwords.txt"
    if not pwd_file.exists():
        return False
        
    try:
        lines = pwd_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return False
    
    gmail_user_solicitado = gmail_user_solicitado.lower().strip()
    if "@gmail.com" in gmail_user_solicitado:
        username, domain = gmail_user_solicitado.split("@", 1)
        solicitado_no_dots = username.replace(".", "") + "@" + domain
    else:
        solicitado_no_dots = gmail_user_solicitado

    user_clean_key = solicitado_no_dots.replace("@", "_at_").replace(".", "_")
    
    for line in lines:
        if "=" in line:
            key, val = line.split("=", 1)
            val_clean = val.strip().strip('"').strip("'")
            if not val_clean:
                continue
            key_name = key.strip().lower()
            if key_name.startswith("gmail_app_password_") or key_name.startswith("imap_password_"):
                email_part = key_name[19:].strip() if key_name.startswith("gmail_app_password_") else key_name[14:].strip()
                if "@" in email_part:
                    usr, dom = email_part.split("@", 1)
                    email_part_no_dots = usr.replace(".", "") + "@" + dom
                    if email_part_no_dots == solicitado_no_dots:
                        return True
            
            key_clean = key.strip().lower().replace("@", "_at_").replace(".", "_")
            if (key_clean == f"gmail_app_password_{user_clean_key}" or 
                key_clean == f"gmail_app_password_{solicitado_no_dots}" or
                key_clean == f"imap_password_{user_clean_key}" or
                key_clean == f"imap_password_{solicitado_no_dots}"):
                return True

    # Fallback solo para la cuenta por defecto cakeseller1234 si existe gmail_app_password=
    if "cakeseller1234" in solicitado_no_dots:
        for line in lines:
            if "=" in line:
                key, val = line.split("=", 1)
                key_stripped = key.strip().lower()
                val_clean = val.strip().strip('"').strip("'")
                if key_stripped in ("gmail_app_password", "imap_password") and val_clean:
                    return True

    return False


def remover_puntos_correo(correo: str) -> str:
    correo = correo.strip()
    if "@" in correo:
        username, domain = correo.split("@", 1)
        return f"{username.replace('.', '')}@{domain}"
    return correo


def verificar_contrasenas_imap_opcion12(correos: list[str]):
    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   OPCIÓN 12: VERIFICAR CONTRASEÑAS IMAP EN PASSWORDS.TXT{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    
    if not correos:
        print(f"\n{Color.WARNING}[Info]{Color.ENDC} No hay correos activos cargados para verificar.")
        return

    faltantes = []
    
    for correo in correos:
        if not tiene_contrasena_imap_registrada(correo):
            correo_limpio = remover_puntos_correo(correo)
            if correo_limpio not in faltantes:
                faltantes.append(correo_limpio)
            
    if not faltantes:
        print(f"\n{Color.GREEN}{Color.BOLD}>>> TODO ESTÁ CORRECTO: Todos los correos tienen su contraseña IMAP registrada. <<<{Color.ENDC}\n")
    else:
        print(f"\n{Color.FAIL}{Color.BOLD}>>> FALTAN REGISTRAR CONTRASEÑAS IMAP EN PASSWORDS.TXT PARA LOS SIGUIENTES CORREOS ({len(faltantes)}): <<<{Color.ENDC}")
        for c in faltantes:
            print(f"  {Color.FAIL}✖ {c}{Color.ENDC}")
def crear_cuentas_familiares_automatico_opcion14():
    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   OPCIÓN 14: CREAR CUENTAS FAMILIARES AUTOMÁTICO (NIGERIA){Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"{Color.FAIL}[Error]{Color.ENDC} Playwright no está instalado. Ejecute 'pip install playwright'.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    path_titulares = SCRIPT_DIR / "crear_cuentastitulares_imap.txt"
    cuentas_map = {}

    if path_titulares.exists():
        for line in path_titulares.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", " ").replace("=", " ").split()
            if len(parts) >= 2:
                correo = parts[0].strip().strip('"').strip("'")
                pwd = parts[1].strip().strip('"').strip("'")
                cuentas_map[correo] = pwd
            elif len(parts) == 1 and "@" in parts[0]:
                correo = parts[0].strip().strip('"').strip("'")
                pwd = buscar_contrasena_cuenta(correo) or "Tidal123456!"
                cuentas_map[correo] = pwd

    if not cuentas_map:
        print(f"\n{Color.WARNING}[Info]{Color.ENDC} No se encontraron cuentas en 'crear_cuentastitulares_imap.txt'.")
        input_correos = input("Ingresa los correos a registrar separados por coma: ").strip()
        if not input_correos:
            print(f"{Color.FAIL}[Error] No se ingresaron correos.{Color.ENDC}")
            input(">>> Presiona Enter para volver al menú principal <<<")
            return
        for c in input_correos.split(","):
            c_clean = c.strip()
            if c_clean:
                pwd = buscar_contrasena_cuenta(c_clean) or "Tidal123456!"
                cuentas_map[c_clean] = pwd

    correos_lista = list(cuentas_map.keys())
    print(f"\nSe cargaron {len(correos_lista)} cuentas para crear como Titulares Familiares desde 'crear_cuentastitulares_imap.txt'.")

    headless_opt = input("\n¿Deseas ejecutar el navegador en segundo plano (headless)? (s/n, por defecto 'n'): ").strip().lower()
    headless = headless_opt in ("s", "si", "yes", "y")

    num_cuentas = len(correos_lista)
    global valid_ng_list, CACHE_PROXIES_NG
    valid_ng_list = []

    print(f"\n{Color.CYAN}[Proxies Nigeria] Obteniendo proxies de Nigeria para el registro inicial...{Color.ENDC}")
    cargar_cache_proxies_validos_desde_disco()
    if CACHE_PROXIES_NG:
        valid_ng_list = list(CACHE_PROXIES_NG)
        print(f"{Color.GREEN}[Proxy Caché] Usando {len(valid_ng_list)} proxies de NIGERIA previamente validados.{Color.ENDC}")
    else:
        try:
            from config_migrar import proxies_cfg
        except ImportError:
            proxies_cfg = None

        if proxies_cfg and proxies_cfg.get("proxy_ng_list"):
            ng_list = proxies_cfg["proxy_ng_list"]
        else:
            proxies_cfg_local = cargar_proxies_desde_txt(preferir_validos=False)
            ng_list = proxies_cfg_local.get("proxy_ng_list", []) if proxies_cfg_local else []

        if ng_list:
            # Margen de repuestos: cada bloqueo antirobot quema el proxy de forma permanente y
            # validar sólo tantos como cuentas dejaba el pool vacío en la primera rotación.
            objetivo_ng = max(num_cuentas * 4, num_cuentas + 15)
            valid_ng_list = probar_y_seleccionar_mejor_proxy(ng_list, "Nigeria", objetivo_ng)
            if valid_ng_list:
                guardar_proxies_validos_txt(SCRIPT_DIR / "lista_proxies_ng_validos.txt", valid_ng_list)
        else:
            print(f"{Color.WARNING}[WARN]{Color.ENDC} No se encontraron proxies de Nigeria en la configuración.")

    # El registro debe verse desde Nigeria: sin proxy NG la cuenta se crearía con geo incorrecto
    # y además expondría la IP real a DataDome, que la marcaría para todo el resto del proceso.
    if not valid_ng_list:
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} No hay proxies de Nigeria válidos y el registro los exige. "
              f"Valida la lista con la opción 13 antes de reintentar.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    alimentar_pool_proxies_nigeria(valid_ng_list)
    GLOBAL_NG_PROXY_POOL.reiniciar_bloqueos()
    GLOBAL_PE_PROXY_POOL.reiniciar_bloqueos()

    global valid_pe_list, CACHE_PROXIES_PE
    print(f"\n{Color.CYAN}[Proxies Perú] Obteniendo proxies de Perú para el pago y checkout...{Color.ENDC}")
    # asegurar_proxies_peru también alimenta GLOBAL_PE_PROXY_POOL, necesario para las rotaciones
    # y para TuneMyMusic. Antes se construía la lista a mano y el pool quedaba vacío.
    valid_pe_list = asegurar_proxies_peru(cantidad_necesaria=num_cuentas)
    if not valid_pe_list:
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} No hay proxies de Perú válidos y el checkout los exige. "
              f"Valida la lista con la opción 13 antes de reintentar.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    # Ninguna ventana simultánea debe compartir IP con otra: limitar el lote al número de proxies
    # realmente disponibles evita que el módulo del índice repita el mismo proxy dentro del lote.
    batch_size = max(1, min(10, len(valid_ng_list), len(valid_pe_list)))
    if batch_size < min(10, num_cuentas):
        print(f"{Color.WARNING}[Proxies]{Color.ENDC} Se procesarán {batch_size} ventana(s) a la vez "
              f"para no repetir proxy entre ventanas simultáneas.")
    total_cuentas = len(correos_lista)
    print(f"\n{Color.CYAN}{Color.BOLD}Iniciando creación de {total_cuentas} cuentas familiares en bloques de máximo {batch_size} ventanas simultáneas...{Color.ENDC}\n")

    success_count = 0
    fail_count = 0

    for b_start in range(0, total_cuentas, batch_size):
        lote_correos = correos_lista[b_start : b_start + batch_size]
        num_lote = len(lote_correos)

        if total_cuentas > batch_size:
            print(f"\n{Color.CYAN}{Color.BOLD}--- Procesando Lote ({b_start + 1} a {b_start + num_lote} de {total_cuentas}) ---{Color.ENDC}")

        def crear_un_titular(idx_rel, correo):
            # Escalonado amplio y con jitter: abrir todas las ventanas a la vez es un patrón que el
            # antirobot puntúa aunque cada una salga por una IP distinta.
            if idx_rel > 1:
                time.sleep((idx_rel - 1) * random.uniform(1.5, 3.0))
            pwd = cuentas_map[correo]

            p_ng = GLOBAL_NG_PROXY_POOL.obtener_proxy_unico() if valid_ng_list else None
            p_server = p_ng.get("server") if p_ng else None
            p_user = p_ng.get("username") if p_ng else None
            p_pass = p_ng.get("password") if p_ng else None
            if valid_ng_list and not p_server:
                print(f"  {Color.FAIL}[Proxy NG] [{correo}] Sin proxy de Nigeria libre; se omite la cuenta.{Color.ENDC}")
                return correo, False

            # Reservar PE único del pool: el índice modular podía repetir IP si otra ventana
            # rotaba al mismo proxy, y el checkout/upgrade se bloqueaban juntos.
            p_pe = GLOBAL_PE_PROXY_POOL.obtener_proxy_unico()
            p_pe_server = p_pe.get("server") if p_pe else None
            p_pe_user = p_pe.get("username") if p_pe else None
            p_pe_pass = p_pe.get("password") if p_pe else None
            if not p_pe_server:
                print(f"  {Color.FAIL}[Proxy PE] [{correo}] Sin proxy de Perú libre; se omite la cuenta.{Color.ENDC}")
                try:
                    GLOBAL_NG_PROXY_POOL.liberar_proxy(p_server)
                except Exception:
                    pass
                return correo, False

            manager = TidalRegisterManager(
                client_email=correo,
                client_pwd=pwd,
                proxy_ng_server=p_server,
                proxy_ng_user=p_user,
                proxy_ng_pass=p_pass,
                proxy_pe_server=p_pe_server,
                proxy_pe_user=p_pe_user,
                proxy_pe_pass=p_pe_pass,
                headless=headless
            )
            print(f"\n{Color.CYAN}{Color.BOLD}[Titular Familiar Auto] Iniciando para: {correo}{Color.ENDC}")
            exito = False
            try:
                exito = manager.run_register_and_upgrade_family()
            except Exception as e_t:
                # Ninguna excepción debe dejar Chrome ni Playwright abiertos al salir del hilo
                print(f"  {Color.FAIL}[ERROR] [{correo}] Proceso interrumpido: {e_t}{Color.ENDC}")
                try:
                    manager.cerrar_navegador()
                except Exception:
                    pass
                exito = False
            finally:
                try:
                    GLOBAL_NG_PROXY_POOL.liberar_proxy(p_server)
                except Exception:
                    pass
                try:
                    GLOBAL_PE_PROXY_POOL.liberar_proxy(p_pe_server)
                except Exception:
                    pass
                cerrar_sesion_imap_hilo()
            return correo, exito

        workers = min(batch_size, num_lote)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(crear_un_titular, idx_rel, correo): correo for idx_rel, correo in enumerate(lote_correos, 1)}
            for future in as_completed(futures):
                correo = futures[future]
                try:
                    c, exito = future.result()
                    if exito:
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    print(f"  {Color.FAIL}[ERROR] Excepción procesando titular {correo}: {e}{Color.ENDC}")
                    fail_count += 1

    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   RESUMEN DE CREACIÓN DE CUENTAS FAMILIARES{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f" Cuentas creadas con éxito: {Color.GREEN}{success_count}{Color.ENDC}")
    print(f" Cuentas fallidas: {Color.FAIL}{fail_count}{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}\n")
    input(">>> Presiona Enter para volver al menú principal <<<")


def menu_principal():
    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   EXTRACTOR DE CORREOS IMAP - TIDAL MIGRATOR{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    
    correos = ingresar_correos()
            
    while True:
        print(f"\n{Color.CYAN}{Color.BOLD}" + "-"*50 + f"{Color.ENDC}")
        if len(correos) == 1:
            print(f"{Color.CYAN}{Color.BOLD} Correo activo: {correos[0]}{Color.ENDC}")
        else:
            print(f"{Color.CYAN}{Color.BOLD} Correos activos:{Color.ENDC}")
            for c in correos:
                print(f"  - {c}")
        print(f"{Color.CYAN}{Color.BOLD}" + "-"*50 + f"{Color.ENDC}")
        print(" 1. Obtener CÓDIGO DE REGISTRO (Welcome / Verification)")
        print(" 2. Obtener CÓDIGO DE ELIMINACIÓN (Delete Account)")
        print(" 3. Obtener CÓDIGO DE INICIO DE SESIÓN (Login Verification)")
        print(" 4. Buscar y aceptar ENLACE DE INVITACIÓN (auto-login + cerrar Chrome)")
        print(" 5. Buscar y completar ENLACE DE RESTABLECIMIENTO (auto-pwd + cerrar Chrome)")
        print(" 6. Cambiar de correo electrónico (define qué cuentas se procesan en el menú)")
        print(" 7. Salir")
        print(" 8. Registrar cuenta(s) automáticamente en TIDAL (Nigeria)")
        print(" 9. Restablecer contraseña(s) automáticamente en TIDAL")
        print(" 10. Iniciar sesión automática (Login automático en TIDAL / TMM)")
        print(" 11. Invitar al plan familiar (Titulares e Invitaciones Automáticas)")
        print(" 12. Verificar contraseñas IMAP registradas en passwords.txt")
        print(" 13. Validar y verificar lista de proxies (Nigeria / Perú)")
        print(" 14. Crear cuentas FAMILIARES AUTOMÁTICO (Registro NG + Checkout + Upgrade Family)")
        print(f"{Color.CYAN}{Color.BOLD}" + "-"*50 + f"{Color.ENDC}")
        
        opcion = input(f"{Color.BOLD}Selecciona una opción (1-14):{Color.ENDC} ").strip()
        
        if opcion in ("1", "2", "3"):
            for correo in correos:
                print(f"\n{Color.BLUE}--- Procesando para: {correo} ---{Color.ENDC}")
                if opcion == "1":
                    codigo = obtener_codigo_via_imap(
                        gmail_user=correo,
                        required_keywords=["registr", "bienven", "código", "code", "verific"],
                        query_exclude="cancel"
                    )
                    if codigo:
                        print(f"{Color.GREEN}{Color.BOLD}>>> CÓDIGO ENCONTRADO: {codigo} <<<{Color.ENDC}")
                    else:
                        print(f"{Color.FAIL}>>> No se encontró ningún código de registro reciente o las credenciales fallaron. <<<{Color.ENDC}")
                        
                elif opcion == "2":
                    codigo = obtener_codigo_via_imap(
                        gmail_user=correo,
                        required_keywords=["elimin", "desactiv", "delete", "code", "codigo"]
                    )
                    if codigo:
                        print(f"{Color.GREEN}{Color.BOLD}>>> CÓDIGO ENCONTRADO: {codigo} <<<{Color.ENDC}")
                    else:
                        print(f"{Color.FAIL}>>> No se encontró ningún código de eliminación reciente o las credenciales fallaron. <<<{Color.ENDC}")
                        
                elif opcion == "3":
                    codigo = obtener_codigo_via_imap(
                        gmail_user=correo,
                        required_keywords=["código", "code", "inici"],
                        query_exclude="cancel"
                    )
                    if codigo:
                        print(f"{Color.GREEN}{Color.BOLD}>>> CÓDIGO ENCONTRADO: {codigo} <<<{Color.ENDC}")
                    else:
                        print(f"{Color.FAIL}>>> No se encontró ningún código de inicio de sesión reciente o las credenciales fallaron. <<<{Color.ENDC}")
            print()

        elif opcion == "5":
            print(f"\n{Color.CYAN}{Color.BOLD}=== RESTABLECIMIENTO DESDE ENLACE IMAP (auto-pwd) ==={Color.ENDC}")
            print("  Buscando enlaces de restablecimiento y contraseñas en sesiones_imap_cuentas.txt...")
            enlaces_reset = {}
            for correo in correos:
                print(f"\n{Color.BLUE}--- Buscando enlace para: {correo} ---{Color.ENDC}")
                pwd_previa = buscar_contrasena_cuenta(correo)
                if pwd_previa:
                    print(f"    {Color.GREEN}[Cuentas] Contraseña encontrada en sesiones_imap_cuentas.txt.{Color.ENDC}")
                else:
                    print(f"    {Color.WARNING}[Cuentas] Sin contraseña anotada para {correo}. "
                          f"Añade 'correo\\tcontraseña' en sesiones_imap_cuentas.txt.{Color.ENDC}")
                enlace = obtener_codigo_via_imap(
                    gmail_user=correo,
                    required_keywords=[
                        "resetting your tidal password",
                        "restablecer tu contraseña de tidal",
                        "reset your password",
                        "link to reset your password",
                    ],
                    query_exclude="cancel",
                    solo_link=True,
                )
                if enlace:
                    preview = enlace if len(enlace) <= 90 else enlace[:90] + "..."
                    print(f"    {Color.GREEN}[IMAP] Enlace de restablecimiento: {preview}{Color.ENDC}")
                    enlaces_reset[correo] = enlace
                else:
                    print(f"    {Color.FAIL}[IMAP] No se encontró enlace de restablecimiento para {correo}{Color.ENDC}")

            if enlaces_reset:
                print(f"\n{Color.CYAN}[Proxies PE] Habilitando proxies de Perú obligatorios para abrir los enlaces...{Color.ENDC}")
                proxies_pe = asegurar_proxies_peru(cantidad_necesaria=len(enlaces_reset))
                if not proxies_pe:
                    print(f"\n{Color.FAIL}[Error]{Color.ENDC} No hay proxies de Perú válidos y esta acción los exige. "
                          f"Valida la lista con la opción 13 antes de reintentar.")
                else:
                    print(f"\nAbriendo {len(enlaces_reset)} restablecimientos en paralelo "
                          f"(auto-relleno de contraseña + cierre al éxito)...")

                    def procesar_reset_hilo(idx, item):
                        if idx > 1:
                            time.sleep((idx - 1) * random.uniform(1.5, 3.0))
                        correo, enlace = item
                        p_pe = GLOBAL_PE_PROXY_POOL.obtener_proxy_unico(espera_s=60.0)
                        if not p_pe and proxies_pe:
                            p_pe = proxies_pe[(idx - 1) % len(proxies_pe)]
                        return abrir_enlace_restablecimiento_con_autocierre(enlace, correo, proxy_pe=p_pe)

                    ok_n, fail_n = 0, 0
                    with ThreadPoolExecutor(max_workers=min(20, len(enlaces_reset))) as executor:
                        items = list(enlaces_reset.items())
                        futures = {
                            executor.submit(procesar_reset_hilo, idx + 1, item): item[0]
                            for idx, item in enumerate(items)
                        }
                        for future in as_completed(futures):
                            correo_f = futures[future]
                            try:
                                if future.result():
                                    ok_n += 1
                                else:
                                    fail_n += 1
                            except Exception as ex_h:
                                fail_n += 1
                                print(f"    {Color.FAIL}[ERROR] Excepción en restablecimiento "
                                      f"de {correo_f}: {ex_h}{Color.ENDC}")
                    print(f"\n{Color.BLUE}Resumen opción 5:{Color.ENDC} "
                          f"{Color.GREEN}OK={ok_n}{Color.ENDC} / {Color.FAIL}fallidas={fail_n}{Color.ENDC}")
            else:
                print(f"\n{Color.FAIL}>>> No se encontró ningún enlace de restablecimiento en las cuentas activas. <<<\n")
            print()
            
        elif opcion == "4":
            print(f"\n{Color.CYAN}{Color.BOLD}=== PROCESANDO INVITACIONES FAMILIARES SIMULTÁNEAMENTE ==={Color.ENDC}")
            # Asignación coordinada por buzón: N alias con puntos del mismo Gmail ya no
            # compiten por el mismo UID (To: canónico) dejando 4/5 sin enlace.
            print("  Buscando y asignando enlaces de invitación (coordinado por buzón Gmail)...")
            print("  Las contraseñas se toman de sesiones_imap_cuentas.txt (correo + contraseña).")
            enlaces_map = asignar_enlaces_invitacion_a_correos(correos)
            for c in correos:
                e = enlaces_map.get(c)
                if not e:
                    # claves pueden estar strip'eadas
                    e = next((enlaces_map[k] for k in enlaces_map if k.strip().lower() == c.strip().lower()), None)
                    if e:
                        enlaces_map[c] = e
                if e:
                    preview = e if len(e) <= 90 else e[:90] + "..."
                    print(f"    {Color.GREEN}[IMAP] Enlace asignado para {c}: {preview}{Color.ENDC}")
                    if buscar_contrasena_cuenta(c):
                        print(f"    {Color.GREEN}[Cuentas] Contraseña lista para auto-login de {c}.{Color.ENDC}")
                    else:
                        print(f"    {Color.WARNING}[Cuentas] Sin contraseña en sesiones_imap_cuentas.txt "
                              f"para {c}.{Color.ENDC}")
                else:
                    print(f"    {Color.FAIL}[IMAP] No se encontró invitación para {c}{Color.ENDC}")

            enlaces_map = {c: enlaces_map[c] for c in correos if enlaces_map.get(c)}

            if enlaces_map:
                global valid_pe_list, CACHE_PROXIES_PE
                # Las invitaciones se abren contra Tidal: siempre por proxy de Perú, sin importar cuántas
                # sean. Antes sólo se usaba proxy con más de 10 enlaces y el resto salía por la IP real.
                print(f"\n{Color.CYAN}[Proxies PE] Habilitando proxies de Perú obligatorios para abrir las invitaciones...{Color.ENDC}")
                proxies_pe = asegurar_proxies_peru(cantidad_necesaria=len(enlaces_map))
                if not proxies_pe:
                    print(f"\n{Color.FAIL}[Error]{Color.ENDC} No hay proxies de Perú válidos y esta acción los exige. "
                          f"Valida la lista con la opción 13 antes de reintentar.")

                print(f"\nAbriendo {len(enlaces_map)} invitaciones familiares en paralelo "
                      f"(auto-login + aceptar + cerrar al éxito, hasta 20 hilos)...")
                
                def procesar_invitacion_hilo(idx, item):
                    # Escalonado amplio y con jitter: abrir todas las ventanas casi a la vez es un
                    # patrón que el antirobot puntúa aunque cada una salga por una IP distinta.
                    if idx > 1:
                        time.sleep((idx - 1) * random.uniform(1.5, 3.0))
                    correo, enlace = item
                    # Reservar IP única del pool (no reutilizar por índice % len: dos hilos
                    # compartían el mismo proxy y generaban ERR_TUNNEL).
                    p_pe = GLOBAL_PE_PROXY_POOL.obtener_proxy_unico(espera_s=60.0)
                    if not p_pe and proxies_pe:
                        p_pe = proxies_pe[(idx - 1) % len(proxies_pe)]
                    abrir_enlace_familia_con_autocierre(enlace, correo, proxy_pe=p_pe)
                    
                with ThreadPoolExecutor(max_workers=min(20, len(enlaces_map))) as executor:
                    items = list(enlaces_map.items())
                    futures = [executor.submit(procesar_invitacion_hilo, idx + 1, item) for idx, item in enumerate(items)]
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as ex_h:
                            print(f"    {Color.FAIL}[ERROR] Excepción en invitación: {ex_h}{Color.ENDC}")
            else:
                print(f"\n{Color.FAIL}>>> No se encontró ningún enlace de invitación en las cuentas activas. <<<\n")
            print()
            
        elif opcion == "6":
            print(f"\n{Color.CYAN}Introduce solo los correos que esta instancia debe procesar "
                  f"(el resto del archivo queda para otras ventanas del script).{Color.ENDC}")
            correos = ingresar_correos()
                    
        elif opcion == "7":
            print(f"\n{Color.BOLD}Saliendo... ¡Hasta pronto!{Color.ENDC}\n")
            break
            
        elif opcion == "8":
            registrar_cuentas_tidal(correos)
            
        elif opcion == "9":
            restablecer_contrasenas_tidal(correos)
            
        elif opcion == "10":
            iniciar_sesion_automatico_tidal(correos)
            
        elif opcion == "11":
            invitar_al_plan_familiar_opcion11()

        elif opcion == "12":
            verificar_contrasenas_imap_opcion12(correos)

        elif opcion == "13":
            validar_proxies_opcion13()

        elif opcion == "14":
            crear_cuentas_familiares_automatico_opcion14()
            
        else:
            print(f"\n{Color.FAIL}[Error]{Color.ENDC} Opción inválida. Selecciona un número del 1 al 14.")


if __name__ == "__main__":
    try:
        # Habilitar soporte de colores ANSI en Windows cmd/powershell
        if os.name == 'nt':
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass
        
    try:
        menu_principal()
    except KeyboardInterrupt:
        print(f"\n\n{Color.WARNING}Ejecución cancelada por el usuario.{Color.ENDC}\n")
        sys.exit(0)
