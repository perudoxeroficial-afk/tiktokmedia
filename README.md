# TikSaveBot

TikSaveBot es un bot de Telegram pensado para descargar videos de TikTok e Instagram Reels con una experiencia más cuidada, rápida y visual dentro del chat. Usa `yt-dlp` para obtener la mejor versión disponible y añade cache, historial, panel admin y una interfaz conversacional más pulida.

## 1. Instalar dependencias

```powershell
pip install -r requirements.txt
```

## 2. Crear tu bot en Telegram

1. Abre `@BotFather` en Telegram.
2. Usa el comando `/newbot`.
3. Copia el token que te entregue.

Textos recomendados para BotFather:

- Nombre: `TikSaveBot`
- Descripción: `Descarga videos de TikTok e Instagram Reels con una experiencia rápida, limpia y premium.`
- About: `Asistente de Telegram para descargar videos desde TikTok e Instagram Reels con cache inteligente, seguimiento de actividad y comandos de administración.`

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

## 7. Experiencia

- interfaz con botones rápidos e inline
- mensajes con estilo premium dentro del chat
- progreso visual por etapas durante la descarga
- detección automática de plataforma
- acciones rápidas después de cada entrega

## 8. Notas

- `yt-dlp` intenta obtener la mejor versión disponible del video.
- Algunos videos privados, restringidos o protegidos pueden fallar.
- El bot soporta TikTok e Instagram Reels.
- El bot incluye teclado rapido y botones inline para que se vea más llamativo en Telegram.
- El proyecto carga variables desde `.env` automáticamente.
- Hay límite de 5 solicitudes por minuto por usuario.
- El bot guarda historial en `data/download_history.jsonl`.
- El bot reutiliza descargas repetidas desde `data/cache_index.json` cuando el archivo local sigue existiendo.
- Las descargas nuevas hacen un reintento automático si falla el primer intento.
- Si tu servicio usa Railpack, `railpack.json` instala `ffmpeg`, `nodejs` y `npm`, y además ejecuta `npm install --prefix toby_lab` durante el build para soportar publicaciones `photo`.
- Usa esto solo con contenido que tengas derecho a descargar y reutilizar.

## 9. Photo Worker

Las publicaciones `photo` de TikTok usan `photo_worker.py`, que obtiene metadata con `@tobyg74/tiktok-api-dl` desde `toby_lab/` y luego arma el MP4 con `ffmpeg`.

Puedes probar el worker aislado así:

```powershell
python photo_worker.py "https://vt.tiktok.com/..."
```

Si funciona, el script devolverá un JSON con el archivo generado y lo guardará en `photo_worker_output/`.
Si falla, devolverá un JSON con el error exacto para investigar esa ruta aparte sin congelar el bot principal.

Dependencias de este laboratorio:

```powershell
cd toby_lab
npm install
```
