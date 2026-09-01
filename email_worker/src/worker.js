/**
 * Recibe correo Tidal vía Cloudflare Email Routing, extrae OTP/enlaces
 * y los sirve por HTTP a sesiones_imap.py (sin Gmail IMAP).
 *
 * Dashboard: Email Routing → Catch-all → Send to this Worker.
 * Opcional: el worker reenvía copia a Gmail (FORWARD_TO).
 */

const MAX_ITEMS = 40;
const KINDS = ["login", "register", "delete", "reset", "invite"];

export default {
  async email(message, env) {
    try {
      const rawBuf = await streamToArrayBuffer(message.raw);
      const raw = new TextDecoder("utf-8", { fatal: false }).decode(rawBuf);
      const decoded = decodeRawMime(raw);
      let alias = pickAlias(message.to, decoded, env.CATCHALL_DOMAIN || "cheapmusic.best");
      if (!alias) alias = String(message.to || "").trim().toLowerCase();
      const item = classifyTidal(decoded, alias);
      console.log(
        "email in",
        JSON.stringify({
          to: message.to || "",
          alias,
          subject: (decoded.headers.subject || "").slice(0, 100),
          kind: item ? item.kind : null,
          stored: !!(item && alias),
        })
      );
      if (item && alias) {
        await pushItem(env, alias, item);
      }
    } catch (err) {
      console.log("email parse error", String(err));
    }
    const fwd = (env.FORWARD_TO || "").trim();
    if (fwd && fwd.includes("@")) {
      try {
        await message.forward(fwd);
      } catch (err) {
        console.log("forward error", String(err));
      }
    }
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return json({ ok: true, service: "tidal-otp-worker" });
    }
    if (!checkSecret(request, url, env)) {
      return json({ ok: false, error: "unauthorized" }, 401);
    }
    if (url.pathname === "/claim") {
      const alias = (url.searchParams.get("alias") || "").trim().toLowerCase();
      const kind = (url.searchParams.get("kind") || "").trim().toLowerCase();
      const afterTs = Number(url.searchParams.get("after_ts") || "0") || 0;
      const maxAge = Number(url.searchParams.get("max_age") || "900") || 900;
      const consume = url.searchParams.get("consume") !== "0";
      if (!alias || !KINDS.includes(kind)) {
        return json({ ok: false, error: "alias and kind required" }, 400);
      }
      try {
        const hit = await claimItem(env, alias, kind, afterTs, maxAge, consume);
        if (!hit) return json({ ok: false, found: false });
        return json({
          ok: true,
          found: true,
          value: hit.value,
          kind: hit.kind,
          ts: hit.ts,
          subject: hit.subject,
          alias,
        });
      } catch (err) {
        console.log("claim error", String(err));
        return json({ ok: false, found: false, error: "claim_failed" }, 200);
      }
    }
    if (url.pathname === "/list") {
      const alias = (url.searchParams.get("alias") || "").trim().toLowerCase();
      const kind = (url.searchParams.get("kind") || "invite").trim().toLowerCase();
      const maxAge = Number(url.searchParams.get("max_age") || "86400") || 86400;
      if (!alias) return json({ ok: false, error: "alias required" }, 400);
      const items = await listItems(env, alias, kind, maxAge);
      return json({ ok: true, items });
    }
    if (url.pathname === "/peek") {
      const alias = (url.searchParams.get("alias") || "").trim().toLowerCase();
      if (!alias) return json({ ok: false, error: "alias required" }, 400);
      const items = await readBox(env, alias);
      return json({ ok: true, alias, count: items.length, items });
    }
    return json({ ok: false, error: "not found" }, 404);
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store, no-cache, must-revalidate",
      "cdn-cache-control": "no-store",
    },
  });
}

function checkSecret(request, url, env) {
  const expected = (env.OTP_SECRET || "").trim();
  if (!expected) return false;
  const got = (request.headers.get("x-otp-secret") || url.searchParams.get("secret") || "").trim();
  return got.length > 0 && got === expected;
}

async function streamToArrayBuffer(stream) {
  const reader = stream.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    total += value.length;
  }
  const result = new Uint8Array(total);
  let i = 0;
  for (const c of chunks) {
    result.set(c, i);
    i += c.length;
  }
  return result;
}

function utf8FromBinary(s) {
  s = String(s || "");
  const bytes = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) bytes[i] = s.charCodeAt(i) & 0xff;
  return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
}

function decodeQP(s) {
  const joined = String(s || "").replace(/=\r?\n/g, "");
  const bytes = [];
  for (let i = 0; i < joined.length; i++) {
    if (
      joined[i] === "=" &&
      i + 2 < joined.length &&
      /^[0-9A-Fa-f]{2}$/.test(joined.slice(i + 1, i + 3))
    ) {
      bytes.push(parseInt(joined.slice(i + 1, i + 3), 16));
      i += 2;
    } else {
      bytes.push(joined.charCodeAt(i) & 0xff);
    }
  }
  return new TextDecoder("utf-8", { fatal: false }).decode(new Uint8Array(bytes));
}

function decodeB64(s) {
  try {
    return utf8FromBinary(atob(String(s || "").replace(/\s/g, "")));
  } catch {
    return String(s || "");
  }
}

function unescapeHtml(s) {
  return String(s || "")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">");
}

function decodeRfc2047(s) {
  return String(s || "")
    .replace(/=\?utf-8\?b\?([A-Za-z0-9+/]+=*)\?=/gi, (_, b) => {
      try {
        return decodeB64(b);
      } catch {
        return _;
      }
    })
    .replace(/=\?utf-8\?q\?([^?]+)\?=/gi, (_, q) => decodeQP(q.replace(/_/g, " ")));
}

function parseHeadersBlock(head) {
  const headers = {};
  let last = "";
  for (const line of String(head || "").split(/\r?\n/)) {
    if (/^[ \t]/.test(line) && last) {
      headers[last] += " " + line.trim();
      continue;
    }
    const m = line.match(/^([A-Za-z0-9-]+):\s*(.*)$/);
    if (m) {
      last = m[1].toLowerCase();
      headers[last] = (headers[last] ? headers[last] + " " : "") + m[2];
    }
  }
  return headers;
}

function decodePartBody(headers, body) {
  const cte = String(headers["content-transfer-encoding"] || "").toLowerCase();
  let text = String(body || "");
  if (cte.includes("base64")) text = decodeB64(text);
  else text = decodeQP(text);
  return unescapeHtml(decodeRfc2047(text));
}

function extractTextParts(raw) {
  const texts = [];
  function walk(chunk) {
    const split = String(chunk || "").split(/\r?\n\r?\n/);
    const head = split[0] || "";
    const body = split.slice(1).join("\n\n") || "";
    const headers = parseHeadersBlock(head);
    const ct = String(headers["content-type"] || "");
    const bm = ct.match(/boundary="?([^";]+)"?/i);
    if (bm) {
      const b = bm[1].trim();
      const esc = b.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const parts = body.split(new RegExp("--" + esc));
      for (const p of parts) {
        const t = p.replace(/^\r?\n/, "");
        if (!t || t.trim() === "--" || t.startsWith("--")) continue;
        walk(t);
      }
      return;
    }
    if (/text\/(plain|html)/i.test(ct) || !ct) {
      texts.push(decodePartBody(headers, body));
    }
  }
  walk(raw);
  return texts.join("\n");
}

function decodeRawMime(raw) {
  let text = decodeRfc2047(String(raw || ""));
  const split = text.split(/\r?\n\r?\n/);
  const head = split[0] || "";
  const rest = split.slice(1).join("\n\n") || text;
  const headers = parseHeadersBlock(head);
  const parts = extractTextParts(text);
  const body = parts || decodeQP(rest);
  const all = unescapeHtml(`${headers.subject || ""}\n${body}`);
  return { raw: text, headers, body, all };
}

function emailsIn(s) {
  return String(s || "").toLowerCase().match(/[a-z0-9._%+\-]+@[a-z0-9.\-]+/g) || [];
}

function aliasOfDomain(email, domain) {
  const e = String(email || "").trim().toLowerCase();
  const dom = String(domain || "cheapmusic.best").toLowerCase();
  return e.endsWith("@" + dom) ? e : "";
}

function firstAliasIn(src, domain) {
  for (const e of emailsIn(src)) {
    const hit = aliasOfDomain(e, domain);
    if (hit) return hit;
  }
  return "";
}

function pickAlias(messageTo, decoded, domain) {
  // Solo el destinatario real: el cuerpo de Tidal puede citar otros @cheapmusic.best
  // y found[0] del blob mezclaba OTP entre ventanas de la misma oleada.
  const dom = String(domain || "cheapmusic.best").toLowerCase();
  const headerSources = [
    messageTo,
    decoded.headers.to,
    decoded.headers["delivered-to"],
    decoded.headers["x-original-to"],
    decoded.headers["envelope-to"],
    decoded.headers["x-envelope-to"],
    decoded.headers["x-forwarded-to"],
    decoded.headers["x-google-original-to"],
  ];
  for (const src of headerSources) {
    const hit = firstAliasIn(src, dom);
    if (hit) return hit;
  }
  const fromBody = firstAliasIn((decoded.all || "").slice(0, 8000), dom);
  if (fromBody) return fromBody;
  const to = String(messageTo || "").trim().toLowerCase();
  return to.includes("@") ? to : "";
}

function stripHtml(s) {
  return String(s || "")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function isDateish(n) {
  return /^20(2[0-9]|3[0-9])(0[1-9]|1[0-2])$/.test(n) || /^20(2[0-9]|3[0-9])$/.test(n);
}

function isJunkOtp(n) {
  n = String(n || "");
  if (n.length < 5 || n.length > 6) return true;
  if (/^0+$/.test(n)) return true;
  if (/^(\d)\1+$/.test(n)) return true;
  if (isDateish(n)) return true;
  return false;
}

function extractOtp(text) {
  const cleaned = String(text || "").replace(/#[0-9A-Fa-f]{3,8}\b/g, " ");
  const stripped = stripHtml(cleaned);
  const bracket = stripped.match(/\[(\d{6})\]/);
  if (bracket && !isJunkOtp(bracket[1])) return bracket[1];
  const lead = stripped.match(/(?:^|\n)\s*(\d{6})\s*[-–—]/);
  if (lead && !isJunkOtp(lead[1])) return lead[1];
  const nearRe =
    /(?:^|[^a-záéíóúñ])(?:c[oó]digo|code|sign[-\s]?in|sign[-\s]?up|verification|one[-\s]?time|introduce|ingresa|enter)[^\d]{0,240}(\d{5,6})/gi;
  for (const m of stripped.matchAll(nearRe)) {
    if (!isJunkOtp(m[1])) return m[1];
  }
  const spaced = stripped.match(/(?<!\d)(\d(?:[\s\u00a0\-]{1,3}\d){5})(?!\d)/);
  if (spaced) {
    const digits = spaced[1].replace(/\D/g, "");
    if (!isJunkOtp(digits)) return digits;
  }
  const nums = [...stripped.matchAll(/(?<!\d)(\d{6})(?!\d)/g)].map((m) => m[1]);
  for (const n of nums) {
    if (!isJunkOtp(n)) return n;
  }
  return null;
}

function extractReset(text) {
  // QP soft wraps / HTML entities often split "resetpass" across lines.
  const t = unescapeHtml(String(text || ""))
    .replace(/=\r?\n/g, "")
    .replace(/&#x2[fF];/g, "/")
    .replace(/&#47;/g, "/");
  const direct = t.match(/https?:\/\/login\.tidal\.com\/resetpass\/[^\s"'<>]+/i);
  if (direct) return direct[0].replace(/[>"']+$/, "");
  const wrapped = t.match(/https?:\/\/[^\s"'<>]*tidal\.com\/[^\s"'<>]*resetpass\/[^\s"'<>]+/i);
  if (wrapped) return wrapped[0].replace(/[>"']+$/, "");
  const ab = t.match(/https?:\/\/ablink\.(?:info\.)?tidal\.com\/[^\s"'<>]+/i);
  if (ab) return ab[0].replace(/[>"']+$/, "");
  const click = t.match(/https?:\/\/[^\s"'<>]*(?:click|email|info)\.tidal\.com\/[^\s"'<>]+/i);
  return click ? click[0].replace(/[>"']+$/, "") : null;
}

function extractInvite(text) {
  const t = unescapeHtml(String(text || ""));
  const pats = [
    /https?:\/\/login\.tidal\.com\/family\/[A-Za-z0-9._~\-\/?=&%]+/i,
    /https?:\/\/account\.tidal\.com\/family\/[A-Za-z0-9._~\-\/?=&%]+/i,
    /https?:\/\/(?:www\.)?tidal\.com\/(?:[a-z]{2}\/)?family\/[A-Za-z0-9._~\-\/?=&%]+/i,
    /https?:\/\/login\.tidal\.com\/[^\s"'<>]*accept[^\s"'<>]*/i,
    /https?:\/\/ablink\.(?:info\.)?tidal\.com\/[^\s"'<>]+/i,
    /https?:\/\/ablink\.[^\s"'<>]*tidal[^\s"'<>]*/i,
  ];
  for (const p of pats) {
    const m = t.match(p);
    if (m && !/resetpass|reset-password|\/privacy|\/terms|\/legal/i.test(m[0])) {
      return m[0].replace(/[>"']+$/, "");
    }
  }
  return null;
}

function classifyTidal(decoded, alias) {
  const subject = decoded.headers.subject || "";
  const visible = `${subject}\n${stripHtml(decoded.body || "")}\n${stripHtml(decoded.all || "")}`;
  const blob = `${subject}\n${visible}\n${decoded.all}`.toLowerCase();
  const full = `${subject}\n${visible}\n${decoded.all}`;
  const ts = Date.now() / 1000;
  const base = {
    alias,
    ts,
    subject: subject.slice(0, 180),
    claimed: false,
    id: `${ts}-${Math.random().toString(16).slice(2, 8)}`,
  };

  if (/invitation cancelled|invitación cancelada|family invitation cancel/.test(blob)) {
    return null;
  }

  const esResetMail =
    /resetpass|restablecer tu contrase|resetting your tidal password|reset your password|restaurar su contrase|forgot your password|link to reset/.test(
      blob
    );
  if (esResetMail) {
    const reset = extractReset(full);
    if (reset) return { ...base, kind: "reset", value: reset };
    // No degradar a invite: un ablink de reset sin "resetpass" en la URL
    // se guardaba como invite y /claim?kind=reset nunca lo encontraba.
    console.log("reset mail without extractable link", (subject || "").slice(0, 80));
    return null;
  }

  const invite = extractInvite(full);
  if (
    invite ||
    /invites you to join|welcome to the family|plan familiar|te ha invitado|join their tidal family|has invited you|invited to a tidal family/.test(blob)
  ) {
    if (invite && !/resetpass/i.test(invite)) return { ...base, kind: "invite", value: invite };
  }

  if (/new login to your account/.test(blob) && !extractOtp(full)) {
    return null;
  }

  const otp = extractOtp(full);
  if (!otp) return null;

  if (
    /verificaci[oó]n de la eliminaci[oó]n|eliminaci[oó]n de tu cuenta|delete your account|account deletion|c[oó]digo para eliminar|to delete your account/.test(
      blob
    )
  ) {
    return { ...base, kind: "delete", value: otp };
  }
  // Registro / alta (opción 4: "Verifica tu correo… completar la creación de tu cuenta")
  if (
    /completar la creaci[oó]n|creaci[oó]n de tu cuenta|creating your account|finish creating|terminar de crear|sign[-\s]?up|bienven|verifica tu correo|verify your email/.test(
      blob
    ) ||
    (/registr/.test(blob) && !/c[oó]digo de inicio|login code|sign-?in code/.test(blob))
  ) {
    return { ...base, kind: "register", value: otp };
  }
  if (
    /c[oó]digo de inicio|login code|sign-?in code|c[oó]digo de acceso|your tidal login code/.test(blob)
  ) {
    return { ...base, kind: "login", value: otp };
  }
  return { ...base, kind: "login", value: otp };
}

function pendingKey(alias, kind) {
  return "pending:" + String(alias || "").trim().toLowerCase() + ":" + String(kind || "");
}

function kvKey(alias) {
  return "box:" + String(alias || "").trim().toLowerCase();
}

function itemKey(alias, id) {
  return "item:" + String(alias || "").trim().toLowerCase() + ":" + String(id || "");
}

function recentKey(alias) {
  return "recent:" + String(alias || "").trim().toLowerCase();
}

function hitKey(alias, tsSec) {
  return "hit:" + String(alias || "").trim().toLowerCase() + ":" + String(Math.floor(Number(tsSec) || 0));
}

const MEM = new Map();

function memItems(alias) {
  const a = String(alias || "").trim().toLowerCase();
  const arr = MEM.get(a);
  return Array.isArray(arr) ? arr : [];
}

function memPush(alias, item) {
  const a = String(alias || "").trim().toLowerCase();
  const arr = memItems(a);
  arr.push(item);
  MEM.set(a, arr.slice(-MAX_ITEMS));
}

function memReplace(alias, items) {
  MEM.set(String(alias || "").trim().toLowerCase(), (items || []).slice(-MAX_ITEMS));
}

function itemMatchesAlias(it, alias) {
  const want = String(alias || "").trim().toLowerCase();
  const got = String((it && it.alias) || "").trim().toLowerCase();
  return !got || !want || got === want;
}

function findUnclaimed(items, kinds, afterTs, maxAge, now, alias) {
  const want = new Set(kinds);
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (!it || !it.value || it.claimed) continue;
    if (!itemMatchesAlias(it, alias)) continue;
    if (!want.has(it.kind)) continue;
    if (afterTs && Number(it.ts) <= afterTs) continue;
    if (now - Number(it.ts) > maxAge) continue;
    return i;
  }
  // Si se busca login/register o OTP genérico, aceptar cualquier OTP numérico no reclamado
  if (kinds.some(k => k === "login" || k === "register" || k === "delete")) {
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i];
      if (!it || !it.value || it.claimed) continue;
      if (!itemMatchesAlias(it, alias)) continue;
      if (typeof it.value === "string" && /^\d{5,6}$/.test(it.value.trim())) {
        if (afterTs && Number(it.ts) <= afterTs) continue;
        if (now - Number(it.ts) > maxAge) continue;
        return i;
      }
    }
  }
  return -1;
}

async function loadRecent(kv, alias) {
  try {
    const raw = await kv.get(recentKey(alias));
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr.filter((it) => it && it.value) : [];
  } catch {
    return [];
  }
}

async function listItemKeys(kv, alias) {
  try {
    const prefix = "item:" + String(alias || "").trim().toLowerCase() + ":";
    const listed = await kv.list({ prefix, limit: 50 });
    return (listed && listed.keys) || [];
  } catch {
    return [];
  }
}

async function loadItemsFromKv(kv, alias) {
  const keys = await listItemKeys(kv, alias);
  const items = [];
  // Lecturas en paralelo: varias ventanas Chrome reclaman a la vez
  const raws = await Promise.all(keys.map((k) => kv.get(k.name).catch(() => null)));
  for (let i = 0; i < keys.length; i++) {
    try {
      const raw = raws[i];
      if (!raw) continue;
      const it = JSON.parse(raw);
      if (it && it.value) items.push({ ...it, _key: keys[i].name });
    } catch {
      /* ignore */
    }
  }
  items.sort((a, b) => Number(a.ts || 0) - Number(b.ts || 0));
  return items;
}

async function readBox(env, alias) {
  const byId = new Map();
  for (const it of memItems(alias)) {
    if (it && it.value) byId.set(it.id || `${it.kind}:${it.value}:${it.ts}`, it);
  }
  if (env && env.OTP) {
    const rec = await loadRecent(env.OTP, alias);
    for (const it of rec) {
      if (it && it.value) byId.set(it.id || `${it.kind}:${it.value}:${it.ts}`, it);
    }
    const fromKv = await loadItemsFromKv(env.OTP, alias);
    for (const it of fromKv) {
      if (it && it.value) byId.set(it.id || `${it.kind}:${it.value}:${it.ts}`, it);
    }
  }
  const items = [...byId.values()].sort((a, b) => Number(a.ts || 0) - Number(b.ts || 0));
  if (items.length) memReplace(alias, items);
  return items;
}

async function pushItem(env, alias, item) {
  memPush(alias, item);
  if (!(env && env.OTP)) return;
  const body = JSON.stringify(item);
  const ttl = { expirationTtl: 60 * 60 * 2 };
  try {
    // 1) item: es la fuente de verdad (sobrevive races de recent:)
    await env.OTP.put(itemKey(alias, item.id), body, ttl);
    // 2) pending: lectura O(1) desde /claim (crítico con N ventanas en paralelo)
    await env.OTP.put(pendingKey(alias, item.kind), body, ttl);
    // 3) recent: merge read-modify-write para no pisar OTPs de otro correo concurrente
    let rec = await loadRecent(env.OTP, alias);
    const byId = new Map();
    for (const it of rec) {
      if (it && it.value) byId.set(it.id || `${it.kind}:${it.value}:${it.ts}`, it);
    }
    for (const it of memItems(alias)) {
      if (it && it.value) byId.set(it.id || `${it.kind}:${it.value}:${it.ts}`, it);
    }
    byId.set(item.id || `${item.kind}:${item.value}:${item.ts}`, item);
    const snap = [...byId.values()]
      .sort((a, b) => Number(a.ts || 0) - Number(b.ts || 0))
      .slice(-15);
    await env.OTP.put(recentKey(alias), JSON.stringify(snap), ttl);
  } catch (err) {
    console.log("kv push error", String(err));
  }
}

async function claimItem(env, alias, kind, afterTs, maxAge, consume) {
  const now = Date.now() / 1000;
  const byId = new Map();

  // A) pending: O(1) — el email handler acaba de escribir aquí
  if (env && env.OTP) {
    const kindsTry = [kind];
    if (kind === "login" || kind === "register") {
      kindsTry.push(kind === "login" ? "register" : "login");
    }
    for (const k of kindsTry) {
      try {
        const raw = await env.OTP.get(pendingKey(alias, k));
        if (!raw) continue;
        const it = JSON.parse(raw);
        if (it && it.value && !it.claimed && itemMatchesAlias(it, alias)) {
          byId.set(it.id || `${it.kind}:${it.value}:${it.ts}`, it);
        }
      } catch {
        /* ignore */
      }
    }
  }

  // B) memoria del isolate (solo útil si email+claim caen en el mismo isolate)
  for (const it of memItems(alias)) {
    if (it && it.value) byId.set(it.id || `${it.kind}:${it.value}:${it.ts}`, it);
  }

  // C) SIEMPRE recent + item: (antes: si recent tenía claimed viejos, NO leía item: nuevos)
  if (env && env.OTP) {
    try {
      const rec = await loadRecent(env.OTP, alias);
      for (const it of rec) {
        if (it && it.value) byId.set(it.id || `${it.kind}:${it.value}:${it.ts}`, it);
      }
      const fromKv = await loadItemsFromKv(env.OTP, alias);
      for (const it of fromKv) {
        if (it && it.value) byId.set(it.id || `${it.kind}:${it.value}:${it.ts}`, it);
      }
    } catch (err) {
      console.log("kv claim read error", String(err));
    }
  }

  let items = [...byId.values()]
    .filter((it) => itemMatchesAlias(it, alias))
    .sort((a, b) => Number(a.ts || 0) - Number(b.ts || 0));
  let idx = findUnclaimed(items, [kind], afterTs, maxAge, now, alias);
  if (idx < 0 && (kind === "login" || kind === "register")) {
    const other = kind === "login" ? "register" : "login";
    idx = findUnclaimed(items, [other], afterTs, maxAge, now, alias);
  }
  if (idx < 0) return null;
  const hit = items[idx];
  if (hit.alias && String(hit.alias).trim().toLowerCase() !== String(alias || "").trim().toLowerCase()) {
    return null;
  }
  if (consume) {
    items[idx] = { ...hit, claimed: true };
    memReplace(alias, items);
    if (env && env.OTP) {
      const marked = { ...hit, claimed: true };
      delete marked._key;
      const body = JSON.stringify(marked);
      const ttl = { expirationTtl: 60 * 60 * 2 };
      try {
        await Promise.all([
          env.OTP.put(hit._key || itemKey(alias, hit.id), body, ttl),
          env.OTP.put(recentKey(alias), JSON.stringify(items.slice(-15)), ttl),
          env.OTP.delete(pendingKey(alias, hit.kind)),
        ]);
      } catch {
        /* ignore */
      }
    }
  }
  return hit;
}

async function listItems(env, alias, kind, maxAge) {
  const now = Date.now() / 1000;
  const items = await readBox(env, alias);
  return items.filter(
    (it) =>
      it &&
      it.kind === kind &&
      it.value &&
      !it.claimed &&
      itemMatchesAlias(it, alias) &&
      now - Number(it.ts) <= maxAge
  );
}
