#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Cliente HTTP del Email Worker de Cloudflare (OTP y enlaces Tidal)."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SCRIPT_DIR = Path(__file__).resolve().parent

_CFG_CACHE: dict | None = None
_CFG_MTIME: float | None = None
_BASELINE_TS: dict[str, float] = {}
_SESSION: requests.Session | None = None
_LAST_NET_ERR = 0.0
_LIST_MANY_OK: bool | None = None


def _http_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=0.2,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=64, pool_maxsize=64)
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
    cfg = {"url": "", "secret": "", "imap_fallback": False, "timeout": 2.5}
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


_WORKER_CATCHALL_DOMAINS = (
    "cheapmusic.best",
    "cheapmusic.beast",  # alias/typo frecuente del dominio catch-all
)


def worker_cubre_alias(alias: str) -> bool:
    """El worker solo recibe el catch-all @cheapmusic.best, no Gmail nativo."""
    a = (alias or "").strip().lower()
    if not worker_habilitado() or "@" not in a:
        return False
    if a.endswith("@gmail.com") or a.endswith("@googlemail.com"):
        return False
    dominio = a.rsplit("@", 1)[-1]
    return dominio in _WORKER_CATCHALL_DOMAINS


def usar_imap_gmail(alias: str) -> bool:
    """IMAP del Gmail de FORWARD_TO. Off para catch-all salvo imap_fallback=1."""
    if not worker_cubre_alias(alias):
        return True
    return bool(worker_config().get("imap_fallback"))


_MIN_UPN_CTA = 140


def upn_token_invite(url: str) -> str:
    u = (url or "").strip()
    if "upn=" not in u.lower():
        return ""
    try:
        from urllib.parse import unquote
        raw = u.split("upn=", 1)[1].split("&")[0]
        return unquote(raw).split("_")[0]
    except Exception:
        return u.split("upn=", 1)[1].split("&")[0].split("_")[0]


def enlace_invite_completo(url: str) -> bool:
    """True si es accept-invite directo o el CTA /ls/click con upn largo (no el logo → ?lid=)."""
    u = (url or "").strip()
    ul = u.lower()
    if not ul.startswith("http"):
        return False
    if "resetpass" in ul or "reset-password" in ul or "/wf/open" in ul:
        return False
    if "tidal.com/?" in ul and "lid=" in ul:
        return False
    if (
        "login.tidal.com/family" in ul
        or "account.tidal.com/family" in ul
        or "accept-invite" in ul
        or "/accept/" in ul
        or "/join/" in ul
    ):
        return True
    if "ablink." in ul and "tidal" in ul and "/ls/click" in ul and "upn=" in ul:
        return len(upn_token_invite(u)) >= _MIN_UPN_CTA
    return False


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
    global _LAST_NET_ERR
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
        # /claim y /list se sondean cada ~80 ms: sin retries (un timeout de 5s×3
        # bloqueaba el OTP varios segundos aunque el mail ya estaba en KV).
        fast = path in ("/claim", "/list", "/peek", "/pipeline-stop")
        timeout = min(float(cfg["timeout"] or 2.5), 1.8) if fast else float(cfg["timeout"] or 2.5)
        getter = requests.get if fast else _http_session().get
        r = getter(url, params=params, headers=headers, timeout=timeout)
        if r.status_code == 401:
            print("    [WORKER] Secreto incorrecto (401). Revisa email_worker_secret en passwords.txt")
            return None
        if r.status_code != 200:
            now = time.time()
            if now - _LAST_NET_ERR >= 8.0:
                print(f"    [WORKER] HTTP {r.status_code} en {path}")
                _LAST_NET_ERR = now
            return None
        data = r.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        now = time.time()
        if now - _LAST_NET_ERR >= 5.0:
            print(f"    [WORKER] Error de red: {e}")
            _LAST_NET_ERR = now
        return None


def _post(path: str, payload: dict, timeout: float | None = None) -> dict | None:
    global _LAST_NET_ERR
    cfg = worker_config()
    if not cfg["url"] or not cfg["secret"]:
        return None
    url = cfg["url"] + path
    headers = {
        "X-OTP-Secret": cfg["secret"],
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Content-Type": "application/json",
    }
    to = float(timeout) if timeout else max(float(cfg.get("timeout") or 5.0), 15.0)
    try:
        r = _http_session().post(
            url,
            json=payload or {},
            headers=headers,
            params={"secret": cfg["secret"], "_": f"{time.time():.3f}"},
            timeout=to,
        )
        if r.status_code == 401:
            print("    [WORKER] Secreto incorrecto (401). Revisa email_worker_secret en passwords.txt")
            return None
        if r.status_code != 200:
            now = time.time()
            if now - _LAST_NET_ERR >= 8.0:
                print(f"    [WORKER] HTTP {r.status_code} en {path}")
                _LAST_NET_ERR = now
            return None
        data = r.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        now = time.time()
        if now - _LAST_NET_ERR >= 5.0:
            print(f"    [WORKER] Error de red POST {path}: {e}")
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
        # Tidal a veces clasifica el alta como login (o al revés); probar el otro kind.
        if not val and kind in ("login", "register"):
            other = "register" if kind == "login" else "login"
            val = reclamar_desde_worker(
                al, other,
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


def _after_ts_claim(alias: str, after_email_id: int = 0, margen_s: float = 8.0) -> float:
    """Convierte baseline IMAP / worker → after_ts.

    El catch-all (@cheapmusic.best) usa email_id dummy=1. Aun así hay que respetar
    marcar_baseline_worker(): si after_ts=0 el /claim devuelve un OTP viejo de KV
    (p. ej. eliminación previa) y Tidal lo rechaza mientras el código nuevo llega.
    """
    ts = baseline_worker(alias)
    if ts > 0:
        return max(0.0, ts - max(0.0, float(margen_s)))
    try:
        eid = int(after_email_id or 0)
    except Exception:
        eid = 0
    if eid <= 1:
        return 0.0
    return 0.0


def reclamar_desde_worker(
    alias: str,
    kind: str,
    max_age_minutes: int = 15,
    after_email_id: int = 0,
    silencioso: bool = False,
    consume: bool = True,
    despues_de: float | None = None,
    margen_s: float | None = None,
) -> str | None:
    alias = (alias or "").strip().lower()
    kind = (kind or "").strip().lower()
    if not worker_cubre_alias(alias) or kind not in ("login", "register", "delete", "reset", "invite"):
        return None
    max_age = max(60, int((max_age_minutes or 15) * 60))
    # delete/reset: margen corto (si es 90s se cuela el OTP de un intento anterior).
    if margen_s is None:
        margen_s = 12.0 if kind in ("delete", "reset") else 90.0
    after_ts = _after_ts_claim(alias, after_email_id, margen_s=min(8.0, float(margen_s)))
    if despues_de:
        try:
            after_ts = max(after_ts, max(0.0, float(despues_de) - float(margen_s)))
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
    got_alias = str(data.get("alias") or "").strip().lower()
    if got_alias and got_alias != alias:
        if not silencioso:
            print(f"    [WORKER] Ignorado OTP de {got_alias} (se pedía {alias})", flush=True)
        return None
    # Invite: no seguir el ablink por HTTP (4–8s c/u y Tidal a menudo cae en ?lid=).
    # El CTA /ls/click se abre en Chrome. Reset sí se resuelve (necesitamos resetpass).
    if kind == "reset":
        val = resolver_enlace_worker(val, kind)
    if not silencioso:
        print(f"    [WORKER] {kind} para {alias}: {val[:96]}")
    return val


def esperar_desde_worker(
    alias: str,
    kind: str,
    *,
    max_wait_s: float = 20.0,
    interval_s: float = 0.08,
    after_email_id: int = 0,
    max_age_minutes: int = 15,
    silencioso: bool = False,
    consume: bool = True,
    despues_de: float | None = None,
    margen_s: float | None = None,
) -> str | None:
    """Sondea /claim cada ~80 ms hasta que el correo llegue al worker."""
    alias = (alias or "").strip().lower()
    if not worker_cubre_alias(alias):
        return None
    t0 = time.time()
    visto = False
    ultimo_hb = 0.0
    tope = max(1.0, float(max_wait_s))
    alt_kinds: list[str] = []
    if kind == "register":
        alt_kinds = ["login"]
    elif kind == "login":
        alt_kinds = ["register"]
    elif kind == "delete":
        alt_kinds = ["login", "register"]

    def _otp_digitos(v: str | None) -> str:
        return "".join(ch for ch in str(v or "") if ch.isdigit())

    while time.time() - t0 < tope:
        val = reclamar_desde_worker(
            alias, kind,
            max_age_minutes=max_age_minutes,
            after_email_id=after_email_id,
            silencioso=True,
            consume=consume,
            despues_de=despues_de,
            margen_s=margen_s,
        )
        if not val:
            for alt_kind in alt_kinds:
                cand = reclamar_desde_worker(
                    alias, alt_kind,
                    max_age_minutes=max_age_minutes,
                    after_email_id=after_email_id,
                    silencioso=True,
                    consume=consume,
                    despues_de=despues_de,
                    margen_s=margen_s,
                )
                if not cand:
                    continue
                if kind == "delete" and not (5 <= len(_otp_digitos(cand)) <= 6):
                    continue
                val = cand
                break
        if val:
            if not silencioso:
                print(f"    [WORKER] {kind} para {alias}: {val[:96]} ({time.time() - t0:.1f}s)", flush=True)
            return val
        elapsed = time.time() - t0
        if not silencioso and elapsed >= 1.5 and (not visto or elapsed - ultimo_hb >= 2.0):
            print(f"    [WORKER] Esperando {kind} para {alias}... ({elapsed:.0f}s/{tope:.0f}s)", flush=True)
            visto = True
            ultimo_hb = elapsed
        time.sleep(max(0.06, float(interval_s)))
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
        if kind == "invite":
            if "resetpass" in fl:
                return ""
            if "family" in fl or "accept" in fl or "/join/" in fl:
                return final
            # tidal.com/?lid=... es home de campaña, no el accept familiar.
            # Conservar el ablink original para que Chrome haga el click real.
            return u
        return final if final.startswith("http") else u
    except Exception:
        return u


_INVITE_QS_RUIDO = frozenset({
    "lid", "utm", "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid", "_ga",
})


def _invite_url_identidad(url: str) -> str:
    """Identidad del invite: UUID de accept-invite, o upn= de ablink (no solo el path)."""
    try:
        p = urlparse((url or "").strip().split("#")[0])
    except Exception:
        return (url or "").strip().lower()
    host_path = f"{(p.netloc or '').lower()}{(p.path or '').rstrip('/').lower()}"
    try:
        qs = parse_qs(p.query, keep_blank_values=True)
    except Exception:
        qs = {}
    keep: list[str] = []
    for k, vals in (qs or {}).items():
        kl = (k or "").lower()
        if kl in _INVITE_QS_RUIDO or kl.startswith("utm_"):
            continue
        for v in vals:
            keep.append(f"{kl}={(v or '').strip()}")
    keep.sort()
    if keep:
        return host_path + "?" + "&".join(keep)
    return host_path


def _email_en_url(url: str) -> str:
    try:
        qs = parse_qs(urlparse(url or "").query)
    except Exception:
        return ""
    for key in ("email", "login_hint", "username", "user", "invitee"):
        for val in qs.get(key) or []:
            v = unquote(val or "").strip()
            if "@" in v:
                return v
    return ""


def reclamar_invites_para_aliases(
    aliases: list[str],
    max_age_minutes: int = 1440,
    max_wait_s: float = 8.0,
) -> tuple[dict[str, str], list[str]]:
    """Reclama invitaciones del worker. Devuelve (asignados, aliases para IMAP).

    Catch-all (@cheapmusic.best): si el worker no tiene el enlace, NO pasa a
    IMAP salvo email_worker_imap_fallback=1.
    """
    asignados = extraer_invites_worker(
        aliases,
        max_age_minutes=max_age_minutes,
        max_wait_s=max_wait_s,
        silencioso=False,
    )
    restantes: list[str] = []
    vistos_asig = {(k or "").strip().lower() for k in asignados}
    for a in aliases or []:
        al = (a or "").strip()
        if not al:
            continue
        if al.lower() in vistos_asig:
            continue
        if worker_cubre_alias(al) and not worker_config().get("imap_fallback"):
            continue
        restantes.append(al)
    return asignados, restantes


def _link_de_item_invite(it: dict | None) -> str:
    if not isinstance(it, dict):
        return ""
    return str(it.get("link") or it.get("value") or "").strip()


def _mejor_invite_de_hits(hits: list | None, alias: str) -> str:
    al = (alias or "").strip().lower()
    mejor = ""
    for it in hits or []:
        link = _link_de_item_invite(it if isinstance(it, dict) else None)
        if not link or "resetpass" in link.lower():
            continue
        em = _email_en_url(link)
        if em and em.strip().lower() != al:
            continue
        if enlace_invite_completo(link):
            if em and em.strip().lower() == al:
                return link
            if not mejor:
                mejor = link
    return mejor


def _listar_invites_lote(aliases: list[str], max_age_minutes: int) -> dict[str, list]:
    """Una sola POST /list-many; si el worker aún no la tiene, GET /list en paralelo."""
    global _LIST_MANY_OK
    cubiertos = []
    vistos = set()
    for a in aliases or []:
        al = (a or "").strip().lower()
        if not al or al in vistos or not worker_cubre_alias(al):
            continue
        vistos.add(al)
        cubiertos.append(al)
    if not cubiertos:
        return {}
    max_age = max(60, int((max_age_minutes or 1440) * 60))
    if _LIST_MANY_OK is not False:
        data = _post("/list-many", {
            "aliases": cubiertos,
            "kind": "invite",
            "max_age": max_age,
        })
        by = (data or {}).get("by_alias") if data and data.get("ok") else None
        if isinstance(by, dict):
            _LIST_MANY_OK = True
            out: dict[str, list] = {}
            for al in cubiertos:
                items = by.get(al) or []
                out[al] = items if isinstance(items, list) else []
            return out
        _LIST_MANY_OK = False

    out = {al: [] for al in cubiertos}

    def _one(al: str) -> tuple[str, list]:
        return al, listar_invites_worker(al, max_age_minutes)

    n_workers = min(32, max(1, len(cubiertos)))
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futs = [ex.submit(_one, al) for al in cubiertos]
        for fut in as_completed(futs):
            try:
                al, hits = fut.result()
                out[al] = hits or []
            except Exception:
                pass
    return out


def extraer_invites_worker(
    aliases: list[str],
    max_age_minutes: int = 2880,
    max_wait_s: float = 8.0,
    silencioso: bool = False,
) -> dict[str, str]:
    """Extrae invitaciones del Email Worker en segundos. Sin IMAP.

    Devuelve {alias_original: url}. Poll corto si el correo acaba de entrar a KV.
    """
    orig: dict[str, str] = {}
    for a in aliases or []:
        al = (a or "").strip()
        if not al or "@" not in al:
            continue
        key = al.lower()
        if key not in orig and worker_cubre_alias(al):
            orig[key] = al
    if not orig:
        return {}
    if not worker_habilitado():
        if not silencioso:
            print("    [WORKER] Sin email_worker_url/secret en passwords.txt.")
        return {}

    asignados: dict[str, str] = {}
    usados_ident: set[str] = set()
    pendientes = list(orig.keys())
    t0 = time.time()
    tope = max(0.0, float(max_wait_s))
    pasada = 0
    while pendientes:
        pasada += 1
        by = _listar_invites_lote(pendientes, max_age_minutes)
        hallados: list[str] = []
        for al in pendientes:
            link = _mejor_invite_de_hits(by.get(al) or [], al)
            if not link:
                continue
            ident = _invite_url_identidad(link)
            if ident and ident in usados_ident:
                continue
            if ident:
                usados_ident.add(ident)
            asignados[orig.get(al, al)] = link
            hallados.append(al)
        for al in hallados:
            pendientes.remove(al)
        if not pendientes:
            break
        if (time.time() - t0) >= tope:
            break
        if pasada == 1 and not silencioso:
            print(
                f"    [WORKER] {len(asignados)}/{len(orig)} invites; "
                f"esperando {len(pendientes)} en KV ({tope:.0f}s)...",
                flush=True,
            )
        time.sleep(0.12)

    if not silencioso:
        dt = time.time() - t0
        print(
            f"    [WORKER] Extraídos {len(asignados)}/{len(orig)} enlace(s) "
            f"en {dt:.1f}s (sin IMAP).",
            flush=True,
        )
        for al in pendientes:
            print(f"    [WORKER] {orig.get(al, al)}: sin invitación en el worker.", flush=True)
    return asignados


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
        out.append({
            "uid": hash(link) & 0x7FFFFFFF,
            "recipients": alias,
            "body": str((it or {}).get("subject") or ""),
            "link": link,
            "score": 100,
        })
    return out


def cubre_y_esperar_reset(alias: str, max_wait_s: float = 75.0) -> tuple[bool, str | None]:
    """Si el catch-all va por worker, espera el enlace de reset (sin IMAP)."""
    if not worker_cubre_alias(alias):
        return False, None
    val = esperar_desde_worker(
        alias, "reset",
        max_wait_s=max(20.0, float(max_wait_s)),
        interval_s=0.35,
        after_email_id=0,
        max_age_minutes=30,
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


def marcar_parada_pipeline(parar: bool) -> bool:
    """Señal /parar del bot → KV del Worker (llega al PC aunque el bot esté en otro sitio)."""
    data = _post("/pipeline-stop", {"stop": bool(parar)}, timeout=8.0)
    return bool(data and data.get("ok"))


def parada_pipeline_pedida() -> bool:
    data = _get("/pipeline-stop", {})
    return bool(data and data.get("stop"))
