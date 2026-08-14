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
        await pushItem(env.OTP, alias, item);
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
      const hit = await claimItem(env.OTP, alias, kind, afterTs, maxAge, consume);
      if (!hit) return json({ ok: false, found: false });
      return json({ ok: true, found: true, value: hit.value, kind: hit.kind, ts: hit.ts, subject: hit.subject });
    }
    if (url.pathname === "/list") {
      const alias = (url.searchParams.get("alias") || "").trim().toLowerCase();
      const kind = (url.searchParams.get("kind") || "invite").trim().toLowerCase();
      const maxAge = Number(url.searchParams.get("max_age") || "86400") || 86400;
      if (!alias) return json({ ok: false, error: "alias required" }, 400);
      const items = await listItems(env.OTP, alias, kind, maxAge);
      return json({ ok: true, items });
    }
    if (url.pathname === "/peek") {
      const alias = (url.searchParams.get("alias") || "").trim().toLowerCase();
      if (!alias) return json({ ok: false, error: "alias required" }, 400);
      const items = await readBox(env.OTP, alias);
      return json({ ok: true, alias, count: items.length, items });
    }
    return json({ ok: false, error: "not found" }, 404);
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
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

function pickAlias(messageTo, decoded, domain) {
  const dom = String(domain || "cheapmusic.best").toLowerCase();
  const blob = [
    messageTo,
    decoded.headers.to,
    decoded.headers["delivered-to"],
    decoded.headers["x-original-to"],
    decoded.headers["x-forwarded-to"],
    decoded.headers["x-google-original-to"],
    decoded.all.slice(0, 12000),
  ].join(" ");
  const found = emailsIn(blob).filter((e) => e.endsWith("@" + dom));
  if (found.length) return found[0];
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
  const nearRe =
    /(?:^|[^a-záéíóúñ])(?:c[oó]digo|code|sign[-\s]?in|verification|one[-\s]?time|introduce|ingresa|enter)[^\d]{0,240}(\d{5,6})/gi;
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
  const t = unescapeHtml(String(text || ""));
  const m = t.match(/https?:\/\/login\.tidal\.com\/resetpass\/[A-Za-z0-9._~\-]+/i);
  return m ? m[0].replace(/[>"']+$/, "") : null;
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

  const reset = extractReset(full);
  if (reset || /resetpass|restablecer tu contrase|resetting your tidal password|reset your password/.test(blob)) {
    if (reset) return { ...base, kind: "reset", value: reset };
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
  if (
    /c[oó]digo de inicio|login code|sign-?in code|c[oó]digo de acceso|your tidal login code/.test(blob)
  ) {
    return { ...base, kind: "login", value: otp };
  }
  if (
    /registr|bienven|sign-?up|finish creating|terminar de crear/.test(blob)
  ) {
    return { ...base, kind: "register", value: otp };
  }
  return { ...base, kind: "login", value: otp };
}

function kvKey(alias) {
  return "box:" + String(alias || "").trim().toLowerCase();
}

async function readBox(kv, alias) {
  try {
    const raw = await kv.get(kvKey(alias));
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

async function writeBox(kv, alias, items) {
  const trimmed = items.slice(-MAX_ITEMS);
  await kv.put(kvKey(alias), JSON.stringify(trimmed), { expirationTtl: 60 * 60 * 36 });
}

async function pushItem(kv, alias, item) {
  const items = await readBox(kv, alias);
  items.push(item);
  await writeBox(kv, alias, items);
}

function findUnclaimed(items, kinds, afterTs, maxAge, now) {
  const want = new Set(kinds);
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (!it || !it.value || it.claimed) continue;
    if (!want.has(it.kind)) continue;
    if (afterTs && Number(it.ts) <= afterTs) continue;
    if (now - Number(it.ts) > maxAge) continue;
    return i;
  }
  return -1;
}

async function claimItem(kv, alias, kind, afterTs, maxAge, consume) {
  const now = Date.now() / 1000;
  const items = await readBox(kv, alias);
  let idx = findUnclaimed(items, [kind], afterTs, maxAge, now);
  if (idx < 0 && (kind === "login" || kind === "register")) {
    const other = kind === "login" ? "register" : "login";
    idx = findUnclaimed(items, [other], afterTs, maxAge, now);
  }
  if (idx < 0) return null;
  const hit = items[idx];
  if (consume) {
    items[idx] = { ...hit, claimed: true };
    await writeBox(kv, alias, items);
  }
  return hit;
}

async function listItems(kv, alias, kind, maxAge) {
  const now = Date.now() / 1000;
  const items = await readBox(kv, alias);
  return items.filter(
    (it) =>
      it &&
      it.kind === kind &&
      it.value &&
      !it.claimed &&
      now - Number(it.ts) <= maxAge
  );
}
