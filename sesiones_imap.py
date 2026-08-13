#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import imaplib
import email
import webbrowser
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

try:
    import winreg  # solo Windows
except ImportError:
    winreg = None


# Configurar salida estándar para UTF-8 en Windows
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
LINKS_EXTRAIDOS_PATH = SCRIPT_DIR / "linksextraidos.txt"

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
TITULARES_FILE_LOCK = threading.RLock()

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
    """True solo si el nombre del CSV es el correo EXACTO (respetando puntos Gmail)."""
    if not path or not client_email:
        return False
    stem = path.stem.strip()
    if not stem or "@" not in stem:
        return False
    return correos_iguales_exacto(stem, client_email)


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
    permitir_alias: bool = False,
    respetar_reservas: bool = True,
) -> Path | None:
    """Localiza el CSV de UNA cuenta en descargas/ por nombre EXACTO (con puntos).

    permitir_alias se ignora (legacy): nunca emparejar hermanos Gmail
    (getm.ushroom9470.csv ≠ getm.ushro.om94.70.csv).
    """
    email_l = (client_email or "").strip().lower()
    if not email_l:
        return None
    archivos = _listar_csvs_descargas()
    if not archivos:
        return None

    exactos = []
    for f in archivos:
        if respetar_reservas and _csv_reservado_por_otra(f, email_l):
            continue
        if correos_iguales_exacto(f.stem.strip(), client_email):
            exactos.append(f)

    if exactos:
        return _elegir_csv_preferido(exactos, client_email)
    return None


def asignar_csvs_a_cuentas(correos: list[str]) -> dict[str, Path | None]:
    """Asigna CSV↔cuenta 1:1 para un lote (opción 8). Solo nombre EXACTO con puntos.

    Un mismo archivo nunca se asigna a dos cuentas. Devuelve dict correo → Path|None.
    """
    resultado: dict[str, Path | None] = {c: None for c in correos}
    if not correos:
        return resultado

    usados: set[str] = set()
    archivos = _listar_csvs_descargas()

    for correo in correos:
        exactos = [
            f for f in archivos
            if correos_iguales_exacto(f.stem.strip(), correo)
            and _csv_clave_reserva(f) not in usados
        ]
        elegido = _elegir_csv_preferido(exactos, correo)
        if elegido:
            resultado[correo] = elegido
            usados.add(_csv_clave_reserva(elegido))
            reservar_csv_para_cuenta(elegido, correo)
        else:
            # Aviso si hay CSV de hermanos Gmail (no se usan)
            hermanos = [
                f.name for f in archivos
                if "@" in f.stem
                and son_correos_equivalentes(f.stem, correo)
                and not correos_iguales_exacto(f.stem, correo)
            ]
            if hermanos:
                print(f"  {Color.WARNING}[CSV] [{correo}] Sin '{correo}.csv'. "
                      f"Hay CSV de hermanos Gmail (NO se usan): {hermanos}. "
                      f"Renombra el archivo al correo exacto.{Color.ENDC}")

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
    if winreg is not None:
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
    """Limpia espacios, invisibles Unicode y basura final; deja el correo en minúsculas.

    Tidal a veces inserta U+200C (zero-width non-joiner) delante del correo en el DOM
    ('\\u200cg.et.mushroom...'). Si ese string llega a imaplib.login(), falla con:
    'ascii' codec can't encode character '\\u200c' — aunque la App Password sea correcta.
    """
    if not email_str:
        return ""
    email_str = str(email_str)
    # Zero-width / BOM / soft hyphen / bidi marks que ensucian correos scrapados del DOM
    email_str = re.sub(
        r"[\u200b\u200c\u200d\u200e\u200f\u202a-\u202e\u2060\ufeff\u00ad]",
        "",
        email_str,
    )
    email_str = email_str.strip().lower()
    email_str = re.sub(r"[\s.,]+$", "", email_str)
    return email_str

def _parse_linea_cuenta_sesiones(line: str) -> tuple[str, str] | None:
    """Parsea una línea de sesiones_imap_cuentas.txt → (correo, contraseña).

    Acepta tab, espacios, coma o '=' como separador. El correo es el primer campo;
    la contraseña es el resto (así L@abuela123 u otras claves con @/espacios no se parten mal).
    No interpreta indentación ni titulares/miembros: cada línea es una credencial plana.
    """
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    if "\t" in line:
        parts = [p.strip() for p in line.split("\t") if p.strip()]
    else:
        line_normalized = line.replace(",", " ").replace("=", " ")
        parts = line_normalized.split()
    if len(parts) < 2:
        return None
    correo = parts[0].strip().strip('"').strip("'")
    if "\t" in line:
        pwd = "\t".join(parts[1:]).strip().strip('"').strip("'")
        if "\t" in pwd:
            pwd = parts[1].strip().strip('"').strip("'")
    else:
        pwd = " ".join(parts[1:]).strip().strip('"').strip("'")
    if not correo or "@" not in correo or not pwd:
        return None
    correo = re.split(r'[\s,;]+', correo, maxsplit=1)[0].strip()
    if "@" not in correo:
        return None
    return correo, pwd


def buscar_contrasena_cuenta(correo_solicitado: str) -> str | None:
    """Busca la contraseña Tidal EXACTA en sesiones_imap_cuentas.txt (o passwords.txt).

    Crítico: en Tidal getm.us.hroom19.55 ≠ get.mushroom1.9.55 aunque Gmail sea el mismo
    buzón. Nunca usar son_correos_equivalentes aquí: devolvía la clave de otra fila
    (p.ej. 292949 en vez de 153351) y tras el reset el login fallaba con 'incorrecta'.
    """
    if not correo_solicitado:
        return None
    correo_solicitado = (correo_solicitado or "").strip()

    # 1. Exacto en sesiones_imap_cuentas.txt (igualdad con puntos)
    path_cuentas = SCRIPT_DIR / "sesiones_imap_cuentas.txt"
    if path_cuentas.exists():
        try:
            for line in path_cuentas.read_text(encoding="utf-8").splitlines():
                parsed = _parse_linea_cuenta_sesiones(line)
                if not parsed:
                    continue
                c, p = parsed
                if correos_iguales_exacto(c, correo_solicitado):
                    return p
        except Exception:
            pass

    # 2. Fallback passwords.txt: solo match exacto del correo
    path_pwds = SCRIPT_DIR / "passwords.txt"
    if path_pwds.exists():
        try:
            for line in path_pwds.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k_clean = k.strip()
                v_clean = v.strip().strip('"').strip("'")
                if correos_iguales_exacto(k_clean, correo_solicitado) and v_clean:
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
            parsed = _parse_linea_cuenta_sesiones(line)
            if not parsed:
                continue
            correo, pwd = parsed
            cuentas_map[correo] = pwd
    except Exception:
        pass
    return cuentas_map


def filtrar_cuentas_por_correos_activos(
    cuentas_map: dict[str, str],
    correos_activos: list[str] | None,
) -> dict[str, str] | None:
    """Deja solo las cuentas del archivo que coinciden EXACTO con los correos del menú.

    Clave del resultado = correo del menú (con sus puntos). Nunca sustituir por un
    hermano Gmail del .txt (getm.ushroom9470 ≠ getm.ushro.om94.70 en Tidal).

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
            if correos_iguales_exacto(c_arch, c_menu):
                elegido = c_arch
                break
        if elegido is None:
            # Aviso: hay hermanos en el archivo (mismo buzón, otros puntos) — NO usarlos
            hermanos = [
                c for c in cuentas_map
                if son_correos_equivalentes(c, c_menu) and not correos_iguales_exacto(c, c_menu)
            ]
            sin_match.append(c_menu)
            if hermanos:
                print(f"  {Color.WARNING}[Correos activos] '{c_menu}' no está EXACTO en el .txt; "
                      f"hay hermanos Gmail (NO se usan): {hermanos}{Color.ENDC}")
            continue
        # Conservar el correo del menú (no el del archivo, aunque sea igual ignorando mayúsculas)
        filtrado[c_menu] = cuentas_map[elegido]
        usados_arch.add(elegido)
        hermanos = [
            c for c in cuentas_map
            if c not in usados_arch
            and son_correos_equivalentes(c, c_menu)
            and not correos_iguales_exacto(c, c_menu)
        ]
        if hermanos:
            print(f"  {Color.CYAN}[Correos activos] '{c_menu}' → fila exacta OK. "
                  f"Hermanos en archivo (ignorados): {hermanos}{Color.ENDC}")

    total_arch = len(cuentas_map)
    print(f"\n{Color.CYAN}[Correos activos]{Color.ENDC} Menú: {len(correos_activos)} | "
          f"Archivo: {total_arch} | A procesar: {len(filtrado)}")
    for c_arch in filtrado:
        print(f"  {Color.GREEN}✓{Color.ENDC} {c_arch}")
    if sin_match:
        print(f"{Color.WARNING}[Correos activos] Sin fila EXACTA en sesiones_imap_cuentas.txt "
              f"(se omiten):{Color.ENDC}")
        for c in sin_match:
            print(f"  {Color.FAIL}✗{Color.ENDC} {c}")

    if not filtrado:
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} Ningún correo activo del menú está EXACTO en "
              f"sesiones_imap_cuentas.txt. Usa la opción 6 o anota correo+contraseña (con los "
              f"mismos puntos) en el archivo.")
        return None

    if len(filtrado) < total_arch:
        print(f"{Color.CYAN}[Correos activos]{Color.ENDC} Se omiten {total_arch - len(filtrado)} "
              f"cuenta(s) del archivo que no están en el menú (otras instancias pueden usarlas).")
    return filtrado


def guardar_credencial_cuenta(correo: str, pwd: str) -> bool:
    """Registra 'correo<TAB>contraseña' en sesiones_imap_cuentas.txt si aún no está.

    Compara EXACTO con puntos: un hermano Gmail es otra fila Tidal y se puede añadir.
    """
    if not correo or not pwd:
        return False
    path_cuentas = SCRIPT_DIR / "sesiones_imap_cuentas.txt"
    with CUENTAS_FILE_LOCK:
        try:
            existentes = []
            if path_cuentas.exists():
                existentes = path_cuentas.read_text(encoding="utf-8").splitlines()
            for line in existentes:
                parsed = _parse_linea_cuenta_sesiones(line)
                if parsed and correos_iguales_exacto(parsed[0], correo):
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
                            'código de acceso', 'access code', 'one-time',
                            'verify your email', 'verifica tu correo', 'verifica tu email',
                            'finish creating your account', 'terminar de crear',
                            '6-digit', '6 digit', '6-dígitos', '6 digitos'];
            if (frases.some(f => txt.includes(f))) return true;
            return document.querySelectorAll('input[maxlength="1"], input[autocomplete="one-time-code"]').length >= 4;
        }"""))
    except Exception:
        return False


def _invite_es_formulario_registro(page) -> bool:
    """True si la invitación cayó en alta de cuenta (aún no registrada): DOB + Suscríbete."""
    try:
        return bool(page.evaluate("""() => {
            const t = document.body ? document.body.innerText.toLowerCase() : '';
            const btnSus = Array.from(document.querySelectorAll('button')).some(b => {
                const x = (b.textContent || '').toLowerCase();
                return x.includes('suscríbete') || x.includes('suscribete')
                    || x.includes('subscribe') || x.includes('crear cuenta')
                    || x.includes('create account');
            });
            const selects = document.querySelectorAll('select').length;
            const dayish = document.querySelector(
                'select[name*="day" i], input[name*="day" i], select[name*="year" i], input[name*="year" i]'
            );
            const dobHint = /fecha de nacimiento|date of birth|cumpleaños|birthday|nacimiento/i.test(t);
            return btnSus && (selects >= 2 || !!dayish || dobHint);
        }"""))
    except Exception:
        return False


def _invite_rellenar_dob_y_terminos(page) -> bool:
    """Rellena DOB 15/08/1995 + checkbox de términos (mismo patrón que opción 8)."""
    try:
        page.evaluate("""() => {
            const fire = (el) => {
                if (!el) return;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            };
            const selects = Array.from(document.querySelectorAll('select'));
            const daySelect = document.querySelector('select[name*="day" i]') || selects[0];
            const monthSelect = document.querySelector('select[name*="month" i]') || selects[1];
            const yearSelect = document.querySelector('select[name*="year" i]') || selects[2];
            if (daySelect) { daySelect.value = "15"; fire(daySelect); }
            else {
                const dayInput = document.querySelector('input[name*="day" i]');
                if (dayInput) { dayInput.value = "15"; fire(dayInput); }
            }
            if (monthSelect) {
                const opts = Array.from(monthSelect.options || []);
                const targets = ["8", "08", "aug", "ago", "august", "agosto"];
                let matched = false;
                for (const opt of opts) {
                    const val = (opt.value || '').trim().toLowerCase();
                    const txt = (opt.textContent || '').trim().toLowerCase();
                    if (targets.some(t => val === t || txt === t || txt.includes(t))) {
                        monthSelect.value = opt.value; fire(monthSelect); matched = true; break;
                    }
                }
                if (!matched && opts.length > 8) {
                    monthSelect.selectedIndex = opts.length === 13 ? 8 : 7;
                    fire(monthSelect);
                }
            } else {
                const monthInput = document.querySelector('input[name*="month" i]');
                if (monthInput) { monthInput.value = "08"; fire(monthInput); }
            }
            if (yearSelect) { yearSelect.value = "1995"; fire(yearSelect); }
            else {
                const yearInput = document.querySelector('input[name*="year" i]');
                if (yearInput) { yearInput.value = "1995"; fire(yearInput); }
            }
            document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                const parentText = cb.parentElement ? (cb.parentElement.textContent || '') : '';
                if (/t[eé]rminos|terms|privacidad|privacy|acuerdo|agree/i.test(parentText)) {
                    if (!cb.checked) {
                        cb.click();
                        if (!cb.checked && cb.parentElement) cb.parentElement.click();
                    }
                }
            });
        }""")
        return True
    except Exception:
        return False


def _invite_pulsar_suscribete(page) -> bool:
    """Pulsa Suscríbete / Create account en el alta desde invitación."""
    try:
        clicked = bool(page.evaluate("""() => {
            document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                const parentText = cb.parentElement ? (cb.parentElement.textContent || '') : '';
                if (/t[eé]rminos|terms|privacidad|privacy|acuerdo|agree/i.test(parentText)) {
                    if (!cb.checked) {
                        cb.click();
                        if (!cb.checked && cb.parentElement) cb.parentElement.click();
                    }
                }
            });
            const btn = document.querySelector('button[type="submit"]') ||
                Array.from(document.querySelectorAll('button')).find(b => {
                    const t = (b.textContent || '').toLowerCase();
                    return t.includes('suscríbete') || t.includes('suscribete')
                        || t.includes('subscribe') || t.includes('crear cuenta')
                        || t.includes('create account');
                });
            if (!btn) return false;
            btn.disabled = false;
            btn.removeAttribute('disabled');
            btn.removeAttribute('aria-disabled');
            try { btn.click(); } catch (e) {}
            try {
                btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
            } catch (e) {}
            return true;
        }"""))
        if clicked:
            return True
    except Exception:
        pass
    btn = esperar_locator_en_frames(
        page,
        [
            "button:has-text('Suscríbete')", "button:has-text('Subscribe')",
            "button:has-text('Create account')", "button:has-text('Crear cuenta')",
            "button[type='submit']",
        ],
        timeout_s=1.5,
    )
    if not btn:
        return False
    try:
        btn.click(force=True, timeout=1500)
        return True
    except Exception:
        try:
            btn.evaluate("b => { b.disabled = false; b.click(); }")
            return True
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


def _invite_enlace_ya_aceptado_o_caducado(page) -> bool:
    """True si Tidal muestra que el enlace ya se aceptó o caducó (sin login pendiente)."""
    if not page:
        return False
    try:
        if page.is_closed():
            return False
    except Exception:
        return False
    try:
        return bool(page.evaluate("""() => {
            try {
                const txt = (document.body && (document.body.innerText || '') || '').toLowerCase();
                if (!txt) return false;
                // ES: "Este enlace de invitación ya se ha aceptado o ha caducado."
                if (txt.includes('ya se ha aceptado') || txt.includes('ya se aceptó')) return true;
                if (txt.includes('ha caducado') && (txt.includes('invit') || txt.includes('enlace'))) return true;
                if (txt.includes('enlace de invitación') && (txt.includes('aceptado') || txt.includes('caducad'))) return true;
                // EN
                if (txt.includes('already been accepted') || txt.includes('already accepted')) return true;
                if (txt.includes('invitation') && (txt.includes('expired') || txt.includes('has expired'))) return true;
                if (txt.includes('invite link') && (txt.includes('accepted') || txt.includes('expired'))) return true;
                return false;
            } catch (e) {
                return false;
            }
        }"""))
    except Exception:
        try:
            txt = ((page.inner_text("body") or "") if page else "").lower()
        except Exception:
            return False
        return (
            "ya se ha aceptado" in txt
            or "already been accepted" in txt
            or ("ha caducado" in txt and "invit" in txt)
            or ("expired" in txt and "invitation" in txt)
        )


def _invite_detectar_exito(page) -> bool:
    """True si la invitación familiar ya quedó aceptada (evaluación instantánea, sin waits)."""
    if not page:
        return False
    try:
        if page.is_closed():
            return False
    except Exception:
        return False
    # Enlace ya usado / caducado = proceso terminado para ese correo
    if _invite_enlace_ya_aceptado_o_caducado(page):
        return True
    try:
        # Una sola evaluate: evita locator.count()/inner_text con default timeout 35s
        # que colgaban el bucle tras pulsar «Aceptar» (navegación a medias).
        return bool(page.evaluate("""() => {
            try {
                const u = (location.href || '').toLowerCase();
                if (u.includes('/success')) return true;
                if (u.includes('family')
                    && !u.includes('/accept')
                    && !u.includes('/login')
                    && !u.includes('signin')
                    && !u.includes('authorize')
                    && (u.includes('tidal.com') || u.includes('account.'))) {
                    return true;
                }
                const txt = (document.body && (document.body.innerText || '') || '').toLowerCase();
                const frags = [
                    'ya está todo', "you're all set", 'youre all set', 'all set',
                    'welcome to the family', 'te has unido', "you've joined",
                    'joined the family', 'formas parte', "you're in", 'preparado',
                    'bienvenido a la familia', 'has joined',
                ];
                return frags.some(f => txt.includes(f));
            } catch (e) {
                return false;
            }
        }"""))
    except Exception:
        # Durante navegación post-aceptar, evaluate puede fallar: no bloquear
        try:
            u = (page.url or "").lower()
            if "/success" in u:
                return True
            if (
                "family" in u
                and "/accept" not in u
                and "/login" not in u
                and "signin" not in u
                and "authorize" not in u
            ):
                return True
        except Exception:
            pass
        return False


def _invite_queda_boton_aceptar(page) -> bool:
    """True si aún hay CTA de aceptación visible (rápido, sin waits largos)."""
    try:
        return bool(page.evaluate("""() => {
            const re = /aceptar invitaci[oó]n|accept invitation|join family|join the family|unirse a la familia|unirse al plan/i;
            const skip = /cookie|preferenc|onetrust/i;
            for (const el of document.querySelectorAll('button, a, [role="button"]')) {
                const t = (el.innerText || el.textContent || '').trim();
                if (!t || t.length > 80 || skip.test(t) || !re.test(t)) continue;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                const r = el.getBoundingClientRect();
                if (r.width > 2 && r.height > 2) return true;
            }
            return false;
        }"""))
    except Exception:
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
    """Avanza un paso del login/alta en la invitación.

    Cubre también cuentas aún no registradas: DOB + términos + Suscríbete + OTP IMAP
    (match exacto por alias para hasta 5 hermanos del mismo Gmail).

    Devuelve: 'ok' | 'progreso' | 'esperar' | 'sin_pwd'.
    """
    _invite_limpiar_cookies_agresivo(page)

    # 1) ¿Ya hay botón de aceptar? (sesión lista)
    if _invite_pulsar_aceptar(page):
        print(f"    [Invitación] [{correo}] Pulsado botón de aceptación.")
        # Esperar éxito real unos segundos; si el CTA desapareció, dar por aceptada
        for _ in range(12):
            time.sleep(0.35)
            if _invite_detectar_exito(page):
                return "ok"
            if not _invite_queda_boton_aceptar(page):
                # Aceptación aplicada (botón ya no está) aunque la URL aún cargue
                return "ok"
        if _invite_detectar_exito(page) or not _invite_queda_boton_aceptar(page):
            return "ok"
        return "progreso"

    if _invite_detectar_exito(page):
        return "ok"

    email_selectors = [
        'input[type="email"]', 'input[name="email"]',
        'input[autocomplete="email"]', '#email',
    ]
    pwd_selectors = ['input[type="password"]', 'input[name="password"]']

    # 1b) Alta de cuenta nueva (invitación a alias aún no registrado en Tidal)
    if _invite_es_formulario_registro(page):
        estado["registro_nuevo"] = True
        if not estado.get("baseline_id"):
            try:
                estado["baseline_id"] = obtener_max_email_id(correo, "tidal")
            except Exception:
                estado["baseline_id"] = 0
        if not estado.get("dob_ok"):
            print(f"    [Invitación] [{correo}] Cuenta aún no registrada: rellenando fecha "
                  f"de nacimiento y términos...")
            _invite_rellenar_dob_y_terminos(page)
            time.sleep(0.35)
            estado["dob_ok"] = True
            return "progreso"
        if not estado.get("suscribete_pulsado") or estado.get("reintentar_suscribete"):
            print(f"    [Invitación] [{correo}] Pulsando Suscríbete (alta automática)...")
            # Refrescar baseline justo antes del clic para no tomar OTP viejos
            try:
                estado["baseline_id"] = obtener_max_email_id(correo, "tidal")
            except Exception:
                pass
            if _invite_pulsar_suscribete(page):
                estado["suscribete_pulsado"] = True
                estado["reintentar_suscribete"] = False
                estado["codigo_intentado"] = False  # permitir OTP de registro
                time.sleep(0.8)
                return "progreso"
            print(f"    {Color.WARNING}[Invitación] [{correo}] No se pudo pulsar Suscríbete.{Color.ENDC}")
            return "esperar"
        # Tras Suscríbete: si sigue el formulario, reintentar una vez
        if estado.get("suscribete_pulsado") and not _invite_hay_pantalla_codigo(page):
            if estado.get("reintentos_suscribete", 0) < 2:
                estado["reintentos_suscribete"] = estado.get("reintentos_suscribete", 0) + 1
                estado["reintentar_suscribete"] = True
                print(f"    [Invitación] [{correo}] Sigue el formulario de alta; "
                      f"reintento Suscríbete ({estado['reintentos_suscribete']}/2)...")
                return "progreso"

    # 2) Pantalla de código OTP (registro o login)
    if _invite_hay_pantalla_codigo(page) or (
        not estado.get("registro_nuevo") and _invite_eval_modo_contrasena(page, "existe")
    ):
        es_alta = bool(estado.get("registro_nuevo"))
        # En alta nueva NO forzar modo contraseña: no existe aún
        if (not es_alta) and _invite_eval_modo_contrasena(page, "existe"):
            if estado.get("intentos_modo_pwd", 0) < 5:
                estado["intentos_modo_pwd"] = estado.get("intentos_modo_pwd", 0) + 1
                print(f"    [Invitación] [{correo}] Pantalla de código detectada. "
                      f"Cambiando a modo contraseña ({estado['intentos_modo_pwd']}/5)...")
                _invite_clic_modo_contrasena(page)
                time.sleep(1.5)
                return "progreso"

        if not encontrar_locator_en_frames(page, pwd_selectors) or es_alta:
            # Evitar martillar IMAP: cooldown corto tras un intento; luego se puede reintentar
            # (p. ej. código rechazado o Resend).
            if estado.get("codigo_intentado") and (time.time() - float(estado.get("codigo_ts") or 0)) < 10:
                return "esperar"
            estado["codigo_intentado"] = True
            estado["codigo_ts"] = time.time()
            if not estado.get("baseline_id"):
                try:
                    estado["baseline_id"] = obtener_max_email_id(correo, "tidal")
                except Exception:
                    estado["baseline_id"] = 0

            tipo = "registro" if es_alta else "acceso"
            print(f"    [Invitación] [{correo}] Obteniendo código de {tipo} por IMAP "
                  f"(alias exacto)...")
            codigo = None
            for intento in range(1, 11):
                if es_alta:
                    codigo = reclamar_otp_registro_para_alias(
                        correo,
                        after_email_id=estado.get("baseline_id") or 0,
                        max_age_minutes=25,
                        silencioso=(intento > 1),
                    )
                else:
                    codigo = reclamar_otp_login_para_alias(
                        correo,
                        after_email_id=estado.get("baseline_id") or 0,
                        max_age_minutes=20,
                        silencioso=(intento > 1),
                    )
                if codigo:
                    break
                # Resend a mitad de camino si no llega
                if intento in (4, 7):
                    try:
                        btn_resend = esperar_locator_en_frames(
                            page,
                            [
                                "button:has-text('Resend code')", "button:has-text('Resend')",
                                "button:has-text('Reenviar código')", "button:has-text('Reenviar')",
                                "a:has-text('Resend')", "a:has-text('Reenviar')",
                            ],
                            timeout_s=1.5,
                        )
                        if btn_resend:
                            print(f"    [Invitación] [{correo}] Pulsando Resend code...")
                            btn_resend.click(force=True)
                            time.sleep(1.2)
                            try:
                                estado["baseline_id"] = obtener_max_email_id(correo, "tidal")
                            except Exception:
                                pass
                    except Exception:
                        pass
                print(f"    [Invitación] [{correo}] Esperando código IMAP ({intento}/10)...")
                time.sleep(1.8)
            if not codigo:
                print(f"    {Color.FAIL}[Invitación] [{correo}] No llegó el código de {tipo}.{Color.ENDC}")
                # Permitir nuevo ciclo IMAP tras el cooldown
                return "esperar"
            print(f"    [Invitación] [{correo}] Código de {tipo} obtenido: {codigo}. Escribiéndolo...")
            wrote = False
            for _fill in range(1, 4):
                wrote = escribir_codigo_verificacion_inteligente(page, codigo)
                time.sleep(0.6)
                if wrote:
                    break
                time.sleep(0.5)
            time.sleep(0.4)
            if wrote:
                # Tras OTP no reabrir ablink: la sesión ya avanzó; un ERR_TUNNEL
                # en tracking quemaría el progreso.
                estado["otp_escrito"] = True
            _invite_pulsar_continuar_o_login(page)
            time.sleep(1.5)
            if _invite_pulsar_aceptar(page):
                time.sleep(1.5)
            if _invite_detectar_exito(page):
                return "ok"
            if wrote:
                return "progreso"
            return "esperar"

    # 3) Campo de contraseña → rellenar y enviar (solo cuentas ya existentes)
    pwd_inp = encontrar_locator_en_frames(page, pwd_selectors)
    if pwd_inp and not estado.get("registro_nuevo"):
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
            try:
                val = (email_inp.input_value() or "").strip()
            except Exception:
                val = ""
            if not val:
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
            if any(x in txt for x in ("inicia sesión", "iniciar sesión", "log in", "sign in")):
                if _invite_pulsar_continuar_o_login(page):
                    time.sleep(2.0)
                    return "progreso"
    except Exception:
        pass

    return "esperar"


def abrir_enlace_familia_con_autocierre(
    url: str,
    correo: str,
    proxy_pe: dict | None = None,
    proxy_ng: dict | None = None,
    headless: bool = False,
) -> bool:
    """Abre el enlace de invitación, completa login/alta + aceptación y cierra Chrome al éxito.

    - Cuenta ya existente (hay pwd o login): proxy PE.
    - Cuenta aún no registrada (alta DOB/Suscríbete): proxy NG (Nigeria), obligatorio.
    Devuelve True si la invitación quedó aceptada.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"{Color.WARNING}[Navegador]{Color.ENDC} Playwright no está instalado. Usando fallback...")
        abrir_enlace_en_perfil_chrome(url, correo)
        return False

    # Evitar ablink en Chrome+proxy NG (ERR_TUNNEL crónico). Resolver a accept directo.
    url_orig = (url or "").strip()
    url = _resolver_ablink_a_invitacion(url_orig)
    if url != url_orig and _es_url_invitacion_directa(url):
        print(f"    {Color.CYAN}[Invitación] [{correo}] Usando URL directa (sin ablink).{Color.ENDC}")

    pwd_cuenta = buscar_contrasena_cuenta(correo)
    # Sin contraseña → se asume alta nueva → Nigeria desde el inicio.
    # Con contraseña → Perú (login). Si luego aparece formulario de alta, se cambia a NG.
    usar_ng_inicial = not bool(pwd_cuenta)
    if usar_ng_inicial:
        if not proxy_ng or not proxy_ng.get("server"):
            try:
                proxy_ng = GLOBAL_NG_PROXY_POOL.obtener_proxy_unico(espera_s=45.0)
            except Exception:
                proxy_ng = None
        if not proxy_ng or not proxy_ng.get("server"):
            print(f"    {Color.FAIL}[Navegador] [{correo}] Sin proxy de Nigeria disponible. "
                  f"El alta de cuentas nuevas exige NG. Se omite.{Color.ENDC}")
            return False
        current_proxy = proxy_ng
        proxy_tipo = "NG"
        # Devolver PE si se reservó de más
        if proxy_pe and proxy_pe.get("server"):
            try:
                GLOBAL_PE_PROXY_POOL.liberar_proxy(proxy_pe.get("server"))
            except Exception:
                pass
            proxy_pe = None
    else:
        if not proxy_pe or not proxy_pe.get("server"):
            print(f"    {Color.FAIL}[Navegador] [{correo}] Sin proxy de Perú disponible. Se omite la "
                  f"invitación antes de exponer tu IP real.{Color.ENDC}")
            return False
        current_proxy = proxy_pe
        proxy_tipo = "PE"

    email_safe = re.sub(r'[^a-zA-Z0-9]', '_', correo)
    profile_dir = Path(tempfile.gettempdir()) / f"tidal_chrome_profile_{email_safe}_{random.randint(1000, 9999)}"
    reparar_perfil_corrupto(profile_dir)

    print(f"    {Color.CYAN}[Navegador]{Color.ENDC} Iniciando automatización familiar ({correo}) "
          f"con proxy {proxy_tipo}"
          f"{' (alta/registro Nigeria)' if proxy_tipo == 'NG' else ' (login/aceptar Perú)'}"
          f"{' [headless]' if (headless or headless_forzado_por_entorno()) else ''}...")

    with sync_playwright() as p:
        base_launch_kwargs = kwargs_launch_persistent(profile_dir, headless=headless)

        def abrir_contexto(proxy, prof_dir, tipo: str):
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
            etiqueta = "NIGERIA" if tipo == "NG" else "PERÚ"
            print(f"    [Proxy {tipo}] [{correo}] Conectando mediante proxy de {etiqueta}: {p_serv}")
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
            ctx.add_init_script(_INVITE_COOKIE_KILLER_INIT)
            return ctx

        context = None
        page = None
        nav_inv_ok = False
        motivo_fallo = "desconocido"
        _max_intentos_inv = 5

        def _cerrar_contexto():
            """Cierre rápido en EL MISMO hilo (Playwright sync no admite close desde otro thread).

            Cerrar pestañas primero hace que la ventana desaparezca al momento; luego
            context.close() suele ser breve. Nunca usar threading aquí → greenlet.error.
            """
            nonlocal context, page
            pages = []
            try:
                if context:
                    pages = list(context.pages)
            except Exception:
                pass
            for pg in pages:
                try:
                    if not pg.is_closed():
                        pg.close()
                except Exception:
                    pass
            page = None

            ctx = context
            context = None
            if not ctx:
                return
            try:
                ctx.close()
            except Exception:
                # TargetClosedError / ya cerrado tras pg.close(): ignorar
                pass

        def _liberar_proxy_actual():
            srv = (current_proxy or {}).get("server")
            if not srv:
                return
            try:
                if proxy_tipo == "NG":
                    GLOBAL_NG_PROXY_POOL.liberar_proxy(srv)
                else:
                    GLOBAL_PE_PROXY_POOL.liberar_proxy(srv)
            except Exception:
                pass

        def _rotar_proxy_y_perfil(razon: str) -> bool:
            """Rota al pool del país actual y descarta el perfil. False si no hay IP."""
            nonlocal current_proxy, profile_dir
            pais = "Nigeria" if proxy_tipo == "NG" else "Perú"
            print(f"    {Color.WARNING}[Invitación] [{correo}] {razon}. "
                  f"Rotando a un proxy de {pais} limpio...{Color.ENDC}")
            pool = GLOBAL_NG_PROXY_POOL if proxy_tipo == "NG" else GLOBAL_PE_PROXY_POOL
            nuevo_proxy = pool.rotar_y_marcar_bloqueado((current_proxy or {}).get("server"))
            if not nuevo_proxy or not nuevo_proxy.get("server"):
                print(f"    {Color.FAIL}[Invitación] [{correo}] No quedan proxies de {pais} limpios.{Color.ENDC}")
                return False
            current_proxy = nuevo_proxy
            _cerrar_contexto()
            perfil_quemado = profile_dir
            profile_dir = Path(tempfile.gettempdir()) / (
                f"tidal_chrome_profile_{email_safe}_{random.randint(1000, 9999)}"
            )
            reparar_perfil_corrupto(profile_dir)
            try:
                shutil.rmtree(perfil_quemado, ignore_errors=True)
            except Exception:
                pass
            return True

        def _cambiar_a_nigeria_para_alta(razon: str) -> bool:
            """Cambia de PE → NG cuando aparece el formulario de registro."""
            nonlocal current_proxy, proxy_tipo, profile_dir, proxy_ng
            if proxy_tipo == "NG":
                return True
            print(f"    {Color.CYAN}[Invitación] [{correo}] {razon}. "
                  f"Cambiando a proxy de NIGERIA para el alta...{Color.ENDC}")
            pe_srv = (current_proxy or {}).get("server")
            _cerrar_contexto()
            if pe_srv:
                try:
                    GLOBAL_PE_PROXY_POOL.liberar_proxy(pe_srv)
                except Exception:
                    pass
            nuevo = proxy_ng if (proxy_ng and proxy_ng.get("server")) else None
            if not nuevo:
                try:
                    nuevo = GLOBAL_NG_PROXY_POOL.obtener_proxy_unico(espera_s=45.0)
                except Exception:
                    nuevo = None
            if not nuevo or not nuevo.get("server"):
                print(f"    {Color.FAIL}[Invitación] [{correo}] Sin proxy NG para registrar la cuenta.{Color.ENDC}")
                return False
            proxy_ng = nuevo
            current_proxy = nuevo
            proxy_tipo = "NG"
            perfil_quemado = profile_dir
            profile_dir = Path(tempfile.gettempdir()) / (
                f"tidal_chrome_profile_{email_safe}_{random.randint(1000, 9999)}"
            )
            reparar_perfil_corrupto(profile_dir)
            try:
                shutil.rmtree(perfil_quemado, ignore_errors=True)
            except Exception:
                pass
            return True

        def _reabrir_invitacion_con_proxy_actual(max_intentos: int = 4) -> bool:
            """Reabre el enlace con el proxy actual; ante ERR_TUNNEL/antibot rota y reintenta.

            Usa URL directa (sin ablink) siempre que se pueda resolver.
            """
            nonlocal context, page, url
            # Re-resolver por si aún es ablink
            if "ablink." in (url or "").lower():
                resuelto = _resolver_ablink_a_invitacion(url)
                if resuelto and resuelto != url:
                    url = resuelto

            ultimo_err = ""
            for intento in range(1, max_intentos + 1):
                try:
                    if context is None:
                        context = abrir_contexto(current_proxy, profile_dir, proxy_tipo)
                        page = context.pages[0] if context.pages else context.new_page()

                    print(f"    [Invitación] [{correo}] Calentando reputación "
                          f"(proxy {proxy_tipo}, intento reopen {intento}/{max_intentos})...")
                    navegar_tidal_tolerante(page, "https://tidal.com/pricing", timeout_ms=45000)
                    time.sleep(random.uniform(1.2, 2.2))
                    aceptar_cookies_con_espera(page)
                    _invite_limpiar_cookies_agresivo(page)
                    time.sleep(0.4)

                    try:
                        navegar_tidal_tolerante(
                            page, "https://account.tidal.com/",
                            referer="https://tidal.com/pricing",
                            timeout_ms=35000,
                        )
                        time.sleep(0.6)
                    except Exception:
                        pass

                    destino = url
                    # Si sigue siendo ablink, intentar resolver otra vez antes del goto
                    if "ablink." in (destino or "").lower():
                        destino2 = _resolver_ablink_a_invitacion(destino)
                        if _es_url_invitacion_directa(destino2):
                            destino = destino2
                            url = destino2

                    print(f"    [Invitación] [{correo}] Reabriendo enlace "
                          f"{'(directo) ' if _es_url_invitacion_directa(destino) else '(tracking) '}"
                          f"con proxy {proxy_tipo}...")
                    try:
                        navegar_tidal_tolerante(
                            page, destino,
                            referer="https://tidal.com/pricing",
                            timeout_ms=60000,
                        )
                    except Exception as e_goto:
                        try:
                            u_now = (page.url or "").lower()
                        except Exception:
                            u_now = ""
                        if url_es_flujo_invitacion_familiar(u_now) and not detectar_pantalla_antirobot(page):
                            print(f"    [Invitación] [{correo}] goto con error ({e_goto}) pero "
                                  f"pestaña ya en flujo familiar: {u_now[:80]}")
                        elif "ablink." in (destino or "").lower():
                            # Último intento: resolver ablink y navegar solo a la URL final
                            destino3 = _resolver_ablink_a_invitacion(destino)
                            if _es_url_invitacion_directa(destino3):
                                print(f"    [Invitación] [{correo}] Reintento con URL resuelta "
                                      f"(sin ablink)...")
                                url = destino3
                                navegar_tidal_tolerante(
                                    page, destino3,
                                    referer="https://tidal.com/pricing",
                                    timeout_ms=60000,
                                )
                            else:
                                raise
                        else:
                            raise

                    time.sleep(1.2)
                    _invite_limpiar_cookies_agresivo(page)

                    try:
                        u_fin = (page.url or "").lower()
                    except Exception:
                        u_fin = ""
                    if detectar_pantalla_antirobot(page):
                        raise RuntimeError("Antibot al reabrir la invitación")
                    if url_es_pagina_marketing(u_fin) or "chrome-error" in u_fin:
                        raise RuntimeError(
                            f"Reapertura quedó en {(u_fin or '?')[:90]} (no es accept/login)"
                        )
                    if not url_es_flujo_invitacion_familiar(u_fin):
                        if not url_es_login_o_cuenta(u_fin):
                            raise RuntimeError(
                                f"Tras reopen URL inesperada: {(u_fin or '?')[:90]}"
                            )
                    return True

                except Exception as e_reab:
                    ultimo_err = str(e_reab)
                    print(f"    {Color.FAIL}[Invitación] [{correo}] Falló reapertura con "
                          f"{proxy_tipo} ({intento}/{max_intentos}): {e_reab}{Color.ENDC}")
                    try:
                        _cerrar_contexto()
                    except Exception:
                        pass
                    if intento >= max_intentos:
                        break
                    razon = "ERR_TUNNEL/proxy al reabrir"
                    if "antibot" in ultimo_err.lower() or "robot" in ultimo_err.lower():
                        razon = "Antibot al reabrir"
                    elif not es_error_proxy_o_red(e_reab) and "tunnel" not in ultimo_err.lower():
                        razon = f"Reapertura fallida: {ultimo_err[:60]}"
                    if not _rotar_proxy_y_perfil(razon):
                        break
                    time.sleep(random.uniform(1.0, 2.0))

            print(f"    {Color.FAIL}[Invitación] [{correo}] No se pudo reabrir tras "
                  f"{max_intentos} intentos. Último error: {ultimo_err[:120]}{Color.ENDC}")
            return False

        for intento_inv in range(1, _max_intentos_inv + 1):
            if context is None:
                context = abrir_contexto(current_proxy, profile_dir, proxy_tipo)
                page = context.pages[0] if context.pages else context.new_page()

            try:
                print(f"    [Invitación] [{correo}] Calentando reputación en tidal.com/pricing "
                      f"(intento {intento_inv}/{_max_intentos_inv}, proxy {proxy_tipo})...")
                navegar_tidal_tolerante(page, "https://tidal.com/pricing", timeout_ms=45000)
                time.sleep(random.uniform(2.0, 3.5))
                aceptar_cookies_con_espera(page)
                _invite_limpiar_cookies_agresivo(page)
                time.sleep(random.uniform(0.5, 1.0))

                # Calentar account antes del ablink (reduce ERR_TUNNEL en tracking links)
                try:
                    navegar_tidal_tolerante(
                        page, "https://account.tidal.com/",
                        referer="https://tidal.com/pricing",
                        timeout_ms=35000,
                    )
                    time.sleep(0.5)
                except Exception:
                    pass

                # Re-resolver por si el enlace IMAP quedó en ablink
                if "ablink." in (url or "").lower():
                    resuelto0 = _resolver_ablink_a_invitacion(url)
                    if _es_url_invitacion_directa(resuelto0):
                        url = resuelto0

                destino_inv = url
                print(f"    [Invitación] [{correo}] Cargando enlace "
                      f"{'(directo) ' if _es_url_invitacion_directa(destino_inv) else '(tracking) '}"
                      f"con referer orgánico...")
                try:
                    navegar_tidal_tolerante(
                        page, destino_inv,
                        referer="https://tidal.com/pricing",
                        timeout_ms=60000,
                    )
                except Exception as e_goto:
                    try:
                        u_now = (page.url or "").lower()
                    except Exception:
                        u_now = ""
                    if url_es_flujo_invitacion_familiar(u_now) and not detectar_pantalla_antirobot(page):
                        print(f"    [Invitación] [{correo}] goto con error pero ya en flujo: "
                              f"{u_now[:80]}")
                    elif "ablink." in (destino_inv or "").lower():
                        destino_alt = _resolver_ablink_a_invitacion(destino_inv)
                        if _es_url_invitacion_directa(destino_alt):
                            print(f"    [Invitación] [{correo}] ablink falló por túnel; "
                                  f"abriendo URL directa...")
                            url = destino_alt
                            navegar_tidal_tolerante(
                                page, destino_alt,
                                referer="https://tidal.com/pricing",
                                timeout_ms=60000,
                            )
                        else:
                            raise
                    else:
                        raise
                time.sleep(2.0)
                _invite_limpiar_cookies_agresivo(page)
                try:
                    url_post = (page.url or "").lower()
                except Exception:
                    url_post = ""
                if "chrome-error" in url_post or "chromewebdata" in url_post:
                    # Último recurso: resolver ablink fuera de Chrome y reintentar directo
                    alt = _resolver_ablink_a_invitacion(url_orig if "ablink." in (url_orig or "").lower() else url)
                    if _es_url_invitacion_directa(alt):
                        url = alt
                        navegar_tidal_tolerante(
                            page, alt,
                            referer="https://tidal.com/pricing",
                            timeout_ms=60000,
                        )
                        time.sleep(1.0)
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
            _liberar_proxy_actual()
            return False

        aceptar_cookies_con_espera(page)
        _invite_limpiar_cookies_agresivo(page)

        # Enlace ya aceptado/caducado → OK inmediato, cerrar Chrome sin login/OTP
        if _invite_enlace_ya_aceptado_o_caducado(page):
            print(f"    {Color.GREEN}[OK] [{correo}] Invitación ya aceptada o caducada. "
                  f"Cerrando Chrome...{Color.ENDC}")
            _cerrar_contexto()
            try:
                prof = profile_dir

                def _rm_async_ya(p_dir):
                    time.sleep(0.5)
                    try:
                        shutil.rmtree(p_dir, ignore_errors=True)
                    except Exception:
                        pass

                threading.Thread(target=_rm_async_ya, args=(Path(prof),), daemon=True).start()
            except Exception:
                pass
            _liberar_proxy_actual()
            return True

        if pwd_cuenta:
            print(f"    [Invitación] [{correo}] Contraseña cargada desde sesiones_imap_cuentas.txt.")
        else:
            print(f"    {Color.CYAN}[Invitación] [{correo}] Sin contraseña en archivo: "
                  f"alta automática con proxy NG + OTP IMAP.{Color.ENDC}")

        # Si ya estamos en el formulario de alta y aún en PE → forzar NG antes de Suscríbete
        if proxy_tipo == "PE" and _invite_es_formulario_registro(page):
            if not _cambiar_a_nigeria_para_alta("Formulario de registro detectado tras abrir el enlace"):
                _cerrar_contexto()
                return False
            if not _reabrir_invitacion_con_proxy_actual():
                _cerrar_contexto()
                _liberar_proxy_actual()
                return False
            # Tras PE→NG el antibot puede aparecer ya en accept-invite
            if detectar_pantalla_antirobot(page):
                if not _rotar_proxy_y_perfil("Antirobot tras cambiar a Nigeria"):
                    _cerrar_contexto()
                    _liberar_proxy_actual()
                    return False
                if not _reabrir_invitacion_con_proxy_actual():
                    _cerrar_contexto()
                    _liberar_proxy_actual()
                    return False

        success_detected = False
        reabiertos_enlace = 0
        rotaciones_antibot_loop = 0
        max_rotaciones_antibot_loop = 5
        estado_login = {}
        print(f"    [Invitación] [{correo}] Completando aceptación automática "
              f"(login PE o alta NG, hasta 5 minutos)...")
        for check_sec in range(300):
            try:
                if page is None:
                    break
                try:
                    url_actual = (page.url or "").lower()
                except Exception:
                    url_actual = ""

                # chrome-error / ERR_TUNNEL mid-flujo: ir a URL directa (nunca insistir en ablink)
                if "chrome-error" in url_actual or "chromewebdata" in url_actual:
                    if reabiertos_enlace >= 4:
                        print(f"    {Color.FAIL}[Invitación] [{correo}] chrome-error persistente. "
                              f"Se omite.{Color.ENDC}")
                        break
                    reabiertos_enlace += 1
                    print(f"    {Color.WARNING}[Invitación] [{correo}] chrome-error/túnel. "
                          f"Recuperando con URL directa ({reabiertos_enlace}/4)...{Color.ENDC}")
                    if "ablink." in (url or "").lower() or "ablink." in (url_orig or "").lower():
                        res = _resolver_ablink_a_invitacion(
                            url if "ablink." in (url or "").lower() else url_orig
                        )
                        if _es_url_invitacion_directa(res):
                            url = res
                    # Si ya se escribió OTP, rotar + reopen completo pierde el avance; intentar
                    # goto directo primero; si falla, rotar y reopen con URL ya resuelta.
                    try:
                        if _es_url_invitacion_directa(url):
                            navegar_tidal_tolerante(
                                page, url,
                                referer="https://tidal.com/pricing",
                                timeout_ms=45000,
                            )
                            time.sleep(1.0)
                            continue
                    except Exception:
                        pass
                    if not _rotar_proxy_y_perfil("ERR_TUNNEL / chrome-error en bucle"):
                        break
                    if not _reabrir_invitacion_con_proxy_actual():
                        break
                    if estado_login.get("otp_escrito"):
                        # Mantener flag OTP: la cuenta ya existe; buscar accept/login
                        estado_login = {
                            k: v for k, v in estado_login.items()
                            if k in ("ng_switch_hecho", "registro_nuevo", "otp_escrito",
                                     "dob_ok", "suscribete_pulsado", "baseline_id")
                        }
                    else:
                        estado_login = {
                            k: v for k, v in estado_login.items()
                            if k in ("ng_switch_hecho", "registro_nuevo")
                        }
                    time.sleep(0.8)
                    continue

                # Antibot a mitad de flujo (p. ej. tras Suscríbete / accept-invite): ROTAR IP
                if check_sec % 2 == 0 or check_sec < 3:
                    try:
                        hay_antibot = detectar_pantalla_antirobot(page)
                    except Exception:
                        hay_antibot = False
                    if hay_antibot:
                        if rotaciones_antibot_loop >= max_rotaciones_antibot_loop:
                            print(f"    {Color.FAIL}[Invitación] [{correo}] Antibot persistente tras "
                                  f"{max_rotaciones_antibot_loop} rotaciones. Se omite.{Color.ENDC}")
                            break
                        rotaciones_antibot_loop += 1
                        print(f"    {Color.WARNING}[Invitación] [{correo}] Antibot en accept/login "
                              f"(rotación {rotaciones_antibot_loop}/{max_rotaciones_antibot_loop})..."
                              f"{Color.ENDC}")
                        # Intento corto de slider antes de quemar IP
                        try:
                            if resolver_slider_captcha_playwright(page):
                                time.sleep(2.0)
                                if not detectar_pantalla_antirobot(page):
                                    print(f"    [Invitación] [{correo}] Slider resuelto; se continúa.")
                                    continue
                        except Exception:
                            pass
                        if not _rotar_proxy_y_perfil(
                            f"Antibot en bucle de aceptación ({proxy_tipo})"
                        ):
                            break
                        if not _reabrir_invitacion_con_proxy_actual():
                            break
                        # Tras rotar, reiniciar progreso de formulario (misma cuenta, IP nueva)
                        # Conservar otp_escrito: tras OTP la cuenta ya existe en Tidal
                        keep = ("ng_switch_hecho", "registro_nuevo")
                        if estado_login.get("otp_escrito"):
                            keep = keep + ("otp_escrito", "dob_ok", "suscribete_pulsado", "baseline_id")
                        estado_login = {k: v for k, v in estado_login.items() if k in keep}
                        time.sleep(0.8)
                        continue

                if url_es_pagina_marketing(url_actual):
                    if reabiertos_enlace < 4:
                        reabiertos_enlace += 1
                        print(f"    [Invitación] [{correo}] Pestaña en marketing/pricing. "
                              f"Reabriendo enlace ({reabiertos_enlace}/4)...")
                        try:
                            if "ablink." in (url or "").lower():
                                res_m = _resolver_ablink_a_invitacion(url)
                                if _es_url_invitacion_directa(res_m):
                                    url = res_m
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

                if check_sec % 2 == 0:
                    try:
                        _invite_limpiar_cookies_agresivo(page)
                    except Exception:
                        pass

                # Alta detectada a mitad de login PE → cambiar a NG y reabrir
                if (
                    proxy_tipo == "PE"
                    and not estado_login.get("ng_switch_hecho")
                    and _invite_es_formulario_registro(page)
                ):
                    estado_login["ng_switch_hecho"] = True
                    if not _cambiar_a_nigeria_para_alta("Cuenta aún no registrada (formulario de alta)"):
                        break
                    if not _reabrir_invitacion_con_proxy_actual():
                        break
                    if detectar_pantalla_antirobot(page):
                        if rotaciones_antibot_loop >= max_rotaciones_antibot_loop:
                            break
                        rotaciones_antibot_loop += 1
                        if not _rotar_proxy_y_perfil("Antibot tras PE→NG"):
                            break
                        if not _reabrir_invitacion_con_proxy_actual():
                            break
                    # Reiniciar progreso de formulario (correo/continuar puede hacer falta otra vez)
                    estado_login = {"ng_switch_hecho": True, "registro_nuevo": True}
                    time.sleep(0.5)
                    continue

                if _invite_enlace_ya_aceptado_o_caducado(page):
                    print(f"    {Color.GREEN}[OK] [{correo}] Invitación ya aceptada o caducada.{Color.ENDC}")
                    success_detected = True
                    break

                if _invite_detectar_exito(page):
                    success_detected = True
                    break

                resultado = _invite_avanzar_login(page, correo, pwd_cuenta, estado_login)
                if resultado == "ok":
                    success_detected = True
                    break
                if resultado == "sin_pwd":
                    time.sleep(0.6)
                    continue
                if resultado == "progreso":
                    time.sleep(0.4)
                    continue
            except Exception as e_loop:
                if check_sec % 30 == 0:
                    print(f"    [Invitación] [{correo}] [WARN] Bucle: {e_loop}")
            time.sleep(0.55)

        if success_detected:
            print(f"    {Color.GREEN}[OK] ¡Invitación familiar aceptada correctamente para {correo}! "
                  f"Cerrando ventana de Chrome...{Color.ENDC}")
            _cerrar_contexto()
            # Borrar perfil en background (no bloquear el hilo de la oleada)
            try:
                prof = profile_dir

                def _rm_async(p_dir):
                    time.sleep(0.8)
                    try:
                        shutil.rmtree(p_dir, ignore_errors=True)
                    except Exception:
                        pass

                threading.Thread(target=_rm_async, args=(Path(prof),), daemon=True).start()
            except Exception:
                pass
        else:
            print(f"    {Color.WARNING}[WARN] No se completó la aceptación automática para {correo} "
                  f"en el tiempo límite. La ventana permanece abierta para revisión.{Color.ENDC}")

        _liberar_proxy_actual()
        return bool(success_detected)


def _norm_correo_simple(c: str) -> str:
    return (c or "").strip().lower()


def _parece_url_invitacion_familiar(url: str) -> bool:
    u = (url or "").strip()
    if not u.lower().startswith("http"):
        return False
    ul = u.lower()
    if "resetpass" in ul or "reset-password" in ul:
        return False
    return (
        "ablink." in ul
        or "tidal.com" in ul
        or "/family/" in ul
        or "/accept" in ul
        or "invite" in ul
    )


def parsear_pares_enlace_invitacion(
    texto: str,
    correos_preferidos: list[str] | None = None,
) -> dict[str, str]:
    """Parsea texto al estilo linksextraidos.txt o líneas sueltas.

    Formatos aceptados:
      N. https://...
         correo: user@gmail.com
      user@gmail.com|https://...
      user@gmail.com\\thttps://...
      https://...   (se empareja por orden con correos_preferidos)
    """
    preferidos = [c.strip() for c in (correos_preferidos or []) if (c or "").strip()]
    pref_por_norm = {_norm_correo_simple(c): c for c in preferidos}
    resultado: dict[str, str] = {}  # correo_norm -> url
    orden_correos: list[str] = []
    urls_sin_correo: list[str] = []
    pendiente_url: str | None = None

    def _registrar(correo: str, url: str) -> None:
        nonlocal pendiente_url
        correo = (correo or "").strip()
        url = (url or "").strip()
        if not correo or not _parece_url_invitacion_familiar(url):
            return
        k = _norm_correo_simple(correo)
        # Preferir la forma del menú activo si coincide
        correo_out = pref_por_norm.get(k, correo)
        if k not in resultado:
            orden_correos.append(correo_out)
        resultado[k] = url
        pendiente_url = None

    for raw in (texto or "").splitlines():
        line = (raw or "").strip()
        if not line or line.startswith("=") or line.upper().startswith("ENLACES DE"):
            continue
        if line.lower().startswith("fecha:") or line.lower().startswith("total:"):
            continue

        # correo: email
        m_correo = re.match(r"^(?:correo|email|cuenta)\s*:\s*(.+)$", line, flags=re.I)
        if m_correo:
            correo = m_correo.group(1).strip()
            if pendiente_url and "@" in correo:
                _registrar(correo, pendiente_url)
            continue

        # email|url  o  email<TAB>url  o  email url
        m_pair = re.match(
            r"^([^\s|;,]{1,}@[^\s|;,]+\.[^\s|;,]+)\s*[|\t]\s*(https?://\S+)\s*$",
            line,
            flags=re.I,
        )
        if not m_pair:
            m_pair = re.match(
                r"^([^\s]{1,}@[^\s]+\.[^\s]+)\s+(https?://\S+)\s*$",
                line,
                flags=re.I,
            )
        if m_pair:
            _registrar(m_pair.group(1), m_pair.group(2))
            continue

        # "N. https://..." o solo URL
        m_num = re.match(r"^\d+[\.\)]\s*(https?://\S+)\s*$", line, flags=re.I)
        if m_num:
            pendiente_url = m_num.group(1).strip()
            urls_sin_correo.append(pendiente_url)
            continue
        if line.lower().startswith("http"):
            # puede traer basura al final
            url_cand = line.split()[0].rstrip("),.;'\"")
            if _parece_url_invitacion_familiar(url_cand):
                pendiente_url = url_cand
                urls_sin_correo.append(url_cand)
            continue

    # Emparejar URLs huérfanas con correos del menú (por orden, sin sobrescribir)
    usados = set(resultado.keys())
    cola_pref = [c for c in preferidos if _norm_correo_simple(c) not in usados]
    # urls_sin_correo puede incluir las que luego tuvieron correo:; quitar las ya usadas
    urls_ya = set(resultado.values())
    huérfanas = [u for u in urls_sin_correo if u not in urls_ya]
    # Si todas las urls_sin_correo tuvieron correo, huérfanas estará vacío — bien
    # Si algunas tuvieron correo vía "correo:", estánaron de pendiente pero siguen en urls_sin_correo
    # Mejor: solo emparejar las que quedaron en pendiente al final + las sin pair
    # Simplificación: si hay preferidos sin enlace, asignar URLs que no están en resultado.values()
    for url in huérfanas:
        if not cola_pref:
            break
        if url in resultado.values():
            continue
        correo = cola_pref.pop(0)
        _registrar(correo, url)

    # Salida con correo canónico del menú cuando exista
    out: dict[str, str] = {}
    for k, url in resultado.items():
        out[pref_por_norm.get(k, k)] = url
    return out


def leer_enlaces_desde_linksextraidos(
    correos: list[str] | None = None,
    path: Path | None = None,
) -> dict[str, str]:
    """Lee linksextraidos.txt; si se pasan correos, solo los que estén en esa lista."""
    path = path or LINKS_EXTRAIDOS_PATH
    if not path.exists():
        return {}
    try:
        texto = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"    {Color.FAIL}[Enlaces] No se pudo leer {path.name}: {e}{Color.ENDC}")
        return {}
    todos = parsear_pares_enlace_invitacion(texto, correos_preferidos=correos)
    if not correos:
        return todos
    want = {_norm_correo_simple(c) for c in correos}
    return {c: u for c, u in todos.items() if _norm_correo_simple(c) in want}


def guardar_enlaces_en_linksextraidos(
    enlaces_map: dict[str, str],
    *,
    merge: bool = True,
    path: Path | None = None,
) -> Path:
    """Anota/actualiza linksextraidos.txt en el formato estándar del proyecto."""
    path = path or LINKS_EXTRAIDOS_PATH
    merged: dict[str, tuple[str, str]] = {}
    orden: list[str] = []

    if merge and path.exists():
        try:
            prev = parsear_pares_enlace_invitacion(
                path.read_text(encoding="utf-8", errors="replace")
            )
            for c, u in prev.items():
                k = _norm_correo_simple(c)
                if k not in merged:
                    orden.append(c)
                merged[k] = (c, u)
        except Exception:
            pass

    for c, u in (enlaces_map or {}).items():
        c = (c or "").strip()
        u = (u or "").strip()
        if not c or not u:
            continue
        k = _norm_correo_simple(c)
        if k not in merged:
            orden.append(c)
            merged[k] = (c, u)
        else:
            # Actualizar URL; conservar etiqueta de correo previa si es la misma cuenta
            old_c, _ = merged[k]
            merged[k] = (old_c if _norm_correo_simple(old_c) == k else c, u)

    # Orden: primero los del merge histórico, luego nuevos
    lineas = [
        "=" * 60,
        "ENLACES DE INVITACIÓN TIDAL FAMILY",
        f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Total: {len(merged)}",
        "=" * 60,
        "",
    ]
    for i, c_key in enumerate(orden, 1):
        k = _norm_correo_simple(c_key)
        if k not in merged:
            continue
        correo, url = merged[k]
        lineas.append(f"{i}. {url}")
        lineas.append(f"   correo: {correo}")
        lineas.append("")

    path.write_text("\n".join(lineas).rstrip() + "\n", encoding="utf-8")
    return path


def pedir_fuente_enlaces_opcion4(correos: list[str]) -> tuple[dict[str, str] | None, str]:
    """Pregunta fuente opcional de enlaces para la opción 4.

    Returns:
      (enlaces_map, origen) — enlaces_map es None si hay que buscar por IMAP.
      origen: 'imap' | 'pegar' | 'archivo'
    """
    print(f"\n{Color.CYAN}Fuente de enlaces de invitación:{Color.ENDC}")
    print("  [Enter] Buscar por IMAP (como siempre)")
    print("  p       Pegar enlaces manualmente ahora")
    print(f"  a       Usar {LINKS_EXTRAIDOS_PATH.name} (correos activos)")
    elec = input(f"{Color.BOLD}Elige [Enter/p/a]:{Color.ENDC} ").strip().lower()

    if elec in ("a", "archivo", "f", "file"):
        mapa = leer_enlaces_desde_linksextraidos(correos)
        if not mapa:
            print(f"    {Color.WARNING}[Enlaces] No hay pares correo↔link en "
                  f"{LINKS_EXTRAIDOS_PATH.name} para los correos activos.{Color.ENDC}")
        else:
            print(f"    {Color.GREEN}[Enlaces] {len(mapa)} enlace(s) cargados desde "
                  f"{LINKS_EXTRAIDOS_PATH.name}.{Color.ENDC}")
        return mapa, "archivo"

    if elec in ("p", "pegar", "paste", "m", "manual"):
        print(f"\n{Color.CYAN}Pega los enlaces. Formatos:{Color.ENDC}")
        print("  correo|https://ablink...   ó   https://... + línea 'correo: user@...'")
        print("  (mismo formato que linksextraidos.txt)")
        print("  Si solo pegas URLs, se emparejan en orden con los correos activos.")
        print(f"{Color.CYAN}Línea vacía para terminar.{Color.ENDC}")
        buf: list[str] = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if not (line or "").strip():
                if buf:
                    break
                # primera vacía: salir sin datos
                break
            buf.append(line)
        mapa = parsear_pares_enlace_invitacion("\n".join(buf), correos_preferidos=correos)
        if not mapa:
            print(f"    {Color.WARNING}[Enlaces] No se pudo parsear ningún par correo↔link.{Color.ENDC}")
        else:
            print(f"    {Color.GREEN}[Enlaces] {len(mapa)} enlace(s) pegados.{Color.ENDC}")
            try:
                path = guardar_enlaces_en_linksextraidos(mapa, merge=True)
                print(f"    {Color.CYAN}[Enlaces] Anotados en {path}{Color.ENDC}")
            except Exception as e:
                print(f"    {Color.WARNING}[Enlaces] No se pudo anotar en "
                      f"{LINKS_EXTRAIDOS_PATH.name}: {e}{Color.ENDC}")
        return mapa, "pegar"

    return None, "imap"


def _imprimir_resumen_opcion4(
    correos_menu: list[str],
    ok_list: list[str],
    fail_list: list[str],
    sin_enlace: list[str] | None = None,
) -> None:
    """Resumen visual de la opción 4: aceptadas vs pendientes/fallidas."""
    def _norm(c: str) -> str:
        return (c or "").strip().lower()

    vistos_ok: set[str] = set()
    ok_unique: list[str] = []
    for c in ok_list:
        k = _norm(c)
        if k and k not in vistos_ok:
            vistos_ok.add(k)
            ok_unique.append(c.strip())

    vistos_fail: set[str] = set()
    fail_unique: list[str] = []
    for c in fail_list:
        k = _norm(c)
        if k and k not in vistos_ok and k not in vistos_fail:
            vistos_fail.add(k)
            fail_unique.append(c.strip())

    # Cualquier correo del menú que no esté en OK ni FAIL
    for c in correos_menu or []:
        k = _norm(c)
        if k and k not in vistos_ok and k not in vistos_fail:
            fail_unique.append(c.strip())
            vistos_fail.add(k)

    sin_enlace = sin_enlace or []
    sin_enlace_set = {_norm(c) for c in sin_enlace}

    print(f"\n{Color.BLUE}{Color.BOLD}" + "=" * 60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   RESUMEN OPCIÓN 4 — INVITACIONES FAMILIARES{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "=" * 60 + f"{Color.ENDC}")
    print(f" Total en menú: {len(correos_menu or [])}")
    print(f" {Color.GREEN}Aceptadas OK: {len(ok_unique)}{Color.ENDC}")
    print(f" {Color.FAIL}Pendientes / fallidas: {len(fail_unique)}{Color.ENDC}")

    if ok_unique:
        print(f"\n{Color.GREEN}{Color.BOLD}✓ Procesadas correctamente:{Color.ENDC}")
        for i, c in enumerate(ok_unique, 1):
            print(f"  {Color.GREEN}{i:2d}. {c}{Color.ENDC}")
    else:
        print(f"\n{Color.WARNING}✓ Ninguna cuenta quedó aceptada en esta corrida.{Color.ENDC}")

    if fail_unique:
        print(f"\n{Color.FAIL}{Color.BOLD}✗ Faltan / fallaron:{Color.ENDC}")
        for i, c in enumerate(fail_unique, 1):
            motivo = "sin enlace IMAP" if _norm(c) in sin_enlace_set else "no aceptada / error"
            print(f"  {Color.FAIL}{i:2d}. {c}{Color.ENDC}  ({motivo})")
    else:
        print(f"\n{Color.GREEN}✗ No quedan pendientes.{Color.ENDC}")

    print(f"{Color.BLUE}{Color.BOLD}" + "=" * 60 + f"{Color.ENDC}")


def abrir_enlace_restablecimiento_con_autocierre(
    url: str,
    correo: str,
    proxy_pe: dict | None = None,
    headless: bool = False,
) -> bool:
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

    print(f"    {Color.CYAN}[Navegador]{Color.ENDC} Automatizando restablecimiento de contraseña ({correo})"
          f"{' [headless]' if (headless or headless_forzado_por_entorno()) else ''}...")

    success_detected = False
    with sync_playwright() as p:
        base_launch_kwargs = kwargs_launch_persistent(profile_dir, headless=headless)

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
                _huella = f"{pwd_cuenta[:2]}…{pwd_cuenta[-2:]}" if len(pwd_cuenta) >= 4 else "(corta)"
                print(f"    [Reset] [{correo}] Colocando contraseña a restablecer "
                      f"(exacta del .txt, len={len(pwd_cuenta)}, huella={_huella})...")

                def _rellenar_pwd_reset(locator, valor: str) -> bool:
                    if not locator:
                        return False
                    try:
                        locator.click(timeout=3000)
                    except Exception:
                        pass
                    try:
                        locator.fill("")
                    except Exception:
                        pass
                    try:
                        locator.fill(valor)
                    except Exception:
                        try:
                            locator.type(valor, delay=40)
                        except Exception:
                            pass
                    time.sleep(0.2)
                    try:
                        if (locator.input_value() or "") == valor:
                            return True
                    except Exception:
                        pass
                    try:
                        ok = locator.evaluate(
                            """(el, v) => {
                                el.focus();
                                const setter = Object.getOwnPropertyDescriptor(
                                    window.HTMLInputElement.prototype, 'value')?.set;
                                if (setter) setter.call(el, v);
                                else el.value = v;
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                try {
                                    el.dispatchEvent(new InputEvent('input', {
                                        bubbles: true, data: v, inputType: 'insertText'
                                    }));
                                } catch (e) {}
                                return (el.value || '') === v;
                            }""",
                            valor,
                        )
                        if ok:
                            return True
                    except Exception:
                        pass
                    try:
                        locator.click(timeout=1500)
                        locator.press("Control+a")
                        locator.type(valor, delay=50)
                        time.sleep(0.15)
                        return (locator.input_value() or "") == valor
                    except Exception:
                        return False

                pwd_new1 = esperar_locator_en_frames(
                    page,
                    [
                        'input[name="newPassword"]',
                        'input[id*="newPassword" i]',
                        'input[autocomplete="new-password"]',
                        'input[type="password"]',
                    ],
                    timeout_s=8.0,
                )
                if not pwd_new1:
                    pwd_new1 = page.locator(
                        'input[name="newPassword"], input[type="password"], input[name="password"]'
                    ).first
                    if not esperar_visibilidad(pwd_new1, 8000):
                        pwd_new1 = None

                if pwd_new1:
                    if not _rellenar_pwd_reset(pwd_new1, pwd_cuenta):
                        print(f"    {Color.FAIL}[Reset] [{correo}] No se pudo escribir la "
                              f"contraseña en el campo principal.{Color.ENDC}")
                    else:
                        try:
                            pwd_new2 = esperar_locator_en_frames(
                                page,
                                [
                                    'input[name="confirmNewPassword"]',
                                    'input[id*="confirm" i]',
                                    'input[name*="confirm" i]',
                                ],
                                timeout_s=2.0,
                            )
                            if not pwd_new2:
                                visibles = []
                                for ip in page.locator('input[type="password"]').all():
                                    try:
                                        if ip.is_visible():
                                            visibles.append(ip)
                                    except Exception:
                                        pass
                                if len(visibles) >= 2:
                                    pwd_new2 = visibles[1]
                            if pwd_new2:
                                _rellenar_pwd_reset(pwd_new2, pwd_cuenta)
                        except Exception:
                            pass

                        try:
                            v1 = (pwd_new1.input_value() or "")
                            if v1 != pwd_cuenta:
                                print(f"    {Color.FAIL}[Reset] [{correo}] Tras rellenar, el campo "
                                      f"no tiene la contraseña del .txt (len leída={len(v1)} vs "
                                      f"{len(pwd_cuenta)}).{Color.ENDC}")
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

                        for _ in range(24):
                            time.sleep(0.5)
                            try:
                                u = (page.url or "").lower()
                            except Exception:
                                u = ""
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
                            if "/success" in u:
                                success_detected = True
                                break
                            if any(x in u for x in ("/login", "signin", "account.tidal")):
                                if "reset" not in u and "newpassword" not in u and "token" not in u:
                                    try:
                                        still = page.locator(
                                            'input[name="newPassword"], input[type="password"]'
                                        ).first
                                        if still.count() == 0 or not still.is_visible():
                                            success_detected = True
                                            break
                                    except Exception:
                                        pass
                            try:
                                if page.locator("text=Algo salió mal").or_(
                                    page.locator("text=Something went wrong")
                                ).or_(page.locator("text=do not match")).count() > 0:
                                    success_detected = False
                                    break
                            except Exception:
                                pass

                        if not success_detected:
                            try:
                                err = page.locator("text=Algo salió mal").or_(
                                    page.locator("text=Something went wrong")
                                ).or_(page.locator("text=do not match")).count()
                                still = page.locator('input[name="newPassword"]').first
                                form_gone = still.count() == 0 or not still.is_visible()
                                if err == 0 and form_gone:
                                    success_detected = True
                            except Exception:
                                pass
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


# Catch-all Cloudflare Email Routing: el alias @dominio llega a un Gmail real (IMAP).
# Se puede ampliar/cambiar en passwords.txt:
#   imap_forward_cheapmusic.best=otro@gmail.com
_FORWARD_IMAP_DEFAULT = {
    "cheapmusic.best": "cakeseller1234@gmail.com",
}


def _pares_passwords_txt() -> list[tuple[str, str]]:
    pwd_file = SCRIPT_DIR / "passwords.txt"
    if not pwd_file.exists():
        return []
    try:
        lines = pwd_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    pares: list[tuple[str, str]] = []
    for line in lines:
        raw = (line or "").strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, val = raw.split("=", 1)
        pares.append((key.strip().lower(), val.strip().strip('"').strip("'")))
    return pares


def _mapa_dominios_forward_imap() -> dict[str, str]:
    """Dominio catch-all → Gmail IMAP. passwords.txt pisa el default."""
    mapa = dict(_FORWARD_IMAP_DEFAULT)
    for key, val in _pares_passwords_txt():
        if not val or "@" not in val:
            continue
        dominio = ""
        if key.startswith("imap_forward_domain_"):
            dominio = key[len("imap_forward_domain_"):].strip()
        elif key.startswith("imap_forward_"):
            dominio = key[len("imap_forward_"):].strip()
        else:
            continue
        dominio = dominio.lstrip("@").lower()
        if dominio:
            mapa[dominio] = val.strip().lower()
    return mapa


def destino_imap_de_alias(correo: str) -> str:
    """Si el dominio tiene catch-all, el Gmail real; si no, el propio correo."""
    correo = (correo or "").strip().lower()
    if "@" not in correo:
        return correo
    _, dom = correo.split("@", 1)
    return _mapa_dominios_forward_imap().get(dom.lower()) or correo


def obtener_credenciales_imap_reales(gmail_user_solicitado: str) -> tuple[str | None, str | None]:
    """Busca en passwords.txt el usuario real de IMAP y su App Password.

    Alias de dominio catch-all (p. ej. titular-0003@cheapmusic.best) se resuelven
    al Gmail de destino (cakeseller1234@gmail.com o imap_forward_<dominio>=).
    """
    pwd_file = SCRIPT_DIR / "passwords.txt"
    if not pwd_file.exists():
        print(f"{Color.FAIL}[Error]{Color.ENDC} No se encuentra el archivo 'passwords.txt' en {pwd_file}.")
        return None, None
        
    try:
        lines = pwd_file.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        print(f"{Color.FAIL}[Error]{Color.ENDC} No se pudo leer 'passwords.txt': {e}")
        return None, None
    
    # 1. Limpiar el correo solicitado (invisibles Unicode + puntos del username de Gmail)
    gmail_user_solicitado = clean_email(gmail_user_solicitado)
    if gmail_user_solicitado:
        gmail_user_solicitado = destino_imap_de_alias(gmail_user_solicitado)
    if not gmail_user_solicitado or "@" not in gmail_user_solicitado:
        return None, None
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
    # Sanear usuario/clave: un U+200C al inicio tumba imaplib con error de codec ascii
    # aunque la App Password sea correcta (síntoma: "No se pudo conectar por IMAP ... \\u200c").
    user_real = clean_email(user_real) or (user_real or "").strip()
    app_pwd = (app_pwd or "").strip().replace("\u200b", "").replace("\u200c", "").replace("\ufeff", "")
    if not user_real or not app_pwd:
        raise RuntimeError(f"Credenciales IMAP incompletas tras limpiar (user={user_real!r}).")

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
    clave = _norm_dots_gmail(destino_imap_de_alias(gmail_user))
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
    return _norm_dots_gmail(user_real or destino_imap_de_alias(gmail_user))


def listar_buzones_imap_de_passwords() -> list[str]:
    """Cuentas IMAP distintas (App Password) registradas en passwords.txt.

    Sirve para buscar un OTP de Tidal cuando llega a otro Gmail distinto del perfil.
    Cada entrada es el correo canónico de login IMAP (suele ser sin puntos).
    """
    pwd_file = SCRIPT_DIR / "passwords.txt"
    if not pwd_file.exists():
        return []
    try:
        lines = pwd_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    vistos: set[str] = set()
    out: list[str] = []
    for line in lines:
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        if not val.strip().strip('"').strip("'"):
            continue
        key_name = key.strip().lower()
        email_part = ""
        if key_name.startswith("gmail_app_password_"):
            email_part = key_name[len("gmail_app_password_"):].strip()
        elif key_name.startswith("imap_password_"):
            email_part = key_name[len("imap_password_"):].strip()
        elif key_name in ("gmail_app_password", "imap_password"):
            email_part = "cakeseller1234@gmail.com"
        if not email_part:
            continue
        # Claves tipo getmushroom1052_at_gmail_com → getmushroom1052@gmail.com
        if "_at_" in email_part and "@" not in email_part:
            email_part = email_part.replace("_at_", "@").replace("_", ".")
            # Evitar get.mushroom... si la clave era sin puntos: re-normalizar gmail
            if "@gmail." in email_part or "@googlemail." in email_part:
                email_part = _norm_dots_gmail(email_part)
        email_part = email_part.strip().lower()
        if "@" not in email_part:
            continue
        clave = _norm_dots_gmail(email_part)
        if clave in vistos:
            continue
        vistos.add(clave)
        out.append(email_part)
    return out


def buzones_imap_candidatos_otp(
    *correos_preferidos: str,
    incluir_resto_passwords: bool = True,
) -> list[str]:
    """Ordena buzones IMAP a sondear: preferidos primero, luego el resto de passwords.txt.

    Deduplica por buzón Gmail (sin puntos). Cada hilo sigue filtrando por el alias EXACTO
    con puntos vía exigir_destinatario_exacto / aliases_solo.
    """
    orden: list[str] = []
    vistos: set[str] = set()

    def _add(correo: str | None):
        c = (correo or "").strip().lower()
        if not c or "@" not in c:
            return
        if not tiene_contrasena_imap_registrada(c):
            return
        user_real, _ = obtener_credenciales_imap_reales(c)
        clave = _norm_dots_gmail(user_real or c)
        if clave in vistos:
            return
        vistos.add(clave)
        orden.append((user_real or c).strip().lower())

    for c in correos_preferidos:
        _add(c)
    if incluir_resto_passwords:
        for c in listar_buzones_imap_de_passwords():
            _add(c)
    return orden


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


def _destinatario_es_para_alias(
    gmail_user: str,
    recipients_text: str,
    cuerpo_text: str = "",
    *,
    exigir_exacto: bool = False,
) -> bool:
    """True solo si el correo de Tidal es para ESTE alias con puntos, no para un hermano.

    Gmail ignora puntos, así que getspoo.ky49.28 y getspoo.ky.4928 comparten buzón. Si se
    compara solo la forma sin puntos, un hilo toma el código del otro y el registro queda
    colgado en /authorize con el código incorrecto.

    Atención (opción 4): si To: solo trae la forma canónica sin puntos, el paso 3 devolvía
    True para TODOS los alias hermanos → un solo UID reclamado y el resto sin enlace.
    Para invitaciones familiares usar asignar_enlaces_invitacion_a_correos().

    exigir_exacto=True (opción 15): nunca aceptar To: canónico sin puntos si el alias pedido
    lleva puntos. Obliga a que el mensaje mencione el alias exacto (evita cruce de hermanos).
    """
    objetivo = (gmail_user or "").strip().lower()
    if not objetivo:
        return False
    objetivo_norm = _norm_dots_gmail(objetivo)
    texto = f"{recipients_text or ''} {cuerpo_text or ''}".lower()
    texto = (
        texto.replace("%40", "@")
        .replace("&#64;", "@")
        .replace("&amp;#64;", "@")
        .replace("&#046;", ".")
        .replace("&dot;", ".")
    )
    # Quitar zero-width / soft hyphen que a veces parten el correo en HTML de Tidal
    texto = re.sub(r"[\u200b\u200c\u200d\ufeff\u00ad]", "", texto)
    addrs = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+", texto)
    # Alias exacto en texto (To/cuerpo), aunque el regex falle con basura HTML
    if objetivo in texto:
        return True
    # Local+dominio partidos por espacios/tags: get.mush room@gmail.com → no; pero
    # get.mushroom.1052 @ gmail.com sí aparece en algunos templates.
    local_obj, _, dom_obj = objetivo.partition("@")
    if local_obj and dom_obj and re.search(
        rf"{re.escape(local_obj)}\s*@\s*{re.escape(dom_obj)}", texto
    ):
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
    local = objetivo.split("@", 1)[0]
    if exigir_exacto and "." in local:
        # Alias con puntos: sin mención exacta no asumir que el canónico es nuestro
        return False

    # Para invitaciones/códigos de un solo hilo: aceptar canónico
    return True


def _extraer_codigos_otp(texto: str, preferir_len: int | None = None) -> list[str]:
    """Extrae códigos OTP de 5–6 dígitos priorizando los junto a 'code'/'código'.

    Antes devolvía el primer número de 6 dígitos del HTML (fechas, tracking, etc.) y
    descartaba OTP reales tipo 202431 por parecer un año → Tidal rechazaba el código.

    preferir_len: si es 5 o 6, esos códigos van primero (p. ej. 6 cajas en eliminación).
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

    # 2) Dígitos partidos por HTML/espacios: "1 2 3 4 5 6" o spans sueltos
    for m in re.finditer(r"(?<!\d)(?:\d[\s\-\u00a0]){4,5}\d(?!\d)", texto):
        _add(re.sub(r"\D", "", m.group(0)), prioritario=True)
    # Dígitos separados solo por etiquetas HTML: >1</span><span>2</...
    solo_digitos_html = re.sub(r"(?<=>)\s+(\d)\s+(?=<)", r"\1", texto)
    for m in re.finditer(
        r"(?<!\d)(?:\d(?:\s*</?(?:span|b|strong|td|font)[^>]*>\s*)+){4,5}\d(?!\d)",
        solo_digitos_html,
        flags=re.I,
    ):
        _add(re.sub(r"\D", "", m.group(0)), prioritario=True)

    # 3) Cualquier 5–6 dígitos suelto (menor prioridad)
    for m in re.findall(r"\b(\d{5,6})\b", texto):
        _add(m, prioritario=False)

    # Preferir longitud pedida (6 en eliminación) y luego 6 dígitos genéricos
    def _ordenar(grupo: list[str]) -> list[str]:
        pref = int(preferir_len) if preferir_len in (5, 6) else 6

        def _clave(c: str):
            return (0 if len(c) == pref else 1, 0 if len(c) == 6 else 1, grupo.index(c))

        return sorted(grupo, key=_clave)

    combined = _ordenar(prioritarios) + _ordenar(normales)
    # Reordenar el resultado global: un 6 dígitos "normal" debe ganar a un 5 prioritario
    # cuando preferir_len=6 (asistente de eliminación con 6 cajas).
    if preferir_len in (5, 6):
        pref = [c for c in combined if len(c) == preferir_len]
        otros = [c for c in combined if len(c) != preferir_len]
        return pref + otros
    return combined


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


KEYWORDS_ELIMINACION_CUENTA = [
    # Frases específicas del OTP de borrado (evitar "elimin" suelto → correos de Family).
    "verificación de la eliminación", "verificacion de la eliminacion",
    "eliminación de tu cuenta", "eliminacion de tu cuenta",
    "eliminar tu cuenta", "delete your account", "account deletion",
    "confirm that you want to delete", "confirma que deseas eliminar",
    "código para eliminar", "codigo para eliminar", "code to delete",
    "para eliminar tu cuenta", "to delete your account",
    "verificación para eliminar", "verificacion para eliminar",
    "confirmation code", "código de confirmación", "codigo de confirmacion",
    "confirmación de eliminación", "confirmacion de eliminacion",
    "confirmation of deletion", "deletion confirmation", "deletion verification",
    "verify deletion", "verifica la eliminación", "verifica la eliminacion",
    "account deletion verification", "código de verificación", "codigo de verificacion",
    "verification code", "security code", "código de seguridad", "codigo de seguridad",
]

# Asuntos/cuerpos que NO son el OTP de borrado de cuenta (opción 15).
EXCLUDE_ELIMINACION_CUENTA = [
    "plan tidal family", "tidal family", "family plan", "plan familiar",
    "eliminado de un plan", "removed from a", "removed from your",
    "se ha eliminado de un plan", "has been removed from",
    "invites you to join", "te ha invitado",
    # No confundir con OTP de registro / bienvenida
    "finish creating your account", "terminar de crear", "sign-up", "sign up",
    "welcome to tidal", "bienvenido a tidal", "bienvenida a tidal",
]

KEYWORDS_REGISTRO_CUENTA = [
    "registr", "bienven", "código", "codigo", "code",
    "verific", "sign-up", "signup", "sign up",
    "finish creating", "terminar de crear",
]

# Asunto real ES: "Restablecer tu contraseña Tidal" (sin "de").
# Cuerpo: "...link para restaurar su contraseña" + https://login.tidal.com/resetpass/UUID
KEYWORDS_RESTABLECER_PWD = [
    "restablecer tu contraseña tidal",
    "restablecer tu contraseña",
    "restablecer tu contrasena tidal",
    "restablecer tu contrasena",
    "restablecer tu contraseña de tidal",  # variante antigua / EN-ES mixto
    "restaurar su contraseña",
    "restaurar su contrasena",
    "resetting your tidal password",
    "reset your tidal password",
    "reset your password",
    "link to reset your password",
    "login.tidal.com/resetpass/",
]


def _texto_excluido_por_frases(texto: str, frases) -> bool:
    t = (texto or "").lower()
    if not t or not frases:
        return False
    if isinstance(frases, str):
        frases = [frases]
    return any((f or "").lower() in t for f in frases if f)

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


def _es_url_invitacion_directa(url: str) -> bool:
    """True si es accept/join/family en login|account.tidal.com (NO ablink tracking)."""
    u = (url or "").strip().lower()
    if not u.startswith("http"):
        return False
    if "ablink." in u or "resetpass" in u or "reset-password" in u:
        return False
    if any(x in u for x in ("/privacy", "/terms", "/legal", "support.tidal.com")):
        return False
    return (
        "login.tidal.com/family" in u
        or "account.tidal.com/family" in u
        or ("/family/" in u and ("accept" in u or "join" in u))
        or "/accept/" in u
        or "/join/" in u
    )


def _resolver_ablink_a_invitacion(url: str, timeout_s: float = 18.0) -> str:
    """Sigue ablink.info.tidal.com hasta la URL final de accept (sin Chrome).

    Los proxies NG suelen romper el túnel a ablink (ERR_TUNNEL); login.tidal.com/family
    o account.tidal.com/family/accept-invite suelen abrir bien con el mismo proxy.
    """
    url = (url or "").strip()
    if not url:
        return url
    if _es_url_invitacion_directa(url):
        return url
    if "ablink." not in url.lower():
        return url

    def _pick_direct(candidatos: list[str]) -> str | None:
        for cand in candidatos:
            c = (cand or "").strip()
            if _es_url_invitacion_directa(c):
                preview = c if len(c) <= 95 else c[:95] + "..."
                print(f"    {Color.CYAN}[Invitación]{Color.ENDC} ablink resuelto → {preview}")
                return c
        return None

    # 1) requests con redirects
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        r = session.get(url, allow_redirects=True, timeout=max(5.0, float(timeout_s)))
        historial: list[str] = []
        for h in (getattr(r, "history", None) or []):
            try:
                historial.append((h.url or "").strip())
            except Exception:
                pass
        final = (getattr(r, "url", None) or "").strip()
        if final:
            historial.append(final)
        # Location headers explícitos
        for h in (getattr(r, "history", None) or []):
            try:
                loc = (h.headers.get("Location") or "").strip()
                if loc.startswith("http"):
                    historial.append(loc)
            except Exception:
                pass

        picked = _pick_direct(list(reversed(historial)))
        if picked:
            return picked

        html = ""
        try:
            html = r.text or ""
        except Exception:
            html = ""
        html_cands = [
            m.group(0).rstrip(").,;'\"").replace("&amp;", "&")
            for m in re.finditer(
                r'https?://(?:login|account)\.tidal\.com/[^"\'\s<>\\]+',
                html,
                flags=re.I,
            )
        ]
        picked = _pick_direct(html_cands)
        if picked:
            return picked

        if final and "tidal.com" in final.lower() and "ablink." not in final.lower():
            return final
    except Exception as e1:
        print(f"    {Color.WARNING}[Invitación]{Color.ENDC} Resolver ablink (requests) falló: "
              f"{type(e1).__name__}: {e1}")

    # 2) Fallback urllib (a veces requests se atasca en el proxy de sistema)
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=max(5.0, float(timeout_s))) as resp:
            final2 = (getattr(resp, "geturl", lambda: "")() or "").strip()
            body = ""
            try:
                body = resp.read(120000).decode("utf-8", errors="ignore")
            except Exception:
                body = ""
        cands2 = [final2] if final2 else []
        cands2.extend(
            m.group(0).rstrip(").,;'\"").replace("&amp;", "&")
            for m in re.finditer(
                r'https?://(?:login|account)\.tidal\.com/[^"\'\s<>\\]+',
                body or "",
                flags=re.I,
            )
        )
        picked = _pick_direct(cands2)
        if picked:
            return picked
        if final2 and "tidal.com" in final2.lower() and "ablink." not in final2.lower():
            return final2
    except Exception as e2:
        print(f"    {Color.WARNING}[Invitación]{Color.ENDC} No se pudo resolver ablink "
              f"({type(e2).__name__}: {e2}). Se usará el tracking (puede fallar por túnel).")
    return url


def _extraer_enlace_invitacion_de_contenido(body_text: str, html_raw: str = "") -> str | None:
    """Solo enlaces reales de invitación familiar (nunca resetpass ni footers genéricos)."""
    JOIN_TEXTS = ["join", "unir", "nete", "accept invitation", "aceptar invit", "unirse"]

    def _es_enlace_invitacion(url: str) -> bool:
        u = (url or "").lower()
        if not u.startswith("http"):
            return False
        # Nunca confundir con restablecimiento de contraseña
        if "resetpass" in u or "reset-password" in u or "forgot" in u:
            return False
        if any(x in u for x in ("/privacy", "/terms", "/legal", "support.tidal.com")):
            return False
        return (
            "login.tidal.com/family" in u
            or "/family/" in u
            or "/accept/" in u
            or "/join/" in u
            or ("ablink." in u and "tidal" in u and any(k in u for k in ("family", "invite", "accept", "join")))
            or ("ablink." in u and "tidal" in u)  # CTA tracking genérico de Tidal
        )

    def _es_ablink_tidal(url: str) -> bool:
        u = (url or "").lower()
        if "resetpass" in u or "reset-password" in u:
            return False
        return "ablink." in u and "tidal" in u

    candidatos_fuertes: list[str] = []
    candidatos_ablink: list[str] = []

    if html_raw:
        try:
            a_tags_full = re.findall(
                r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', html_raw, re.I
            )
            for href, inner_html in a_tags_full:
                href = (href or "").strip()
                inner_text = re.sub(r'<[^>]+>', '', inner_html or "").strip().lower()
                if _es_enlace_invitacion(href) and not _es_ablink_tidal(href):
                    candidatos_fuertes.append(href)
                    continue
                if _es_enlace_invitacion(href) and _es_ablink_tidal(href):
                    candidatos_ablink.append(href)
                    continue
                # CTA "Join / Accept" cuyo href es ablink de tracking de Tidal
                if _es_ablink_tidal(href) and any(jt in inner_text for jt in JOIN_TEXTS):
                    candidatos_ablink.append(href)
        except Exception:
            pass

        # Texto visible del <a> que ya es la URL de family
        try:
            a_tags = re.findall(r'<a[^>]+href=["\'][^"\']+["\'][^>]*>([\s\S]*?)</a>', html_raw, re.I)
            for inner_html in a_tags:
                inner_text = re.sub(r'<[^>]+>', '', inner_html).strip()
                if _es_enlace_invitacion(inner_text) and not _es_ablink_tidal(inner_text):
                    candidatos_fuertes.append(inner_text)
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

    for link in enlaces:
        if _es_enlace_invitacion(link) and not _es_ablink_tidal(link):
            candidatos_fuertes.append(link)
        elif _es_ablink_tidal(link):
            candidatos_ablink.append(link)

    def _rank_directa(u: str) -> tuple:
        ul = u.lower()
        if "login.tidal.com/family" in ul:
            return (0, u)
        if "account.tidal.com/family" in ul or "accept-invite" in ul:
            return (1, u)
        if "/accept" in ul or "/join" in ul:
            return (2, u)
        return (3, u)

    if candidatos_fuertes:
        candidatos_fuertes.sort(key=_rank_directa)
        elegido = candidatos_fuertes[0]
        return elegido if _es_url_invitacion_directa(elegido) else _resolver_ablink_a_invitacion(elegido)

    # Solo ablink: resolver a URL final antes de devolver (evita ERR_TUNNEL en Chrome+proxy)
    if candidatos_ablink:
        return _resolver_ablink_a_invitacion(candidatos_ablink[0])
    return None


def _emails_en_texto_invitacion(recipients: str, cuerpo: str) -> list[str]:
    """Extrae direcciones útiles (To/cuerpo/query de links) normalizando HTML entities."""
    texto = f"{recipients or ''} {cuerpo or ''}".lower()
    texto = (
        texto.replace("%40", "@")
        .replace("&#64;", "@")
        .replace("&amp;#64;", "@")
        .replace("&#046;", ".")
        .replace("&dot;", ".")
        .replace("%2e", ".")
    )
    texto = re.sub(r"[\u200b\u200c\u200d\ufeff\u00ad]", "", texto)
    addrs = re.findall(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+", texto)
    # Parámetros de URL: email= / invitee= / username=
    for m in re.finditer(
        r"(?:email|invitee|invited|username|user|account|to)=([a-z0-9._%+\-]+@[a-z0-9.\-]+)",
        texto,
        flags=re.I,
    ):
        addrs.append(m.group(1).lower())
    # Deduplicar preservando orden
    out, seen = [], set()
    for a in addrs:
        a = a.strip().lower()
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _puntuar_invitacion_para_alias(alias: str, recipients: str, cuerpo: str) -> int:
    """Score de atribución alias↔correo.

    >=85: alias exacto con puntos (seguro).
    40: solo canónico sin puntos y sin hermano exacto en el mensaje.
    0: incompatible / pertenece a otro hermano.
    """
    alias = (alias or "").strip().lower()
    if not alias or "@" not in alias:
        return 0
    alias_norm = _norm_dots_gmail(alias)
    addrs = _emails_en_texto_invitacion(recipients, cuerpo)
    texto = f"{recipients or ''} {cuerpo or ''}".lower()
    texto = texto.replace("%40", "@").replace("&#64;", "@")

    # 1) Exacto como dirección parseada (To / cuerpo / query)
    if alias in addrs:
        return 100

    # 2) Exacto como token (evita substring: get.mush... dentro de get.m.ush...)
    if re.search(rf"(?<![a-z0-9._%+\-]){re.escape(alias)}(?![a-z0-9._%+\-])", texto):
        return 95

    # 3) Otro alias con puntos del mismo buzón aparece → este mensaje NO es nuestro
    hermanos = [
        a for a in addrs
        if _norm_dots_gmail(a) == alias_norm
        and a != alias
        and a != alias_norm
        and "." in a.split("@", 1)[0]
    ]
    if hermanos:
        return 0

    # 4) Solo forma canónica (sin puntos): débil — no basta para mezclar hermanos
    if alias_norm in addrs or re.search(
        rf"(?<![a-z0-9._%+\-]){re.escape(alias_norm)}(?![a-z0-9._%+\-])", texto
    ):
        return 40

    # Sin rastro del buzón: no atribuir a ciegas
    return 0


def _imap_decode_header_value(raw) -> str:
    from email.header import decode_header
    if not raw:
        return ""
    try:
        parts = []
        for part_bytes, charset in decode_header(raw):
            if isinstance(part_bytes, bytes):
                parts.append(part_bytes.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(part_bytes or "")
        return "".join(parts)
    except Exception:
        try:
            return str(raw)
        except Exception:
            return ""


def _imap_asunto_parece_invitacion(subject: str) -> bool:
    t = (subject or "").lower()
    if not t:
        return False
    if "cancel" in t:
        return False
    claves = (
        "family", "familia", "invit", "join", "unir", "welcome to the",
        "plan familiar", "has invited", "te ha invitado",
    )
    return any(k in t for k in claves)


def _imap_parse_fetch_messages(data) -> dict[int, "email.message.Message"]:
    """Parsea respuesta UID FETCH multi-mensaje → {uid: Message}."""
    import email
    out: dict[int, email.message.Message] = {}
    if not data:
        return out
    pending_uid = None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and item[1]:
            meta = item[0]
            if isinstance(meta, bytes):
                m = re.search(br"UID\s+(\d+)", meta, flags=re.I)
                if m:
                    pending_uid = int(m.group(1))
            try:
                msg = email.message_from_bytes(item[1])
            except Exception:
                pending_uid = None
                continue
            if pending_uid:
                out[pending_uid] = msg
                pending_uid = None
    return out


def _imap_fetch_headers_lote(mail, uid_bytes_list: list[bytes]) -> dict[int, "email.message.Message"]:
    """Fetch RFC822.HEADER en lotes (mucho más rápido que RFC822 uno a uno)."""
    import email
    out: dict[int, email.message.Message] = {}
    if not uid_bytes_list:
        return out
    chunk = 80
    for i in range(0, len(uid_bytes_list), chunk):
        lote = uid_bytes_list[i:i + chunk]
        uid_set = b",".join(lote)
        try:
            status, data = mail.uid("fetch", uid_set, "(RFC822.HEADER)")
        except Exception:
            for uid_b in lote:
                try:
                    st, d = mail.uid("fetch", uid_b, "(RFC822.HEADER)")
                    if st != "OK" or not d:
                        continue
                    parsed = _imap_parse_fetch_messages(d)
                    if parsed:
                        out.update(parsed)
                    else:
                        for item in d:
                            if isinstance(item, tuple) and len(item) >= 2 and item[1]:
                                try:
                                    out[int(uid_b)] = email.message_from_bytes(item[1])
                                except Exception:
                                    pass
                except Exception:
                    continue
            continue
        if status != "OK" or not data:
            continue
        out.update(_imap_parse_fetch_messages(data))
    return out


def _imap_fetch_bodies_lote(mail, uids: list[int]) -> dict[int, "email.message.Message"]:
    """Fetch BODY.PEEK[] en lotes (evita N round-trips por candidato)."""
    out: dict[int, email.message.Message] = {}
    if not uids:
        return out
    import email
    uid_bytes = [str(u).encode() for u in uids if u]
    chunk = 25  # cuerpos son más pesados que headers
    for i in range(0, len(uid_bytes), chunk):
        lote = uid_bytes[i:i + chunk]
        uid_set = b",".join(lote)
        data = None
        try:
            status, data = mail.uid("fetch", uid_set, "(BODY.PEEK[])")
        except Exception:
            status = "NO"
        if status != "OK" or not data:
            try:
                status, data = mail.uid("fetch", uid_set, "(RFC822)")
            except Exception:
                status, data = "NO", None
        if status == "OK" and data:
            parsed = _imap_parse_fetch_messages(data)
            if parsed:
                out.update(parsed)
                continue
        # Fallback uno a uno
        for uid_b in lote:
            try:
                st, d = mail.uid("fetch", uid_b, "(BODY.PEEK[])")
            except Exception:
                try:
                    st, d = mail.uid("fetch", uid_b, "(RFC822)")
                except Exception:
                    continue
            if st != "OK" or not d:
                continue
            parsed = _imap_parse_fetch_messages(d)
            if parsed:
                out.update(parsed)
            else:
                for item in d:
                    if isinstance(item, tuple) and len(item) >= 2 and item[1]:
                        try:
                            out[int(uid_b)] = email.message_from_bytes(item[1])
                        except Exception:
                            pass
    return out


def listar_invitaciones_familiares_buzon(
    gmail_user: str,
    max_age_minutes: int = 1440,
    max_mensajes: int = 500,
    aliases_objetivo: list[str] | None = None,
) -> list[dict]:
    """Lista invitaciones familiares del buzón (rápido: headers en lote → body solo candidatos).

    1) SEARCH UID (barato)
    2) FETCH HEADER por lotes de 60
    3) RFC822 completo SOLO si el asunto parece invitación (o To: trae un alias exacto)
    """
    import email
    from datetime import datetime, timezone, timedelta
    from email.utils import parsedate_to_datetime

    user_real, app_pwd = obtener_credenciales_imap_reales(gmail_user)
    if not user_real or not app_pwd:
        print(f"    {Color.WARNING}[IMAP]{Color.ENDC} Sin credenciales IMAP para listar invitaciones de {gmail_user}.")
        return []

    aliases_l = [(a or "").strip().lower() for a in (aliases_objetivo or []) if (a or "").strip()]
    resultados: list[dict] = []
    stack = contextlib.ExitStack()
    try:
        max_age_s = max(60, int((max_age_minutes or 1440) * 60))
        since_dt = datetime.now(timezone.utc) - timedelta(seconds=max_age_s)
        since_str = (since_dt - timedelta(days=1)).strftime("%d-%b-%Y")
        tope = max(50, int(max_mensajes or 500))

        print(f"    {Color.CYAN}[IMAP]{Color.ENDC} Listando invitaciones (rápido) en "
              f"{_servidor_imap_para(user_real)} ({user_real}) "
              f"SINCE {since_str}, hasta {tope} UIDs...")
        mail = stack.enter_context(sesion_imap(user_real, app_pwd))

        status, messages = mail.uid("search", None, f'(FROM "tidal" SINCE {since_str})')
        if status != "OK" or not messages or not messages[0]:
            status, messages = mail.uid("search", None, '(FROM "tidal")')
        if status != "OK" or not messages or not messages[0]:
            return []

        all_ids = messages[0].split()
        msg_ids = all_ids[-tope:]
        # Mantener orden cronológico ascendente en el lote; luego invertimos resultados
        print(f"    {Color.CYAN}[IMAP]{Color.ENDC} {len(all_ids)} UIDs tidal → "
              f"cabeceras de {len(msg_ids)} (lotes)...")

        headers_map = _imap_fetch_headers_lote(mail, msg_ids)
        candidatos_uid: list[int] = []
        meta_por_uid: dict[int, dict] = {}

        for uid_b in reversed(msg_ids):  # recientes primero
            try:
                uid = int(uid_b)
            except ValueError:
                continue
            msg_h = headers_map.get(uid)
            if not msg_h:
                continue

            # Antigüedad por Date del header
            try:
                date_str = msg_h.get("Date")
                if date_str:
                    age = (
                        datetime.now(timezone.utc)
                        - parsedate_to_datetime(date_str).astimezone(timezone.utc)
                    ).total_seconds()
                    if age > max_age_s:
                        continue
            except Exception:
                pass

            subject_text = _imap_decode_header_value(msg_h.get("Subject"))
            to_header = (msg_h.get("To") or "").lower()
            delivered_to = (msg_h.get("Delivered-To") or "").lower()
            envelope_to = (msg_h.get("Envelope-To") or "").lower()
            x_original = (msg_h.get("X-Original-To") or "").lower()
            x_forwarded = (msg_h.get("X-Forwarded-To") or "").lower()
            recipients = f"{to_header} {delivered_to} {envelope_to} {x_original} {x_forwarded}"

            parece_inv = _imap_asunto_parece_invitacion(subject_text)
            to_exacto = bool(aliases_l) and any(a in recipients for a in aliases_l)
            if not parece_inv and not to_exacto:
                # Asunto genérico: aún puede ser invite; mirar keywords cortas en Subject vacío
                # → no full-fetch (ahorra tiempo). Si To exacto sí.
                continue

            candidatos_uid.append(uid)
            meta_por_uid[uid] = {
                "subject": subject_text,
                "recipients": recipients,
            }

        print(f"    {Color.CYAN}[IMAP]{Color.ENDC} Candidatos por cabecera: {len(candidatos_uid)} "
              f"(se descarga cuerpo solo de estos)...")

        bodies_map = _imap_fetch_bodies_lote(mail, candidatos_uid)
        for uid in candidatos_uid:
            msg = bodies_map.get(uid)
            if not msg:
                continue

            meta = meta_por_uid.get(uid) or {}
            subject_text = meta.get("subject") or _imap_decode_header_value(msg.get("Subject"))
            body_text, html_raw = _extraer_cuerpo_y_html_msg(msg)
            text_to_check = f"{subject_text} {body_text}".lower()
            if not any(kw.lower() in text_to_check for kw in KEYWORDS_INVITACION_FAMILIAR):
                continue
            if "cancel" in text_to_check:
                continue
            link = _extraer_enlace_invitacion_de_contenido(body_text, html_raw)
            if not link:
                continue

            recipients = meta.get("recipients") or ""
            if not recipients:
                recipients = " ".join([
                    (msg.get("To") or ""),
                    (msg.get("Delivered-To") or ""),
                    (msg.get("X-Original-To") or ""),
                ]).lower()

            resultados.append({
                "uid": uid,
                "recipients": recipients,
                "body": f"{subject_text}\n{body_text}\n{html_raw}",
                "link": link,
            })

        print(f"    {Color.CYAN}[IMAP]{Color.ENDC} {len(resultados)} invitación(es) con enlace usable.")
    except Exception as e:
        print(f"    {Color.FAIL}[IMAP]{Color.ENDC} Error listando invitaciones: {e}")
    finally:
        stack.close()
    return resultados


def _buscar_invitaciones_dirigidas_lote(
    mail,
    aliases: list[str],
    max_age_minutes: int = 1440,
    uids_excluir: set[int] | None = None,
    links_excluir: set[str] | None = None,
) -> dict[str, dict]:
    """Una sola conexión: SEARCH por alias → headers en lote → bodies en lote."""
    import email
    from datetime import datetime, timezone, timedelta
    from email.utils import parsedate_to_datetime

    uids_excluir = set(uids_excluir or set())
    links_excluir = set(links_excluir or set())
    max_age_s = max(60, int((max_age_minutes or 1440) * 60))
    since_dt = datetime.now(timezone.utc) - timedelta(seconds=max_age_s)
    since_str = (since_dt - timedelta(days=1)).strftime("%d-%b-%Y")
    hallados: dict[str, dict] = {}

    aliases_norm = []
    for a in aliases:
        a = (a or "").strip().lower()
        if a and "@" in a and a not in aliases_norm:
            aliases_norm.append(a)
    if not aliases_norm:
        return hallados

    # 1) SEARCH barato por alias (TO exacto → TEXT alias; sin TEXT local que trae basura)
    uids_por_alias: dict[str, list[int]] = {}
    todos_uids: list[int] = []
    vistos: set[int] = set()
    for alias in aliases_norm:
        msg_ids = []
        for crit in (
            f'(FROM "tidal" SINCE {since_str} TO "{alias}")',
            f'(FROM "tidal" SINCE {since_str} TEXT "{alias}")',
            f'(FROM "tidal" TEXT "{alias}")',
        ):
            try:
                status, messages = mail.uid("search", None, crit)
            except Exception:
                continue
            if status == "OK" and messages and messages[0]:
                msg_ids = messages[0].split()
                if msg_ids:
                    break
        if not msg_ids:
            continue
        uids_alias: list[int] = []
        for msg_id in reversed(msg_ids[-6:]):
            try:
                uid = int(msg_id)
            except ValueError:
                continue
            if uid in uids_excluir or uid in vistos:
                continue
            uids_alias.append(uid)
            vistos.add(uid)
            todos_uids.append(uid)
        if uids_alias:
            uids_por_alias[alias] = uids_alias

    if not todos_uids:
        return hallados

    # 2) Headers en lote → filtrar edad / asunto
    headers_map = _imap_fetch_headers_lote(
        mail, [str(u).encode() for u in todos_uids]
    )
    candidatos_body: list[int] = []
    meta_por_uid: dict[int, dict] = {}
    for uid in todos_uids:
        msg_h = headers_map.get(uid)
        if not msg_h:
            candidatos_body.append(uid)  # sin header: intentar body igual
            continue
        try:
            date_str = msg_h.get("Date")
            if date_str:
                age = (
                    datetime.now(timezone.utc)
                    - parsedate_to_datetime(date_str).astimezone(timezone.utc)
                ).total_seconds()
                if age > max_age_s:
                    continue
        except Exception:
            pass
        subject_text = _imap_decode_header_value(msg_h.get("Subject"))
        if subject_text and "cancel" in subject_text.lower():
            continue
        recipients = " ".join([
            (msg_h.get("To") or ""),
            (msg_h.get("Delivered-To") or ""),
            (msg_h.get("X-Original-To") or ""),
            (msg_h.get("X-Forwarded-To") or ""),
        ]).lower()
        parece = _imap_asunto_parece_invitacion(subject_text) or any(
            a in recipients for a in aliases_norm
        )
        if not parece and subject_text:
            # Asunto raro: igual puede ser invite con To canónico; dejar pasar
            # solo si algún alias aparece en recipients o subject vacío
            if not any(a in recipients or a.split("@")[0] in recipients for a in aliases_norm):
                # Sin señal: aún así incluir (pocos UIDs por alias)
                pass
        candidatos_body.append(uid)
        meta_por_uid[uid] = {"subject": subject_text, "recipients": recipients}

    # 3) Bodies en lote
    bodies_map = _imap_fetch_bodies_lote(mail, candidatos_body)

    # 4) Emparejar cada alias con el mejor match exacto
    for alias in aliases_norm:
        if alias in hallados:
            continue
        for uid in uids_por_alias.get(alias, []):
            if uid in uids_excluir:
                continue
            msg = bodies_map.get(uid)
            if not msg:
                continue
            meta = meta_por_uid.get(uid) or {}
            subject_text = meta.get("subject") or _imap_decode_header_value(msg.get("Subject"))
            body_text, html_raw = _extraer_cuerpo_y_html_msg(msg)
            text_to_check = f"{subject_text} {body_text}".lower()
            if not any(kw.lower() in text_to_check for kw in KEYWORDS_INVITACION_FAMILIAR):
                continue
            if "cancel" in text_to_check:
                continue
            link = _extraer_enlace_invitacion_de_contenido(body_text, html_raw)
            if not link or link in links_excluir or "resetpass" in link.lower():
                continue
            recipients = meta.get("recipients") or " ".join([
                (msg.get("To") or ""),
                (msg.get("Delivered-To") or ""),
                (msg.get("X-Original-To") or ""),
                (msg.get("X-Forwarded-To") or ""),
            ]).lower()
            cuerpo = f"{subject_text}\n{body_text}\n{html_raw}"
            score = _puntuar_invitacion_para_alias(alias, recipients, cuerpo)
            if score < 85:
                continue
            hallados[alias] = {
                "uid": uid,
                "recipients": recipients,
                "body": cuerpo,
                "link": link,
                "score": score,
            }
            uids_excluir.add(uid)
            links_excluir.add(link)
            break
    return hallados


def _buscar_invitacion_por_alias_exacto(
    gmail_user: str,
    alias: str,
    max_age_minutes: int = 1440,
    uids_excluir: set[int] | None = None,
    links_excluir: set[str] | None = None,
) -> dict | None:
    """Wrapper: abre sesión y busca un solo alias (compatibilidad)."""
    user_real, app_pwd = obtener_credenciales_imap_reales(gmail_user)
    if not user_real or not app_pwd:
        return None
    with sesion_imap(user_real, app_pwd) as mail:
        found = _buscar_invitaciones_dirigidas_lote(
            mail,
            [alias],
            max_age_minutes=max_age_minutes,
            uids_excluir=uids_excluir,
            links_excluir=links_excluir,
        )
    return found.get((alias or "").strip().lower())


# Ventana por defecto para invitaciones familiares (opción 4): 48 h.
MAX_AGE_INVITACION_FAMILIAR_MIN = 2880


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
    max_age = MAX_AGE_INVITACION_FAMILIAR_MIN

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
            # Dirigido primero (rápido); fallback al helper genérico.
            dirigido = _buscar_invitacion_por_alias_exacto(
                solo, solo, max_age_minutes=max_age
            )
            enlace = (dirigido or {}).get("link") if dirigido else None
            if not enlace or "resetpass" in (enlace or "").lower():
                enlace = obtener_codigo_via_imap(
                    gmail_user=solo,
                    required_keywords=KEYWORDS_INVITACION_FAMILIAR,
                    query_exclude="cancel",
                    solo_link=True,
                    max_age_minutes=max_age,
                    aliases_solo=[solo],
                    exigir_destinatario_exacto=True,
                )
            if enlace and "resetpass" not in (enlace or "").lower():
                asignados[solo] = enlace
            elif enlace:
                print(f"    {Color.WARNING}[IMAP]{Color.ENDC} Se ignoró un enlace resetpass "
                      f"para {solo} (no es invitación familiar).")
            continue

        n_alias = len(aliases_unicos)
        pendientes = list(aliases_unicos)
        uids_usados: set[int] = set()
        links_usados: set[str] = set()
        invitaciones: list[dict] = []

        def _asignar(alias: str, uid: int, link: str, score: int, tipo: str) -> None:
            if alias not in pendientes:
                return
            if uid and uid in uids_usados:
                return
            if link in links_usados:
                return
            if uid and not _reclamar_uid_correo(buzon_clave, uid):
                return
            asignados[alias] = link
            pendientes.remove(alias)
            if uid:
                uids_usados.add(uid)
            links_usados.add(link)
            color = Color.GREEN if score >= 85 else Color.WARNING
            print(f"    {color}[IMAP]{Color.ENDC} Invitación UID {uid} → {alias} "
                  f"({tipo}, score={score})")

        # Pasada 1 (rápida): SEARCH dirigido por alias — evita escanear 100+ invites.
        print(f"    {Color.CYAN}[IMAP]{Color.ENDC} Buzón {_norm_dots_gmail(buzon_clave)}: "
              f"{n_alias} alias — búsqueda dirigida primero "
              f"(ventana {max_age // 60} h)...")
        user_real, app_pwd = obtener_credenciales_imap_reales(aliases_unicos[0])
        if user_real and app_pwd:
            try:
                with sesion_imap(user_real, app_pwd) as mail:
                    dirigidos = _buscar_invitaciones_dirigidas_lote(
                        mail,
                        list(pendientes),
                        max_age_minutes=max_age,
                        uids_excluir=uids_usados,
                        links_excluir=links_usados,
                    )
                for alias, dirigido in dirigidos.items():
                    _asignar(
                        alias,
                        int(dirigido.get("uid") or 0),
                        dirigido.get("link") or "",
                        int(dirigido.get("score") or 95),
                        "búsqueda dirigida",
                    )
            except Exception as e_dir:
                print(f"    {Color.WARNING}[IMAP]{Color.ENDC} Búsqueda dirigida: {e_dir}")

        # Pasada 2: listado masivo solo para los que faltan (headers lote + bodies lote).
        if pendientes:
            tope_msgs = max(250, len(pendientes) * 6, 120)
            print(f"    {Color.CYAN}[IMAP]{Color.ENDC} Listado masivo para "
                  f"{len(pendientes)} pendiente(s) (hasta {tope_msgs} UIDs)...")
            invitaciones = listar_invitaciones_familiares_buzon(
                aliases_unicos[0],
                max_age_minutes=max_age,
                max_mensajes=tope_msgs,
                aliases_objetivo=list(pendientes),
            )
            if invitaciones:
                print(f"    {Color.CYAN}[IMAP]{Color.ENDC} {len(invitaciones)} candidata(s) "
                      f"en listado masivo.")
            candidatos: list[tuple[int, int, str, str]] = []
            for inv in invitaciones:
                uid = int(inv.get("uid") or 0)
                link = (inv.get("link") or "").strip()
                if not link or "resetpass" in link.lower():
                    continue
                if uid and uid in uids_usados:
                    continue
                if link in links_usados:
                    continue
                for alias in pendientes:
                    sc = _puntuar_invitacion_para_alias(
                        alias, inv.get("recipients") or "", inv.get("body") or ""
                    )
                    if sc <= 0:
                        continue
                    candidatos.append((sc, uid, alias, link))
            candidatos.sort(key=lambda x: (x[0], x[1]), reverse=True)
            for sc, uid, alias, link in candidatos:
                if not pendientes:
                    break
                if sc < 85:
                    continue
                _asignar(alias, uid, link, sc, "match exacto")

        # Pasada 3 (canónico): SOLO si queda 1 alias y exactamente 1 invitación libre
        if len(pendientes) == 1 and invitaciones:
            alias_unico = pendientes[0]
            libres = []
            for inv in invitaciones:
                uid = int(inv.get("uid") or 0)
                link = (inv.get("link") or "").strip()
                if not link or "resetpass" in link.lower():
                    continue
                if uid and uid in uids_usados:
                    continue
                if link in links_usados:
                    continue
                sc = _puntuar_invitacion_para_alias(
                    alias_unico, inv.get("recipients") or "", inv.get("body") or ""
                )
                if sc >= 40:
                    libres.append((sc, uid, link))
            if len(libres) == 1:
                sc, uid, link = libres[0]
                _asignar(alias_unico, uid, link, sc, "To: canónico (único restante)")
            elif len(libres) > 1:
                print(f"    {Color.WARNING}[IMAP]{Color.ENDC} {len(libres)} invitaciones canónicas "
                      f"para {alias_unico}; no se asigna a ciegas (riesgo de link incorrecto).")

        print(f"    {Color.CYAN}[IMAP]{Color.ENDC} Asignadas {n_alias - len(pendientes)}/{n_alias}; "
              f"pendientes {len(pendientes)}.")
        for a in pendientes:
            print(f"    {Color.FAIL}[IMAP]{Color.ENDC} Sin invitación atribuible a {a} "
                  f"(el correo de Tidal debe mencionar el alias EXACTO con puntos).")

    return asignados


def reclamar_otp_registro_para_alias(
    alias: str,
    after_email_id: int = 0,
    max_age_minutes: int = 20,
    silencioso: bool = True,
) -> str | None:
    """OTP de registro para UN alias (con puntos), bajo candado corto del buzón.

    No serializa la UI de Suscríbete: solo la lectura IMAP, con match exacto
    para que hermanos del mismo Gmail no se roben el código.
    """
    alias = (alias or "").strip().lower()
    if not alias or "@" not in alias:
        return None
    with _lock_registro_mismo_buzon(alias):
        return obtener_codigo_via_imap(
            gmail_user=alias,
            required_keywords=KEYWORDS_REGISTRO_CUENTA,
            query_exclude="cancel",
            after_email_id=after_email_id,
            max_age_minutes=max_age_minutes,
            aliases_solo=[alias],
            preferir_otp_len=6,
            exigir_destinatario_exacto=True,
            silencioso=silencioso,
        )


KEYWORDS_LOGIN_ACCESO = [
    "sign-in code", "signin code", "login code", "código de acceso", "codigo de acceso",
    "código de inicio", "codigo de inicio", "access code", "verification code",
    "código", "codigo", "code", "inici",
]


def reclamar_otp_login_para_alias(
    alias: str,
    after_email_id: int = 0,
    max_age_minutes: int = 20,
    silencioso: bool = True,
) -> str | None:
    """OTP de inicio de sesión para UN alias exacto (opción 4 / login)."""
    alias = (alias or "").strip().lower()
    if not alias or "@" not in alias:
        return None
    with _lock_registro_mismo_buzon(alias):
        return obtener_codigo_via_imap(
            gmail_user=alias,
            required_keywords=KEYWORDS_LOGIN_ACCESO,
            query_exclude="cancel",
            after_email_id=after_email_id,
            max_age_minutes=max_age_minutes,
            aliases_solo=[alias],
            preferir_otp_len=6,
            exigir_destinatario_exacto=True,
            silencioso=silencioso,
        )


def reclamar_otp_eliminacion_para_alias(
    alias: str,
    after_email_id: int = 0,
    preferir_otp_len: int | None = 5,
    max_age_minutes: int = 45,
    permitir_canonico: bool = False,
) -> str | None:
    """Obtiene el OTP de borrado para UN alias (con puntos), sin saturar IMAP.

    - Un solo hilo a la vez por buzón Gmail (evita el colapso de N ventanas en paralelo).
    - Primero match EXACTO con puntos.
    - Si permitir_canonico: To: sin puntos se reparte 1 UID libre por hilo (bajo el mismo lock).
    """
    import email as email_lib
    from email.header import decode_header
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    alias = (alias or "").strip().lower()
    if not alias or "@" not in alias:
        return None

    with _lock_registro_mismo_buzon(alias):
        # 1) Match estricto por puntos
        codigo = obtener_codigo_via_imap(
            gmail_user=alias,
            required_keywords=KEYWORDS_ELIMINACION_CUENTA,
            query_exclude=EXCLUDE_ELIMINACION_CUENTA,
            after_email_id=after_email_id,
            max_age_minutes=max_age_minutes,
            aliases_solo=[alias],
            preferir_otp_len=preferir_otp_len,
            exigir_destinatario_exacto=True,
            silencioso=True,
        )
        if codigo:
            print(f"    {Color.GREEN}[IMAP]{Color.ENDC} OTP eliminación (exacto) → {alias}: {codigo}")
            return codigo

        if not permitir_canonico:
            return None

        # 2) Listar OTPs de borrado recientes y atribuir (exacto fuerte o canónico libre)
        user_real, app_pwd = obtener_credenciales_imap_reales(alias)
        if not user_real or not app_pwd:
            return None
        buzon_clave = _buzon_imap_clave(alias, user_real)
        max_age_s = max(60, int((max_age_minutes or 45) * 60))
        candidatos: list[dict] = []
        try:
            with sesion_imap(user_real, app_pwd) as mail:
                if after_email_id and int(after_email_id) > 0:
                    criteria = f'(UID {int(after_email_id) + 1}:* FROM "tidal")'
                else:
                    criteria = '(FROM "tidal")'
                status, messages = mail.uid("search", None, criteria)
                if status != "OK" or not messages or not messages[0]:
                    if after_email_id:
                        status, messages = mail.uid("search", None, '(FROM "tidal")')
                if status != "OK" or not messages or not messages[0]:
                    return None
                msg_ids = messages[0].split()[-40:]
                msg_ids.reverse()
                for msg_id in msg_ids:
                    try:
                        uid = int(msg_id)
                    except ValueError:
                        uid = 0
                    with _IMAP_UIDS_LOCK:
                        if uid and uid in _IMAP_UIDS_RECLAMADOS.get(buzon_clave, set()):
                            continue
                    status, msg_data = mail.uid("fetch", msg_id, "(RFC822)")
                    if status != "OK" or not msg_data or not msg_data[0]:
                        continue
                    msg = email_lib.message_from_bytes(msg_data[0][1])
                    # Antigüedad / baseline
                    is_newer = (not after_email_id) or (uid > int(after_email_id))
                    is_recent = False
                    try:
                        date_str = msg.get("Date")
                        if date_str:
                            age = (datetime.now(timezone.utc) - parsedate_to_datetime(date_str).astimezone(timezone.utc)).total_seconds()
                            is_recent = age <= max_age_s
                    except Exception:
                        pass
                    if not (is_newer or is_recent):
                        continue
                    subject_text = ""
                    try:
                        subject_header = msg.get("Subject")
                        if subject_header:
                            parts = []
                            for part_bytes, charset in decode_header(subject_header):
                                if isinstance(part_bytes, bytes):
                                    parts.append(part_bytes.decode(charset or "utf-8", errors="replace"))
                                else:
                                    parts.append(part_bytes or "")
                            subject_text = "".join(parts)
                    except Exception:
                        pass
                    body_text, _html = _extraer_cuerpo_y_html_msg(msg)
                    text_to_check = f"{subject_text} {body_text}"
                    if not any(kw.lower() in text_to_check.lower() for kw in KEYWORDS_ELIMINACION_CUENTA):
                        continue
                    if _texto_excluido_por_frases(text_to_check, EXCLUDE_ELIMINACION_CUENTA):
                        continue
                    codigos = _extraer_codigos_otp(text_to_check, preferir_len=preferir_otp_len)
                    if not codigos:
                        continue
                    recipients = " ".join([
                        (msg.get("To") or ""),
                        (msg.get("Delivered-To") or ""),
                        (msg.get("X-Original-To") or ""),
                        (msg.get("X-Forwarded-To") or ""),
                    ]).lower()
                    score = _puntuar_invitacion_para_alias(alias, recipients, body_text)
                    # Exacto en cuerpo/To sube score
                    if _destinatario_es_para_alias(alias, recipients, body_text, exigir_exacto=True):
                        score = max(score, 95)
                    elif _destinatario_es_para_alias(alias, recipients, body_text, exigir_exacto=False):
                        score = max(score, 40)
                    else:
                        # Sin rastro del alias: NO atribuir (score 10 robaba OTPs ajenos →
                        # código incorrecto → Tidal vuelve a /profile sin borrar).
                        continue
                    if score < 40:
                        continue
                    candidatos.append({
                        "uid": uid,
                        "otp": codigos[0],
                        "score": score,
                        "subject": subject_text[:60],
                    })
        except Exception as e:
            print(f"    {Color.WARNING}[IMAP]{Color.ENDC} Error listando OTP eliminación para {alias}: {e}")
            return None

        if not candidatos:
            return None

        # Preferir match fuerte (>=85); canónico solo con score >= 40 (mismo buzón + To/cuerpo)
        fuertes = [c for c in candidatos if c["score"] >= 85]
        pool = fuertes if fuertes else [c for c in candidatos if c["score"] >= 40]
        if not pool:
            return None
        for c in sorted(pool, key=lambda x: (-x["score"], -x["uid"])):
            if c["uid"] and not _reclamar_uid_correo(buzon_clave, c["uid"]):
                continue
            modo = "exacto" if c["score"] >= 85 else "canónico"
            print(f"    {Color.GREEN}[IMAP]{Color.ENDC} OTP eliminación ({modo}, score={c['score']}) "
                  f"UID {c['uid']} → {alias}: {c['otp']} ({c['subject']!r})")
            return c["otp"]
        return None


def _imap_since_str(max_age_minutes: int | None) -> str:
    """Fecha IMAP SINCE en inglés (strftime %b depende del locale y Gmail la rechaza)."""
    from datetime import datetime, timedelta, timezone
    mins = max(15, int(max_age_minutes or 15))
    dt = datetime.now(timezone.utc) - timedelta(minutes=mins, days=1)
    meses = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{dt.day:02d}-{meses[dt.month - 1]}-{dt.year}"


def _imap_aliases_to_dirigido(aliases_ok: list[str], user_real: str) -> list[str]:
    """Aliases cuyo To: se puede buscar en el servidor (dominio catch-all, no Gmail)."""
    out: list[str] = []
    vistos: set[str] = set()
    for a in aliases_ok or []:
        a = (a or "").strip().lower()
        if not a or "@" not in a or a in vistos:
            continue
        dom = a.split("@", 1)[1]
        if "gmail.com" in dom or "googlemail.com" in dom:
            continue
        vistos.add(a)
        out.append(a)
    return out


def _imap_uid_search(
    mail,
    query_from: str,
    aliases_ok: list[str],
    user_real: str,
    after_email_id: int = 0,
    max_age_minutes: int = 15,
) -> tuple[list, bool]:
    """UIDs de Tidal. dirigido=True si filtró por To: del alias catch-all."""
    query_from = (query_from or "tidal").strip() or "tidal"
    since = _imap_since_str(max_age_minutes)
    after = int(after_email_id or 0)
    dirigidos = _imap_aliases_to_dirigido(aliases_ok, user_real)

    def _try(*parts):
        try:
            status, messages = mail.uid("search", None, *parts)
            if status == "OK" and messages and messages[0]:
                return messages[0].split()
        except Exception:
            return None
        return None

    newer = "newer_than:1d"
    if max_age_minutes and int(max_age_minutes) >= 1440:
        newer = "newer_than:7d"
    elif max_age_minutes and int(max_age_minutes) >= 120:
        newer = "newer_than:2d"

    for alias in dirigidos[:8]:
        uids = _try("X-GM-RAW", f"from:{query_from} to:{alias} {newer}")
        if uids:
            return uids, True
        uids = _try(f'(FROM "{query_from}" TO "{alias}" SINCE {since})')
        if uids:
            return uids, True
        uids = _try(f'(FROM "{query_from}" TO "{alias}")')
        if uids:
            return uids, True

    if after > 0:
        uids = _try(f'(UID {after + 1}:* FROM "{query_from}" SINCE {since})')
        if uids:
            return uids, False
        uids = _try(f'(UID {after + 1}:* FROM "{query_from}")')
        if uids:
            return uids, False
    uids = _try(f'(FROM "{query_from}" SINCE {since})')
    if uids:
        return uids, False
    uids = _try(f'(FROM "{query_from}")')
    if uids:
        return uids, False
    return [], False


def obtener_codigo_via_imap(gmail_user="cakeseller1234@gmail.com", gmail_app_password=None, 
                             query_from="tidal", required_keywords=None, query_exclude=None, 
                             max_age_minutes=15, after_email_id=0, solo_link=False,
                             aliases_extra=None, preferir_otp_len: int | None = None,
                             exigir_destinatario_exacto: bool = False,
                             aliases_solo: list | None = None,
                             silencioso: bool = False) -> str | None:
    """Lee correos de Gmail via IMAP sin necesidad de abrir el navegador.
    Requiere una 'App Password' de Google.
    Busca dinámicamente en passwords.txt la contraseña específica del correo.

    aliases_extra: otros correos/alias que también cuentan como destinatario válido
    (p. ej. nombre de acceso + correo registrado en opción 15).
    preferir_otp_len: 5 o 6 para priorizar esa longitud (p. ej. 6 cajas en eliminación).
    exigir_destinatario_exacto: si True, no aceptar To: canónico sin puntos cuando el alias
    pedido lleva puntos (crítico en opción 15 con hermanos del mismo buzón).
    aliases_solo: si se pasa, SOLO esos alias cuentan como destinatario (no el login IMAP
    del buzón). Evita que getmushroom1052@ (sin puntos) robe el OTP de get.mushroom.1052
    al sondear el mismo u otro Gmail.
    """
    import imaplib
    import email
    from email.header import decode_header
    from datetime import datetime, timezone
    
    user_real, app_pwd = obtener_credenciales_imap_reales(gmail_user)
    if not user_real or not app_pwd:
        if not silencioso:
            print(f"    {Color.WARNING}[IMAP]{Color.ENDC} No se encontraron credenciales de IMAP válidas para {gmail_user}.")
        return None

    aliases_ok = []
    origen_aliases = aliases_solo if aliases_solo is not None else [gmail_user, *(aliases_extra or [])]
    for a in origen_aliases:
        a_l = (a or "").strip().lower()
        if a_l and a_l not in aliases_ok:
            aliases_ok.append(a_l)
    if not aliases_ok:
        aliases_ok = [(gmail_user or "").strip().lower()]
    
    # ExitStack en lugar de un 'with' anidado para no reindentar todo el recorrido de mensajes:
    # el cierre queda garantizado igualmente al salir de la función.
    stack = contextlib.ExitStack()
    try:
        if not silencioso:
            alias_hint = aliases_ok[0] if aliases_ok else gmail_user
            print(f"    {Color.CYAN}[IMAP]{Color.ENDC} Consultando {_servidor_imap_para(user_real)} "
                  f"({user_real}) por {alias_hint}...")
        mail = stack.enter_context(sesion_imap(user_real, app_pwd))

        # Catch-all: buscar por To: del alias (Gmail X-GM-RAW / TO) + SINCE.
        # Antes era FROM tidal de TODO el buzón y FETCH de 50 RFC822 → minutos.
        msg_ids, dirigido = _imap_uid_search(
            mail, query_from, aliases_ok, user_real,
            after_email_id=after_email_id, max_age_minutes=max_age_minutes,
        )
        if not msg_ids:
            if not silencioso:
                print(f"    {Color.WARNING}[IMAP]{Color.ENDC} No se encontraron correos de '{query_from}'.")
            return None

        limite_msgs = 20 if dirigido else 50
        if dirigido:
            if solo_link or (max_age_minutes and int(max_age_minutes) >= 120):
                limite_msgs = 80
        else:
            if solo_link or (max_age_minutes and int(max_age_minutes) >= 120):
                limite_msgs = 400
            if max_age_minutes and int(max_age_minutes) >= 1440:
                limite_msgs = 500
        msg_ids = msg_ids[-limite_msgs:]
        msg_ids.reverse()
        if not silencioso:
            if dirigido and aliases_ok:
                filtro = f"to:{aliases_ok[0]}"
            else:
                filtro = f"from:{query_from} reciente"
            print(f"    {Color.CYAN}[IMAP]{Color.ENDC} {len(msg_ids)} mensaje(s) ({filtro})...")
        
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
            date_parsed = False
            try:
                from email.utils import parsedate_to_datetime
                date_str = msg.get("Date")
                if date_str:
                    msg_date = parsedate_to_datetime(date_str)
                    now_tz = datetime.now(timezone.utc)
                    age_seconds = (now_tz - msg_date.astimezone(timezone.utc)).total_seconds()
                    date_parsed = True
                    if age_seconds <= max_age_s:
                        is_recent_age = True
            except Exception:
                pass
                
            if not (is_newer_id or is_recent_age):
                # Ignorar correos antiguos
                continue
            # Enlaces (reset/invite) sin baseline: exigir frescura real (reset caduca ~10 min)
            if after_email_id == 0 and solo_link and date_parsed and not is_recent_age:
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
            match_headers = any(
                _destinatario_es_para_alias(
                    a, recipients, "", exigir_exacto=exigir_destinatario_exacto
                ) for a in aliases_ok
            )
            
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

            if not match_headers and not any(
                _destinatario_es_para_alias(
                    a, recipients, body_text, exigir_exacto=exigir_destinatario_exacto
                ) for a in aliases_ok
            ):
                continue

            buzon_clave = _buzon_imap_clave(gmail_user, user_real)
            # Saltar UIDs que otro hilo concurrente ya consumió (mismo buzón, otro alias)
            with _IMAP_UIDS_LOCK:
                if msg_id_int and msg_id_int in _IMAP_UIDS_RECLAMADOS.get(buzon_clave, set()):
                    continue
            
            text_to_check = f"{subject_text} {body_text}"
            
            # Verificar keywords requeridas
            if required_keywords:
                text_l = text_to_check.lower()
                cumple = any((kw or "").lower() in text_l for kw in required_keywords)
                # Reset ES: asunto "Restablecer tu contraseña Tidal" (sin "de") + URL resetpass
                if not cumple and solo_link:
                    es_kw_reset = any(
                        any(x in (kw or "").lower() for x in (
                            "resetpass", "reset", "restablec", "restaurar", "password", "contrase",
                        ))
                        for kw in required_keywords
                    )
                    if es_kw_reset and (
                        "login.tidal.com/resetpass/" in text_l
                        or "restablecer tu contraseña" in text_l
                        or "restablecer tu contrasena" in text_l
                        or "restaurar su contraseña" in text_l
                        or "restaurar su contrasena" in text_l
                        or "resetting your tidal password" in text_l
                    ):
                        cumple = True
                if not cumple:
                    continue
            
            # Verificar exclusion (una frase o lista)
            if query_exclude and _texto_excluido_por_frases(text_to_check, query_exclude):
                continue
            
            # Buscar enlaces: si son keywords de invitación familiar, usar extractor estricto
            # (nunca resetpass / footers genéricos que mezclaban links en la opción 4).
            es_busqueda_invitacion = bool(
                required_keywords
                and any(
                    (kw or "").lower() in (
                        "invites you to join", "welcome to the family", "family plan",
                        "plan familiar", "te ha invitado", "join their tidal family",
                    )
                    or "invit" in (kw or "").lower()
                    or "family" in (kw or "").lower()
                    for kw in (required_keywords or [])
                )
            )
            es_busqueda_reset = bool(
                required_keywords
                and any(
                    any(x in (kw or "").lower() for x in (
                        "resetpass", "resetting your", "restablecer tu", "restaurar su",
                        "link to reset", "reset your password",
                    ))
                    for kw in required_keywords
                )
            )
            if es_busqueda_invitacion:
                html_para_link = ""
                try:
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/html":
                                html_para_link = part.get_payload(decode=True).decode(
                                    "utf-8", errors="replace"
                                )
                                break
                    elif msg.get_content_type() == "text/html":
                        html_para_link = msg.get_payload(decode=True).decode(
                            "utf-8", errors="replace"
                        )
                except Exception:
                    html_para_link = ""

                link_inv = _extraer_enlace_invitacion_de_contenido(body_text, html_para_link)
                if link_inv:
                    if not _reclamar_uid_correo(buzon_clave, msg_id_int):
                        continue
                    if not silencioso:
                        print(f"    {Color.GREEN}[IMAP]{Color.ENDC} Enlace invitación para "
                              f"{gmail_user} (UID {msg_id_int}).")
                    return link_inv
                # Sin enlace de family válido en este mensaje: seguir al siguiente UID
                if solo_link:
                    continue

            # Buscar codigo OTP (juntos o partidos por HTML) en asunto + cuerpo
            if not solo_link:
                codigos = _extraer_codigos_otp(text_to_check, preferir_len=preferir_otp_len)
                if codigos:
                    if not _reclamar_uid_correo(buzon_clave, msg_id_int):
                        continue
                    if not silencioso:
                        print(f"    {Color.GREEN}[IMAP]{Color.ENDC} Código para {gmail_user} "
                              f"(UID {msg_id_int}, OTP={codigos[0]}, asunto: {(subject_text or '')[:60]!r}).")
                    return codigos[0]
                # Keywords OK pero sin OTP: útil para diagnosticar HTML partido / baseline
                if not silencioso:
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
                            if inner_text.startswith("http") and (
                                "login.tidal.com/resetpass/" in inner_lower
                                or "login.tidal.com/family/" in inner_lower
                            ):
                                # En búsquedas de invitación, ignorar resetpass
                                if es_busqueda_invitacion and "resetpass" in inner_lower:
                                    continue
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
                            href_l = (href or "").lower()
                            if es_busqueda_invitacion and "resetpass" in href_l:
                                continue
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
                if es_busqueda_invitacion and "resetpass" in link_lower:
                    continue
                if "login.tidal.com/resetpass/" in link_lower or "login.tidal.com/family/" in link_lower or "/accept/" in link_lower or "/join/" in link_lower:
                    if es_busqueda_invitacion and "resetpass" in link_lower:
                        continue
                    if not _reclamar_uid_correo(buzon_clave, msg_id_int):
                        break
                    return link
            
            # Prioridad 2: Fallback a cualquier otro enlace dinámico de Tidal (incluyendo tracking click/ablink)
            # En invitaciones/reset NO usar este fallback (mezcla footers / otros mails).
            if not es_busqueda_invitacion and not es_busqueda_reset:
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


def headless_forzado_por_entorno() -> bool:
    """True si TIDAL_HEADLESS=1/true/yes (p. ej. servicio systemd en Contabo)."""
    v = (os.environ.get("TIDAL_HEADLESS") or "").strip().lower()
    return v in ("1", "true", "yes", "y", "si", "sí")


def kwargs_launch_persistent(profile_dir, *, headless: bool = False) -> dict:
    """Kwargs comunes para launch_persistent_context (PC Windows o VPS Linux headless)."""
    usar_headless = bool(headless) or headless_forzado_por_entorno()
    es_linux = sys.platform.startswith("linux")
    args = list(CHROME_SILENT_ARGS)
    # Contabo/VPS como root: Chromium EXIGE --no-sandbox. No meterlo en ignore_default_args.
    ignore = ["--enable-automation"]
    if usar_headless or es_linux:
        for extra in (
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ):
            if extra not in args:
                args.append(extra)
    else:
        # Windows (histórico): no forzar el default de Playwright
        ignore.append("--no-sandbox")

    kwargs = {
        "user_data_dir": str(profile_dir),
        "headless": usar_headless,
        "args": args,
        "ignore_default_args": ignore,
        "viewport": {"width": 1280, "height": 800},
        "locale": "es-ES",
    }
    # Desactiva sandbox a nivel Playwright (root en Linux)
    if usar_headless or es_linux:
        kwargs["chromium_sandbox"] = False
    # En Windows preferir Chrome instalado; en Linux/VPS usar Chromium de Playwright.
    if sys.platform == "win32":
        kwargs["channel"] = "chrome"
    return kwargs

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

        patrones = [
            "nos aseguramos de que", "nos dirigimos a usted", "no a un robot",
            "desliza hacia la derecha",
            "making sure you are not a robot", "not a robot", "slide to right",
            "verify you are human", "confirmar que eres humano",
            "access denied", "error code 1020", "unusual activity",
            "bot detection", "acceso está restringido", "acceso restringido", "restringido temporalmente",
            "comportamiento del navegador nos ha intrigado",
            "something about your browser", "triggered this security",
            "datadome", "cf-challenge", "just a moment",
        ]

        # PRIMERO el texto antibot: el interstitial de Tidal a veces deja inputs en el DOM
        # y el early-return por formulario ocultaba el bloqueo (opción 4 no rotaba IP).
        for frame in page.frames:
            try:
                body_text = frame.evaluate("() => document.body ? document.body.innerText : ''")
                if body_text:
                    body_text_lower = body_text.lower()
                    for pat in patrones:
                        if pat in body_text_lower:
                            print(f"  [Anti-bot DEBUG] Detectado bloqueo/error de IP por patrón: '{pat}'")
                            return True

                    if re.search(
                        r"403\s*ERROR|generated by cloudfront|request blocked|access denied|"
                        r"error code 1020|ray id|not a robot|no a un robot",
                        body_text,
                        re.I,
                    ):
                        print("  [Anti-bot DEBUG] Detectado bloqueo por body text en frame")
                        return True
            except Exception:
                continue

        try:
            titulo = page.title()
            if re.search(
                r"403|request could not be satisfied|access denied|attention required|"
                r"security|blocked|datadome|robot",
                titulo,
                re.I,
            ):
                print(f"  [Anti-bot DEBUG] Detectado por TITULO: '{titulo}'")
                return True
        except Exception:
            pass

        # Error genérico de login: también bloquea el avance, pero se recupera distinto
        if es_pantalla_error_login_tidal(page):
            print("  [Anti-bot DEBUG] Detectado error genérico de login.tidal.com ('Algo salió mal')")
            return True

        # Solo si NO hay señales de antibot: formulario visible ⇒ página usable
        if _formulario_login_visible(page):
            return False
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

def boton_eliminar_cuenta_habilitado(page):
    """Localiza el botón rosa del asistente ('delete-button') habilitado.

    NO usa enlaces genéricos 'Eliminar cuenta' del menú/sidebar: esos están siempre
    clicables y al pulsarlos se sale del paso de verificación hacia el inicio sin borrar.
    """
    page = pagina_vigente(page)
    for frame in page.frames:
        try:
            loc = frame.locator("button.delete-button")
            cnt = loc.count()
            for idx in range(cnt):
                btn = loc.nth(idx)
                try:
                    if not btn.is_visible():
                        continue
                    if btn.is_disabled():
                        continue
                    # Preferir el CTA ancho del asistente
                    return btn
                except Exception:
                    continue
        except Exception:
            continue
        # Fallback estricto: botón cuyo texto exacto es Eliminar cuenta / Delete account
        # y que NO esté en el nav lateral (sin .delete-button a veces en A/B).
        try:
            clicked_info = frame.evaluate("""() => {
                const isVisible = (e) => {
                    const st = window.getComputedStyle(e);
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    const r = e.getBoundingClientRect();
                    return r.width > 40 && r.height > 20;
                };
                const inNav = (e) => !!(e.closest('nav, aside, [class*="sidebar"], [class*="SideBar"], header'));
                const cand = Array.from(document.querySelectorAll('button'))
                    .filter(b => {
                        if (b.disabled || !isVisible(b) || inNav(b)) return false;
                        const t = (b.textContent || '').trim().toLowerCase();
                        return t === 'eliminar cuenta' || t === 'delete account';
                    });
                if (!cand.length) return null;
                // Preferir fullwidth / delete-button
                cand.sort((a, b) => {
                    const score = (el) => (el.classList.contains('delete-button') ? 0 : 1)
                        + (el.classList.contains('fullwidth') ? 0 : 1);
                    return score(a) - score(b);
                });
                const el = cand[0];
                el.setAttribute('data-tidal-del-btn', '1');
                return true;
            }""")
            if clicked_info:
                btn = frame.locator('button[data-tidal-del-btn="1"]').first
                if btn.count() > 0 and btn.is_visible() and not btn.is_disabled():
                    return btn
        except Exception:
            continue
    return None


def esperar_boton_eliminar_cuenta_habilitado(page, timeout_s: float = 12.0):
    """Espera a que el botón delete-button del asistente se habilite tras un OTP válido."""
    limite = time.time() + max(1.0, float(timeout_s))
    while time.time() < limite:
        page = pagina_vigente(page)
        btn = boton_eliminar_cuenta_habilitado(page)
        if btn:
            return btn
        time.sleep(0.35)
    return None


def clic_confirmar_eliminacion_asistente(page) -> bool:
    """Pulsa el CTA real del asistente de borrado (nunca el enlace del menú)."""
    page = pagina_vigente(page)
    btn = boton_eliminar_cuenta_habilitado(page)
    if btn:
        try:
            btn.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            btn.click(timeout=8000)
            return True
        except Exception:
            try:
                btn.click(force=True, timeout=5000)
                return True
            except Exception:
                pass
    # JS: solo button.delete-button enabled, fuera del nav
    try:
        return bool(page.evaluate("""() => {
            const isVisible = (e) => {
                const st = window.getComputedStyle(e);
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                const r = e.getBoundingClientRect();
                return r.width > 40 && r.height > 20;
            };
            const inNav = (e) => !!(e.closest('nav, aside, [class*="sidebar"], [class*="SideBar"], header'));
            const btns = Array.from(document.querySelectorAll('button.delete-button, button'))
                .filter(b => {
                    if (b.disabled || !isVisible(b) || inNav(b)) return false;
                    if (b.classList.contains('delete-button')) return true;
                    const t = (b.textContent || '').trim().toLowerCase();
                    return t === 'eliminar cuenta' || t === 'delete account';
                });
            if (!btns.length) return false;
            btns.sort((a, b) => (b.classList.contains('delete-button') ? 1 : 0)
                - (a.classList.contains('delete-button') ? 1 : 0));
            btns[0].click();
            return true;
        }"""))
    except Exception:
        return False


def url_parece_exito_o_fin_eliminacion(url: str) -> bool:
    u = (url or "").lower()
    if not u:
        return False
    if "account-deleted" in u or "deletion-success" in u or "deleted=true" in u:
        return True
    if "login.tidal.com" in u or "/authorize" in u:
        return True
    return False


def url_parece_abandono_eliminacion(url: str) -> bool:
    """True si salimos del asistente hacia el inicio/overview sin completar el borrado."""
    u = (url or "").lower()
    if not u:
        return False
    if "account-deletion" in u:
        return False
    if url_parece_exito_o_fin_eliminacion(u):
        return False
    # tidal.com marketing / overview de cuenta = se salió del wizard
    if u.rstrip("/") in ("https://tidal.com", "http://tidal.com", "https://www.tidal.com"):
        return True
    if "tidal.com" in u and "account.tidal.com" not in u and "login.tidal.com" not in u:
        return True
    if "account.tidal.com" in u and any(x in u for x in (
        "/profile", "/subscription", "/overview", "/payment", "/family", "/store"
    )):
        return True
    if u.rstrip("/").endswith("account.tidal.com"):
        return True
    return False

def contar_cajas_otp_visibles(page) -> int:
    """Cuenta inputs OTP visibles (maxlength=1 / one-time-code) en la página actual."""
    try:
        page = pagina_vigente(page)
        n = page.evaluate("""() => {
            const esOtp = (el) => {
                const max = (el.getAttribute('maxlength') || '').trim();
                const ac = (el.autocomplete || '').toLowerCase();
                const mode = (el.inputMode || '').toLowerCase();
                const name = (el.name || '').toLowerCase();
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') < 0.1)
                    return false;
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) return false;
                // Solo cajas de un dígito (asistente de eliminación Tidal) o one-time-code único
                return max === '1' || ac === 'one-time-code'
                    || (mode === 'numeric' && max === '1')
                    || ((name.includes('code') || name.includes('otp')) && max === '1');
            };
            return Array.from(document.querySelectorAll('input')).filter(esOtp).length;
        }""")
        return int(n or 0)
    except Exception:
        return 0


def leer_otp_cajas_visibles(page) -> str:
    """Lee el valor concatenado de las cajas OTP visibles."""
    try:
        page = pagina_vigente(page)
        return page.evaluate("""() => {
            const esOtp = (el) => {
                const max = (el.getAttribute('maxlength') || '').trim();
                const ac = (el.autocomplete || '').toLowerCase();
                const mode = (el.inputMode || '').toLowerCase();
                const name = (el.name || '').toLowerCase();
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') < 0.1)
                    return false;
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) return false;
                return max === '1' || ac === 'one-time-code'
                    || (mode === 'numeric' && max === '1')
                    || ((name.includes('code') || name.includes('otp')) && max === '1');
            };
            return Array.from(document.querySelectorAll('input'))
                .filter(esOtp)
                .map(el => (el.value || '').trim())
                .join('');
        }""") or ""
    except Exception:
        return ""


def escribir_codigo_verificacion_inteligente(page, codigo: str) -> bool:
    """Ingresa un código de verificación (una caja o N cajas OTP). Exige lectura == código.

    Crítico en eliminación (opción 15): hay que rellenar TODAS las cajas visibles.
    Nunca acepta éxito solo porque JS escribió en el DOM (React dejaría el botón disabled).
    """
    codigo = re.sub(r"\D", "", str(codigo or ""))
    if not codigo:
        return False

    page = pagina_vigente(page)
    n_cajas = contar_cajas_otp_visibles(page)
    codigos_a_probar = [codigo]
    if n_cajas >= 4:
        if len(codigo) > n_cajas:
            codigos_a_probar = [codigo[:n_cajas]]
        elif len(codigo) < n_cajas:
            pad = ("0" * (n_cajas - len(codigo))) + codigo
            codigos_a_probar = [pad, codigo]
            print(f"  [OTP] Código de {len(codigo)} dígitos con {n_cajas} cajas; "
                  f"se probará también '{pad}'.")

    for codigo_try in codigos_a_probar:
        if _escribir_codigo_otp_intento(page, codigo_try):
            return True
    return False


def _escribir_codigo_otp_intento(page, codigo: str) -> bool:
    """Un intento de escritura OTP (longitud fija)."""
    codigo = re.sub(r"\D", "", str(codigo or ""))
    if not codigo:
        return False
    page = pagina_vigente(page)

    for frame in page.frames:
        try:
            code_inputs = []
            for _poll in range(10):
                inputs = frame.locator('input').all()
                code_inputs = []
                otp_estrictos = []
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
                        es_otp_estricto = (
                            maxlength == "1"
                            or autocomplete == "one-time-code"
                            or "code" in name
                            or "otp" in name
                        )
                        es_candidato = (
                            es_otp_estricto
                            or mode == "numeric"
                            or "code" in placeholder
                            or "código" in placeholder
                            or "codigo" in placeholder
                            or "digit" in aria
                            or "código" in aria
                            or "codigo" in aria
                            or type_attr in ["", "text", "number", "tel", "password"]
                        )
                        if es_candidato:
                            code_inputs.append(ip)
                            if es_otp_estricto:
                                otp_estrictos.append(ip)
                    except Exception:
                        pass
                if len(otp_estrictos) >= min(4, len(codigo)):
                    code_inputs = otp_estrictos
                elif len(otp_estrictos) == 1 and len(codigo) >= 4:
                    code_inputs = otp_estrictos
                # Códigos de registro (≥4): exigir cajas OTP reales antes de salir del poll
                if len(codigo) >= 4:
                    if len(otp_estrictos) >= min(4, len(codigo)) or (
                        len(otp_estrictos) == 1 and len(codigo) >= 4
                    ):
                        code_inputs = otp_estrictos
                        break
                elif len(code_inputs) >= min(4, len(codigo)) or (
                    code_inputs and len(codigo) <= 8 and len(code_inputs) == 1
                ):
                    break
                time.sleep(0.35)

            if not code_inputs:
                continue

            # Códigos largos (registro/login Tidal): NO usar inputs genéricos type=text.
            # Si aún no hay cajas OTP estrictas, fallar rápido para que el caller espere la UI.
            if len(codigo) >= 4:
                if len(otp_estrictos) >= min(4, len(codigo)):
                    code_inputs = otp_estrictos
                elif len(otp_estrictos) == 1:
                    code_inputs = otp_estrictos
                else:
                    # Sin cajas OTP reales todavía → no inventar con campos sueltos
                    continue
            elif len(otp_estrictos) >= 1:
                code_inputs = otp_estrictos

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

            def _exito_completo(objetivos, esperado: str) -> bool:
                if leer_cajas(objetivos) == esperado:
                    return True
                return leer_otp_cajas_visibles(page) == esperado

            # Varias cajas OTP
            if len(code_inputs) >= 4:
                if len(code_inputs) < len(codigo):
                    continue
                objetivos = code_inputs[:len(codigo)]

                # 1) Teclado humano (mejor con React)
                limpiar_cajas(code_inputs[:len(codigo)])
                try:
                    objetivos[0].click(timeout=800)
                except Exception:
                    pass
                time.sleep(0.15)
                try:
                    page.keyboard.type(codigo, delay=90)
                except Exception:
                    pass
                time.sleep(0.3)
                if _exito_completo(objetivos, codigo):
                    return True

                # 2) Caja por caja
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
                if _exito_completo(objetivos, codigo):
                    return True

                # 3) JS + InputEvent — éxito solo si la lectura coincide
                try:
                    ok_js = frame.evaluate(
                        """([digitos]) => {
                            const esOtp = (el) => {
                                const t = (el.type || '').toLowerCase();
                                if (['email','checkbox','radio','submit','button','file','hidden'].includes(t)) return false;
                                const max = el.getAttribute('maxlength') || '';
                                const mode = (el.inputMode || '').toLowerCase();
                                const ac = (el.autocomplete || '').toLowerCase();
                                const name = (el.name || '').toLowerCase();
                                return max === '1' || ac === 'one-time-code' || mode === 'numeric'
                                    || name.includes('code') || name.includes('otp');
                            };
                            const inputs = Array.from(document.querySelectorAll('input')).filter(el => {
                                if (!esOtp(el)) return false;
                                const r = el.getBoundingClientRect();
                                return r.width > 0 && r.height > 0;
                            });
                            if (inputs.length < digitos.length) return false;
                            const targets = inputs.slice(0, digitos.length);
                            targets.forEach((el, i) => {
                                el.focus();
                                const setter = Object.getOwnPropertyDescriptor(
                                    window.HTMLInputElement.prototype, 'value')?.set;
                                if (setter) setter.call(el, digitos[i]);
                                else el.value = digitos[i];
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                try {
                                    el.dispatchEvent(new InputEvent('input', {
                                        bubbles: true, data: digitos[i], inputType: 'insertText'
                                    }));
                                } catch (e) {}
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                el.dispatchEvent(new KeyboardEvent('keyup', {
                                    bubbles: true, key: digitos[i]
                                }));
                            });
                            return targets.every((el, i) => (el.value || '') === digitos[i]);
                        }""",
                        list(codigo),
                    )
                    if ok_js and _exito_completo(objetivos, codigo):
                        return True
                except Exception:
                    pass
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
            try:
                ok = frame.evaluate(
                    """(code) => {
                        const el = document.querySelector('input[autocomplete="one-time-code"]')
                            || document.querySelector('input[name="code"]')
                            || document.querySelector('input[inputmode="numeric"]')
                            || document.querySelector('input[maxlength="1"]');
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
                if ok and (target.input_value() or "").strip() == codigo:
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
            navegar_tidal_tolerante(self.page, "https://tidal.com/pricing", timeout_ms=18000)
            time.sleep(random.uniform(0.5, 1.0))
            aceptar_cookies_con_espera(self.page, intentos=1, pausa_s=0.15)
            time.sleep(0.2)
            try:
                navegar_tidal_tolerante(
                    self.page, "https://account.tidal.com/",
                    referer="https://tidal.com/pricing",
                    timeout_ms=18000,
                )
            except Exception:
                navegar_tidal_tolerante(
                    self.page, "https://account.tidal.com/login",
                    referer="https://tidal.com/pricing",
                    timeout_ms=18000,
                )
            time.sleep(1.0)
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
            pricing_ok = False
            for _intento_pr in range(1, 3):
                try:
                    navegar_tidal_tolerante(self.page, "https://tidal.com/pricing", timeout_ms=18000)
                    time.sleep(random.uniform(0.4, 0.8))
                    aceptar_cookies_con_espera(self.page, intentos=1, pausa_s=0.15)
                    pricing_ok = True
                    break
                except Exception as e:
                    print(f"  [Registro] [{self.client_email}] [WARN] Pricing falló "
                          f"(intento {_intento_pr}/2): {e}")
                    if self.use_proxy and es_error_proxy_o_red(e):
                        print(f"  [Registro] [{self.client_email}] Túnel/proxy caído en pricing. "
                              f"Rotando NG y reintentando...")
                        try:
                            self.ejecutar_rotacion_proxy_y_recargar()
                        except Exception:
                            pass
                        time.sleep(0.8)
                    elif _intento_pr < 2:
                        time.sleep(0.8)
            if not pricing_ok:
                print(f"  [Registro] [{self.client_email}] [WARN] Se continúa sin pricing estable.")

            print(f"  [Registro] [{self.client_email}] Cargando account.tidal.com/ con referer...")
            account_ok = False
            for _intento_acc in range(1, 3):
                try:
                    navegar_tidal_tolerante(
                        self.page, "https://account.tidal.com/",
                        referer="https://tidal.com/pricing",
                        timeout_ms=18000,
                    )
                    account_ok = True
                    break
                except Exception as e:
                    print(f"  [Registro] [{self.client_email}] [WARN] Timeout en navegación inicial "
                          f"(intento {_intento_acc}/2): {e}")
                    url_now = ""
                    try:
                        url_now = (getattr(self.page, "url", "") or "").lower()
                    except Exception:
                        pass
                    # chrome-error / ERR_TUNNEL: rotar NG antes de insistir con el mismo proxy muerto
                    if self.use_proxy and (
                        es_error_proxy_o_red(e)
                        or "chrome-error://" in url_now
                        or "chromewebdata" in url_now
                    ):
                        print(f"  [Registro] [{self.client_email}] Túnel/proxy caído al abrir cuenta. "
                              f"Rotando NG...")
                        try:
                            self.ejecutar_rotacion_proxy_y_recargar()
                        except Exception:
                            pass
                        time.sleep(0.8)
                        continue
                    # authorize?email= con Error: recuperar vía pricing (sin quemar proxy aún)
                    if es_pantalla_error_login_tidal(self.page) or url_es_oauth_login_roto(
                        getattr(self.page, "url", "") or ""
                    ):
                        try:
                            self.recuperar_login_tras_error_tidal()
                            account_ok = True
                            break
                        except Exception:
                            pass
                    if _intento_acc < 2:
                        time.sleep(0.8)
            if not account_ok:
                print(f"  [Registro] [{self.client_email}] [WARN] Se continúa tras fallos al abrir account.tidal.com.")

            email_input = esperar_locator_en_frames(self.page, ['input[type="email"]', 'input[name="email"]'], timeout_s=12.0)
            if not email_input:
                # Si falló, rotamos el proxy una única vez y reintentamos por pricing (nunca authorize)
                if self.use_proxy:
                    print(f"  [Registro] [{self.client_email}] No se localizó el campo de correo. Rotando proxy y reintentando...")
                    self.ejecutar_rotacion_proxy_y_recargar()
                    time.sleep(0.8)
                    if not _formulario_login_visible(self.page):
                        try:
                            self.recuperar_login_tras_error_tidal()
                        except Exception:
                            pass
                    email_input = esperar_locator_en_frames(self.page, ['input[type="email"]', 'input[name="email"]'], timeout_s=12.0)
                    
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

                time.sleep(0.85)
            
            # Verificar si pide contraseña (cuenta ya registrada)
            pwd_input_check = encontrar_locator_en_frames(self.page, ['input[type="password"]', 'input[name="password"]'])
            if pwd_input_check:
                print(f"  {Color.WARNING}[Registro] La cuenta {self.client_email} ya está registrada en TIDAL. Omitiendo...{Color.ENDC}")
                registro_exitoso = True
                return True

            print("  [Registro] Rellenando fecha de nacimiento (15/08/1995)...")
            # Baseline IMAP ANTES del formulario: así el clic a Suscríbete no espera al buzón.
            max_id_previo_prefetch = 0
            try:
                max_id_previo_prefetch = obtener_max_email_id(self.client_email)
            except Exception:
                max_id_previo_prefetch = 0

            # Un solo evaluate (día/mes/año + términos) para habilitar Suscríbete sin pausas largas.
            try:
                self.page.evaluate("""
                    () => {
                        const fire = (el) => {
                            if (!el) return;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        };
                        const selects = Array.from(document.querySelectorAll('select'));
                        const daySelect = document.querySelector('select[name*="day" i]') || selects[0];
                        const monthSelect = document.querySelector('select[name*="month" i]') || selects[1];
                        const yearSelect = document.querySelector('select[name*="year" i]') || selects[2];
                        if (daySelect) { daySelect.value = "15"; fire(daySelect); }
                        else {
                            const dayInput = document.querySelector('input[name*="day" i]');
                            if (dayInput) { dayInput.value = "15"; fire(dayInput); }
                        }
                        if (monthSelect) {
                            const opts = Array.from(monthSelect.options || []);
                            const targets = ["8", "08", "aug", "ago", "august", "agosto"];
                            let matched = false;
                            for (const opt of opts) {
                                const val = (opt.value || '').trim().toLowerCase();
                                const txt = (opt.textContent || '').trim().toLowerCase();
                                if (targets.some(t => val === t || txt === t || txt.includes(t))) {
                                    monthSelect.value = opt.value; fire(monthSelect); matched = true; break;
                                }
                            }
                            if (!matched && opts.length > 8) {
                                monthSelect.selectedIndex = opts.length === 13 ? 8 : 7;
                                fire(monthSelect);
                            }
                        } else {
                            const monthInput = document.querySelector('input[name*="month" i]');
                            if (monthInput) { monthInput.value = "08"; fire(monthInput); }
                        }
                        if (yearSelect) { yearSelect.value = "1995"; fire(yearSelect); }
                        else {
                            const yearInput = document.querySelector('input[name*="year" i]');
                            if (yearInput) { yearInput.value = "1995"; fire(yearInput); }
                        }
                        document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                            const parentText = cb.parentElement ? (cb.parentElement.textContent || '') : '';
                            if (/t[eé]rminos|terms|privacidad|privacy|acuerdo|agree/i.test(parentText)) {
                                if (!cb.checked) {
                                    cb.click();
                                    if (!cb.checked && cb.parentElement) cb.parentElement.click();
                                }
                            }
                        });
                    }
                """)
            except Exception as e_dob:
                print(f"  [Registro] [{self.client_email}] [WARN] Relleno rápido DOB/términos: {e_dob}")
            time.sleep(0.25)

            print("  [Registro] Marcando checkbox de términos...")
            # Reafirmar checkbox por si React no registró el clic del bloque anterior
            try:
                self.page.evaluate("""
                    () => {
                        document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                            const parentText = cb.parentElement ? (cb.parentElement.textContent || '') : '';
                            if (/t[eé]rminos|terms|privacidad|privacy|acuerdo|agree/i.test(parentText)) {
                                if (!cb.checked) {
                                    cb.click();
                                    if (!cb.checked && cb.parentElement) cb.parentElement.click();
                                }
                            }
                        });
                    }
                """)
            except Exception:
                pass
            time.sleep(0.15)
            
            # Candado IMAP corto en reclamar_otp_registro_para_alias (UI de Suscríbete en paralelo).
            max_id_previo = max_id_previo_prefetch
            if not max_id_previo:
                try:
                    max_id_previo = obtener_max_email_id(self.client_email)
                except Exception:
                    max_id_previo = 0

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
                # Clic inmediato por JS (habilita botón + click). Evita esperar locator 5s.
                clicked = False
                try:
                    clicked = bool(self.page.evaluate("""() => {
                        document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                            const parentText = cb.parentElement ? (cb.parentElement.textContent || '') : '';
                            if (/t[eé]rminos|terms|privacidad|privacy|acuerdo|agree/i.test(parentText)) {
                                if (!cb.checked) {
                                    cb.click();
                                    if (!cb.checked && cb.parentElement) cb.parentElement.click();
                                }
                            }
                        });
                        const btn = document.querySelector('button[type="submit"]') ||
                            Array.from(document.querySelectorAll('button')).find(b => {
                                const t = (b.textContent || '').toLowerCase();
                                return t.includes('suscríbete') || t.includes('suscribete')
                                    || t.includes('subscribe') || t.includes('crear cuenta')
                                    || t.includes('create account');
                            });
                        if (!btn) return false;
                        btn.disabled = false;
                        btn.removeAttribute('disabled');
                        btn.removeAttribute('aria-disabled');
                        try { btn.click(); } catch (e) {}
                        try {
                            btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                        } catch (e) {}
                        return true;
                    }"""))
                except Exception:
                    clicked = False
                if clicked:
                    return
                btn_sub = esperar_locator_en_frames(
                    self.page,
                    [
                        "button:has-text('Suscríbete')", "button:has-text('Subscribe')",
                        "button:has-text('Create account')", "button:has-text('Crear cuenta')",
                        "button[type='submit']",
                    ],
                    timeout_s=1.5
                )
                if btn_sub:
                    try:
                        btn_sub.click(force=True, timeout=1500)
                    except Exception:
                        try:
                            btn_sub.evaluate("b => { b.disabled = false; b.click(); }")
                        except Exception:
                            pass

            def _asegurar_otp_tras_suscribirse() -> bool:
                """Tras Suscríbete: esperar OTP, recuperar authorize/antirobot o reintentar clic.

                Poll corto de UI + peek IMAP (match exacto) para no esperar 12s ciegos.
                """
                _pulsar_suscribete()
                time.sleep(0.35)

                for intento_rec in range(1, 5):
                    # ~7s de poll rápido; cada ~1.4s mirar IMAP por si el correo ya llegó
                    for poll in range(20):
                        if _pantalla_otp_registro():
                            return True
                        if poll > 0 and poll % 4 == 0:
                            try:
                                codigo_previo = reclamar_otp_registro_para_alias(
                                    self.client_email,
                                    after_email_id=max_id_previo,
                                    max_age_minutes=20,
                                    silencioso=True,
                                )
                                if codigo_previo and not str(codigo_previo).startswith("http"):
                                    print(f"  [Registro] [{self.client_email}] OTP ya en IMAP "
                                          f"({codigo_previo}) aunque la UI aún no muestra Verify.")
                                    self._otp_registro_prefetch = codigo_previo
                                    return True
                            except Exception:
                                pass
                        time.sleep(0.35)

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
                          f"(intento recuperación {intento_rec}/4). URL={url_now}")
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
                            raise RuntimeError("__REINICIAR_FORMULARIO_REGISTRO__")
                    except RuntimeError:
                        raise
                    except Exception:
                        pass

                    # Peek IMAP final de este ciclo (por si el poll lo saltó)
                    try:
                        codigo_previo = reclamar_otp_registro_para_alias(
                            self.client_email,
                            after_email_id=max_id_previo,
                            max_age_minutes=20,
                            silencioso=False,
                        )
                        if codigo_previo and not str(codigo_previo).startswith("http"):
                            print(f"  [Registro] [{self.client_email}] OTP ya llegó por IMAP "
                                  f"({codigo_previo}) aunque la UI no mostró Verify. Se continúa.")
                            self._otp_registro_prefetch = codigo_previo
                            return True
                    except Exception:
                        pass

                    # Seguir en el formulario: volver a pulsar Suscríbete
                    if _sigue_en_formulario_registro():
                        print(f"  [Registro] [{self.client_email}] Sigue el formulario; "
                              f"reintentando Suscríbete...")
                        _pulsar_suscribete()
                        time.sleep(1.0)
                        continue

                    # Página rara: rotar proxy NG y reiniciar formulario
                    if self.use_proxy and intento_rec >= 2:
                        print(f"  [Registro] [{self.client_email}] Rotando proxy NG y "
                              f"reiniciando formulario de registro...")
                        try:
                            self.ejecutar_rotacion_proxy_y_recargar()
                        except Exception:
                            pass
                        raise RuntimeError("__REINICIAR_FORMULARIO_REGISTRO__")

                    time.sleep(0.8)

                return _pantalla_otp_registro() or bool(getattr(self, "_otp_registro_prefetch", None))

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
                        timeout_s=12.0
                    )
                    if not email_input:
                        try:
                            self.recuperar_login_tras_error_tidal()
                        except Exception:
                            pass
                        email_input = esperar_locator_en_frames(
                            self.page,
                            ['input[type="email"]', 'input[name="email"]', '#email'],
                            timeout_s=12.0
                        )
                    if not email_input:
                        continue
                    try:
                        email_input.fill("")
                        email_input.fill(self.client_email)
                        email_input.press("Enter")
                    except Exception:
                        pass
                    time.sleep(0.85)
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
            codigo_guardado = getattr(self, "_otp_registro_prefetch", None)
            self._otp_registro_prefetch = None

            for ronda in range(1, 5):
                if self._sesion_post_registro_detectada():
                    print(f"  [Registro] {Color.GREEN}[{self.client_email}] Sesión/cuenta ya activa. "
                          f"Continuando...{Color.ENDC}")
                    codigo_aceptado = True
                    break

                codigo = codigo_guardado
                if not codigo:
                    print(f"  [Registro] Buscando código de registro vía IMAP (ronda {ronda}/4)...")
                    for intento in range(1, 9):
                        print(f"  [Registro] Intento {intento}/8: Buscando correo...")
                        codigo = reclamar_otp_registro_para_alias(
                            self.client_email,
                            after_email_id=max_id_previo,
                            max_age_minutes=20,
                            silencioso=(intento > 1),
                        )
                        if codigo:
                            codigo_guardado = codigo
                            break
                        if intento in (3, 6) and _pantalla_otp_registro():
                            try:
                                btn_resend = esperar_locator_en_frames(
                                    self.page,
                                    [
                                        "button:has-text('Resend code')", "button:has-text('Resend')",
                                        "button:has-text('Reenviar código')", "button:has-text('Reenviar')",
                                        "a:has-text('Resend')", "a:has-text('Reenviar')",
                                    ],
                                    timeout_s=1.5,
                                )
                                if btn_resend:
                                    print(f"  [Registro] [{self.client_email}] Pulsando Resend code...")
                                    btn_resend.click(force=True)
                                    time.sleep(1.2)
                                    max_id_previo = obtener_max_email_id(self.client_email)
                            except Exception:
                                pass
                        if intento < 8:
                            if self._sesion_post_registro_detectada():
                                codigo_aceptado = True
                                break
                            print("  [Registro] Correo no encontrado aún. Esperando 1.5s...")
                            time.sleep(1.5)
                    if codigo_aceptado:
                        break
                else:
                    print(f"  [Registro] [{self.client_email}] Reutilizando OTP ya leído "
                          f"({codigo}) — reintento {ronda}/4...")

                if not codigo:
                    if self._confirmar_registro_completado(timeout_s=8.0):
                        print(f"  [Registro] {Color.GREEN}[{self.client_email}] Cuenta ya registrada "
                              f"pese a no re-leer OTP. Continuando...{Color.ENDC}")
                        codigo_aceptado = True
                        break
                    ultimo_error_codigo = (
                        "No se pudo extraer el código de verificación del correo de manera automática."
                    )
                    break

                if codigo.startswith("http"):
                    reg_page = self.context.new_page()
                    reg_page.goto(codigo)
                    time.sleep(1.2)
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
                    ],
                    timeout_s=8.0,
                )
                time.sleep(0.25)

                print(f"  [Registro] [{self.client_email}] Escribiendo código OTP ({codigo})...")
                fill_ok = False
                for intento_fill in range(1, 4):
                    if self._sesion_post_registro_detectada():
                        fill_ok = True
                        break
                    wrote = escribir_codigo_verificacion_inteligente(self.page, codigo)
                    time.sleep(0.6)
                    if self._sesion_post_registro_detectada():
                        print(f"  [Registro] [{self.client_email}] OTP aceptado por Tidal "
                              f"(sesión detectada tras el relleno).")
                        fill_ok = True
                        break
                    if wrote:
                        fill_ok = True
                        break
                    print(f"  [Registro] {Color.WARNING}[WARN] Relleno OTP falló "
                          f"(intento {intento_fill}/3)...{Color.ENDC}")
                    time.sleep(0.7)

                if not fill_ok:
                    try:
                        self.page.keyboard.press("Enter")
                    except Exception:
                        pass
                    time.sleep(0.9)
                    if self._sesion_post_registro_detectada():
                        codigo_aceptado = True
                        break
                    print(f"  [Registro] {Color.WARNING}[WARN] No se pudo rellenar las cajas OTP; "
                          f"se reintenta con el mismo código (sin buscar otro correo).{Color.ENDC}")
                    ultimo_error_codigo = "No se pudieron rellenar las cajas del código OTP."
                    time.sleep(0.5)
                    continue

                time.sleep(0.3)
                try:
                    self.page.keyboard.press("Enter")
                    time.sleep(0.5)
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
                    timeout_s=2.0
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

                time.sleep(1.4)
                if self._sesion_post_registro_detectada():
                    codigo_aceptado = True
                    print("  [Registro] Código aceptado. Esperando procesamiento de la cuenta...")
                    break

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
                    codigo_guardado = None
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
                            btn_resend.click(force=True)
                            time.sleep(1.2)
                    except Exception:
                        pass
                    max_id_previo = obtener_max_email_id(self.client_email)
                    continue

                codigo_aceptado = True
                print("  [Registro] Código enviado. Esperando procesamiento de la cuenta...")
                break

            if not codigo_aceptado:
                print(f"  [Registro] [{self.client_email}] Última comprobación: ¿la cuenta "
                      f"quedó creada aunque falló la verificación OTP?")
                if self._confirmar_registro_completado(timeout_s=10.0):
                    print(f"  [Registro] {Color.GREEN}[{self.client_email}] Sí: sesión activa. "
                          f"Se continúa.{Color.ENDC}")
                    codigo_aceptado = True
                else:
                    raise RuntimeError(
                        ultimo_error_codigo
                        or f"No se pudo verificar el correo de registro para {self.client_email}."
                    )

            print("  [Registro] Esperando redirección automática al perfil o cuenta...")
            registro_exitoso = self._confirmar_registro_completado(timeout_s=28.0)

            if registro_exitoso:
                if cerrar_navegador_al_final:
                    print(f"  {Color.GREEN}[OK] ¡Registro completado y verificado con éxito para {self.client_email}! "
                          f"Cerrando ventana automáticamente...{Color.ENDC}")
                else:
                    print(f"  {Color.GREEN}[OK] ¡Registro completado y verificado con éxito para {self.client_email}! "
                          f"Registro confirmado.{Color.ENDC}")
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
            try:
                if self.page and not self.page.is_closed() and self._confirmar_registro_completado(timeout_s=12.0):
                    print(f"  [Registro] {Color.GREEN}[{self.client_email}] La cuenta SÍ quedó registrada. "
                          f"Se ignora el error OTP y se continúa.{Color.ENDC}")
                    registro_exitoso = True
                    try:
                        cookies_proxy = self.context.cookies()
                        self.cookies_tidal = [
                            c for c in cookies_proxy if "tidal.com" in c.get("domain", "")
                        ]
                    except Exception:
                        self.cookies_tidal = []
                    return True
            except Exception:
                pass
            return False
        finally:
            if cerrar_navegador_al_final:
                # Opción 8 ya no continúa a TuneMyMusic: liberar NG y PE al cerrar.
                self.cerrar_navegador(liberar_ng=True, liberar_pe=True)
            elif registro_exitoso:
                if getattr(self, "proxy_ng_server", None):
                    try:
                        GLOBAL_NG_PROXY_POOL.liberar_proxy(self.proxy_ng_server)
                    except Exception:
                        pass
                    self.proxy_ng_server = None
                    self.proxy_ng_user = None
                    self.proxy_ng_pass = None
            try:
                # Solo limpiar perfil temporal si NO fue exitoso y el navegador ya está cerrado:
                # borrarlo con Chrome abierto dejaba el perfil a medias y bloqueado en Windows.
                if cerrar_navegador_al_final and not registro_exitoso:
                    self.limpiar_perfil_temporal()
                    if not self.main_profile.exists():
                        print(f"  [Registro] Limpiado perfil temporal por fallo: {self.main_profile}")
            except Exception as ex:
                pass

    def _sesion_post_registro_detectada(self) -> bool:
        """True si tras el OTP ya hay sesión (aunque el relleno diga fallo)."""
        try:
            self.page = pagina_vigente(self.page)
            if not self.page or self.page.is_closed():
                return False
            url = (self.page.url or "").lower()
            if self._url_indica_cuenta_activa(url):
                hay_login = False
                try:
                    hay_login = bool(encontrar_locator_en_frames(
                        self.page,
                        ['input[type="email"]', 'input[name="email"]', '#email'],
                    ))
                except Exception:
                    pass
                if not hay_login:
                    return True
            if (
                ("account.tidal.com" in url or "listen.tidal.com" in url or "tidal.com/browse" in url)
                and "login.tidal.com" not in url
                and "/authorize" not in url
            ):
                return True
        except Exception:
            pass
        return False

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
                        aceptar_cookies_con_espera(self.page, intentos=1, pausa_s=0.15)
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
            time.sleep(0.35)
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

    def run_tmm_transfer(
        self,
        event_subir_csv: threading.Event,
        cancelar_event: threading.Event | None = None,
    ) -> None:
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
            
            # Esperar ENTER del hilo principal (o cancelación)
            while True:
                if cancelar_event is not None and cancelar_event.is_set():
                    print(f"  [TuneMyMusic] [{self.client_email}] Transferencia cancelada.")
                    return
                if event_subir_csv.wait(timeout=0.5):
                    break

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
            
        p_serv = self.proxy_pe_server
        if p_serv and not p_serv.startswith("http"):
            p_serv = "http://" + p_serv
        proxy_dict = {"server": p_serv}
        if self.proxy_pe_user:
            proxy_dict["username"] = self.proxy_pe_user
        if self.proxy_pe_pass:
            proxy_dict["password"] = self.proxy_pe_pass
        print(f"  [Proxy PE] [{self.client_email}] Usando proxy de PERÚ para el restablecimiento: {p_serv}")
            
        launch_kwargs = kwargs_launch_persistent(self.main_profile, headless=self.headless)
        launch_kwargs["proxy"] = proxy_dict
            
        try:
            self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:
            print(f"  [Navegador] [WARN] Falló el lanzamiento: {e}. Reparando y reintentando...")
            reparar_perfil_corrupto(self.main_profile)
            time.sleep(2.0)
            # Si falló por channel=chrome en Linux, reintentar sin channel
            if "channel" in launch_kwargs:
                launch_kwargs.pop("channel", None)
            self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
            
        self.context.set_default_navigation_timeout(60000)
        self.context.set_default_timeout(45000)
        self.context.add_init_script(STEALTH_SCRIPT)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.client_email = self.client_email
        self.page.manager = self
        try:
            self.page.bring_to_front()
        except Exception:
            pass

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
                    required_keywords=KEYWORDS_RESTABLECER_PWD,
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
    with TITULARES_FILE_LOCK:
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



def agrupar_miembros_por_titular_familiar(
    correos_miembros: list[str],
    path: Path | None = None,
) -> tuple[list[dict], list[str], Path]:
    """Agrupa correos de miembros según bloques TITULAR / MIEMBROS de titular_familiar.txt.

    Cada trabajo: {"correo_titular": str, "miembros": [str, ...]}.
    Un miembro solo se asigna al primer titular cuyo bloque MIEMBROS lo liste
    (comparación EXACTA con puntos).
    """
    if path is None:
        path = SCRIPT_DIR / "titular_familiar.txt"
        if not path.exists():
            path = SCRIPT_DIR / "perfiles" / "familiar_titular.txt"

    titulares, _ = parsear_titular_familiar_txt_opcion11(path)
    trabajos: list[dict] = []
    asignados: list[str] = []

    for t in titulares:
        plan = list(t.get("miembros_invitar") or [])
        if not plan:
            continue
        miembros_t: list[str] = []
        for m in correos_miembros:
            if not m or "@" not in str(m):
                continue
            if any(correos_iguales_exacto(m, a) for a in asignados):
                continue
            if any(correos_iguales_exacto(m, p) for p in plan):
                miembros_t.append(str(m).strip())
                asignados.append(str(m).strip())
        if miembros_t:
            trabajos.append({
                "correo_titular": (t.get("correo") or "").strip(),
                "miembros": miembros_t,
            })

    sin_mapa = [
        str(m).strip()
        for m in correos_miembros
        if m and "@" in str(m) and not any(correos_iguales_exacto(m, a) for a in asignados)
    ]
    return trabajos, sin_mapa, path


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




def _rellenar_email_invitar_react(input_loc, email_objetivo: str) -> bool:
    """Rellena el input de invitar de forma compatible con React (native value setter)."""
    email_objetivo = (email_objetivo or "").strip()
    if not email_objetivo:
        return False
    try:
        input_loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        input_loc.click(timeout=2000)
    except Exception:
        try:
            input_loc.focus()
        except Exception:
            pass
    # 1) Playwright fill + eventos
    try:
        input_loc.fill("")
        input_loc.fill(email_objetivo)
        try:
            input_loc.dispatch_event("input")
            input_loc.dispatch_event("change")
        except Exception:
            pass
        try:
            val = (input_loc.input_value(timeout=800) or "").strip()
            if val.lower() == email_objetivo.lower():
                return True
        except Exception:
            pass
    except Exception:
        pass
    # 2) Setter nativo (React controlled inputs)
    try:
        ok = bool(input_loc.evaluate(
            """(el, value) => {
                try {
                    const proto = window.HTMLInputElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && desc.set) desc.set.call(el, value);
                    else el.value = value;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
                    return (el.value || '').trim().toLowerCase() === String(value).trim().toLowerCase();
                } catch (e) { return false; }
            }""",
            email_objetivo,
        ))
        if ok:
            return True
    except Exception:
        pass
    # 3) Tecleo humano
    try:
        if rellenar_campo_humanizado(input_loc, email_objetivo):
            return True
    except Exception:
        pass
    try:
        val = (input_loc.input_value(timeout=500) or "").strip()
        return val.lower() == email_objetivo.lower()
    except Exception:
        return False

def _familia_ui_lista_para_invitar(page) -> bool:
    """True si hay botón/campo de invitar en /family (no basta 'member' genérico en el body)."""
    try:
        page = pagina_vigente(page)
        if not page or page.is_closed():
            return False
        # Campo email/texto del formulario de invitar ya abierto
        if encontrar_locator_en_frames(
            page,
            [
                'input[type="email"]',
                'input[placeholder*="Correo" i]',
                'input[placeholder*="email" i]',
                'input[placeholder*="Email" i]',
            ],
        ):
            return True
        # Botón para abrir el formulario
        if encontrar_locator_en_frames(
            page,
            [
                "button:has-text('Invitar a un familiar')",
                "button:has-text('Invite a family member')",
                "button:has-text('Add family member')",
                "button:has-text('Invitar miembro')",
                "button:has-text('Invite member')",
                "button:has-text('Añadir a un familiar')",
                "a:has-text('Invitar a un familiar')",
                "a:has-text('Invite a family member')",
                "[role='button']:has-text('Invitar a un familiar')",
                "[role='button']:has-text('Invite a family member')",
            ],
        ):
            return True
        # Fallback JS: texto exacto de CTA en botones
        return bool(page.evaluate("""() => {
            const kws = [
                'invitar a un familiar', 'invite a family member', 'add family member',
                'invitar miembro', 'invite member', 'añadir a un familiar', 'agregar miembro'
            ];
            const nodes = Array.from(document.querySelectorAll('button, a, [role="button"], div, span, p'));
            return nodes.some(el => {
                const t = (el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                if (!t || t.length > 60) return false;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                return kws.some(k => t === k || t.includes(k));
            });
        }"""))
    except Exception:
        return False

def _abrir_formulario_invitar_familiar(page, pausa_s: float = 0.7) -> bool:
    """Pulsa el CTA que abre el formulario de invitación. True si tras el clic hay input."""
    page = pagina_vigente(page)
    # Ya visible
    if encontrar_locator_en_frames(
        page,
        [
            'input[type="email"]',
            'input[placeholder*="Correo" i]',
            'input[placeholder*="email" i]',
            'input[placeholder*="Email" i]',
        ],
    ):
        return True

    selectores_abrir = [
        "button:has-text('Invitar a un familiar')",
        "button:has-text('Invite a family member')",
        "button:has-text('Add family member')",
        "button:has-text('Invitar miembro')",
        "button:has-text('Invite member')",
        "button:has-text('Añadir a un familiar')",
        "button:has-text('Agregar miembro')",
        "a:has-text('Invitar a un familiar')",
        "a:has-text('Invite a family member')",
        "[role='button']:has-text('Invitar a un familiar')",
        "[role='button']:has-text('Invite a family member')",
        "text=Invitar a un familiar",
        "text=Invite a family member",
    ]
    btn = esperar_locator_en_frames(page, selectores_abrir, timeout_s=12.0)
    if btn:
        print("    [Invitar] Abriendo formulario de invitación...")
        try:
            btn.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        if not hacer_clic_humanizado(page, btn):
            try:
                btn.click(force=True, timeout=5000)
            except Exception:
                try:
                    btn.evaluate("el => el.click()")
                except Exception:
                    pass
        time.sleep(pausa_s + 0.8)
        page = pagina_vigente(page)
        if encontrar_locator_en_frames(
            page,
            [
                'input[type="email"]',
                'input[placeholder*="Correo" i]',
                'input[placeholder*="email" i]',
                'input[placeholder*="Email" i]',
                'input[type="text"]',
            ],
        ):
            return True

    # Fallback JS por si Playwright no ve el botón (shadow/SPA)
    try:
        clicked = page.evaluate("""() => {
            const kws = [
                'invitar a un familiar', 'invite a family member', 'add family member',
                'invitar miembro', 'invite member', 'añadir a un familiar', 'agregar miembro'
            ];
            const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            for (const el of nodes) {
                const t = (el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                if (!t || t.length > 60) continue;
                if (!kws.some(k => t.includes(k))) continue;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                el.click();
                return true;
            }
            return false;
        }""")
        if clicked:
            print("    [Invitar] CTA de invitar pulsado vía JS.")
            time.sleep(pausa_s + 1.0)
            return True
    except Exception:
        pass
    return False

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

    # Asegurar que estamos en /family antes de buscar el formulario
    try:
        url_now = (page.url or "").lower()
    except Exception:
        url_now = ""
    if "family" not in url_now or "login" in url_now or "authorize" in url_now:
        print("    [Invitar] Navegando a account.tidal.com/family antes de invitar...")
        if not _recargar_pagina_familia(page):
            print("    [Invitar] Error: no se pudo cargar /family.")
            return "fallo"
        page = pagina_vigente(page)

    # Si el miembro ya está en la lista, no volver a invitar (evita el "error inesperado")
    if _miembro_presente_en_pagina_familia(page, email_objetivo):
        print(f"    [Invitar] El correo {email_objetivo} ya figura en el plan familiar.")
        return "ya_miembro"

    selectores_input = [
        'input[type="email"]',
        'input[placeholder*="Correo electrónico" i]',
        'input[placeholder*="Correo" i]',
        'input[placeholder*="email" i]',
        'input[placeholder*="Email" i]',
        'input[id*="email" i]',
        'input[name*="email" i]',
        'input[autocomplete="email"]',
        'input[type="text"]',
    ]

    # 1) Abrir formulario si hace falta y localizar el input
    target_frame = None
    input_loc = None

    for _intento_ui in range(1, 4):
        page = pagina_vigente(page)
        for frame in _frames_visibles(page):
            for sel in selectores_input:
                try:
                    loc = frame.locator(sel).first
                    if loc.count() > 0 and loc.is_visible(timeout=600):
                        # Evitar campos de login residuales en otras rutas
                        try:
                            typ = (loc.get_attribute("type") or "").lower()
                            ph = (loc.get_attribute("placeholder") or "").lower()
                            name = (loc.get_attribute("name") or "").lower()
                        except Exception:
                            typ = ph = name = ""
                        if typ == "password":
                            continue
                        if typ in ("", "text") and "email" not in ph and "correo" not in ph and "email" not in name:
                            # text genérico: solo aceptar si el formulario de invite está abierto
                            # (hay botón Invitar/Invite cercano o placeholder vacío típico)
                            pass
                        input_loc = loc
                        target_frame = frame
                        break
                except Exception:
                    continue
            if input_loc:
                break

        if input_loc:
            break

        print(f"    [Invitar] Formulario no visible (intento {_intento_ui}/3). Abriendo CTA...")
        _abrir_formulario_invitar_familiar(page, pausa_s=pausa_s)
        time.sleep(0.6)

    if not input_loc:
        # Último intento con esperar_locator (más tolerante)
        input_loc = esperar_locator_en_frames(
            page,
            [
                'input[type="email"]',
                'input[placeholder*="Correo" i]',
                'input[placeholder*="email" i]',
                'input[placeholder*="Email" i]',
            ],
            timeout_s=8.0,
        )
        if input_loc:
            target_frame = page.main_frame
            try:
                for frame in _frames_visibles(page):
                    for sel in selectores_input[:6]:
                        loc = frame.locator(sel).first
                        if loc.count() > 0 and loc.is_visible(timeout=300):
                            target_frame = frame
                            input_loc = loc
                            raise StopIteration
            except StopIteration:
                pass
            except Exception:
                pass

    if not input_loc or not target_frame:
        try:
            u = page.url
            snippet = page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 220)"
            )
        except Exception:
            u, snippet = "?", ""
        print("    [Invitar] Error: No se encontró el campo de correo para invitar.")
        print(f"    [Invitar] DEBUG url={u}")
        print(f"    [Invitar] DEBUG texto={snippet!r}")
        return "fallo"

    # 2) Rellenar el correo
    print(f"    [Invitar] Escribiendo correo: {email_objetivo}")
    if not _rellenar_email_invitar_react(input_loc, email_objetivo):
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
        # Búsqueda amplia en toda la página (a veces el submit está fuera del frame del input)
        button_loc = esperar_locator_en_frames(
            page,
            [
                'button:text-is("Invitar")',
                'button:text-is("Invite")',
                'button:text-is("Enviar")',
                'button:text-is("Send")',
                "form button[type='submit']",
            ],
            timeout_s=5.0,
        )
        if button_loc:
            try:
                txt = (button_loc.inner_text() or "").strip().lower()
                if len(txt) > 12 and ("familiar" in txt or "family member" in txt):
                    button_loc = None
            except Exception:
                pass

    if not button_loc:
        print("    [Invitar] Error: No se encontró el botón de enviar invitación. Probando Enter...")
        try:
            input_loc.press("Enter")
            time.sleep(pausa_s + 1.0)
            if _miembro_presente_en_pagina_familia(page, email_objetivo):
                return "ok"
            try:
                val = (input_loc.input_value() or "").strip()
            except Exception:
                val = "x"
            if not val:
                return "ok"
        except Exception:
            pass
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
                 perfil_dir: Path | None = None,
                 trabajos_por_titular: list[dict] | None = None,
                 titulares_shared: list[dict] | None = None,
                 path_titulares: Path | None = None):
        self.queue_miembros = queue_miembros
        self.trabajos_por_titular = list(trabajos_por_titular or [])
        self.titulares_shared = titulares_shared
        self.path_titulares = path_titulares
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


    def _hay_login_titular_visible(self) -> bool:
        """True si la pestaña está en OAuth / formulario de acceso (no sesión de cuenta)."""
        try:
            self.page = pagina_vigente(self.page)
            if not self.page or self.page.is_closed():
                return True
            url = (self.page.url or "").lower()
            if "/login/tidal/return" in url or "/login/tidal/callback" in url:
                return False
            if (
                "account.tidal.com" in url
                and "/login" not in url
                and "authorize" not in url
                and any(p in url for p in (
                    "/family", "/profile", "/subscription", "/overview",
                    "/payment", "/store",
                ))
            ):
                return False
            if "login.tidal.com" in url or "/authorize" in url:
                return True
            if "account.tidal.com" in url and "/login" in url:
                return True
            if encontrar_locator_en_frames(
                self.page, ['input[type="password"]', 'input[name="password"]']
            ):
                return True
            if contar_cajas_otp_visibles(self.page) >= 1:
                return True
            return False
        except Exception:
            return False

    def _sesion_cuenta_ya_abierta(self) -> bool:
        """True si la pestaña ya está en cuenta autenticada (sin formulario OAuth/login)."""
        try:
            self.page = pagina_vigente(self.page)
            if not self.page or self.page.is_closed():
                return False
            if self._hay_login_titular_visible():
                return False
            if encontrar_locator_en_frames(
                self.page, ['input[type="password"]', 'input[name="password"]']
            ):
                return False
            if contar_cajas_otp_visibles(self.page) >= 1:
                return False
            if _familia_ui_lista_para_invitar(self.page):
                return True
            url = (self.page.url or "").lower()
            if "account.tidal.com" in url and "/login" not in url and "authorize" not in url:
                return True
            if "listen.tidal.com" in url or "my.tidal.com" in url:
                return True
        except Exception:
            pass
        return False

    def asegurar_familia_para_invitar(self, titular) -> bool:
        """Tras un login OK: solo asegura /family. NO relanza OTP/IMAP."""
        self.client_email = titular.get("correo") or self.client_email
        try:
            if self._sesion_cuenta_ya_abierta() or self._sesion_titular_activa(titular):
                if self._abrir_panel_familia(titular):
                    return True
                if _recargar_pagina_familia(self.page):
                    return True
                print(f"  [Inviter] {Color.WARNING}[{titular['correo']}] Sesión activa pero /family "
                      f"no muestra CTA; se intenta invitar igual.{Color.ENDC}")
                return True
        except Exception as e:
            print(f"  [Inviter] [WARN] No se pudo reafirmar /family: {e}")
        return False

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
            self.page = pagina_vigente(self.page)
            print(f"  [Inviter] Preparando /family para invitar a {miembro_correo}...")
            family_ok = _recargar_pagina_familia(self.page)
            self.page = pagina_vigente(self.page)
            try:
                url_now = (self.page.url or "").lower()
            except Exception:
                url_now = ""
            if not family_ok and ("family" not in url_now or "login" in url_now or "authorize" in url_now):
                print(f"  [Inviter] ERROR al navegar a /family")
                return False
            if not family_ok:
                print(f"  [Inviter] [WARN] /family cargó lento; se intenta invitar igual...")
            aceptar_cookies_con_espera(self.page)

            if not _familia_ui_lista_para_invitar(self.page):
                print(f"  [Inviter] UI de invitar no visible aún; reabriendo formulario...")
                _abrir_formulario_invitar_familiar(self.page, pausa_s=0.8)
                self.page = pagina_vigente(self.page)

            if _miembro_presente_en_pagina_familia(self.page, miembro_correo):
                print(f"  {Color.GREEN}[Inviter] [OK] {miembro_correo} ya figura en el plan familiar (sin reinvitar).{Color.ENDC}")
                return True

            hermano = _alias_gmail_hermano_en_plan(self.page, miembro_correo)
            if hermano:
                print(f"  {Color.WARNING}[Inviter] [WARN] En el plan ya está '{hermano}' "
                      f"(mismo buzón Gmail que {miembro_correo}, distintos puntos). "
                      f"Se invita de todos modos: en Tidal son cuentas distintas.{Color.ENDC}")

            if invitar_miembro_plan_familiar_con_reintentos(self.page, miembro_correo, intentos=4, pausa_s=0.8):
                print(f"  {Color.GREEN}[Inviter] [OK] Invitación enviada / confirmada para {miembro_correo}.{Color.ENDC}")
                return True

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
            if self.titulares_shared is not None and self.path_titulares is not None:
                titulares, path = self.titulares_shared, self.path_titulares
            else:
                titulares, path = cargar_titulares_familiares()
            if not titulares:
                print(f"  {Color.FAIL}[Inviter] ERROR: No se encontraron titulares en "
                      f"perfiles/familiar_titular.txt ni titular_familiar.txt{Color.ENDC}")
                return

            # Preferir trabajos ya mapeados (opción 9: miembro → titular de titular_familiar.txt).
            trabajos = list(self.trabajos_por_titular or [])
            if not trabajos:
                # Compatibilidad: cola plana → resolver contra bloques MIEMBROS del archivo.
                miembros_cola: list[str] = []
                while True:
                    item = self.queue_miembros.get()
                    if item is None:
                        self.queue_miembros.task_done()
                        break
                    miembros_cola.append(str(item).strip())
                    self.queue_miembros.task_done()
                trabajos, sin_mapa, path = agrupar_miembros_por_titular_familiar(miembros_cola, path)
                for m in sin_mapa:
                    print(f"  {Color.WARNING}[Inviter] '{m}' no aparece en ningún bloque MIEMBROS "
                          f"de {path.name}; se omite.{Color.ENDC}")

            if not trabajos:
                print(f"  {Color.FAIL}[Inviter] ERROR: No hay miembros asignados a ningún titular "
                      f"en {path.name}.{Color.ENDC}")
                return

            print(f"  [Inviter] {Color.CYAN}[{self.client_email}] Plan según {path.name}:{Color.ENDC}")
            for tj in trabajos:
                print(f"    • Titular {tj['correo_titular']} → {len(tj['miembros'])} miembro(s): "
                      f"{tj['miembros']}")

            self.abrir_navegador()
            self.logout_titular()

            for tj in trabajos:
                correo_titular = (tj.get("correo_titular") or "").strip()
                miembros = list(tj.get("miembros") or [])
                if not correo_titular or not miembros:
                    continue

                titular = next(
                    (t for t in titulares if correos_iguales_exacto(t.get("correo") or "", correo_titular)),
                    None,
                )
                if titular is None:
                    print(f"  {Color.FAIL}[Inviter] Titular '{correo_titular}' no está en "
                          f"{path.name}. Se omiten: {miembros}{Color.ENDC}")
                    continue

                print(f"\n  [Inviter] {Color.CYAN}{Color.BOLD}=== Titular {titular['correo']} "
                      f"({len(miembros)} invitación/es) ==={Color.ENDC}")
                self.logout_titular()

                logeado = self.asegurar_login_titular(titular)
                if not logeado:
                    print(f"  {Color.FAIL}[Inviter] ERROR al logearse en titular: "
                          f"{titular['correo']}. Se omiten sus miembros.{Color.ENDC}")
                    continue

                tiene_cupos = self.sincronizar_y_validar_cupos_titular(titular, titulares, path)
                if not tiene_cupos:
                    print(f"  {Color.WARNING}[Inviter] Titular {titular['correo']} sin cupos "
                          f"(lleno). No se reasignan miembros a otro titular.{Color.ENDC}")
                    continue

                for miembro_correo in miembros:
                    print(f"\n  [Inviter] Procesando invitación para: {miembro_correo} "
                          f"(titular {titular['correo']})...")

                    # Revalidar cupos antes de cada invitación
                    if int(titular.get("usados") or 0) >= 5 or titular.get("estado") == "lleno":
                        print(f"  {Color.WARNING}[Inviter] Titular {titular['correo']} ya está "
                              f"lleno; se detienen el resto de sus invitaciones.{Color.ENDC}")
                        break

                    # Solo reafirmar /family; NO relanzar IMAP/OTP (causa el WARN
                    # "No se pudo escribir el código" con la sesión ya abierta).
                    if not self.asegurar_familia_para_invitar(titular):
                        if self._hay_login_titular_visible():
                            print(f"  {Color.FAIL}[Inviter] Se perdió la sesión del titular "
                                  f"{titular['correo']}.{Color.ENDC}")
                            break
                        print(f"  {Color.WARNING}[Inviter] /family inestable para "
                              f"{titular['correo']}; se intenta invitar igual.{Color.ENDC}")

                    invitado_ok = self.enviar_invitacion_familiar(titular, miembro_correo)
                    if invitado_ok:
                        miembro_clean = miembro_correo.strip().rstrip('.').lower()
                        miembros_unicos = []
                        for m in titular.get("miembros", []):
                            m_c = m.strip().rstrip('.').lower()
                            if (
                                m_c
                                and m_c not in miembros_unicos
                                and m_c != titular["correo"].strip().lower()
                            ):
                                miembros_unicos.append(m_c)
                        if miembro_clean not in miembros_unicos:
                            miembros_unicos.append(miembro_clean)

                        titular["miembros"] = miembros_unicos
                        titular["usados"] = len(titular["miembros"])

                        esta_lleno_real = False
                        if titular["usados"] >= 5:
                            time.sleep(1.0)
                            try:
                                esta_lleno_real = self.page.evaluate("""() => {
                                    const bodyText = document.body ? document.body.innerText : '';
                                    if (/\\b5\\s*(?:de|of)\\s*5\\b/i.test(bodyText)) return true;
                                    const addKws = ['invitar a un familiar', 'invitar familiar',
                                        'invitar miembro', 'agregar miembro', 'add family member',
                                        'add member'];
                                    const buttons = Array.from(document.querySelectorAll(
                                        'button, a, div, span, p, [role="button"]'));
                                    const hasAddBtn = buttons.some(el => {
                                        const t = (el.textContent || '').trim().toLowerCase();
                                        return addKws.some(kw => t.includes(kw));
                                    });
                                    return !hasAddBtn;
                                }""")
                            except Exception:
                                esta_lleno_real = titular["usados"] >= 5

                        if esta_lleno_real:
                            titular["estado"] = "lleno"
                            print(f"  [Inviter] {Color.WARNING}El plan familiar de "
                                  f"{titular['correo']} se ha llenado (5/5).{Color.ENDC}")

                        guardar_titulares_familiares(titulares, path)
                    else:
                        print(f"  [Inviter] {Color.WARNING}Invitación no confirmada para "
                              f"{miembro_correo}; se continúa con el siguiente.{Color.ENDC}")

                    time.sleep(random.uniform(2.0, 3.5))

                # Cerrar sesión antes del siguiente titular (puntos distintos = cuenta distinta)
                self.logout_titular()

        except Exception as ex:
            print(f"  {Color.FAIL}[Inviter] ERROR crítico en hilo inviter: {ex}{Color.ENDC}")
        finally:
            self.cerrar_recursos()
            cerrar_sesion_imap_hilo()
            print("  [Inviter] Hilo de invitación finalizado y ventana de Chrome cerrada.")


def restablecer_contrasenas_tidal(correos=None, *, headless: bool | None = None, interactive: bool = True):
    """Restablece contraseñas (opción 9).

    interactive=False: sin prompts (bot Telegram / VPS). headless=True por defecto en ese modo.
    Devuelve dict con ok_list / fail_list / success_count / fail_count.
    """
    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   RESTABLECIMIENTO AUTOMÁTICO DE CONTRASEÑAS TIDAL{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")

    def _pause_o_return(msg: str | None = None, *, error: str | None = None):
        if msg:
            print(msg)
        if interactive:
            input(">>> Presiona Enter para volver al menú principal <<<")
        err = error or (msg or "Error en opción 9")
        # Limpiar códigos ANSI del mensaje para Telegram
        err_plain = re.sub(r"\x1b\[[0-9;]*m", "", str(err))
        return {
            "ok_list": [],
            "fail_list": list(correos or []),
            "success_count": 0,
            "fail_count": len(correos or []),
            "error": err_plain.strip()[:500],
        }
    
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return _pause_o_return(
            f"{Color.FAIL}[Error]{Color.ENDC} Playwright no está instalado. "
            f"Ejecute 'pip install playwright' e instale los navegadores con 'playwright install'."
        )

    path_cuentas = SCRIPT_DIR / "sesiones_imap_cuentas.txt"
    if not path_cuentas.exists():
        return _pause_o_return(
            f"\n{Color.FAIL}[Error]{Color.ENDC} El archivo 'sesiones_imap_cuentas.txt' no existe "
            f"en la carpeta actual."
        )
        
    cuentas_map = cargar_mapa_cuentas_sesiones()
    if not cuentas_map:
        return _pause_o_return(
            f"\n{Color.FAIL}[Error]{Color.ENDC} No se encontraron cuentas válidas en "
            f"'sesiones_imap_cuentas.txt' (formato: correo contraseña)."
        )

    cuentas_map = filtrar_cuentas_por_correos_activos(cuentas_map, correos)
    if cuentas_map is None:
        if interactive:
            input(">>> Presiona Enter para volver al menú principal <<<")
        return {
            "ok_list": [],
            "fail_list": list(correos or []),
            "success_count": 0,
            "fail_count": len(correos or []),
            "error": (
                "Ningún correo del menú está en sesiones_imap_cuentas.txt "
                "(hace falta: correo\\tcontraseña exactos)."
            ),
        }
        
    correos_lista = list(cuentas_map.keys())
    print(f"\nSe procesarán {len(correos_lista)} cuenta(s) (filtradas por correos activos del menú).")

    if headless is None:
        if interactive:
            headless_opt = input(
                "\n¿Deseas ejecutar el navegador en segundo plano (headless)? (s/n, por defecto 'n'): "
            ).strip().lower()
            headless = headless_opt in ("s", "si", "yes", "y")
        else:
            headless = True
    headless = bool(headless) or headless_forzado_por_entorno()
    if headless:
        print(f"  {Color.CYAN}[Opción 9] Modo headless activado.{Color.ENDC}")

    success_count = 0
    fail_count = 0
    ok_list: list[str] = []
    fail_list: list[str] = []

    num_cuentas = len(correos_lista)
    print(f"\n{Color.CYAN}[Proxies PE] Habilitando proxies de Perú obligatorios para restablecimiento (Opción 9)...{Color.ENDC}")
    valid_pe_list = asegurar_proxies_peru(cantidad_necesaria=num_cuentas + 5)
    if not valid_pe_list:
        return _pause_o_return(
            f"\n{Color.FAIL}[Error]{Color.ENDC} No hay proxies de Perú válidos y esta opción los exige "
            f"(solicitud y enlace de restablecimiento). Valida la lista con la opción 13 antes de continuar."
        )

    inviter_threads: list[threading.Thread] = []
    # Invitar solo los correos procesados, cada uno al titular de su bloque en titular_familiar.txt
    path_titular = SCRIPT_DIR / "titular_familiar.txt"
    if not path_titular.exists():
        path_titular = SCRIPT_DIR / "perfiles" / "familiar_titular.txt"

    trabajos_inv, sin_mapa_inv, path_titular = agrupar_miembros_por_titular_familiar(
        correos_lista, path_titular
    )
    if sin_mapa_inv:
        print(f"\n{Color.WARNING}[Paso 9] Sin titular en {path_titular.name} para "
              f"{len(sin_mapa_inv)} correo(s) (no pasarán a invitación):{Color.ENDC}")
        for m in sin_mapa_inv:
            print(f"    ✗ {m}")
    if trabajos_inv:
        print(f"\n  [Paso 9] Invitaciones según bloques de {path_titular.name}:")
        total_m = 0
        for tj in trabajos_inv:
            total_m += len(tj["miembros"])
            print(f"    • {tj['correo_titular']} ← {tj['miembros']}")
        print(f"  [Paso 9] Total miembros a invitar: {total_m}")

        titulares_shared, path_tit_shared = cargar_titulares_familiares()
        if path_titular.exists():
            path_tit_shared = path_titular

        def _slug_titular(correo: str) -> str:
            c = (correo or "").strip().lower()
            if "@" in c:
                local, dom = c.split("@", 1)
                return f"{local.replace('.', '')}@{dom}"
            return re.sub(r"[^a-z0-9]+", "_", c) or "titular"

        def _run_inviter_titular(idx: int, tj: dict):
            if idx > 1:
                time.sleep((idx - 1) * 0.4)
            correo_t = (tj.get("correo_titular") or "").strip()
            temp_dir = Path(tempfile.mkdtemp(prefix=f"tidal_op9_{_slug_titular(correo_t)}_"))
            inv = TidalFamilyInviter(
                queue.Queue(),
                client_email=correo_t or f"titular_{idx}",
                perfil_dir=temp_dir,
                trabajos_por_titular=[tj],
                titulares_shared=titulares_shared,
                path_titulares=path_tit_shared,
            )
            try:
                print(f"  [Paso 9] {Color.CYAN}Invitador paralelo #{idx}: {correo_t} "
                      f"({len(tj.get('miembros') or [])} miembro(s)){Color.ENDC}")
                inv.run_inviter()
            finally:
                try:
                    inv.cerrar_recursos()
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

        n_tit = len(trabajos_inv)
        if n_tit > 1:
            print(f"  [Paso 9] {Color.GREEN}{n_tit} titulares → invitaciones en SIMULTÁNEO "
                  f"(Chrome independiente por titular).{Color.ENDC}")
        for idx, tj in enumerate(trabajos_inv, 1):
            th = threading.Thread(
                target=_run_inviter_titular,
                args=(idx, tj),
                daemon=True,
                name=f"op9-inviter-{idx}",
            )
            inviter_threads.append(th)
            th.start()
        print(f"  [Paso 9] Invitador(es) familiar(es) iniciado(s) en paralelo con los restablecimientos.")
    else:
        print(f"\n{Color.WARNING}[Paso 9] Ningún correo procesado figura como MIEMBROS en "
              f"{path_titular.name}. Se omite la invitación al plan familiar.{Color.ENDC}")

    batch_size = 5
    total_cuentas = len(correos_lista)
    n_oleadas = max(1, (total_cuentas + batch_size - 1) // batch_size)
    if total_cuentas > batch_size:
        print(f"\n{Color.CYAN}{Color.BOLD}[Opción 9] {total_cuentas} cuentas → {n_oleadas} oleadas "
              f"de hasta {batch_size} ventanas en simultáneo (solo proxy PE)...{Color.ENDC}\n")
    else:
        print(f"\n{Color.CYAN}{Color.BOLD}[Opción 9] Restableciendo {total_cuentas} cuenta(s) "
              f"(hasta {batch_size} ventanas en simultáneo, solo proxy PE)...{Color.ENDC}\n")

    for b_start in range(0, total_cuentas, batch_size):
        lote_correos = correos_lista[b_start : b_start + batch_size]
        num_cuentas_lote = len(lote_correos)

        barreras_lote = {
            "inicio": BarreraTolerante(num_cuentas_lote),
            "post_solicitud": BarreraTolerante(num_cuentas_lote),
            "post_link": BarreraTolerante(num_cuentas_lote),
            "final": BarreraTolerante(num_cuentas_lote)
        }

        workers = min(batch_size, num_cuentas_lote)
        n_oleada = (b_start // batch_size) + 1
        if n_oleadas > 1:
            print(f"\n{Color.BLUE}{Color.BOLD}=== Oleada {n_oleada}/{n_oleadas}: "
                  f"{num_cuentas_lote} cuenta(s) "
                  f"({b_start + 1}-{b_start + num_cuentas_lote} de {total_cuentas}) ==={Color.ENDC}")
            for c_o in lote_correos:
                print(f"    • {c_o}")

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
                        ok_list.append(c)
                    else:
                        fail_count += 1
                        fail_list.append(c)
                except Exception as e:
                    print(f"  {Color.FAIL}[ERROR] Excepción inesperada procesando {correo}: {e}{Color.ENDC}")
                    fail_count += 1
                    fail_list.append(correo)

        if n_oleada < n_oleadas:
            print(f"  {Color.CYAN}[Opción 9] Oleada {n_oleada}/{n_oleadas} terminada. "
                  f"Pasando a la siguiente...{Color.ENDC}")
            time.sleep(1.5)
    vivos = [t for t in inviter_threads if t.is_alive()]
    if vivos:
        print(f"\n{Color.CYAN}Esperando a que finalicen {len(vivos)} invitador(es) familiar(es)...{Color.ENDC}")
        for th in vivos:
            th.join(timeout=1200.0)
        vivos2 = [t for t in inviter_threads if t.is_alive()]
        if vivos2:
            print(f"  {Color.WARNING}[Paso 9] {len(vivos2)} invitador(es) siguen en curso tras 20 min; "
                  f"los hilos daemon terminarán al cerrar el proceso.{Color.ENDC}")

    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   RESUMEN DEL RESTABLECIMIENTO{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f" Cuentas procesadas con éxito: {Color.GREEN}{success_count}{Color.ENDC}")
    print(f" Cuentas fallidas: {Color.FAIL}{fail_count}{Color.ENDC}")

    if ok_list:
        print(f"\n{Color.GREEN}{Color.BOLD}✓ Restablecidas OK:{Color.ENDC}")
        for i, c in enumerate(ok_list, 1):
            print(f"  {Color.GREEN}{i:2d}. {c}{Color.ENDC}")

    if fail_list:
        print(f"\n{Color.FAIL}{Color.BOLD}✗ Fallaron:{Color.ENDC}")
        for i, c in enumerate(fail_list, 1):
            print(f"  {Color.FAIL}{i:2d}. {c}{Color.ENDC}")
    elif success_count > 0:
        print(f"\n{Color.GREEN}✗ Ninguna cuenta falló.{Color.ENDC}")

    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}\n")
    print(f"{Color.GREEN}{Color.BOLD}>>> Proceso finalizado. Regresando al menú principal...{Color.ENDC}\n")
    return {
        "ok_list": list(ok_list),
        "fail_list": list(fail_list),
        "success_count": success_count,
        "fail_count": fail_count,
    }


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
        self.correo_registrado_perfil = None
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
        """Confirma que el destino del código es usable vía IMAP (correo registrado / passwords.txt).

        Exacto con puntos: hermano Gmail ≠ misma cuenta Tidal.
        """
        target = (target_email_clean or "").strip().lower()
        login = (self.client_email or "").strip().lower()
        perfil = (getattr(self, "correo_registrado_perfil", None) or "").strip().lower()
        if perfil and not target:
            target = perfil

        try:
            del_body = self.page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            return True

        candidatos = []
        vistos = set()
        for c in re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', del_body or ""):
            c_l = c.strip().lower()
            if c_l.endswith("tidal.com") or c_l in vistos:
                continue
            vistos.add(c_l)
            candidatos.append(c_l)

        if not candidatos:
            print(f"  [Eliminación] [{self.client_email}] [Info] La pantalla no muestra el correo de "
                  f"destino; se continúa (IMAP → {target or login}).")
            return True

        for c in candidatos:
            if target and correos_iguales_exacto(c, target):
                print(f"  [Eliminación] {Color.GREEN}[OK] Destino visible '{c}' coincide EXACTO "
                      f"con '{target}'.{Color.ENDC}")
                return True

        if target and perfil and correos_iguales_exacto(perfil, target):
            for c in candidatos:
                if correos_iguales_exacto(c, login):
                    print(f"  [Eliminación] {Color.WARNING}[Info] La pantalla muestra el nombre de "
                          f"acceso '{c}', no el correo registrado.{Color.ENDC}")
                    print(f"  [Eliminación] {Color.GREEN}[OK] Se continúa: IMAP en '{target}'.{Color.ENDC}")
                    return True

        if login:
            for c in candidatos:
                if correos_iguales_exacto(c, login) and (
                    not target or correos_iguales_exacto(login, target)
                ):
                    print(f"  [Eliminación] {Color.GREEN}[OK] Destino '{c}' coincide EXACTO "
                          f"con la cuenta de acceso/IMAP.{Color.ENDC}")
                    return True

        for c in candidatos:
            if target and son_correos_equivalentes(c, target) and not correos_iguales_exacto(c, target):
                print(f"  [Eliminación] {Color.FAIL}[ALERTA] Visible '{c}' es hermano Gmail de "
                      f"'{target}' (puntos distintos = otra cuenta Tidal).{Color.ENDC}")
                if login and correos_iguales_exacto(c, login):
                    return True
                if perfil and correos_iguales_exacto(c, perfil):
                    return True

        print(f"  [Eliminación] {Color.FAIL}[ALERTA] Correos visibles: {', '.join(candidatos)}."
              f"{Color.ENDC}")
        print(f"  [Eliminación] {Color.FAIL}Ninguno coincide EXACTO con '{target or login}'.{Color.ENDC}")
        return False

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

    def confirmar_cuenta_eliminada(self, timeout_s: float = 10.0) -> bool:
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
                if "account-deleted" in url or "deletion-success" in url:
                    return True
                if self.hay_formulario_login_visible():
                    return True
            except Exception:
                pass
            return False

        def _url_indica_sesion_viva() -> bool:
            try:
                self.page = pagina_vigente(self.page)
                url = (self.page.url or "").lower()
                if not url or "login.tidal.com" in url or "/authorize" in url:
                    return False
                if "account.tidal.com" in url and "/login" not in url:
                    if "/login/tidal/return" in url:
                        return False
                    return True
            except Exception:
                pass
            return False

        try:
            self.page = pagina_vigente(self.page)
            # Si tras confirmar ya estamos en login/authorize, no hace falta /profile.
            if _url_indica_borrado():
                print(f"  [Eliminación] [{self.client_email}] Ya en login/authorize. "
                      f"Cuenta eliminada confirmada.")
                return True

            print(f"  [Eliminación] [{self.client_email}] Verificando borrado en account.tidal.com/profile...")
            try:
                # commit + timeout corto: la redirección a /authorize cuenta como borrado
                self.page.goto(
                    "https://account.tidal.com/profile",
                    wait_until="commit",
                    timeout=12000,
                )
            except Exception as e_nav:
                if _url_indica_borrado():
                    print(
                        f"  [Eliminación] [{self.client_email}] Timeout/aborto en /profile pero la "
                        f"pestaña ya está en login/authorize. Cuenta eliminada confirmada."
                    )
                    return True
                print(f"  [Eliminación] [{self.client_email}] [WARN] Navegación a /profile falló: {e_nav}")
                try:
                    self.page.goto(
                        "https://account.tidal.com/profile",
                        wait_until="commit",
                        timeout=8000,
                    )
                except Exception as e2:
                    if _url_indica_borrado():
                        print(
                            f"  [Eliminación] [{self.client_email}] Tras reintento, URL en "
                            f"login/authorize. Cuenta eliminada confirmada."
                        )
                        return True
                    print(f"  [Eliminación] [{self.client_email}] [WARN] No se pudo verificar borrado en perfil: {e2}")
                    return False

            # Sondeo rápido: no esperar domcontentloaded completo (proxy PE lento).
            limite = time.time() + max(3.0, float(timeout_s))
            while time.time() < limite:
                self.page = pagina_vigente(self.page)
                if _url_indica_borrado():
                    print(f"  [Eliminación] [{self.client_email}] /profile → login/authorize. "
                          f"Cuenta eliminada confirmada.")
                    return True
                if _url_indica_sesion_viva():
                    url = (self.page.url or "")[:80]
                    print(f"  [Eliminación] [{self.client_email}] /profile sigue en cuenta ({url}). "
                          f"Borrado NO confirmado.")
                    return False
                time.sleep(0.2)

            if _url_indica_borrado():
                print(f"  [Eliminación] [{self.client_email}] Llegó a login/authorize al final de la espera. "
                      f"Cuenta eliminada confirmada.")
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

    def esperar_y_confirmar_eliminacion(
        self,
        timeout_s: float = 8.0,
        confirm_timeout_s: float = 10.0,
    ) -> bool:
        """Tras pulsar confirmar: espera señal UI y confirma borrado real vía /profile.

        No trata 'login' en la URL del asistente/OAuth como éxito (falso positivo habitual).
        Si seguimos en view=verify, reintenta el CTA antes de ir a /profile (ir demasiado
        pronto a /profile con sesión viva se interpretaba como 'abandono' sin borrar).
        """
        self.page = pagina_vigente(self.page)
        senal_ui = False
        limite = time.time() + max(3.0, float(timeout_s))
        while time.time() < limite:
            try:
                self.page = pagina_vigente(self.page)
                url = (self.page.url or "").lower()
                if "deleted" in url or "account-deleted" in url or "deletion-success" in url:
                    senal_ui = True
                    break
                if "login.tidal.com" in url or ("/authorize" in url and "account-deletion" not in url):
                    senal_ui = True
                    break
                if self._texto_exito_eliminacion_visible():
                    senal_ui = True
                    break
                if self._error_codigo_eliminacion_visible():
                    print(f"  [Eliminación] [{self.client_email}] Código rechazado en el asistente.")
                    return False
                # Seguir en verify: esperar (no saltar a /profile todavía)
                if "account-deletion" in url or "view=verify" in url:
                    time.sleep(0.25)
                    continue
                if url_parece_exito_o_fin_eliminacion(url):
                    senal_ui = True
                    break
                # Salida a overview/profile sin login: aún puede ser redirección prematura
                if url_parece_abandono_eliminacion(url):
                    break
            except Exception:
                pass
            time.sleep(0.25)

        # Si el CTA no enganchó y seguimos en verify, un clic más antes de /profile
        try:
            url_now = (pagina_vigente(self.page).url or "").lower()
        except Exception:
            url_now = ""
        if ("view=verify" in url_now or "account-deletion" in url_now) and not senal_ui:
            if not self._error_codigo_eliminacion_visible():
                print(f"  [Eliminación] [{self.client_email}] Aún en asistente; "
                      f"reintentando CTA delete-button antes de /profile...")
                try:
                    clic_confirmar_eliminacion_asistente(self.page)
                except Exception:
                    pass
                time.sleep(1.2)
                try:
                    url_now = (pagina_vigente(self.page).url or "").lower()
                except Exception:
                    url_now = ""
                if url_parece_exito_o_fin_eliminacion(url_now) or self._texto_exito_eliminacion_visible():
                    senal_ui = True

        if senal_ui:
            print(f"  [Eliminación] [{self.client_email}] Señal de éxito en UI; confirmando en /profile...")
        else:
            print(f"  [Eliminación] [{self.client_email}] Sin señal UI clara; confirmando borrado en /profile...")

        return self.confirmar_cuenta_eliminada(confirm_timeout_s)

    def _reintentar_eliminacion_con_otp(self, codigo: str, max_intentos: int = 3) -> bool:
        """Si acabamos en /profile con sesión (falso abandono), reabre el asistente y reintenta."""
        codigo = (codigo or "").strip()
        if not codigo:
            return False
        for intento in range(1, max_intentos + 1):
            print(f"  [Eliminación] [{self.client_email}] Recuperación post-/profile "
                  f"({intento}/{max_intentos}): reabriendo asistente de eliminación...")
            try:
                self.page = pagina_vigente(self.page)
                # Si ya estamos en login, el borrado sí ocurrió
                url0 = (self.page.url or "").lower()
                if "login.tidal.com" in url0 or "/authorize" in url0:
                    if self.confirmar_cuenta_eliminada(6.0):
                        return True
                self.page.goto(
                    "https://account.tidal.com/account-deletion",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                time.sleep(1.0)
                aceptar_cookies_con_espera(self.page)
                if "account-deletion" not in (self.page.url or "").lower():
                    btn_entrada = encontrar_locator_en_frames(
                        self.page,
                        ["a:has-text('Eliminar cuenta')", "button:has-text('Eliminar cuenta')",
                         "a:has-text('Delete account')", "button:has-text('Delete account')"],
                    )
                    if btn_entrada:
                        try:
                            btn_entrada.click(timeout=3000)
                        except Exception:
                            btn_entrada.click(force=True, timeout=3000)
                        time.sleep(1.5)
                if not self.hay_campo_codigo():
                    if not self.recorrer_asistente_eliminacion():
                        print(f"  [Eliminación] [{self.client_email}] [WARN] No se alcanzó "
                              f"pantalla OTP en recuperación {intento}.")
                        continue
                if not self.hay_campo_codigo():
                    # ¿Cuenta ya borrada?
                    if self.confirmar_cuenta_eliminada(6.0):
                        return True
                    continue
                if not escribir_codigo_verificacion_inteligente(self.page, codigo):
                    print(f"  [Eliminación] [{self.client_email}] [WARN] No se pudo reescribir OTP.")
                    continue
                if not esperar_boton_eliminar_cuenta_habilitado(self.page, timeout_s=8.0):
                    print(f"  [Eliminación] [{self.client_email}] [WARN] delete-button no se habilitó "
                          f"con este OTP (¿código incorrecto/ya usado?).")
                    # Pedir OTP fresco
                    nuevo = reclamar_otp_eliminacion_para_alias(
                        alias=self.correo_registrado_perfil or self.client_email,
                        after_email_id=0,
                        preferir_otp_len=contar_cajas_otp_visibles(self.page) or 5,
                        max_age_minutes=30,
                        permitir_canonico=True,
                    )
                    if nuevo and nuevo != codigo:
                        codigo = nuevo
                        print(f"  [Eliminación] [{self.client_email}] OTP fresco para recuperación: {codigo}")
                        escribir_codigo_verificacion_inteligente(self.page, codigo)
                        if not esperar_boton_eliminar_cuenta_habilitado(self.page, timeout_s=6.0):
                            continue
                    else:
                        continue
                print(f"  [Eliminación] [{self.client_email}] Pulsando CTA en recuperación {intento}...")
                if not clic_confirmar_eliminacion_asistente(self.page):
                    continue
                time.sleep(1.0)
                if self.esperar_y_confirmar_eliminacion(8.0, confirm_timeout_s=10.0):
                    return True
            except Exception as e_rec:
                print(f"  [Eliminación] [{self.client_email}] [WARN] Recuperación {intento}: {e_rec}")
            time.sleep(1.0)
        return False

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

    def run_auto_login(self, modo: str = "tmm") -> bool:
        """modo='tmm' → opción 10 (login + TuneMyMusic). modo='eliminar' → opción 15 (login + borrar)."""
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

            if modo == "eliminar":
                return self._flujo_eliminar_cuenta_opcion15()
            
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

                    if current_email_value and correos_iguales_exacto(current_email_value, target_email_clean):
                        print(f"  [Verificación Email] {Color.GREEN}[OK] El correo registrado ya coincide "
                              f"EXACTO con el de acceso: {self.client_email}{Color.ENDC}")
                        correo_perfil_correcto = True
                        break
                    if current_email_value and son_correos_equivalentes(current_email_value, target_email_clean):
                        print(f"  [Verificación Email] {Color.WARNING}[ACTUALIZANDO] Perfil tiene hermano "
                              f"Gmail '{current_email_value}' ≠ '{self.client_email}' (puntos distintos). "
                              f"Se fuerza el correo EXACTO del menú.{Color.ENDC}")
                    else:
                        print(f"  [Verificación Email] {Color.WARNING}[ACTUALIZANDO] Reemplazando correo "
                              f"registrado '{current_email_value}' por '{self.client_email}'...{Color.ENDC}")
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

                    if val_post and correos_iguales_exacto(val_post, target_email_clean):
                        print(f"  [Verificación Email] {Color.GREEN}[ÉXITO] Correo actualizado y verificado "
                              f"EXACTO: {self.client_email}{Color.ENDC}")
                        correo_perfil_correcto = True
                        break
                    if val_post and son_correos_equivalentes(val_post, target_email_clean):
                        print(f"  [Verificación Email] {Color.WARNING}[WARN] Perfil aún muestra hermano "
                              f"Gmail '{val_post}' ≠ '{self.client_email}'. Reintentando...{Color.ENDC}")
                    else:
                        print(f"  [Verificación Email] {Color.WARNING}[WARN] El correo aún no coincide "
                              f"tras guardar (leído: '{val_post}'). Reintentando...{Color.ENDC}")

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

                                if self.esperar_y_confirmar_eliminacion(5.0, confirm_timeout_s=8.0):
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
                                            timeout_s=3.0
                                        )
                                        if btn_final:
                                            print(f"  [Eliminación] [{self.client_email}] Pulsando botón de confirmación final...")
                                            btn_final.click()
                                            if self.esperar_y_confirmar_eliminacion(5.0, confirm_timeout_s=8.0):
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

    def leer_correo_electronico_perfil(self) -> str | None:
        """Lee el 'Correo electrónico' de Información general en /profile (NO lo cambia).

        Distinto del 'Nombre de acceso' del sidebar/login: el código de eliminación llega
        al correo registrado en Información general.
        """
        print(f"  [Perfil] [{self.client_email}] Abriendo account.tidal.com/profile para leer "
              f"'Correo electrónico'...")
        navegar_tidal_tolerante(
            self.page,
            "https://account.tidal.com/profile",
            timeout_ms=60000,
        )
        manejar_bloqueos_e_intervencion(self.page, "Perfil Tidal")
        aceptar_cookies_con_espera(self.page)
        self.page = pagina_vigente(self.page)
        time.sleep(1.5)

        url = (self.page.url or "").lower()
        if ("login.tidal.com" in url or "/authorize" in url
                or self.hay_formulario_login_visible()):
            print(f"  [Perfil] {Color.WARNING}[WARN] [{self.client_email}] Redirigió al login. "
                  f"Rehaciendo sesión...{Color.ENDC}")
            if not self.rehacer_login_credenciales():
                return None
            navegar_tidal_tolerante(self.page, "https://account.tidal.com/profile", timeout_ms=45000)
            manejar_bloqueos_e_intervencion(self.page, "Perfil Tidal")
            time.sleep(1.5)

        correo = None
        try:
            correo = self.page.evaluate(r"""() => {
                const stripInv = (s) => (s || '').replace(/[\u200b\u200c\u200d\u200e\u200f\u202a-\u202e\u2060\ufeff\u00ad]/g, '');
                const norm = (s) => stripInv(s).replace(/\s+/g, ' ').trim().toLowerCase();
                const esEmail = (s) => /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/.test(stripInv(s || '').trim());
                const labelsOk = new Set([
                    'correo electrónico', 'correo electronico', 'email address', 'e-mail', 'email'
                ]);
                // Evitar el bloque de "Nombre de acceso" / login name
                const labelsEvitar = ['nombre de acceso', 'username', 'login name', 'nombre de usuario'];

                const nodos = Array.from(document.querySelectorAll(
                    'p, span, div, label, dt, dd, li, h2, h3, h4, strong, b'
                ));
                for (let i = 0; i < nodos.length; i++) {
                    const el = nodos[i];
                    const txt = norm(el.textContent);
                    if (!labelsOk.has(txt)) continue;
                    // Si el ancestro habla de "nombre de acceso", saltar
                    const ancestro = el.closest('section, article, div, li, form') || el.parentElement;
                    const ancTxt = norm(ancestro ? ancestro.innerText.slice(0, 220) : '');
                    if (labelsEvitar.some(l => ancTxt.startsWith(l) || ancTxt.includes('\n' + l))) {
                        // puede ser el mismo card; mirar solo el contenedor pequeño
                    }
                    const cont = el.parentElement || el;
                    const bloque = cont.closest('div') || cont;
                    const candidatos = [];
                    const pushEmails = (root) => {
                        if (!root) return;
                        const t = stripInv(root.innerText || root.textContent || '');
                        for (const m of t.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g) || []) {
                            if (!m.toLowerCase().endsWith('@tidal.com')) candidatos.push(m.trim());
                        }
                    };
                    pushEmails(bloque);
                    if (el.nextElementSibling) pushEmails(el.nextElementSibling);
                    if (cont.nextElementSibling) pushEmails(cont.nextElementSibling);
                    // Deduplicar preservando orden
                    const vistos = new Set();
                    for (const c of candidatos) {
                        const k = c.toLowerCase();
                        if (vistos.has(k)) continue;
                        vistos.add(k);
                        if (esEmail(c)) return stripInv(c).trim();
                    }
                }

                // Fallback: sección "Información general" / "General information"
                const body = stripInv(document.body ? document.body.innerText : '');
                const reSec = /informaci[oó]n\s+general|general\s+information/i;
                const idx = body.search(reSec);
                if (idx >= 0) {
                    const trozo = body.slice(idx, idx + 800);
                    const mCorreo = trozo.match(
                        /correo\s+electr[oó]nico|email\s+address|e-?mail[\s\S]{0,80}?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})/i
                    );
                    if (mCorreo && mCorreo[1] && !mCorreo[1].toLowerCase().endsWith('@tidal.com')) {
                        return mCorreo[1].trim();
                    }
                }
                return null;
            }""")
        except Exception as e_js:
            print(f"  [Perfil] [{self.client_email}] [WARN] No se pudo leer el DOM del perfil: {e_js}")
            correo = None

        if correo:
            correo = clean_email(correo)
            print(f"  [Perfil] [{self.client_email}] Correo electrónico registrado en perfil: "
                  f"{Color.CYAN}{correo}{Color.ENDC}")
            return correo

        print(f"  [Perfil] {Color.FAIL}[ERROR] [{self.client_email}] No se encontró el campo "
              f"'Correo electrónico' en Información general.{Color.ENDC}")
        return None

    def _flujo_eliminar_cuenta_opcion15(self) -> bool:
        """Tras login: verifica correo del perfil en passwords.txt (IMAP) y elimina la cuenta."""
        print(f"  [Eliminación] [{self.client_email}] Modo opción 15: verificar correo registrado → "
              f"eliminar cuenta (sin TuneMyMusic ni cambio de correo).")

        if not self.es_sesion_activa() and not self.confirmar_sesion_en_perfil(15.0):
            print(f"  [Login] {Color.WARNING}[WARN] [{self.client_email}] Sesión perdida antes del perfil. "
                  f"Intentando recuperar...{Color.ENDC}")
            if not self.rehacer_login_credenciales():
                self.abortar_barreras()
                return self.finalizar_sin_exito("Sin sesión Tidal antes de leer el perfil.")

        correo_perfil = None
        for intento in range(1, 4):
            try:
                correo_perfil = self.leer_correo_electronico_perfil()
                if correo_perfil:
                    break
            except Exception as e_perf:
                print(f"  [Perfil] [{self.client_email}] [WARN] Intento {intento}/3 falló: {e_perf}")
            time.sleep(1.5)

        if not correo_perfil:
            self.abortar_barreras()
            return self.finalizar_sin_exito(
                "No se pudo leer el 'Correo electrónico' del perfil. No se elimina la cuenta."
            )

        self.correo_registrado_perfil = clean_email(correo_perfil)
        login_email = clean_email(self.client_email or "")
        if not correos_iguales_exacto(self.correo_registrado_perfil, login_email):
            print(f"  [Perfil] [{self.client_email}] Nombre de acceso/login: {login_email}")
            print(f"  [Perfil] [{self.client_email}] Correo registrado (destino del código): "
                  f"{self.correo_registrado_perfil}")
            if son_correos_equivalentes(self.correo_registrado_perfil, login_email):
                print(f"  [Perfil] [{self.client_email}] {Color.WARNING}[WARN] Login y correo "
                      f"registrado son hermanos Gmail (puntos distintos = cuentas Tidal distintas). "
                      f"El OTP se leerá del correo registrado EXACTO.{Color.ENDC}")

        if not tiene_contrasena_imap_registrada(self.correo_registrado_perfil):
            print(f"  [IMAP] {Color.FAIL}[ABORTADO] [{self.client_email}] El correo registrado "
                  f"'{self.correo_registrado_perfil}' NO tiene App Password / IMAP en "
                  f"passwords.txt.{Color.ENDC}")
            print(f"  [IMAP] Sin eso no llegaría el código de eliminación. Añádelo y reintenta.")
            self.abortar_barreras()
            return self.finalizar_sin_exito(
                f"Correo del perfil '{self.correo_registrado_perfil}' ausente en passwords.txt."
            )

        user_imap, _pwd_imap = obtener_credenciales_imap_reales(self.correo_registrado_perfil)
        print(f"  [IMAP] {Color.GREEN}[OK] [{self.client_email}] '{self.correo_registrado_perfil}' "
              f"está en passwords.txt (buzón IMAP: {user_imap or self.correo_registrado_perfil})."
              f"{Color.ENDC}")

        # --- Eliminación ---
        exito_eliminacion = False
        print(f"\n  [Eliminación] [{self.client_email}] Iniciando eliminación de cuenta Tidal...")
        try:
            try:
                self.page.bring_to_front()
            except Exception:
                pass

            # Solo el correo REGISTRADO (con puntos). El login puede ser otro hermano Gmail.
            aliases_imap = [self.correo_registrado_perfil]

            buzones_preferidos = buzones_imap_candidatos_otp(
                self.correo_registrado_perfil,
                self.client_email,
                incluir_resto_passwords=False,
            )
            if not buzones_preferidos:
                buzones_preferidos = [self.correo_registrado_perfil]
            pref_claves = {_norm_dots_gmail(x) for x in buzones_preferidos}
            buzones_extra = [
                b for b in listar_buzones_imap_de_passwords()
                if _norm_dots_gmail(b) not in pref_claves
            ]

            baselines_por_buzon: dict[str, int] = {}
            for buzon in buzones_preferidos:
                try:
                    baselines_por_buzon[buzon] = obtener_max_email_id(buzon, "tidal")
                except Exception:
                    baselines_por_buzon[buzon] = 0
            base_del_id = baselines_por_buzon.get(buzones_preferidos[0], 0)
            print(f"  [Eliminación] [{self.client_email}] Baseline IMAP de "
                  f"{self.correo_registrado_perfil} antes de disparar el envío: {base_del_id}")
            if buzones_extra:
                print(f"  [Eliminación] [{self.client_email}] Hay {len(buzones_extra)} Gmail extra "
                      f"en passwords.txt; se usarán solo si hace falta (intentos 6/12/18).")

            print(f"  [Eliminación] [{self.client_email}] Abriendo asistente de eliminación...")
            self.page.goto(
                "https://account.tidal.com/account-deletion",
                wait_until="domcontentloaded",
                timeout=35000,
            )
            time.sleep(2.0)
            aceptar_cookies_con_espera(self.page)
            manejar_bloqueos_e_intervencion(self.page, "Eliminación de Cuenta")

            if "account-deletion" not in (self.page.url or ""):
                print(f"  [Eliminación] [{self.client_email}] Redirigido fuera del asistente. "
                      f"Buscando enlace 'Eliminar cuenta'...")
                btn_entrada = encontrar_locator_en_frames(
                    self.page,
                    ["a:has-text('Eliminar cuenta')", "button:has-text('Eliminar cuenta')",
                     "a:has-text('Delete account')", "button:has-text('Delete account')"]
                )
                if btn_entrada:
                    btn_entrada.click()
                    time.sleep(3.0)
                else:
                    self.page.goto(
                        "https://account.tidal.com/account-deletion",
                        wait_until="domcontentloaded",
                        timeout=25000,
                    )
                    time.sleep(2.5)
                if "account-deletion" not in (self.page.url or ""):
                    raise RuntimeError("Tidal no permitió abrir el asistente de eliminación de cuenta.")

            if not self.recorrer_asistente_eliminacion():
                raise RuntimeError("No se alcanzó la pantalla del código del asistente de eliminación.")

            if not self.verificar_destino_del_codigo(self.correo_registrado_perfil):
                raise RuntimeError(
                    f"Tidal enviaría el código a un correo distinto de '{self.correo_registrado_perfil}'."
                )

            codigo_eliminacion = None
            # Cuántas cajas OTP hay (Tidal eliminación suele ser 5 dígitos). Guía la extracción IMAP.
            n_cajas_otp = contar_cajas_otp_visibles(self.page)
            prefer_len = n_cajas_otp if n_cajas_otp in (5, 6) else 5
            print(f"  [Eliminación] [{self.client_email}] Cajas OTP visibles: {n_cajas_otp or '?'}; "
                  f"se prioriza código de {prefer_len} dígitos.")

            print(f"  [Eliminación] [{self.client_email}] Buscando código en IMAP "
                  f"(destinatario EXACTO con puntos: {self.correo_registrado_perfil})...")
            for intento in range(1, 19):
                if intento in (2, 8, 14):
                    try:
                        self.forzar_reenvio_codigo()
                    except Exception as e_reenv:
                        print(f"  [Eliminación] [{self.client_email}] [WARN] Reenvío: {e_reenv}")
                # Baseline solo al inicio; luego mirar recientes (códigos ya en bandeja).
                usar_baseline = intento <= 2
                after_pref = (
                    baselines_por_buzon.get(buzones_preferidos[0], 0) if usar_baseline else 0
                )
                print(f"  [Eliminación] [{self.client_email}] Intento {intento}/18: "
                      f"buscando correo de eliminación"
                      f"{'' if usar_baseline else ' (ventana reciente sin baseline)'}...")
                try:
                    # Un hilo por buzón: evita colapsar Gmail cuando hay muchos alias a la vez.
                    # Desde intento 3 permite To: canónico repartido 1 UID/hilo si no hay exacto.
                    codigo_eliminacion = reclamar_otp_eliminacion_para_alias(
                        alias=self.correo_registrado_perfil,
                        after_email_id=after_pref,
                        preferir_otp_len=prefer_len,
                        max_age_minutes=45,
                        permitir_canonico=(intento >= 3),
                    )
                    # Otros Gmail solo si el preferido no tiene nada (casos raros)
                    if not codigo_eliminacion and intento in (6, 12, 18) and buzones_extra:
                        for buzon in buzones_extra[:4]:
                            codigo_eliminacion = obtener_codigo_via_imap(
                                gmail_user=buzon,
                                required_keywords=KEYWORDS_ELIMINACION_CUENTA,
                                query_exclude=EXCLUDE_ELIMINACION_CUENTA,
                                after_email_id=0,
                                max_age_minutes=45,
                                aliases_solo=aliases_imap,
                                preferir_otp_len=prefer_len,
                                exigir_destinatario_exacto=True,
                                silencioso=True,
                            )
                            if codigo_eliminacion:
                                print(f"  [Eliminación] [{self.client_email}] Código hallado en buzón "
                                      f"'{buzon}' (otro Gmail; match por puntos).")
                                break
                except Exception as e_imap:
                    print(f"  [Eliminación] [{self.client_email}] [WARN] IMAP intento {intento}: {e_imap}")
                    codigo_eliminacion = None

                if codigo_eliminacion:
                    digs = re.sub(r"\D", "", str(codigo_eliminacion))
                    n_ahora = contar_cajas_otp_visibles(self.page) or prefer_len
                    if n_ahora >= 5 and len(digs) != n_ahora:
                        print(f"  [Eliminación] [{self.client_email}] OTP '{digs}' "
                              f"({len(digs)} dígitos) vs {n_ahora} cajas visibles; "
                              f"se intentará escribir/adaptar de todos modos.")
                    break
                if intento < 18:
                    time.sleep(2.5)

            if not codigo_eliminacion:
                print(f"  {Color.FAIL}[Eliminación] [{self.client_email}] No se obtuvo el código "
                      f"vía IMAP en {self.correo_registrado_perfil}.{Color.ENDC}")
            else:
                print(f"  [Eliminación] [{self.client_email}] {Color.GREEN}Código obtenido: "
                      f"{codigo_eliminacion}{Color.ENDC}")
                # Tras la espera IMAP la pestaña puede haber perdido foco o el DOM OTP.
                try:
                    self.page = pagina_vigente(self.page)
                    self.page.bring_to_front()
                except Exception:
                    pass
                if not self.hay_campo_codigo():
                    print(f"  [Eliminación] [{self.client_email}] [WARN] Sin cajas OTP visibles "
                          f"tras IMAP; reabriendo asistente...")
                    try:
                        self.page.goto(
                            "https://account.tidal.com/account-deletion",
                            wait_until="domcontentloaded",
                            timeout=35000,
                        )
                        time.sleep(1.5)
                        aceptar_cookies_con_espera(self.page)
                        self.recorrer_asistente_eliminacion()
                    except Exception as e_re:
                        print(f"  [Eliminación] [{self.client_email}] [WARN] Reapertura: {e_re}")

                codigo_escrito = False
                for intento_write in range(1, 5):
                    if not self.hay_campo_codigo():
                        print(f"  [Eliminación] [{self.client_email}] [WARN] Intento escritura "
                              f"{intento_write}/4: aún no hay campo de código.")
                        time.sleep(1.0)
                        continue
                    if escribir_codigo_verificacion_inteligente(self.page, codigo_eliminacion):
                        # Éxito real = delete-button del asistente habilitado (NO el menú lateral)
                        btn_ready = esperar_boton_eliminar_cuenta_habilitado(self.page, timeout_s=8.0)
                        if btn_ready:
                            codigo_escrito = True
                            break
                        print(f"  [Eliminación] [{self.client_email}] [WARN] OTP escrito pero "
                              f"button.delete-button sigue deshabilitado (intento {intento_write}/4). "
                              f"Reescribiendo...")
                        time.sleep(0.8)
                        continue
                    print(f"  [Eliminación] [{self.client_email}] [WARN] Escritura OTP falló "
                          f"({intento_write}/4). Reintentando...")
                    time.sleep(1.2)

                if codigo_escrito:
                    print(f"  [Eliminación] [{self.client_email}] Código ingresado correctamente "
                          f"(CTA delete-button habilitado).")
                    time.sleep(0.6)
                    print(f"  [Eliminación] [{self.client_email}] Confirmando eliminación "
                          f"(asistente, no menú)...")
                    clicked = clic_confirmar_eliminacion_asistente(self.page)
                    if not clicked:
                        print(f"  {Color.FAIL}[Eliminación] [{self.client_email}] No se pudo pulsar "
                              f"button.delete-button del asistente.{Color.ENDC}")
                    else:
                        time.sleep(1.0)
                        # Si tras el clic seguimos en verify con OTP, el clic no aplicó
                        try:
                            url_mid = (pagina_vigente(self.page).url or "").lower()
                        except Exception:
                            url_mid = ""
                        if "view=verify" in url_mid and self.hay_campo_codigo():
                            print(f"  [Eliminación] [{self.client_email}] [WARN] Seguimos en "
                                  f"view=verify tras el clic; reintentando CTA...")
                            time.sleep(0.8)
                            clic_confirmar_eliminacion_asistente(self.page)
                            time.sleep(1.2)

                        if self.esperar_y_confirmar_eliminacion(8.0, confirm_timeout_s=10.0):
                            print(f"  [Eliminación] {Color.GREEN}[OK] Cuenta {self.client_email} "
                                  f"eliminada correctamente.{Color.ENDC}")
                            exito_eliminacion = True
                        else:
                            url_post = ""
                            try:
                                url_post = (pagina_vigente(self.page).url or "").lower()
                            except Exception:
                                pass
                            if url_parece_exito_o_fin_eliminacion(url_post):
                                print(f"  [Eliminación] {Color.GREEN}[OK] Cuenta {self.client_email} "
                                      f"eliminada (URL login/authorize).{Color.ENDC}")
                                exito_eliminacion = True
                            else:
                                # /profile con sesión NO es abandono definitivo: a menudo el script
                                # navega ahí para verificar antes de que el CTA termine, o el OTP
                                # era incorrecto. Reabrir asistente y reintentar.
                                print(f"  [Eliminación] [{self.client_email}] {Color.WARNING}Borrado "
                                      f"no confirmado (URL={url_post[:70] or '?'}). "
                                      f"Reintentando asistente...{Color.ENDC}")
                                if self._reintentar_eliminacion_con_otp(codigo_eliminacion, max_intentos=3):
                                    print(f"  [Eliminación] {Color.GREEN}[OK] Cuenta {self.client_email} "
                                          f"eliminada tras recuperación.{Color.ENDC}")
                                    exito_eliminacion = True
                                else:
                                    print(f"  {Color.FAIL}[Eliminación] [{self.client_email}] "
                                          f"No se pudo confirmar el borrado tras reintentos."
                                          f"{Color.ENDC}")
                else:
                    print(f"  {Color.FAIL}[Eliminación] [{self.client_email}] No se pudo ingresar "
                          f"el código de forma que habilite button.delete-button del asistente."
                          f"{Color.ENDC}")
        except Exception as ex_el:
            print(f"  {Color.FAIL}[Eliminación] [ERROR] [{self.client_email}] {ex_el}{Color.ENDC}")

        self.eliminacion_ok = exito_eliminacion
        if exito_eliminacion:
            print(f"  [Navegador] [{self.client_email}] Cerrando ventana de Chrome...")
            self.cerrar_recursos()
            return True
        return self.finalizar_sin_exito("No se completó la eliminación de la cuenta.")

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


def eliminar_cuentas_tidal_automatico_opcion15(correos):
    """Opción 15: eliminar cuenta (proxy PE) y luego registrar de nuevo (proxy NG + PE pago)."""
    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   ELIMINAR + REGISTRAR CUENTA TIDAL AUTOMÁTICO{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.CYAN}Fase 1 (Perú): login → leer correo del perfil → IMAP → eliminar.{Color.ENDC}")
    print(f"{Color.CYAN}Fase 2 (Nigeria): registrar de nuevo las cuentas eliminadas "
          f"(pago con proxy PE).{Color.ENDC}")

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print(f"{Color.FAIL}[Error]{Color.ENDC} Playwright no está instalado. Ejecute 'pip install playwright' "
              f"e instale los navegadores con 'playwright install'.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    path_cuentas = SCRIPT_DIR / "sesiones_imap_cuentas.txt"
    if not path_cuentas.exists():
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} El archivo 'sesiones_imap_cuentas.txt' no existe en la carpeta actual.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return

    cuentas_map = cargar_mapa_cuentas_sesiones()
    if not cuentas_map:
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} No se encontraron cuentas válidas en "
              f"'sesiones_imap_cuentas.txt' (formato: correo contraseña).")
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
    correos_eliminados: list[str] = []
    elim_lock = threading.Lock()

    num_cuentas = len(correos_lista)
    print(f"\n{Color.CYAN}[Proxies PE] Habilitando proxies de Perú obligatorios (Fase 1 — eliminar)...{Color.ENDC}")
    valid_pe_list = asegurar_proxies_peru(cantidad_necesaria=num_cuentas)
    if not valid_pe_list:
        print(f"\n{Color.FAIL}[Error]{Color.ENDC} No hay proxies de Perú válidos y esta opción los exige.")
        input(">>> Presiona Enter para volver al menú principal <<<")
        return
    GLOBAL_PE_PROXY_POOL.reiniciar_bloqueos()

    batch_size = 10
    total_cuentas = len(correos_lista)

    print(f"\n{Color.CYAN}{Color.BOLD}=== FASE 1: Eliminar {total_cuentas} cuentas (proxy PE) "
          f"(máx. {batch_size} ventanas en paralelo; OTP por alias exacto con puntos) ==={Color.ENDC}\n")

    hermanos = {}
    for c in correos_lista:
        hermanos.setdefault(_norm_dots_gmail(c), []).append(c)
    multi = {k: v for k, v in hermanos.items() if len(v) > 1}
    if multi:
        print(f"{Color.CYAN}[Opción 15] Alias del mismo buzón Gmail en el mismo lote "
              f"(sin olas: cada OTP se atribuye por los puntos del correo):{Color.ENDC}")
        for _buz, aliases in multi.items():
            print(f"  • {' | '.join(aliases)}")

    idx_global = 0
    for b_start in range(0, total_cuentas, batch_size):
        if b_start > 0:
            GLOBAL_PE_PROXY_POOL.reiniciar_bloqueos()
        lote_correos = correos_lista[b_start: b_start + batch_size]
        num_cuentas_lote = len(lote_correos)
        barreras_lote = {"inicio": threading.Barrier(num_cuentas_lote)}
        workers = num_cuentas_lote
        if total_cuentas > batch_size:
            print(f"\n{Color.CYAN}{Color.BOLD}--- Lote "
                  f"({b_start + 1} a {b_start + num_cuentas_lote} de {total_cuentas}) "
                  f"---{Color.ENDC}")

        def eliminar_un_correo(idx_rel, correo):
            if idx_rel > 1:
                time.sleep((idx_rel - 1) * 1.5)
            nonlocal_idx = idx_global + idx_rel
            contrasena = cuentas_map[correo]

            p_pe = GLOBAL_PE_PROXY_POOL.obtener_proxy_unico()
            if not p_pe:
                print(f"  {Color.FAIL}[Proxy PE] [{correo}] Sin proxy disponible; se omite.{Color.ENDC}")
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
                thread_index=nonlocal_idx,
                mantener_ventana_si_falla=mantener_ventanas,
            )
            managers.append(manager)
            print(f"\n{Color.CYAN}{Color.BOLD}[Eliminar Automático] Iniciando proceso para: {correo}{Color.ENDC}")
            try:
                exito = manager.run_auto_login(modo="eliminar")
            finally:
                cerrar_sesion_imap_hilo()
            if exito:
                with elim_lock:
                    if correo not in correos_eliminados:
                        correos_eliminados.append(correo)
            return correo, exito

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(eliminar_un_correo, idx_rel, correo): correo
                for idx_rel, correo in enumerate(lote_correos, 1)
            }
            for future in as_completed(futures):
                correo_f = futures[future]
                try:
                    _c, exito = future.result()
                    if exito:
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as ex_h:
                    fail_count += 1
                    print(f"  {Color.FAIL}[ERROR] Excepción eliminando {correo_f}: {ex_h}{Color.ENDC}")
        idx_global += len(lote_correos)

    total_login = sum(1 for m in managers if getattr(m, "login_ok", False))
    total_perfil = sum(1 for m in managers if getattr(m, "correo_registrado_perfil", None))
    total_eliminadas = sum(1 for m in managers if getattr(m, "eliminacion_ok", False))
    # Preferir lista explícita (mismo orden de éxito)
    if not correos_eliminados:
        correos_eliminados = [
            m.client_email for m in managers if getattr(m, "eliminacion_ok", False)
        ]

    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"   RESUMEN FASE 1 — ELIMINAR (proxy PE)")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f" Cuentas procesadas: {total_cuentas}")
    print(f" Inicios de sesión correctos: {total_login}")
    print(f" Correos de perfil leídos: {total_perfil}")
    print(f" Cuentas eliminadas: {total_eliminadas}")
    print(f" Procesos completos: {success_count}")
    print(f" Procesos incompletos: {fail_count}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}\n")

    if fail_count and mantener_ventanas:
        print(f"{Color.WARNING}Las ventanas de las cuentas incompletas ya se cerraron tras el plazo "
              f"de revisión manual.{Color.ENDC}")

    if not correos_eliminados:
        print(f"\n{Color.WARNING}[Opción 15] Ninguna cuenta se eliminó con éxito. "
              f"Se omite la fase de registro.{Color.ENDC}")
        print(f"\n{Color.GREEN}{Color.BOLD}>>> Proceso finalizado. Regresando al menú principal...{Color.ENDC}\n")
        return

    print(f"\n{Color.CYAN}{Color.BOLD}=== FASE 2: Registrar {len(correos_eliminados)} cuenta(s) "
          f"eliminada(s) (proxy NG + PE pago) ==={Color.ENDC}")
    for c in correos_eliminados:
        print(f"  • {c}")

    # Tidal a veces tarda unos segundos en liberar el correo tras el borrado
    print(f"\n{Color.CYAN}[Opción 15] Esperando 2s antes del registro (liberación del correo en Tidal)...{Color.ENDC}")
    time.sleep(2.0)

    global valid_ng_list, CACHE_PROXIES_NG
    valid_ng_list = []
    cargar_cache_proxies_validos_desde_disco()
    if CACHE_PROXIES_NG:
        valid_ng_list = list(CACHE_PROXIES_NG)
        print(f"\n{Color.GREEN}[Proxy Caché] Usando {len(valid_ng_list)} proxies de NIGERIA "
              f"previamente verificados.{Color.ENDC}")
    else:
        proxies_cfg = cargar_proxies_desde_txt(preferir_validos=False)
        if proxies_cfg and proxies_cfg.get("proxy_ng_list"):
            ng_list = proxies_cfg["proxy_ng_list"]
            print(f"\nSe encontraron {len(ng_list)} proxies para NIGERIA.")
            valid_ng_list = probar_y_seleccionar_mejor_proxy(
                ng_list, "NIGERIA", max(len(correos_eliminados) * 4, len(correos_eliminados) + 15)
            )
            if valid_ng_list:
                guardar_proxies_validos_txt(SCRIPT_DIR / "lista_proxies_ng_validos.txt", valid_ng_list)
        else:
            print(f"\n{Color.WARNING}[Proxy]{Color.ENDC} No se encontraron proxies de Nigeria.")

    alimentar_pool_proxies_nigeria(valid_ng_list)
    GLOBAL_NG_PROXY_POOL.reiniciar_bloqueos()
    GLOBAL_PE_PROXY_POOL.reiniciar_bloqueos()

    use_proxy_ng = bool(valid_ng_list)
    if not use_proxy_ng:
        print(f"\n{Color.WARNING}[WARN]{Color.ENDC} No hay proxies de Nigeria válidos.")
        confirm = input("¿Continuar el registro con tu IP local/VPN? (s/n, por defecto 'n'): ").strip().lower()
        if confirm not in ("s", "si", "yes", "y"):
            print("Registro cancelado. Las cuentas ya fueron eliminadas.")
            print(f"\n{Color.GREEN}{Color.BOLD}>>> Proceso finalizado. Regresando al menú principal...{Color.ENDC}\n")
            return

    print(f"\n{Color.CYAN}[Proxies PE] Habilitando proxies de Perú para el pago del registro...{Color.ENDC}")
    proxies_pe_reg = asegurar_proxies_peru(cantidad_necesaria=len(correos_eliminados))
    if not proxies_pe_reg:
        print(f"\n{Color.WARNING}[WARN]{Color.ENDC} No hay proxies PE para el pago del registro.")
        confirm_pe = input("¿Continuar el registro sin proxy PE? (s/n, por defecto 'n'): ").strip().lower()
        if confirm_pe not in ("s", "si", "sí", "yes", "y"):
            print("Registro cancelado. Las cuentas ya fueron eliminadas.")
            print(f"\n{Color.GREEN}{Color.BOLD}>>> Proceso finalizado. Regresando al menú principal...{Color.ENDC}\n")
            return

    reg_ok = 0
    reg_fail = 0
    reg_lock = threading.Lock()
    workers_reg = min(10, len(correos_eliminados))
    print(f"\n{Color.CYAN}{Color.BOLD}Registrando {len(correos_eliminados)} cuentas "
          f"(hasta {workers_reg} hilos, proxy NG)...{Color.ENDC}\n")

    def registrar_tras_borrar(idx, correo):
        nonlocal reg_ok, reg_fail
        if idx > 1:
            time.sleep((idx - 1) * 0.2)
        p_ng_server = p_ng_user = p_ng_pass = None
        p_pe_server = p_pe_user = p_pe_pass = None
        manager = None
        exito = False
        try:
            if use_proxy_ng and valid_ng_list:
                p_ng = GLOBAL_NG_PROXY_POOL.obtener_proxy_unico()
                if not p_ng:
                    print(f"  {Color.FAIL}[Proxy NG] [{correo}] Sin proxy de Nigeria libre; se omite.{Color.ENDC}")
                    with reg_lock:
                        reg_fail += 1
                    return correo, False
                p_ng_server = p_ng.get("server")
                p_ng_user = p_ng.get("username")
                p_ng_pass = p_ng.get("password")

            if proxies_pe_reg:
                p_pe = GLOBAL_PE_PROXY_POOL.obtener_proxy_unico()
                if p_pe:
                    p_pe_server = p_pe.get("server")
                    p_pe_user = p_pe.get("username")
                    p_pe_pass = p_pe.get("password")

            # Misma contraseña anotada (si el registro la pide más adelante / opción 9)
            pwd_prev = cuentas_map.get(correo) or ""
            manager = TidalRegisterManager(
                client_email=correo,
                client_pwd=pwd_prev,
                proxy_ng_server=p_ng_server,
                proxy_ng_user=p_ng_user,
                proxy_ng_pass=p_ng_pass,
                proxy_pe_server=p_pe_server,
                proxy_pe_user=p_pe_user,
                proxy_pe_pass=p_pe_pass,
                headless=headless,
            )
            print(f"\n{Color.CYAN}{Color.BOLD}[Registro post-eliminación] {correo}{Color.ENDC}")
            exito = manager.run_registration(cerrar_navegador_al_final=True)
            try:
                manager.cerrar_navegador(liberar_ng=True, liberar_pe=True)
            except Exception:
                pass
            try:
                manager.limpiar_perfil_temporal()
            except Exception:
                pass
            with reg_lock:
                if exito:
                    reg_ok += 1
                    print(f"  {Color.GREEN}[Registro] [{correo}] Completado tras eliminación.{Color.ENDC}")
                else:
                    reg_fail += 1
            return correo, exito
        except Exception as e_reg:
            if manager is not None:
                try:
                    manager.cerrar_navegador(liberar_ng=True, liberar_pe=True)
                except Exception:
                    pass
                try:
                    manager.limpiar_perfil_temporal()
                except Exception:
                    pass
            with reg_lock:
                if not exito:
                    reg_fail += 1
            print(f"  {Color.FAIL}[ERROR] Registro de {correo}: {e_reg}{Color.ENDC}")
            return correo, False
        finally:
            cerrar_sesion_imap_hilo()
            if not exito:
                try:
                    GLOBAL_NG_PROXY_POOL.liberar_proxy(p_ng_server)
                except Exception:
                    pass
                try:
                    GLOBAL_PE_PROXY_POOL.liberar_proxy(p_pe_server)
                except Exception:
                    pass

    with ThreadPoolExecutor(max_workers=workers_reg) as executor:
        futures = {
            executor.submit(registrar_tras_borrar, idx, correo): correo
            for idx, correo in enumerate(correos_eliminados, 1)
        }
        for future in as_completed(futures):
            correo_f = futures[future]
            try:
                future.result()
            except Exception as ex_h:
                with reg_lock:
                    reg_fail += 1
                print(f"  {Color.FAIL}[ERROR] Excepción registrando {correo_f}: {ex_h}{Color.ENDC}")

    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"   RESUMEN FASE 2 — REGISTRAR (proxy NG)")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f" Cuentas a registrar (eliminadas OK): {len(correos_eliminados)}")
    print(f" Registros correctos: {Color.GREEN}{reg_ok}{Color.ENDC}")
    print(f" Registros fallidos: {Color.FAIL}{reg_fail}{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}\n")

    print(f"\n{Color.GREEN}{Color.BOLD}>>> Proceso eliminar+registrar finalizado. "
          f"Regresando al menú principal...{Color.ENDC}\n")


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

    print(f"\n{Color.CYAN}[Opción 8] Solo registro Tidal (sin pago/TuneMyMusic): no se reservan proxies PE.{Color.ENDC}")

    headless_opt = input("\n¿Deseas ejecutar el navegador en segundo plano (headless)? (s/n, por defecto 'n'): ").strip().lower()
    headless = headless_opt in ("s", "si", "yes", "y")
    
    success_count = 0
    fail_count = 0
    estado_lock = threading.Lock()

    def registrar_un_correo(idx, correo):
        nonlocal success_count, fail_count
        p_ng_server = p_ng_user = p_ng_pass = None
        manager = None
        exito = False
        proxies_ya_liberados = False
        try:
            if use_proxy and valid_ng_list:
                p_ng = GLOBAL_NG_PROXY_POOL.obtener_proxy_unico()
                if not p_ng:
                    print(f"  {Color.FAIL}[Proxy NG] [{correo}] Sin proxy de Nigeria libre; se omite la cuenta.{Color.ENDC}")
                    with estado_lock:
                        fail_count += 1
                    return correo, False
                p_ng_server = p_ng.get("server")
                p_ng_user = p_ng.get("username")
                p_ng_pass = p_ng.get("password")

            manager = TidalRegisterManager(
                client_email=correo,
                client_pwd="",
                proxy_ng_server=p_ng_server,
                proxy_ng_user=p_ng_user,
                proxy_ng_pass=p_ng_pass,
                proxy_pe_server=None,
                proxy_pe_user=None,
                proxy_pe_pass=None,
                headless=headless
            )

            print(f"\n{Color.CYAN}{Color.BOLD}[Registro Concurrente] Iniciando proceso para: {correo}{Color.ENDC}")
            # cerrar_navegador_al_final=True ya libera NG/PE en el finally de run_registration.
            exito = manager.run_registration(cerrar_navegador_al_final=True)
            proxies_ya_liberados = True
            if not exito:
                try:
                    manager.limpiar_perfil_temporal()
                except Exception:
                    pass
                with estado_lock:
                    fail_count += 1
                return correo, False

            try:
                manager.limpiar_perfil_temporal()
            except Exception:
                pass
            with estado_lock:
                success_count += 1
            print(f"  {Color.GREEN}[Registro] [{correo}] Completado. Opción 8 finaliza aquí (sin TuneMyMusic).{Color.ENDC}")
            return correo, True
        except Exception as e_reg:
            if manager is not None:
                try:
                    if not proxies_ya_liberados:
                        manager.cerrar_navegador(liberar_ng=True, liberar_pe=True)
                        proxies_ya_liberados = True
                    else:
                        # Navegador ya cerrado por run_registration; no liberar proxy otra vez.
                        manager.cerrar_navegador(liberar_ng=False, liberar_pe=False)
                except Exception:
                    pass
                try:
                    manager.limpiar_perfil_temporal()
                except Exception:
                    pass
            with estado_lock:
                if not exito:
                    fail_count += 1
            print(f"  {Color.FAIL}[ERROR] Excepción en registro de {correo}: {e_reg}{Color.ENDC}")
            raise
        finally:
            cerrar_sesion_imap_hilo()
            # Evitar doble liberar_proxy: si run_registration ya devolvió el NG al pool,
            # liberarlo otra vez podía marcar como libre un proxy ya asignado a otro hilo.
            if not exito and not proxies_ya_liberados and p_ng_server:
                try:
                    GLOBAL_NG_PROXY_POOL.liberar_proxy(p_ng_server)
                except Exception:
                    pass
                if manager is not None:
                    manager.proxy_ng_server = None
                    manager.proxy_pe_server = None

    if not correos:
        print(f"\n{Color.WARNING}[Opción 8] No hay correos para registrar.{Color.ENDC}")
        return
    workers = min(10, len(correos))
    print(f"\n{Color.CYAN}{Color.BOLD}Iniciando registro de {len(correos)} cuentas de forma simultánea (usando {workers} hilos)...{Color.ENDC}\n")
    print(f"{Color.CYAN}Opción 8: solo registro Tidal. Al terminar cada cuenta se cierra el proceso (sin TuneMyMusic).{Color.ENDC}\n")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(registrar_un_correo, idx, correo): correo for idx, correo in enumerate(correos, 1)}
        for future in as_completed(futures):
            correo = futures[future]
            try:
                future.result()
            except Exception as e:
                print(f"  {Color.FAIL}[ERROR] Excepción inesperada procesando {correo}: {e}{Color.ENDC}")

    print(f"\n{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}   RESUMEN DEL REGISTRO{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}")
    print(f" Cuentas procesadas con éxito: {Color.GREEN}{success_count}{Color.ENDC}")
    print(f" Cuentas fallidas: {Color.FAIL}{fail_count}{Color.ENDC}")
    print(f"{Color.BLUE}{Color.BOLD}" + "="*60 + f"{Color.ENDC}\n")
    print(f"\n{Color.GREEN}{Color.BOLD}>>> Proceso de registro finalizado. Regresando al menú principal...{Color.ENDC}\n")



def parsear_titular_familiar_txt_opcion11(path: Path) -> tuple[list[dict], list[str]]:
    """Lee titular_familiar.txt.

    Formato por bloques (con metadatos; el script lo escribe al guardar):
        TITULAR
        correo@..., 0, disponible, []
        MIEMBROS:
        miembro1@...
        miembro2@...

    Formato compacto (pegado masivo):
        titular@gmail.com
          miembro1@gmail.com
          miembro2@gmail.com
        (línea sin indentar = titular; indentada = miembro; línea en blanco opcional)

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

    def _email_limpio(raw: str) -> str:
        """Extrae el correo: ignora tabs/espacios y columnas extra (p. ej. email\\tpwd)."""
        s = (raw or "").strip()
        if not s:
            return ""
        # Formato compacto pegado desde Excel/bloc: correo<TAB>contraseña
        primer = re.split(r'[\s,;]+', s, maxsplit=1)[0].strip()
        return re.sub(r'^[\s\.]+|[\s\.]+$', '', primer)

    def _titular_nuevo(correo: str, usados: int = 0, estado: str = "disponible",
                       miembros: list | None = None) -> dict:
        miembros = list(miembros or [])
        usados = max(usados, len(miembros))
        if usados >= 5 or "lleno" in (estado or "").lower():
            estado = "lleno"
        return {
            "correo": correo,
            "usados": usados,
            "estado": estado,
            "miembros": miembros,
            "miembros_invitar": [],
        }

    def _parse_linea_titular(line_clean: str) -> dict | None:
        if "," not in line_clean or "@" not in line_clean:
            return None
        parts = [p.strip() for p in line_clean.split(",")]
        if not parts or "@" not in parts[0]:
            return None
        correo_t = _email_limpio(parts[0])
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
        return _titular_nuevo(correo_t, usados=usados, estado=estado, miembros=miembros_detalles)

    lineas_utiles = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]

    # Detectar formato por bloques (línea TITULAR)
    tiene_bloques = any(
        ln.strip().upper() == "TITULAR" or ln.strip().upper().startswith("TITULAR")
        for ln in lineas_utiles
    )

    # Formato compacto: miembros indentados (espacios/tab) sin keyword TITULAR
    tiene_compacto = (not tiene_bloques) and any(
        (ln.startswith(" ") or ln.startswith("\t")) and "@" in ln
        for ln in lines
        if ln.strip() and not ln.strip().startswith("#")
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
                    correo_m = _email_limpio(line_clean)
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

    if tiene_compacto:
        actual = None
        for line in lines:
            raw = line.rstrip("\r\n")
            line_clean = raw.strip()
            if not line_clean or line_clean.startswith("#"):
                continue
            upper = line_clean.upper()
            if upper == "TITULAR" or upper.startswith("TITULAR") or upper.startswith("MIEMBROS"):
                continue
            indented = bool(raw) and raw[0] in " \t"
            if indented:
                if "@" not in line_clean or actual is None:
                    continue
                correo_m = _email_limpio(line_clean)
                if correo_m and "@" in correo_m:
                    if correo_m not in actual["miembros_invitar"]:
                        actual["miembros_invitar"].append(correo_m)
                    miembros_planos.append(correo_m)
                continue
            # Sin indentar: nuevo titular (email solo o línea con metadatos)
            if actual:
                titulares.append(actual)
            parsed = _parse_linea_titular(line_clean)
            if parsed:
                actual = parsed
            elif "@" in line_clean and "," not in line_clean:
                correo_t = _email_limpio(line_clean)
                actual = _titular_nuevo(correo_t) if correo_t and "@" in correo_t else None
            else:
                actual = None
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
                correo_m = _email_limpio(line_clean)
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
    
    gmail_user_solicitado = destino_imap_de_alias((gmail_user_solicitado or "").lower().strip())
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
    forwards: dict[str, str] = {}
    
    for correo in correos:
        correo_l = (correo or "").strip().lower()
        dest = destino_imap_de_alias(correo_l)
        if dest != correo_l and "@" in correo_l:
            forwards[correo_l.split("@", 1)[1]] = dest
        if not tiene_contrasena_imap_registrada(correo):
            correo_limpio = remover_puntos_correo(correo)
            if correo_limpio not in faltantes:
                faltantes.append(correo_limpio)

    if forwards:
        print(f"\n{Color.CYAN}Catch-all (Cloudflare Email Routing):{Color.ENDC}")
        for dom, dest in sorted(forwards.items()):
            print(f"  @{dom} → IMAP {dest}")
            
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
        print(" 4. Aceptar ENLACE DE INVITACIÓN (IMAP / pegar links / linksextraidos.txt + auto + cerrar)")
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
        print(" 15. ELIMINAR (proxy PE) + REGISTRAR (proxy NG) cuenta(s) TIDAL")
        print(f"{Color.CYAN}{Color.BOLD}" + "-"*50 + f"{Color.ENDC}")
        
        opcion = input(f"{Color.BOLD}Selecciona una opción (1-15):{Color.ENDC} ").strip()
        
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
                enlace = None
                for intento_imap in range(1, 6):
                    enlace = obtener_codigo_via_imap(
                        gmail_user=correo,
                        required_keywords=KEYWORDS_RESTABLECER_PWD,
                        query_exclude="cancel",
                        solo_link=True,
                        max_age_minutes=20,
                        silencioso=(intento_imap > 1),
                    )
                    if enlace:
                        break
                    if intento_imap < 5:
                        print(f"    {Color.WARNING}[IMAP] Sin enlace aún (intento "
                              f"{intento_imap}/5). Reintentando...{Color.ENDC}")
                        time.sleep(2.5)
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
            print("  Cuentas ya registradas: login con proxy PE (sesiones_imap_cuentas.txt o código IMAP).")
            print("  Cuentas aún sin registrar: alta automática con proxy NG (DOB + Suscríbete + OTP IMAP).")
            print("  Hasta 5 alias del mismo Gmail en paralelo sin mezclar códigos.")

            enlaces_manual, origen_enlaces = pedir_fuente_enlaces_opcion4(correos)
            if origen_enlaces == "imap":
                # Asignación coordinada por buzón: N alias con puntos del mismo Gmail ya no
                # compiten por el mismo UID (To: canónico) dejando 4/5 sin enlace.
                print("  Buscando y asignando enlaces de invitación (coordinado por buzón Gmail)...")
                enlaces_map = asignar_enlaces_invitacion_a_correos(correos)
            else:
                enlaces_map = dict(enlaces_manual or {})

            for c in correos:
                e = enlaces_map.get(c)
                if not e:
                    # claves pueden estar strip'eadas
                    e = next(
                        (enlaces_map[k] for k in enlaces_map
                         if k.strip().lower() == c.strip().lower()),
                        None,
                    )
                    if e:
                        enlaces_map[c] = e
                if e:
                    preview = e if len(e) <= 90 else e[:90] + "..."
                    etiqueta = "IMAP" if origen_enlaces == "imap" else (
                        "Archivo" if origen_enlaces == "archivo" else "Manual"
                    )
                    print(f"    {Color.GREEN}[{etiqueta}] Enlace para {c}: {preview}{Color.ENDC}")
                    if buscar_contrasena_cuenta(c):
                        print(f"    {Color.GREEN}[Cuentas] Contraseña lista para auto-login de {c}.{Color.ENDC}")
                    else:
                        print(f"    {Color.CYAN}[Cuentas] Sin contraseña en sesiones_imap_cuentas.txt "
                              f"para {c} — si es cuenta nueva se hará el alta automática; "
                              f"si ya existe se usará código IMAP.{Color.ENDC}")
                else:
                    origen_msg = "IMAP" if origen_enlaces == "imap" else "fuente elegida"
                    print(f"    {Color.FAIL}[{origen_msg}] No se encontró invitación para {c}{Color.ENDC}")

            enlaces_map = {c: enlaces_map[c] for c in correos if enlaces_map.get(c)}
            # Anotar en linksextraidos.txt (también los hallados por IMAP)
            if enlaces_map:
                try:
                    path_links = guardar_enlaces_en_linksextraidos(enlaces_map, merge=True)
                    print(f"\n  {Color.CYAN}[Enlaces] Guardados/actualizados en {path_links}{Color.ENDC}")
                except Exception as e_save:
                    print(f"\n  {Color.WARNING}[Enlaces] No se pudo escribir "
                          f"{LINKS_EXTRAIDOS_PATH.name}: {e_save}{Color.ENDC}")

            sin_enlace = [c for c in correos if not enlaces_map.get(c)]
            ok_list: list[str] = []
            fail_list: list[str] = list(sin_enlace)  # sin invitación = pendiente/fallo

            if enlaces_map:
                global valid_pe_list, CACHE_PROXIES_PE, valid_ng_list, CACHE_PROXIES_NG
                # Alta de cuentas nuevas → Nigeria. Login de cuentas existentes → Perú.
                sin_pwd = [c for c in enlaces_map if not buscar_contrasena_cuenta(c)]
                con_pwd = [c for c in enlaces_map if buscar_contrasena_cuenta(c)]
                print(f"\n{Color.CYAN}[Opción 4] {len(sin_pwd)} cuenta(s) sin pwd → alta con proxy NG; "
                      f"{len(con_pwd)} con pwd → login/aceptar con proxy PE.{Color.ENDC}")

                proxies_ng = []
                # Siempre alimentar NG: si una cuenta "con pwd" resulta no registrada,
                # el flujo cambia a Nigeria en caliente.
                print(f"\n{Color.CYAN}[Proxies NG] Habilitando proxies de Nigeria para altas "
                      f"desde invitación...{Color.ENDC}")
                cargar_cache_proxies_validos_desde_disco()
                if CACHE_PROXIES_NG:
                    valid_ng_list = list(CACHE_PROXIES_NG)
                    print(f"  {Color.GREEN}[Proxy Caché] Usando {len(valid_ng_list)} proxies NG "
                          f"verificados.{Color.ENDC}")
                else:
                    proxies_cfg = cargar_proxies_desde_txt(preferir_validos=False)
                    ng_list = (proxies_cfg or {}).get("proxy_ng_list") or []
                    if ng_list:
                        valid_ng_list = probar_y_seleccionar_mejor_proxy(
                            ng_list, "NIGERIA", max(len(enlaces_map) * 4, len(enlaces_map) + 15)
                        )
                        if valid_ng_list:
                            guardar_proxies_validos_txt(
                                SCRIPT_DIR / "lista_proxies_ng_validos.txt", valid_ng_list
                            )
                    else:
                        valid_ng_list = []
                alimentar_pool_proxies_nigeria(valid_ng_list)
                GLOBAL_NG_PROXY_POOL.reiniciar_bloqueos()
                proxies_ng = list(valid_ng_list or [])
                if sin_pwd and not proxies_ng:
                    print(f"\n{Color.FAIL}[Error]{Color.ENDC} Hay cuentas sin registrar y no hay "
                          f"proxies de Nigeria. Valida con la opción 13.")
                    fail_list.extend(list(enlaces_map.keys()))
                    _imprimir_resumen_opcion4(correos, ok_list, fail_list, sin_enlace)
                    print()
                    continue

                print(f"\n{Color.CYAN}[Proxies PE] Habilitando proxies de Perú para login/aceptar "
                      f"invitaciones...{Color.ENDC}")
                proxies_pe = asegurar_proxies_peru(cantidad_necesaria=len(enlaces_map))
                if con_pwd and not proxies_pe:
                    print(f"\n{Color.FAIL}[Error]{Color.ENDC} No hay proxies de Perú válidos y hay "
                          f"cuentas con login. Valida la lista con la opción 13.")
                    fail_list.extend(list(enlaces_map.keys()))
                    _imprimir_resumen_opcion4(correos, ok_list, fail_list, sin_enlace)
                    print()
                    continue
                if not proxies_pe and not proxies_ng:
                    print(f"\n{Color.FAIL}[Error]{Color.ENDC} Sin proxies PE ni NG. Abortando opción 4.")
                    fail_list.extend(list(enlaces_map.keys()))
                    _imprimir_resumen_opcion4(correos, ok_list, fail_list, sin_enlace)
                    print()
                    continue

                items = list(enlaces_map.items())
                tam_oleada = 5
                if len(items) > 10:
                    oleadas = [items[i:i + tam_oleada] for i in range(0, len(items), tam_oleada)]
                    print(f"\n{Color.CYAN}[Opción 4] {len(items)} invitaciones → "
                          f"{len(oleadas)} oleadas de hasta {tam_oleada}.{Color.ENDC}")
                else:
                    oleadas = [items]
                    print(f"\nAbriendo {len(items)} invitaciones familiares "
                          f"(alta NG / login PE + aceptar + cerrar, hasta "
                          f"{min(tam_oleada, len(items))} en paralelo)...")

                def procesar_invitacion_hilo(idx, item):
                    correo, enlace = item
                    # Más separación en altas NG: 5 ablink simultáneos → ERR_TUNNEL frecuente
                    if buscar_contrasena_cuenta(correo):
                        if idx > 1:
                            time.sleep((idx - 1) * random.uniform(1.5, 3.0))
                    else:
                        time.sleep((idx - 1) * random.uniform(2.8, 4.5))
                    p_pe = None
                    p_ng = None
                    if buscar_contrasena_cuenta(correo):
                        p_pe = GLOBAL_PE_PROXY_POOL.obtener_proxy_unico(espera_s=60.0)
                        if not p_pe and proxies_pe:
                            p_pe = proxies_pe[(idx - 1) % len(proxies_pe)]
                    else:
                        p_ng = GLOBAL_NG_PROXY_POOL.obtener_proxy_unico(espera_s=60.0)
                        if not p_ng and proxies_ng:
                            p_ng = proxies_ng[(idx - 1) % len(proxies_ng)]
                    return correo, bool(abrir_enlace_familia_con_autocierre(
                        enlace, correo, proxy_pe=p_pe, proxy_ng=p_ng
                    ))

                for n_oleada, oleada in enumerate(oleadas, 1):
                    if len(oleadas) > 1:
                        print(f"\n{Color.BLUE}{Color.BOLD}=== Oleada {n_oleada}/{len(oleadas)}: "
                              f"{len(oleada)} invitación(es) ==={Color.ENDC}")
                        for correo_o, _ in oleada:
                            print(f"    • {correo_o}")
                    workers = min(tam_oleada, len(oleada))
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        futures = {
                            executor.submit(procesar_invitacion_hilo, idx + 1, item): item[0]
                            for idx, item in enumerate(oleada)
                        }
                        for future in as_completed(futures):
                            correo_f = futures[future]
                            try:
                                c_res, exito = future.result()
                                if exito:
                                    ok_list.append(c_res)
                                else:
                                    fail_list.append(c_res)
                            except Exception as ex_h:
                                fail_list.append(correo_f)
                                print(f"    {Color.FAIL}[ERROR] Excepción en invitación "
                                      f"de {correo_f}: {ex_h}{Color.ENDC}")
                    if n_oleada < len(oleadas):
                        print(f"  {Color.CYAN}[Opción 4] Oleada {n_oleada} terminada. "
                              f"Pasando a la siguiente...{Color.ENDC}")
                        time.sleep(1.5)
            else:
                print(f"\n{Color.FAIL}>>> No se encontró ningún enlace de invitación en las cuentas activas. <<<\n")

            _imprimir_resumen_opcion4(correos, ok_list, fail_list, sin_enlace)
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

        elif opcion == "15":
            eliminar_cuentas_tidal_automatico_opcion15(correos)
            
        else:
            print(f"\n{Color.FAIL}[Error]{Color.ENDC} Opción inválida. Selecciona un número del 1 al 15.")


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
