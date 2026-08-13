#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Wrappers no interactivos de opciones 1–5 / 9 para el bot de Telegram."""

from __future__ import annotations

import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import sesiones_imap as S


def _norm_correos(correos: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for c in correos or []:
        c = re_sub_email(c)
        if not c or "@" not in c:
            continue
        k = c.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def re_sub_email(c: str) -> str:
    import re
    return re.sub(r"^[\s\.]+|[\s\.]+$", "", (c or "").strip())


def ejecutar_opcion12(
    correos: list[str],
    *,
    cancel_check=None,
    headless: bool = False,
    **_kwargs,
) -> dict[str, Any]:
    """Opción 12: verificar que cada correo tiene App Password en passwords.txt."""
    correos = _norm_correos(correos)
    if not correos:
        return {
            "ok_list": [],
            "fail_list": [],
            "error": "Sin correos activos",
        }

    print("=== OPCIÓN 12 — Verificar IMAP en passwords.txt ===")
    print(f"  Cuentas: {len(correos)}")
    ok_list: list[str] = []
    fail_list: list[str] = []
    for correo in correos:
        if cancel_check and cancel_check():
            fail_list.extend([c for c in correos if c not in ok_list and c not in fail_list])
            break
        if S.tiene_contrasena_imap_registrada(correo):
            ok_list.append(correo)
            print(f"  OK  {correo}")
        else:
            fail_list.append(correo)
            print(f"  FALTA  {correo}")

    if not fail_list:
        print(">>> TODO OK: todos tienen App Password IMAP.")
    else:
        print(f">>> Faltan {len(fail_list)} en passwords.txt")
    return {
        "ok_list": ok_list,
        "fail_list": fail_list,
        "success_count": len(ok_list),
        "fail_count": len(fail_list),
    }


def _clave_gmail_app_password(correo: str) -> str:
    return f"gmail_app_password_{(correo or '').strip().lower()}"


def upsert_app_password_imap(correo: str, app_password: str) -> tuple[bool, str]:
    """Añade o actualiza App Password en passwords.txt. Devuelve (ok, mensaje)."""
    correo = re_sub_email(correo).lower()
    app_password = re.sub(r"\s+", "", (app_password or "").strip())
    if not correo or "@" not in correo:
        return False, "Correo inválido"
    if len(app_password) < 8:
        return False, "App Password demasiado corta"

    path = S.SCRIPT_DIR / "passwords.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except Exception as e:
        return False, f"No se pudo leer passwords.txt: {e}"

    # Match por buzón canónico (sin puntos en el local-part de Gmail)
    if "@gmail.com" in correo:
        user, dom = correo.split("@", 1)
        canon = user.replace(".", "") + "@" + dom
    else:
        canon = correo

    new_line = f"{_clave_gmail_app_password(correo)}={app_password}"
    out: list[str] = []
    replaced = False
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            out.append(line)
            continue
        key, _, _val = raw.partition("=")
        key_l = key.strip().lower()
        email_part = ""
        if key_l.startswith("gmail_app_password_") and "@" in key_l:
            email_part = key_l[len("gmail_app_password_") :]
        elif key_l.startswith("imap_password_") and "@" in key_l:
            email_part = key_l[len("imap_password_") :]
        if email_part:
            if "@gmail.com" in email_part:
                u, d = email_part.split("@", 1)
                ep_canon = u.replace(".", "") + "@" + d
            else:
                ep_canon = email_part
            if ep_canon == canon:
                if not replaced:
                    out.append(new_line)
                    replaced = True
                # omitir duplicados del mismo buzón
                continue
        out.append(line)

    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(new_line)

    try:
        path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    except Exception as e:
        return False, f"No se pudo escribir passwords.txt: {e}"
    accion = "actualizada" if replaced else "añadida"
    return True, f"App Password {accion} para {correo}"


def parse_y_guardar_imap_passwords(text: str) -> dict[str, Any]:
    """Parsea líneas correo + app password y las guarda en passwords.txt."""
    ok_list: list[str] = []
    fail_list: list[str] = []
    mensajes: list[str] = []
    text = (text or "").strip()
    if not text:
        return {"ok_list": [], "fail_list": [], "error": "Texto vacío"}

    # Formatos por línea:
    #  email@x.com abcd efgh ijkl mnop
    #  email@x.com:abcdefghijklmnop
    #  email@x.com=abcdefghijklmnop
    #  gmail_app_password_email@x.com=abcd...
    #  email@x.com\\napppassword  (bloques de 2 líneas)
    lines = [ln.strip() for ln in text.replace("\r", "").splitlines() if ln.strip()]
    i = 0
    while i < len(lines):
        ln = lines[i]
        correo = ""
        pwd = ""

        m = re.match(
            r"^(?:gmail_app_password_|imap_password_)?([^\s=:]+@[^\s=:]+)\s*[=:]\s*(.+)$",
            ln,
            re.I,
        )
        if m:
            correo, pwd = m.group(1), m.group(2)
        else:
            m2 = re.match(r"^([^\s]+@[^\s]+)\s+(.+)$", ln)
            if m2:
                correo, pwd = m2.group(1), m2.group(2)
            elif "@" in ln and i + 1 < len(lines) and "@" not in lines[i + 1]:
                correo = ln
                pwd = lines[i + 1]
                i += 1
            else:
                fail_list.append(ln[:60])
                mensajes.append(f"No se entendió: {ln[:80]}")
                i += 1
                continue

        ok, msg = upsert_app_password_imap(correo, pwd)
        mensajes.append(msg)
        if ok:
            ok_list.append(re_sub_email(correo).lower())
        else:
            fail_list.append(re_sub_email(correo).lower() or ln[:40])
        i += 1

    return {
        "ok_list": ok_list,
        "fail_list": fail_list,
        "mensajes": mensajes,
        "success_count": len(ok_list),
        "fail_count": len(fail_list),
    }


def ejecutar_consulta_codigo_imap(
    correos: list[str],
    *,
    tipo: str = "registro",
    cancel_check=None,
    headless: bool = False,  # ignorado (solo IMAP)
    **_kwargs,
) -> dict[str, Any]:
    """Opciones 1/2/3: leer OTP por IMAP (registro / eliminación / login)."""
    correos = _norm_correos(correos)
    if not correos:
        return {
            "ok_list": [],
            "fail_list": [],
            "codigos": {},
            "error": "Sin correos",
        }

    tipo = (tipo or "registro").strip().lower()
    # Misma lógica que el menú CLI (opciones 1 / 2 / 3).
    if tipo in ("1", "reg", "registro", "op1"):
        tipo = "registro"
        titulo = "OPCIÓN 1 — Código de REGISTRO"
        keywords = ["registr", "bienven", "código", "code", "verific"]
        exclude: str | list | None = "cancel"
        prefer_len: int | None = None
    elif tipo in ("2", "elim", "eliminacion", "eliminación", "op2", "delete"):
        tipo = "eliminacion"
        titulo = "OPCIÓN 2 — Código de ELIMINACIÓN"
        keywords = ["elimin", "desactiv", "delete", "code", "codigo"]
        exclude = None
        prefer_len = None
    else:
        tipo = "login"
        titulo = "OPCIÓN 3 — Código de LOGIN"
        keywords = ["código", "code", "inici"]
        exclude = "cancel"
        prefer_len = None

    print(f"=== {titulo} (bot IMAP) ===")
    print(f"  Cuentas: {len(correos)}")

    codigos: dict[str, str] = {}
    ok_list: list[str] = []
    fail_list: list[str] = []

    for correo in correos:
        if cancel_check and cancel_check():
            print("[IMAP] Cancelado.")
            fail_list.extend([c for c in correos if c not in ok_list and c not in fail_list])
            break
        print(f"--- {correo} ---")
        if not S.tiene_contrasena_imap_registrada(correo):
            print(f"  [IMAP] Sin App Password en passwords.txt para {correo}")
            fail_list.append(correo)
            continue
        try:
            kwargs_imap: dict[str, Any] = {
                "gmail_user": correo,
                "required_keywords": keywords,
                "query_exclude": exclude,
                "silencioso": True,
            }
            if prefer_len is not None:
                kwargs_imap["preferir_otp_len"] = prefer_len
            codigo = S.obtener_codigo_via_imap(**kwargs_imap)
        except Exception as e:
            print(f"  [ERROR] {correo}: {e}")
            fail_list.append(correo)
            continue
        if codigo:
            codigos[correo] = str(codigo)
            ok_list.append(correo)
            # Señal limpia para Telegram (un mensaje por código, fácil de copiar)
            print(f"__COPY_ITEM__:{correo}\t{codigo}")
            print(f"  >>> CODIGO: {codigo} <<<")
        else:
            fail_list.append(correo)
            print("  >>> No se encontro codigo reciente <<<")

    print(f"Resumen {tipo}: OK={len(ok_list)} / fallidas={len(fail_list)}")
    return {
        "ok_list": ok_list,
        "fail_list": fail_list,
        "codigos": codigos,
        "tipo": tipo,
        "success_count": len(ok_list),
        "fail_count": len(fail_list),
    }


def ejecutar_opcion1(correos: list[str], **kwargs) -> dict[str, Any]:
    return ejecutar_consulta_codigo_imap(correos, tipo="registro", **kwargs)


def ejecutar_opcion2(correos: list[str], **kwargs) -> dict[str, Any]:
    return ejecutar_consulta_codigo_imap(correos, tipo="eliminacion", **kwargs)


def ejecutar_opcion3(correos: list[str], **kwargs) -> dict[str, Any]:
    return ejecutar_consulta_codigo_imap(correos, tipo="login", **kwargs)


def ejecutar_opcion4(
    correos: list[str],
    *,
    fuente: str = "imap",
    headless: bool = False,
    cancel_check=None,
) -> dict[str, Any]:
    """Aceptar invitaciones familiares (opción 4) sin menú interactivo.

    fuente: 'imap' | 'archivo' (linksextraidos.txt)
    """
    correos = _norm_correos(correos)
    if not correos:
        print("[Op4] Sin correos activos.")
        return {"ok_list": [], "fail_list": [], "sin_enlace": [], "enlaces": {}}

    print("=== OPCIÓN 4 (bot) — invitaciones familiares ===")
    print(f"  Correos: {len(correos)} | fuente={fuente} | headless={headless}")

    origen = (fuente or "imap").strip().lower()
    if origen in ("archivo", "a", "file", "links"):
        enlaces_map = S.leer_enlaces_desde_linksextraidos(correos)
        origen_enlaces = "archivo"
        print(f"  [Enlaces] {len(enlaces_map)} desde linksextraidos.txt")
    else:
        print("  Buscando enlaces por IMAP...")
        enlaces_map = S.asignar_enlaces_invitacion_a_correos(correos)
        origen_enlaces = "imap"

    for c in correos:
        e = enlaces_map.get(c)
        if not e:
            e = next(
                (enlaces_map[k] for k in enlaces_map if k.strip().lower() == c.strip().lower()),
                None,
            )
            if e:
                enlaces_map[c] = e
        if e:
            preview = e if len(e) <= 90 else e[:90] + "..."
            print(f"    [OK enlace] {c}: {preview}")
            print(f"__COPY_ITEM__:{c}\t{e}")
        else:
            print(f"    [SIN enlace] {c}")

    enlaces_map = {c: enlaces_map[c] for c in correos if enlaces_map.get(c)}
    if enlaces_map:
        try:
            path_links = S.guardar_enlaces_en_linksextraidos(enlaces_map, merge=True)
            print(f"  [Enlaces] Guardados en {path_links}")
        except Exception as e_save:
            print(f"  [WARN] No se pudo escribir linksextraidos: {e_save}")

    sin_enlace = [c for c in correos if not enlaces_map.get(c)]
    ok_list: list[str] = []
    fail_list: list[str] = list(sin_enlace)

    if not enlaces_map:
        print(">>> No se encontró ningún enlace de invitación.")
        S._imprimir_resumen_opcion4(correos, ok_list, fail_list, sin_enlace)
        return {
            "ok_list": ok_list,
            "fail_list": fail_list,
            "sin_enlace": sin_enlace,
            "enlaces": {},
        }

    # Proxies NG + PE (misma lógica que menú opción 4)
    sin_pwd = [c for c in enlaces_map if not S.buscar_contrasena_cuenta(c)]
    con_pwd = [c for c in enlaces_map if S.buscar_contrasena_cuenta(c)]
    print(f"  sin pwd→NG: {len(sin_pwd)} | con pwd→PE: {len(con_pwd)}")

    S.cargar_cache_proxies_validos_desde_disco()
    if S.CACHE_PROXIES_NG:
        S.valid_ng_list = list(S.CACHE_PROXIES_NG)
    else:
        proxies_cfg = S.cargar_proxies_desde_txt(preferir_validos=False)
        ng_list = (proxies_cfg or {}).get("proxy_ng_list") or []
        if ng_list:
            S.valid_ng_list = S.probar_y_seleccionar_mejor_proxy(
                ng_list, "NIGERIA", max(len(enlaces_map) * 4, len(enlaces_map) + 15)
            )
            if S.valid_ng_list:
                S.guardar_proxies_validos_txt(
                    S.SCRIPT_DIR / "lista_proxies_ng_validos.txt", S.valid_ng_list
                )
        else:
            S.valid_ng_list = []
    S.alimentar_pool_proxies_nigeria(S.valid_ng_list)
    S.GLOBAL_NG_PROXY_POOL.reiniciar_bloqueos()
    proxies_ng = list(S.valid_ng_list or [])
    if sin_pwd and not proxies_ng:
        print("[Error] Cuentas sin registrar y sin proxies NG.")
        fail_list.extend(list(enlaces_map.keys()))
        S._imprimir_resumen_opcion4(correos, ok_list, fail_list, sin_enlace)
        return {
            "ok_list": ok_list,
            "fail_list": fail_list,
            "sin_enlace": sin_enlace,
            "enlaces": dict(enlaces_map),
        }

    proxies_pe = S.asegurar_proxies_peru(cantidad_necesaria=len(enlaces_map))
    if con_pwd and not proxies_pe:
        print("[Error] Cuentas con login y sin proxies PE.")
        fail_list.extend(list(enlaces_map.keys()))
        S._imprimir_resumen_opcion4(correos, ok_list, fail_list, sin_enlace)
        return {
            "ok_list": ok_list,
            "fail_list": fail_list,
            "sin_enlace": sin_enlace,
            "enlaces": dict(enlaces_map),
        }
    if not proxies_pe and not proxies_ng:
        print("[Error] Sin proxies PE ni NG.")
        fail_list.extend(list(enlaces_map.keys()))
        S._imprimir_resumen_opcion4(correos, ok_list, fail_list, sin_enlace)
        return {
            "ok_list": ok_list,
            "fail_list": fail_list,
            "sin_enlace": sin_enlace,
            "enlaces": dict(enlaces_map),
        }

    items = list(enlaces_map.items())
    tam_oleada = max(1, int(os.environ.get("TIDAL_MAX_PARALLEL") or "5"))
    oleadas = [items[i:i + tam_oleada] for i in range(0, len(items), tam_oleada)]
    print(f"  {len(items)} invitaciones → {len(oleadas)} oleada(s) de hasta {tam_oleada}")

    def procesar_invitacion_hilo(idx, item):
        if cancel_check and cancel_check():
            return item[0], False
        correo, enlace = item
        if S.buscar_contrasena_cuenta(correo):
            if idx > 1:
                time.sleep((idx - 1) * random.uniform(1.5, 3.0))
        else:
            time.sleep((idx - 1) * random.uniform(2.8, 4.5))
        p_pe = None
        p_ng = None
        if S.buscar_contrasena_cuenta(correo):
            p_pe = S.GLOBAL_PE_PROXY_POOL.obtener_proxy_unico(espera_s=60.0)
            if not p_pe and proxies_pe:
                p_pe = proxies_pe[(idx - 1) % len(proxies_pe)]
        else:
            p_ng = S.GLOBAL_NG_PROXY_POOL.obtener_proxy_unico(espera_s=60.0)
            if not p_ng and proxies_ng:
                p_ng = proxies_ng[(idx - 1) % len(proxies_ng)]
        return correo, bool(
            S.abrir_enlace_familia_con_autocierre(
                enlace, correo, proxy_pe=p_pe, proxy_ng=p_ng, headless=headless
            )
        )

    for n_oleada, oleada in enumerate(oleadas, 1):
        if cancel_check and cancel_check():
            print("[Op4] Cancelado por el usuario.")
            break
        print(f"=== Oleada {n_oleada}/{len(oleadas)}: {len(oleada)} ===")
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
                    print(f"    [ERROR] {correo_f}: {ex_h}")
        if n_oleada < len(oleadas):
            time.sleep(1.5)

    S._imprimir_resumen_opcion4(correos, ok_list, fail_list, sin_enlace)
    return {
        "ok_list": ok_list,
        "fail_list": fail_list,
        "sin_enlace": sin_enlace,
        "origen": origen_enlaces,
        "enlaces": dict(enlaces_map),
    }


def ejecutar_opcion5(
    correos: list[str],
    *,
    headless: bool = False,
    cancel_check=None,
) -> dict[str, Any]:
    """Abrir enlaces de restablecimiento desde IMAP (opción 5)."""
    correos = _norm_correos(correos)
    if not correos:
        print("[Op5] Sin correos activos.")
        return {"ok_list": [], "fail_list": [], "enlaces": {}}

    print("=== OPCIÓN 5 (bot) — reset desde enlace IMAP ===")
    print(f"  headless={headless}")
    enlaces_reset: dict[str, str] = {}
    for correo in correos:
        if cancel_check and cancel_check():
            break
        print(f"--- Buscando enlace para: {correo} ---")
        pwd_previa = S.buscar_contrasena_cuenta(correo)
        if not pwd_previa:
            print(f"  [WARN] Sin contraseña en sesiones_imap_cuentas.txt para {correo}")
        enlace = None
        for intento_imap in range(1, 6):
            if cancel_check and cancel_check():
                break
            enlace = S.obtener_codigo_via_imap(
                gmail_user=correo,
                required_keywords=S.KEYWORDS_RESTABLECER_PWD,
                query_exclude="cancel",
                solo_link=True,
                max_age_minutes=20,
                silencioso=True,
            )
            if enlace:
                break
            if intento_imap < 5:
                print(f"  [IMAP] Sin enlace (intento {intento_imap}/5)...")
                time.sleep(2.5)
        if enlace:
            preview = enlace if len(enlace) <= 90 else enlace[:90] + "..."
            print(f"  [IMAP] Enlace: {preview}")
            print(f"__COPY_ITEM__:{correo}\t{enlace}")
            enlaces_reset[correo] = enlace
        else:
            print(f"  [IMAP] No se encontró enlace para {correo}")

    if not enlaces_reset:
        print(">>> No se encontró ningún enlace de restablecimiento.")
        return {
            "ok_list": [],
            "fail_list": list(correos),
            "enlaces": {},
        }

    print("[Proxies PE] Preparando proxies...")
    proxies_pe = S.asegurar_proxies_peru(cantidad_necesaria=len(enlaces_reset))
    if not proxies_pe:
        print("[Error] No hay proxies de Perú válidos.")
        return {
            "ok_list": [],
            "fail_list": list(enlaces_reset.keys()),
            "enlaces": dict(enlaces_reset),
            "error": "Sin proxies PE",
        }

    ok_list: list[str] = []
    fail_list: list[str] = [c for c in correos if c not in enlaces_reset]
    items = list(enlaces_reset.items())
    tam = max(1, int(os.environ.get("TIDAL_MAX_PARALLEL") or "5"))
    oleadas = [items[i:i + tam] for i in range(0, len(items), tam)]

    def procesar_reset_hilo(idx, item):
        if cancel_check and cancel_check():
            return item[0], False
        if idx > 1:
            time.sleep((idx - 1) * random.uniform(1.5, 3.0))
        correo, enlace = item
        p_pe = S.GLOBAL_PE_PROXY_POOL.obtener_proxy_unico(espera_s=60.0)
        if not p_pe and proxies_pe:
            p_pe = proxies_pe[(idx - 1) % len(proxies_pe)]
        return correo, bool(
            S.abrir_enlace_restablecimiento_con_autocierre(
                enlace, correo, proxy_pe=p_pe, headless=headless
            )
        )

    for n, oleada in enumerate(oleadas, 1):
        if cancel_check and cancel_check():
            print("[Op5] Cancelado.")
            break
        print(f"=== Oleada {n}/{len(oleadas)}: {len(oleada)} ===")
        with ThreadPoolExecutor(max_workers=min(tam, len(oleada))) as executor:
            futures = {
                executor.submit(procesar_reset_hilo, idx + 1, item): item[0]
                for idx, item in enumerate(oleada)
            }
            for future in as_completed(futures):
                correo_f = futures[future]
                try:
                    c, exito = future.result()
                    if exito:
                        ok_list.append(c)
                    else:
                        fail_list.append(c)
                except Exception as ex_h:
                    fail_list.append(correo_f)
                    print(f"  [ERROR] {correo_f}: {ex_h}")

    print(f"Resumen opción 5: OK={len(ok_list)} / fallidas={len(fail_list)}")
    return {
        "ok_list": ok_list,
        "fail_list": fail_list,
        "enlaces": dict(enlaces_reset),
    }


def ejecutar_opcion9(
    correos: list[str],
    *,
    headless: bool = False,
    cancel_check=None,
) -> dict[str, Any]:
    """Restablecer contraseñas (opción 9) sin prompts."""
    correos = _norm_correos(correos)
    if not correos:
        print("[Op9] Sin correos activos.")
        return {"ok_list": [], "fail_list": [], "success_count": 0, "fail_count": 0, "error": "Sin correos"}
    if cancel_check and cancel_check():
        return {
            "ok_list": [],
            "fail_list": correos,
            "success_count": 0,
            "fail_count": len(correos),
            "error": "Cancelado",
        }

    # Diagnóstico rápido antes de abrir Chrome (útil en VPS)
    path_c = S.SCRIPT_DIR / "sesiones_imap_cuentas.txt"
    path_p = S.SCRIPT_DIR / "passwords.txt"
    print(f"[Op9] Cuentas pedidas: {correos}")
    print(f"[Op9] sesiones_imap_cuentas.txt existe: {path_c.exists()}")
    print(f"[Op9] passwords.txt existe: {path_p.exists()}")
    if path_c.exists():
        for c in correos:
            pwd = S.buscar_contrasena_cuenta(c)
            print(f"[Op9] pwd en archivo para {c}: {'SI' if pwd else 'NO'}")
            imap_ok = S.tiene_contrasena_imap_registrada(c)
            print(f"[Op9] IMAP passwords.txt para {c}: {'SI' if imap_ok else 'NO'}")

    result = S.restablecer_contrasenas_tidal(
        correos, headless=headless, interactive=False
    )
    return result or {
        "ok_list": [],
        "fail_list": correos,
        "success_count": 0,
        "fail_count": len(correos),
        "error": "Sin resultado de restablecer_contrasenas_tidal",
    }
