# Email Worker Tidal (OTP y enlaces)

Sustituye Gmail IMAP para `*@cheapmusic.best`: el correo entra en Cloudflare, el worker extrae el código o el link y el script lo pide por HTTP. Las cuentas `@gmail.com` nativas siguen por IMAP.

URL desplegada: `https://otp.cheapmusic.best`

## 1. Desplegar

En `email_worker/`:

```bash
npx wrangler login
npx wrangler kv namespace create OTP
npx wrangler secret put OTP_SECRET
npx wrangler deploy
```

El worker queda en `otp.cheapmusic.best` (custom domain; no hace falta workers.dev).

## 2. Email Routing (Cloudflare)

Dominio `cheapmusic.best` → **Email** → **Email Routing**:

1. Catch-all (o regla `*@cheapmusic.best`).
2. Acción: **Send to a Worker** → `tidal-otp-worker`.
3. El worker reenvía copia a `cakeseller1234@gmail.com` (`FORWARD_TO` en `wrangler.toml`).

Si la UI no deja Worker + Gmail a la vez, deja solo Worker: el `forward()` del script hace la copia.

## 3. passwords.txt

```
email_worker_url=https://otp.cheapmusic.best
email_worker_secret=EL_MISMO_SECRETO_QUE_PUSISTE_EN_WRANGLER
```

No pongas `email_worker_imap_fallback=1` salvo que quieras IMAP de respaldo (Gmail puede colgarse).

## 4. Comprobar

Abre `https://otp.cheapmusic.best/health` — debe devolver `{"ok":true,"service":"tidal-otp-worker"}`.

Opción **12** del menú muestra si el worker responde. Opción **3** (OTP login) debe imprimir `[WORKER] login para titular-...@cheapmusic.best`.

| Flujo | Menú | kind worker |
|---|---|---|
| OTP registro | 1, 8, 14, 15 | register |
| OTP eliminación | 2, 15 | delete |
| OTP login | 3, 4, 10 | login |
| Invitación familiar | 4, 11 | invite |
| Reset de contraseña | 5, 9 | reset |
