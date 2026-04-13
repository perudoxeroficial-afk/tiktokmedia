import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import yt_dlp


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tiktok_bot")

URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def extract_url(text: str) -> str | None:
    match = URL_PATTERN.search(text or "")
    return match.group(0) if match else None


def download_tiktok(url: str) -> tuple[Path, str]:
    temp_dir = Path(tempfile.mkdtemp(prefix="tiktok_bot_"))
    output_template = str(temp_dir / "%(id)s.%(ext)s")
    options = {
        "outtmpl": output_template,
        "format": "mp4/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = Path(ydl.prepare_filename(info))
        if file_path.suffix.lower() != ".mp4":
            mp4_candidate = file_path.with_suffix(".mp4")
            if mp4_candidate.exists():
                file_path = mp4_candidate

        title = info.get("title") or "video_tiktok"
        return file_path, title


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Enviame un enlace de TikTok y voy a intentar devolverte el video en la mejor version disponible."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Uso:\n"
        "1. Copia el link del video de TikTok.\n"
        "2. Pegalo en este chat.\n"
        "3. Espero unos segundos y te envio el archivo."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    url = extract_url(update.message.text)
    if not url:
        await update.message.reply_text("No encontré un enlace válido en tu mensaje.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)
    status = await update.message.reply_text("Procesando el enlace...")

    try:
        file_path, title = await asyncio.to_thread(download_tiktok, url)
    except Exception as exc:
        logger.exception("No se pudo descargar el video")
        await status.edit_text(
            "No pude descargar ese video. Puede ser privado, restringido o requerir otro método."
        )
        return

    try:
        with file_path.open("rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption=title[:1000],
                supports_streaming=True,
            )
        await status.delete()
    except Exception:
        logger.exception("No se pudo enviar el video")
        await status.edit_text("Pude descargar el video, pero falló el envío por Telegram.")
    finally:
        try:
            file_path.unlink(missing_ok=True)
            file_path.parent.rmdir()
        except OSError:
            logger.warning("No se pudo limpiar el archivo temporal %s", file_path)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Falta la variable de entorno TELEGRAM_BOT_TOKEN. Configúrala antes de iniciar el bot."
        )

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot iniciado")
    application.run_polling()


if __name__ == "__main__":
    main()
