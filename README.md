# Bot de Telegram para TikTok

Este proyecto crea un bot simple de Telegram que recibe enlaces de TikTok e intenta descargar el video en la mejor calidad disponible usando `yt-dlp`.

## 1. Instalar dependencias

```powershell
pip install -r requirements.txt
```

## 2. Crear tu bot en Telegram

1. Abre `@BotFather` en Telegram.
2. Usa el comando `/newbot`.
3. Copia el token que te entregue.

## 3. Configurar el token

En PowerShell:

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
- pega un enlace de TikTok
- espera la respuesta del bot

## Notas

- `yt-dlp` intenta obtener la mejor versión disponible del video.
- Algunos videos privados, restringidos o protegidos pueden fallar.
- Usa esto solo con contenido que tengas derecho a descargar y reutilizar.
