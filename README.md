# Bot de Telegram para TikTok e Instagram Reels

Este proyecto crea un bot de Telegram que recibe enlaces de TikTok e Instagram Reels e intenta descargar el video en la mejor calidad disponible usando `yt-dlp`.

## 1. Instalar dependencias

```powershell
pip install -r requirements.txt
```

## 2. Crear tu bot en Telegram

1. Abre `@BotFather` en Telegram.
2. Usa el comando `/newbot`.
3. Copia el token que te entregue.

## 3. Configurar el token

Opción recomendada: crea un archivo `.env` en esta carpeta con este contenido:

```env
TELEGRAM_BOT_TOKEN=AQUI_TU_TOKEN
ADMIN_USER_IDS=123456789
```

También puedes usar PowerShell:

```powershell
$env:TELEGRAM_BOT_TOKEN="AQUI_TU_TOKEN"
```

## 4. Iniciar el bot

```powershell
python bot.py
```

## 5. Usarlo

En tu chat con el bot:

- envía `/start`
- pega un enlace de TikTok o Instagram Reel, o usa `/descargar https://...`
- espera la respuesta del bot
- el archivo también quedará guardado en `downloads/`

## 6. Comandos

- `/start` muestra bienvenida
- `/help` muestra ayuda
- `/descargar LINK` descarga directamente desde comando
- `/estado` muestra si el bot sigue online
- `/menu` abre el menu rapido con botones
- `/stats` muestra estadísticas si tu usuario está en `ADMIN_USER_IDS`
- `/last` muestra las últimas descargas si eres admin
- `/errors` muestra los últimos errores guardados si eres admin
- `/top` muestra links y usuarios más activos si eres admin

## Notas

- `yt-dlp` intenta obtener la mejor versión disponible del video.
- Algunos videos privados, restringidos o protegidos pueden fallar.
- El bot soporta TikTok e Instagram Reels.
- El bot incluye teclado rapido y botones inline para que se vea más llamativo en Telegram.
- El proyecto carga variables desde `.env` automáticamente.
- Hay límite de 5 solicitudes por minuto por usuario.
- El bot guarda historial en `data/download_history.jsonl`.
- El bot reutiliza descargas repetidas desde `data/cache_index.json` cuando el archivo local sigue existiendo.
- Las descargas nuevas hacen un reintento automático si falla el primer intento.
- Usa esto solo con contenido que tengas derecho a descargar y reutilizar.
