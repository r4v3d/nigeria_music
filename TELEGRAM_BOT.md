# Bot Telegram + Contabo (sesiones_imap)

Controla las opciones **1–5** y **9** desde el móvil. Códigos (1–3) y links (4–5) llegan en mensajes cortos con bloque `<code>` para copiar con un toque. 4/5/9 abren Chrome **con ventana** (vía Xvfb en Contabo) y proxies PE/NG.

Los alias `@cheapmusic.best` usan el **Email Worker** (`otp.cheapmusic.best`), igual que `sesiones_imap.py`. En el VPS, `passwords.txt` debe incluir `email_worker_url` y `email_worker_secret`. Gmail nativo sigue por IMAP.

## 1. Preparar el bot

1. En Telegram: [@BotFather](https://t.me/BotFather) → `/newbot` → copia el token.
2. Habla con tu bot y envía `/whoami` (tras el primer arranque) o usa [@userinfobot](https://t.me/userinfobot) para tu **user id** numérico.

## 2. Contabo (Ubuntu)

```bash
sudo mkdir -p /opt/tidal-nigeria
# Sube el repo (git clone o scp) a /opt/tidal-nigeria
cd /opt/tidal-nigeria

sudo apt update
sudo apt install -y python3 python3-venv python3-pip xvfb \
  libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 \
  libasound2t64 || sudo apt install -y libasound2

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium   # si hace falta
```

Copia credenciales (desde tu PC, **no** a GitHub):

- `passwords.txt`
- `sesiones_imap_cuentas.txt`
- `lista_proxies_pe.txt` / `lista_proxies_ng.txt` (o `*_validos.txt`)
- `titular_familiar.txt` (si usas invitaciones en op9)

```bash
cp .env.example .env
nano .env   # TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_IDS
```

## 3. Servicio systemd

```bash
sudo cp deploy/tidal-telegram.service /etc/systemd/system/
# Ajusta User= y rutas si no usas /opt/tidal-nigeria ni root
sudo systemctl daemon-reload
sudo systemctl enable --now tidal-telegram
sudo journalctl -u tidal-telegram -f
```

## 4. Uso en Telegram

| Comando | Acción |
|---------|--------|
| `/start` | Menú con botones |
| `/correos` | Reemplazar lista (luego pega alias) |
| `/lista` | Ver correos activos |
| `/limpiar` | Vaciar lista |
| `/imap` | Añadir App Password a `passwords.txt` |
| `/op1` | Código de registro (IMAP) |
| `/op2` | Código de eliminación (IMAP) |
| `/op3` | Código de login (IMAP) |
| `/op4` | Aceptar invitaciones (IMAP) |
| `/op4_archivo` | Igual, desde `linksextraidos.txt` |
| `/op5` | Abrir enlace reset desde IMAP |
| `/op9` | Restablecer contraseñas |
| `/op12` | Verificar IMAP en `passwords.txt` |
| `/status` | Job en curso + logs |
| `/cancel` | Cancelar job o modo pegado (no borra correos) |

Solo un job a la vez. Códigos/links llegan en mensajes cortos con bloque tocable para copiar.

## 5. Seguridad

- Whitelist obligatoria (`TELEGRAM_ALLOWED_IDS`).
- No subas `.env`, `passwords.txt` ni proxies a Git.
- Firewall: SSH + salida HTTPS; el bot usa **long-polling** (no abras puerto 443 entrante).
- Confirma que el tráfico a Tidal va por proxy (si un proxy falla, el script omite en lugar de usar IP Contabo en los flujos PE/NG obligatorios).

## 6. RAM Contabo

Si el VPS es pequeño (2 GB), pon en `.env`:

```
TIDAL_MAX_PARALLEL=3
```

(y en el futuro se puede bajar el `tam_oleada` de los runners).
