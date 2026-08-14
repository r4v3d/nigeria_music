#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Bot de Telegram para lanzar opciones 1–5 / 9 de sesiones_imap.py en un VPS (Contabo).

Uso:
  1. Copia .env.example → .env y rellena TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_IDS
  2. python telegram_bot.py
     (Chrome visible por defecto; TELEGRAM_FORCE_HEADLESS=1 solo si hace falta)

Comandos: /start /correos /lista /limpiar /imap /op1 /op2 /op3 /op4 /op5 /op9 /op12 /status /cancel /help
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

# Cargar .env simple (sin dependencia obligatoria de python-dotenv)
_ENV_PATH = Path(__file__).resolve().parent / ".env"


def _load_dotenv(path: Path = _ENV_PATH) -> None:
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_dotenv()
# Navegador visible por defecto (menos errores). Solo headless si lo pides explícito:
# TELEGRAM_FORCE_HEADLESS=1
_force_hl = (os.environ.get("TELEGRAM_FORCE_HEADLESS") or "").strip().lower()
if _force_hl in ("1", "true", "yes", "y"):
    os.environ["TIDAL_HEADLESS"] = "1"
else:
    os.environ["TIDAL_HEADLESS"] = "0"

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError:
    print("Falta python-telegram-bot. Instala: pip install 'python-telegram-bot>=21'")
    sys.exit(1)

from telegram_jobs import Job, JobQueue

SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(SCRIPT_DIR)

# Estado por chat: lista de correos activos
_CORREOS: dict[int, list[str]] = {}
_AWAITING_CORREOS: set[int] = set()
_AWAITING_IMAP_PWD: set[int] = set()
_LOG_BUFFERS: dict[int, list[str]] = {}  # chat_id -> líneas pendientes a flush
_LOG_LOCK = asyncio.Lock() if False else None  # placeholder; usamos threading + asyncio later

ALLOWED_IDS: set[int] = set()
for part in (os.environ.get("TELEGRAM_ALLOWED_IDS") or "").split(","):
    part = part.strip()
    if part.isdigit():
        ALLOWED_IDS.add(int(part))

TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
LOG_EVERY_N = max(3, int(os.environ.get("TELEGRAM_LOG_EVERY") or "8"))
MAX_OLEADA = max(1, int(os.environ.get("TIDAL_MAX_PARALLEL") or "5"))


def _allowed(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    if not ALLOWED_IDS:
        # Sin whitelist configurada: denegar todo (seguro por defecto)
        return False
    return int(user.id) in ALLOWED_IDS


async def _deny(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Acceso denegado. Tu user id no está en TELEGRAM_ALLOWED_IDS."
        )


def _parse_correos(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").replace(",", "\n").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("/"):
            continue
        # quitar numeración "1. mail@"
        line = re.sub(r"^\d+[\.\)]\s*", "", line)
        line = re.sub(r"^[\s\.]+|[\s\.]+$", "", line)
        if "@" not in line:
            continue
        k = line.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(line)
    return out


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1 Registro", callback_data="run:op1"),
                InlineKeyboardButton("2 Eliminar", callback_data="run:op2"),
                InlineKeyboardButton("3 Login", callback_data="run:op3"),
            ],
            [
                InlineKeyboardButton("4 Invitaciones", callback_data="run:op4:imap"),
                InlineKeyboardButton("4 Archivo", callback_data="run:op4:archivo"),
            ],
            [
                InlineKeyboardButton("5 Reset IMAP", callback_data="run:op5"),
                InlineKeyboardButton("9 Restablecer", callback_data="run:op9"),
            ],
            [
                InlineKeyboardButton("12 Verificar IMAP", callback_data="run:op12"),
                InlineKeyboardButton("➕ App Password", callback_data="ask_imap"),
            ],
            [
                InlineKeyboardButton("📋 Lista", callback_data="lista"),
                InlineKeyboardButton("✏️ Cambiar correos", callback_data="ask_correos"),
                InlineKeyboardButton("🗑 Limpiar", callback_data="limpiar"),
            ],
            [
                InlineKeyboardButton("Status", callback_data="status"),
                InlineKeyboardButton("Cancelar", callback_data="cancel"),
            ],
        ]
    )


_ANSI_RE = re.compile(r"(?:\x1b|\033)?\[[0-9;]*m")
_TITULOS = {
    "op1": "REGISTRO",
    "op2": "ELIMINACIÓN",
    "op3": "LOGIN",
    "op4": "INVITACIÓN",
    "op5": "RESET",
    "op9": "RESTABLECER",
    "op12": "VERIFICAR IMAP",
}


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "").replace("\r", "").strip()


def _html_esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _es_job_imap_limpio(name: str) -> bool:
    """Jobs donde no conviene inundar Telegram con logs técnicos."""
    return name in ("op1", "op2", "op3", "op4", "op5", "op12")


def _format_copy_item(titulo: str, correo: str, valor: str, *, es_link: bool) -> str:
    """Mensaje corto: toca el bloque <code> para copiar en Telegram."""
    label = "Link" if es_link else "Código"
    return (
        f"<b>{_html_esc(titulo)}</b>\n"
        f"{_html_esc(correo)}\n\n"
        f"{label}:\n"
        f"<code>{_html_esc(valor)}</code>"
    )


def _format_summary(name: str, result: dict | None, error: str | None) -> str:
    """Resumen corto (HTML). Los códigos/links ya se envían uno a uno."""
    if error:
        return f"<b>{_html_esc(name)}</b> error\n<code>{_html_esc(error[:1200])}</code>"
    if not result:
        return f"<b>{_html_esc(name)}</b> terminado (sin resumen)."
    ok = result.get("ok_list") or []
    fail = result.get("fail_list") or []
    err = (result.get("error") or "").strip()
    lines = [
        f"<b>{_html_esc(_TITULOS.get(name, name))}</b> listo",
        f"OK: {len(ok)} · Sin resultado: {len(fail)}",
    ]
    if err:
        lines.append(f"\nMotivo:\n<code>{_html_esc(err[:600])}</code>")
    if fail:
        label = "Faltan en passwords.txt:" if name == "op12" else "Sin resultado:"
        lines.append(f"\n{label}")
        for c in fail[:25]:
            lines.append(f"· {_html_esc(c)}")
        if len(fail) > 25:
            lines.append(f"· … +{len(fail) - 25}")
    return "\n".join(lines)


# --- Job queue + log bridge ---
_main_loop: asyncio.AbstractEventLoop | None = None
_app_ref = None
_job_queue: JobQueue | None = None
_pending_log: dict[int, list[str]] = {}
_log_counts: dict[int, int] = {}


def _on_job_log(job: Job, line: str) -> None:
    """Llamado desde el hilo worker; agenda envío async a Telegram."""
    line = _strip_ansi(line)
    if not line:
        return

    # Ítem listo para copiar (código o link) — un mensaje limpio por cuenta
    if line.startswith("__COPY_ITEM__:"):
        payload = line[len("__COPY_ITEM__:") :]
        correo, sep, valor = payload.partition("\t")
        if sep and valor and job.chat_id and _main_loop and _app_ref:
            es_link = valor.strip().lower().startswith("http")
            titulo = _TITULOS.get(job.name, job.name.upper())
            text = _format_copy_item(titulo, correo.strip(), valor.strip(), es_link=es_link)
            asyncio.run_coroutine_threadsafe(
                _safe_send_html(job.chat_id, text),
                _main_loop,
            )
        return

    if line.startswith("__JOB_DONE__:"):
        chat_id = job.chat_id
        if chat_id and _main_loop and _app_ref:
            asyncio.run_coroutine_threadsafe(
                _send_job_finished(chat_id, job),
                _main_loop,
            )
        return

    chat_id = job.chat_id
    if not chat_id:
        return
    # op1/2/3: solo mensajes copiables + resumen final (sin spam IMAP/ANSI)
    if _es_job_imap_limpio(job.name):
        return

    buf = _pending_log.setdefault(chat_id, [])
    buf.append(line)
    _log_counts[chat_id] = _log_counts.get(chat_id, 0) + 1
    flush = (
        _log_counts[chat_id] % LOG_EVERY_N == 0
        or "RESUMEN" in line.upper()
        or line.startswith("===")
        or "[OK]" in line
        or "[ERROR]" in line
    )
    if flush and buf and _main_loop and _app_ref:
        text = "\n".join(buf[-LOG_EVERY_N:])
        _pending_log[chat_id] = []
        asyncio.run_coroutine_threadsafe(
            _safe_send(chat_id, text[-3500:]),
            _main_loop,
        )


async def _send_job_finished(chat_id: int, job: Job) -> None:
    """Resumen corto. Códigos/links ya salieron como mensajes <code> limpios."""
    await _safe_send_html(chat_id, _format_summary(job.name, job.result, job.error))


async def _safe_send(chat_id: int, text: str) -> None:
    # Texto plano: logs con _ / * rompen Markdown de Telegram
    try:
        await _app_ref.bot.send_message(chat_id=chat_id, text=_strip_ansi(text)[:4000])
    except Exception:
        pass


async def _safe_send_html(chat_id: int, text: str) -> None:
    try:
        await _app_ref.bot.send_message(
            chat_id=chat_id,
            text=text[:4000],
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            await _app_ref.bot.send_message(chat_id=chat_id, text=_strip_ansi(text)[:4000])
        except Exception:
            pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        await _deny(update)
        return
    uid = update.effective_user.id
    n = len(_CORREOS.get(uid, []))
    await update.effective_message.reply_text(
        "Bot Tidal / sesiones_imap (Contabo)\n"
        f"Tu id: {uid}\n"
        f"Correos activos: {n}\n\n"
        "Cambiar correos: botón «Cambiar correos» o /correos\n"
        "Ver lista: /lista · Borrar: /limpiar\n"
        "App Password IMAP: /imap · Verificar: /op12\n"
        "Cancelar solo detiene un job en curso (no borra correos).",
        reply_markup=_menu_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        await _deny(update)
        return
    await update.effective_message.reply_text(
        "/start — menú\n"
        "/correos — reemplazar lista (luego pega mails)\n"
        "/lista — ver correos activos\n"
        "/limpiar — vaciar lista\n"
        "/imap — añadir App Password a passwords.txt\n"
        "/op1 — código registro\n"
        "/op2 — código eliminación\n"
        "/op3 — código login\n"
        "/op4 — invitaciones\n"
        "/op4_archivo — invitaciones desde archivo\n"
        "/op5 — enlace reset\n"
        "/op9 — restablecer contraseñas\n"
        "/op12 — verificar Email Worker + IMAP\n"
        "/status — job actual\n"
        "/cancel — cancelar job o modo pegado\n"
        "/whoami — tu Telegram user id"
    )


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Permitido sin whitelist para poder configurar TELEGRAM_ALLOWED_IDS la primera vez
    uid = update.effective_user.id if update.effective_user else "?"
    ok = (isinstance(uid, int) and uid in ALLOWED_IDS) if ALLOWED_IDS else False
    await update.effective_message.reply_text(
        f"Tu Telegram user id: {uid}\n"
        f"Whitelist: {'OK' if ok else 'NO (anadelo a TELEGRAM_ALLOWED_IDS en .env)'}",
    )


async def cmd_correos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        await _deny(update)
        return
    uid = update.effective_user.id
    _AWAITING_IMAP_PWD.discard(uid)
    args_text = " ".join(context.args or [])
    if args_text.strip():
        mails = _parse_correos(args_text)
        if mails:
            _CORREOS[uid] = mails
            _AWAITING_CORREOS.discard(uid)
            await update.effective_message.reply_text(
                f"Lista reemplazada: {len(mails)} correo(s).\n"
                + "\n".join(f"• {c}" for c in mails[:30]),
                reply_markup=_menu_keyboard(),
            )
            return
    _AWAITING_CORREOS.add(uid)
    actual = _CORREOS.get(uid) or []
    previa = f"\nAhora tienes {len(actual)}. Al pegar se REEMPLAZA toda la lista." if actual else ""
    await update.effective_message.reply_text(
        "Pega los correos (uno por línea)."
        f"{previa}\n"
        "Envía '.' o /cancel para salir sin cambiar."
    )


async def cmd_lista(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        await _deny(update)
        return
    uid = update.effective_user.id
    mails = _CORREOS.get(uid) or []
    if not mails:
        await update.effective_message.reply_text(
            "No hay correos activos.\nUsa «Cambiar correos» o /correos.",
            reply_markup=_menu_keyboard(),
        )
        return
    await update.effective_message.reply_text(
        f"Correos activos ({len(mails)}):\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(mails, 1)),
        reply_markup=_menu_keyboard(),
    )


async def cmd_limpiar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        await _deny(update)
        return
    uid = update.effective_user.id
    _CORREOS[uid] = []
    _AWAITING_CORREOS.discard(uid)
    _AWAITING_IMAP_PWD.discard(uid)
    await update.effective_message.reply_text(
        "Lista de correos vaciada.",
        reply_markup=_menu_keyboard(),
    )


async def cmd_imap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        await _deny(update)
        return
    uid = update.effective_user.id
    _AWAITING_CORREOS.discard(uid)
    args_text = " ".join(context.args or []).strip()
    if args_text:
        from telegram_runners import parse_y_guardar_imap_passwords

        result = parse_y_guardar_imap_passwords(args_text)
        await _reply_imap_save(update, result)
        return
    _AWAITING_IMAP_PWD.add(uid)
    await update.effective_message.reply_text(
        "Pega App Password(s) IMAP, formatos:\n"
        "• correo@gmail.com abcd efgh ijkl mnop\n"
        "• correo@gmail.com:abcdefghijklmnop\n"
        "• correo en una línea y la App Password en la siguiente\n\n"
        "Se guarda en passwords.txt del VPS.\n"
        "Envía '.' o /cancel para salir."
    )


async def _reply_imap_save(update: Update, result: dict) -> None:
    ok = result.get("ok_list") or []
    fail = result.get("fail_list") or []
    msgs = result.get("mensajes") or []
    lines = [f"IMAP passwords.txt: OK {len(ok)} · fallos {len(fail)}"]
    for m in msgs[:20]:
        lines.append(f"· {m}")
    if len(msgs) > 20:
        lines.append(f"· … +{len(msgs) - 20}")
    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=_menu_keyboard(),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    uid = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    if not text:
        return

    # Modo App Password IMAP
    if uid in _AWAITING_IMAP_PWD:
        if text in (".", "/cancel", "cancel", "Cancelar"):
            _AWAITING_IMAP_PWD.discard(uid)
            await update.effective_message.reply_text("Modo IMAP cancelado.", reply_markup=_menu_keyboard())
            return
        from telegram_runners import parse_y_guardar_imap_passwords

        _AWAITING_IMAP_PWD.discard(uid)
        result = parse_y_guardar_imap_passwords(text)
        await _reply_imap_save(update, result)
        return

    # Modo cambiar correos
    if uid in _AWAITING_CORREOS:
        if text in (".", "/cancel", "cancel", "Cancelar"):
            _AWAITING_CORREOS.discard(uid)
            await update.effective_message.reply_text(
                "Sin cambios en la lista.",
                reply_markup=_menu_keyboard(),
            )
            return
        mails = _parse_correos(text)
        _AWAITING_CORREOS.discard(uid)
        if not mails:
            await update.effective_message.reply_text(
                "No se detectaron correos válidos. Prueba /correos de nuevo."
            )
            return
        _CORREOS[uid] = mails
        await update.effective_message.reply_text(
            f"Lista reemplazada: {len(mails)} correo(s).\n"
            + "\n".join(f"• {c}" for c in mails[:30]),
            reply_markup=_menu_keyboard(),
        )
        return

    # Atajo: pegar lista de mails sin /correos (1+ líneas con @)
    if "@" in text and ("\n" in text or text.count("@") >= 1):
        # Evitar capturar líneas de app password sueltas
        if "gmail_app_password_" in text.lower() or re.search(r"=\s*\w{8,}", text):
            return
        mails = _parse_correos(text)
        # Solo auto-aceptar si parece lista de emails (todas las líneas útiles son mails)
        if mails and (len(mails) >= 1 and ("\n" in text or len(mails) == 1)):
            # Un solo email en una línea: también aceptar para facilitar el cambio
            if "\n" not in text and len(mails) == 1 and len(text.split()) == 1:
                _CORREOS[uid] = mails
                await update.effective_message.reply_text(
                    f"Lista reemplazada: 1 correo\n• {mails[0]}",
                    reply_markup=_menu_keyboard(),
                )
                return
            if "\n" in text:
                _CORREOS[uid] = mails
                await update.effective_message.reply_text(
                    f"Lista actualizada: {len(mails)} correo(s).",
                    reply_markup=_menu_keyboard(),
                )


async def _start_job(update: Update, name: str, **kwargs) -> None:
    if not _allowed(update):
        await _deny(update)
        return
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    # Salir de modos de pegado al lanzar job
    _AWAITING_CORREOS.discard(uid)
    _AWAITING_IMAP_PWD.discard(uid)
    correos = _CORREOS.get(uid) or []
    if not correos:
        await update.effective_message.reply_text(
            "No hay correos activos. Usa «Cambiar correos» o /correos."
        )
        return
    snap = _job_queue.status_snapshot()
    if snap.get("current"):
        cur = snap["current"]
        await update.effective_message.reply_text(
            f"Ya hay un job en curso: #{cur['job_id']} {cur['name']} ({cur['status']}).\n"
            f"Puedes cambiar la lista con /correos (vale para el próximo job).\n"
            f"Para parar el actual: /cancel."
        )
        return
    job = _job_queue.submit(name, correos, chat_id=chat_id, **kwargs)
    if name in ("op1", "op2", "op3"):
        modo = "@cheapmusic.best por Email Worker; Gmail por IMAP. Código listo para copiar."
    elif name in ("op4", "op5"):
        modo = "Chrome visible + proxies. Link listo para copiar."
    elif name == "op12":
        modo = "Verifica Email Worker (@cheapmusic.best) y App Passwords IMAP."
    else:
        modo = "Chrome visible + proxies PE/NG."
    await update.effective_message.reply_text(
        f"Job #{job.job_id} {name} encolado\n"
        f"Cuentas: {len(correos)}\n"
        f"{modo}",
    )


async def cmd_op1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_job(update, "op1")


async def cmd_op2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_job(update, "op2")


async def cmd_op3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_job(update, "op3")


async def cmd_op4(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_job(update, "op4", fuente="imap", headless=False)


async def cmd_op4_archivo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_job(update, "op4", fuente="archivo", headless=False)


async def cmd_op5(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_job(update, "op5", headless=False)


async def cmd_op9(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_job(update, "op9", headless=False)


async def cmd_op12(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_job(update, "op12")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        await _deny(update)
        return
    snap = _job_queue.status_snapshot()
    cur = snap.get("current")
    if not cur:
        n = len(_CORREOS.get(update.effective_user.id, []))
        await update.effective_message.reply_text(
            f"Sin job en curso. Pending queue: {snap.get('pending', 0)}\nCorreos activos: {n}"
        )
        return
    logs = cur.get("recent_logs") or []
    # Filtrar ruido / señales internas
    logs = [l for l in logs if not l.startswith("__")]
    tail = "\n".join(_strip_ansi(x) for x in logs[-8:]) if logs else "(sin logs aún)"
    await update.effective_message.reply_text(
        f"Job #{cur['job_id']} {cur['name']} — {cur['status']}\n"
        f"Cuentas: {cur['correos']}\n\nÚltimos logs:\n{tail[-3500:]}"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        await _deny(update)
        return
    uid = update.effective_user.id
    msgs: list[str] = []
    if uid in _AWAITING_CORREOS:
        _AWAITING_CORREOS.discard(uid)
        msgs.append("Saliste del modo cambiar correos (lista intacta).")
    if uid in _AWAITING_IMAP_PWD:
        _AWAITING_IMAP_PWD.discard(uid)
        msgs.append("Saliste del modo App Password IMAP.")
    if _job_queue and _job_queue.request_cancel():
        msgs.append("Cancelación pedida: el job parará en el próximo checkpoint.")
    if not msgs:
        n = len(_CORREOS.get(uid, []))
        await update.effective_message.reply_text(
            "Nada que cancelar (no hay job ni modo pegado).\n\n"
            f"Correos activos: {n}\n"
            "• Cambiar lista → /correos o «Cambiar correos»\n"
            "• Ver lista → /lista\n"
            "• Vaciar → /limpiar\n"
            "(Cancelar no borra correos.)",
            reply_markup=_menu_keyboard(),
        )
        return
    await update.effective_message.reply_text(
        "\n".join(msgs),
        reply_markup=_menu_keyboard(),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        await query.edit_message_text("Acceso denegado.")
        return
    data = query.data or ""
    # Reusar effective_message del callback
    update._effective_message = query.message  # type: ignore

    if data == "status":
        await cmd_status(update, context)
        return
    if data == "cancel":
        await cmd_cancel(update, context)
        return
    if data == "ask_correos":
        await cmd_correos(update, context)
        return
    if data == "ask_imap":
        await cmd_imap(update, context)
        return
    if data == "lista":
        await cmd_lista(update, context)
        return
    if data == "limpiar":
        await cmd_limpiar(update, context)
        return
    if data == "run:op1":
        await _start_job(update, "op1")
        return
    if data == "run:op2":
        await _start_job(update, "op2")
        return
    if data == "run:op3":
        await _start_job(update, "op3")
        return
    if data == "run:op4:imap":
        await _start_job(update, "op4", fuente="imap", headless=False)
        return
    if data == "run:op4:archivo":
        await _start_job(update, "op4", fuente="archivo", headless=False)
        return
    if data == "run:op5":
        await _start_job(update, "op5", headless=False)
        return
    if data == "run:op9":
        await _start_job(update, "op9", headless=False)
        return
    if data == "run:op12":
        await _start_job(update, "op12")
        return


def main() -> None:
    global _job_queue, _main_loop, _app_ref

    if not TOKEN:
        print("Falta TELEGRAM_BOT_TOKEN en .env")
        sys.exit(1)
    if not ALLOWED_IDS:
        print(
            "AVISO: TELEGRAM_ALLOWED_IDS vacío — el bot denegará a todos.\n"
            "Añade tu user id (usa /whoami tras poner temporalmente tu id)."
        )

    _job_queue = JobQueue(log_callback=_on_job_log)

    async def _post_init(application: Application) -> None:
        global _main_loop
        _main_loop = asyncio.get_running_loop()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(_post_init)
        .build()
    )
    _app_ref = app

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("correos", cmd_correos))
    app.add_handler(CommandHandler("lista", cmd_lista))
    app.add_handler(CommandHandler("limpiar", cmd_limpiar))
    app.add_handler(CommandHandler("imap", cmd_imap))
    app.add_handler(CommandHandler("op1", cmd_op1))
    app.add_handler(CommandHandler("op2", cmd_op2))
    app.add_handler(CommandHandler("op3", cmd_op3))
    app.add_handler(CommandHandler("op4", cmd_op4))
    app.add_handler(CommandHandler("op4_archivo", cmd_op4_archivo))
    app.add_handler(CommandHandler("op5", cmd_op5))
    app.add_handler(CommandHandler("op9", cmd_op9))
    app.add_handler(CommandHandler("op12", cmd_op12))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        print(f"[Bot] Error: {type(err).__name__}: {err}")
        try:
            if isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text(
                    f"Error interno: {type(err).__name__}. Revisa el log del VPS."
                )
        except Exception:
            pass

    app.add_error_handler(_on_error)

    print("Bot Telegram arrancando (long-polling)...")
    print(f"  ALLOWED_IDS={sorted(ALLOWED_IDS) or '(vacío — deniega todo)'}")
    print(f"  TIDAL_HEADLESS={os.environ.get('TIDAL_HEADLESS')}")
    print(f"  WORKDIR={SCRIPT_DIR}")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
