#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Cliente HTTP del Email Worker de Cloudflare (OTP y enlaces Tidal)."""

from __future__ import annotations

import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SCRIPT_DIR = Path(__file__).resolve().parent

_CFG_CACHE: dict | None = None
_CFG_MTIME: float | None = None
_BASELINE_TS: dict[str, float] = {}
_SESSION: requests.Session | None = None
_LAST_NET_ERR = 0.0


def _http_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=0.2,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
        sess = requests.Session()
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _SESSION = sess
    return _SESSION


def _pares_passwords() -> list[tuple[str, str]]:
    pwd_file = SCRIPT_DIR / "passwords.txt"
    if not pwd_file.exists():
        return []
    try:
        lines = pwd_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for line in lines:
        raw = (line or "").strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, val = raw.split("=", 1)
        out.append((key.strip().lower(), val.strip().strip('"').strip("'")))
    return out


def worker_config() -> dict:
    global _CFG_CACHE, _CFG_MTIME
    pwd_file = SCRIPT_DIR / "passwords.txt"
    try:
        mtime = pwd_file.stat().st_mtime
    except Exception:
        mtime = None
    if _CFG_CACHE is not None and mtime == _CFG_MTIME:
        return _CFG_CACHE
    cfg = {"url": "", "secret": "", "imap_fallback": False, "timeout": 8.0}
    for key, val in _pares_passwords():
        if key in ("email_worker_url", "otp_worker_url") and val:
            cfg["url"] = val.rstrip("/")
        elif key in ("email_worker_secret", "otp_worker_secret") and val:
            cfg["secret"] = val
        elif key in ("email_worker_imap_fallback", "otp_worker_imap_fallback"):
            cfg["imap_fallback"] = val.lower() in ("1", "true", "si", "yes", "on")
        elif key == "email_worker_timeout":
            try:
                cfg["timeout"] = float(val)
            except Exception:
                pass
    _CFG_CACHE = cfg
    _CFG_MTIME = mtime
    return cfg


def worker_habilitado() -> bool:
    cfg = worker_config()
    return bool(cfg.get("url") and cfg.get("secret"))


def worker_cubre_alias(alias: str) -> bool:
    """El worker solo recibe el catch-all (p. ej. @cheapmusic.best), no Gmail nativo."""
    a = (alias or "").strip().lower()
    if not worker_habilitado() or "@" not in a:
        return False
    if a.endswith("@gmail.com") or a.endswith("@googlemail.com"):
        return False
    return True


def marcar_baseline_worker(alias: str) -> None:
    a = (alias or "").strip().lower()
    if a:
        _BASELINE_TS[a] = time.time()


def baseline_worker(alias: str) -> float:
    return float(_BASELINE_TS.get((alias or "").strip().lower(), 0) or 0)


def kind_desde_keywords(required_keywords, solo_link: bool = False) -> str:
    blob = " ".join(str(k or "").lower() for k in (required_keywords or []))
    if solo_link:
        if any(x in blob for x in ("resetpass", "reset", "restablec", "restaurar", "password", "contrase")):
            return "reset"
        if any(x in blob for x in ("invit", "family", "familia", "join")):
            return "invite"
        return "invite"
    if any(x in blob for x in ("elimin", "desactiv", "delete")):
        return "delete"
    if any(x in blob for x in ("registr", "bienven", "sign-up", "signup", "finish creating", "terminar de crear")):
        return "register"
    if any(x in blob for x in ("inici", "login", "sign-in", "signin", "acceso")):
        return "login"
    if "code" in blob or "código" in blob or "codigo" in blob or "verific" in blob:
        return "login"
    return "login"


def _get(path: str, params: dict) -> dict | None:
    cfg = worker_config()
    if not cfg["url"] or not cfg["secret"]:
        return None
    url = cfg["url"] + path
    headers = {
        "X-OTP-Secret": cfg["secret"],
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    try:
        r = _http_session().get(url, params=params, headers=headers, timeout=cfg["timeout"])
        if r.status_code == 401:
            print("    [WORKER] Secreto incorrecto (401). Revisa email_worker_secret en passwords.txt")
            return None
        if r.status_code != 200:
            print(f"    [WORKER] HTTP {r.status_code} en {path}")
            return None
        data = r.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        global _LAST_NET_ERR
        now = time.time()
        if now - _LAST_NET_ERR >= 5.0:
            print(f"    [WORKER] Error de red: {e}")
            _LAST_NET_ERR = now
        return None


def intentar_via_worker(
    alias: str,
    required_keywords=None,
    solo_link: bool = False,
    max_age_minutes: int = 15,
    after_email_id: int = 0,
    silencioso: bool = False,
    aliases: list[str] | None = None,
) -> tuple[str | None, bool]:
    """Devuelve (valor, omitir_imap). omitir_imap=True si el catch-all va por worker."""
    candidatos = []
    for a in [alias, *(aliases or [])]:
        al = (a or "").strip().lower()
        if al and al not in candidatos:
            candidatos.append(al)
    if not candidatos:
        return None, False

    kind = kind_desde_keywords(required_keywords, solo_link)
    skip_all = True
    any_cover = False
    for al in candidatos:
        if not worker_cubre_alias(al):
            skip_all = False
            continue
        any_cover = True
        val = reclamar_desde_worker(
            al, kind,
            max_age_minutes=max_age_minutes,
            after_email_id=after_email_id,
            silencioso=silencioso,
        )
        if val:
            return val, True
        if worker_config().get("imap_fallback"):
            skip_all = False
    if any_cover and skip_all:
        return None, True
    return None, False


def _after_ts_claim(alias: str, after_email_id: int = 0) -> float:
    """Convierte baseline IMAP → after_ts del worker.

    obtener_max_email_id() para catch-all devuelve 1 (dummy). Tratarlo como filtro
    real excluía el OTP que Tidal ya había metido en KV (reloj local vs Cloudflare,
    o baseline tomada al ver la pantalla de código, *después* de que llegara el mail).
    """
    try:
        eid = int(after_email_id or 0)
    except Exception:
        eid = 0
    if eid <= 1:
        return 0.0
    ts = baseline_worker(alias)
    if ts <= 0:
        return 0.0
    return max(0.0, ts - 300.0)


def reclamar_desde_worker(
    alias: str,
    kind: str,
    max_age_minutes: int = 15,
    after_email_id: int = 0,
    silencioso: bool = False,
    consume: bool = True,
    despues_de: float | None = None,
) -> str | None:
    alias = (alias or "").strip().lower()
    kind = (kind or "").strip().lower()
    if not worker_cubre_alias(alias) or kind not in ("login", "register", "delete", "reset", "invite"):
        return None
    max_age = max(60, int((max_age_minutes or 15) * 60))
    after_ts = _after_ts_claim(alias, after_email_id)
    if despues_de:
        try:
            after_ts = max(after_ts, float(despues_de))
        except Exception:
            pass
    params = {
        "alias": alias,
        "kind": kind,
        "after_ts": f"{after_ts:.3f}",
        "max_age": str(max_age),
        "consume": "1" if consume else "0",
        "secret": worker_config()["secret"],
        "_": f"{time.time():.3f}",
    }
    data = _get("/claim", params)
    if not data or not data.get("ok") or not data.get("value"):
        return None
    val = str(data.get("value") or "").strip()
    if not val:
        return None
    if kind in ("invite", "reset"):
        val = resolver_enlace_worker(val, kind)
    if not silencioso:
        print(f"    [WORKER] {kind} para {alias}: {val[:96]}")
    return val


def esperar_desde_worker(
    alias: str,
    kind: str,
    *,
    max_wait_s: float = 18.0,
    interval_s: float = 0.22,
    after_email_id: int = 0,
    max_age_minutes: int = 15,
    silencioso: bool = False,
    consume: bool = True,
    despues_de: float | None = None,
) -> str | None:
    """Sondea /claim cada ~200 ms hasta que el correo llegue al worker."""
    alias = (alias or "").strip().lower()
    if not worker_cubre_alias(alias):
        return None
    t0 = time.time()
    visto = False
    ultimo_hb = 0.0
    tope = max(1.0, float(max_wait_s))
    while time.time() - t0 < tope:
        val = reclamar_desde_worker(
            alias, kind,
            max_age_minutes=max_age_minutes,
            after_email_id=after_email_id,
            silencioso=True,
            consume=consume,
            despues_de=despues_de,
        )
        if val:
            if not silencioso:
                print(f"    [WORKER] {kind} para {alias}: {val[:96]} ({time.time() - t0:.1f}s)", flush=True)
            return val
        elapsed = time.time() - t0
        if not silencioso and elapsed >= 2.0 and (not visto or elapsed - ultimo_hb >= 3.0):
            print(f"    [WORKER] Esperando {kind} para {alias}... ({elapsed:.0f}s/{tope:.0f}s)", flush=True)
            visto = True
            ultimo_hb = elapsed
        time.sleep(max(0.08, float(interval_s)))
    return None


def resolver_enlace_worker(url: str, kind: str = "invite") -> str:
    """Si Tidal mandó ablink de tracking, sigue redirects hasta family/resetpass."""
    u = (url or "").strip()
    if not u.startswith("http"):
        return u
    ul = u.lower()
    if "ablink." not in ul and "info.tidal.com" not in ul:
        return u
    try:
        r = requests.get(
            u,
            allow_redirects=True,
            timeout=(4, 8),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
            },
        )
        final = (getattr(r, "url", None) or u).strip()
        fl = final.lower()
        if kind == "reset" and "resetpass" in fl:
            return final
        if kind == "invite" and (
            "family" in fl or "accept" in fl or "/join/" in fl
        ) and "resetpass" not in fl:
            return final
        return final if final.startswith("http") else u
    except Exception:
        return u


def reclamar_invites_para_aliases(
    aliases: list[str],
    max_age_minutes: int = 1440,
) -> tuple[dict[str, str], list[str]]:
    """Reclama invitaciones del worker. Devuelve (asignados, aliases para IMAP)."""
    asignados: dict[str, str] = {}
    restantes: list[str] = []
    skip_imap = not worker_config().get("imap_fallback")
    for a in aliases or []:
        al = (a or "").strip()
        if not al:
            continue
        if not worker_cubre_alias(al):
            restantes.append(al)
            continue
        enlace = reclamar_desde_worker(
            al, "invite", max_age_minutes=max_age_minutes, silencioso=False,
        )
        if enlace and "resetpass" not in enlace.lower():
            asignados[al] = enlace
            continue
        if skip_imap:
            continue
        restantes.append(al)
    return asignados, restantes


def listar_invites_worker(alias: str, max_age_minutes: int = 1440) -> list[dict]:
    alias = (alias or "").strip().lower()
    if not worker_cubre_alias(alias):
        return []
    max_age = max(60, int((max_age_minutes or 1440) * 60))
    data = _get("/list", {
        "alias": alias,
        "kind": "invite",
        "max_age": str(max_age),
        "secret": worker_config()["secret"],
    })
    if not data or not data.get("ok"):
        return []
    out: list[dict] = []
    for it in data.get("items") or []:
        link = str((it or {}).get("value") or "").strip()
        if not link.startswith("http") or "resetpass" in link.lower():
            continue
        link = resolver_enlace_worker(link, "invite")
        if "resetpass" in link.lower():
            continue
        out.append({
            "uid": hash(link) & 0x7FFFFFFF,
            "recipients": alias,
            "body": str((it or {}).get("subject") or ""),
            "link": link,
            "score": 100,
        })
    return out


def cubre_y_esperar_reset(alias: str, max_wait_s: float = 20.0) -> tuple[bool, str | None]:
    """Si el catch-all va por worker, espera el enlace de reset (sin IMAP)."""
    if not worker_cubre_alias(alias):
        return False, None
    val = esperar_desde_worker(
        alias, "reset",
        max_wait_s=max(6.0, float(max_wait_s)),
        interval_s=0.22,
        after_email_id=0,
        max_age_minutes=20,
        silencioso=False,
    )
    return True, val


def worker_salud() -> tuple[bool, str]:
    cfg = worker_config()
    if not cfg["url"]:
        return False, "sin email_worker_url en passwords.txt"
    if not cfg["secret"]:
        return False, "sin email_worker_secret en passwords.txt"
    try:
        r = requests.get(cfg["url"] + "/health", timeout=5)
        if r.status_code == 200:
            return True, cfg["url"]
        return False, f"HTTP {r.status_code} {cfg['url']}"
    except Exception as e:
        return False, f"{cfg['url']} ({e})"
