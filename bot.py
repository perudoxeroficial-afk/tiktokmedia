import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from collections import defaultdict, deque

from dotenv import load_dotenv
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
BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX_REQUESTS = 5

user_requests: dict[int, deque[float]] = defaultdict(deque)
stats = {
    "started_at": time.time(),
    "total_requests": 0,
    "successful_downloads": 0,
    "failed_downloads": 0,
}


def extract_url(text: str) -> str | None:
    match = URL_PATTERN.search(text or "")
    return match.group(0) if match else None


def is_tiktok_url(url: str) -> bool:
    lowered = url.lower()
    return "tiktok.com/" in lowered or "vm.tiktok.com/" in lowered


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or "video_tiktok"


def parse_admin_ids() -> set[int]:
    raw_value = os.getenv("ADMIN_USER_IDS", "").strip()
    if not raw_value:
        return set()

    admin_ids = set()
    for part in raw_value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            admin_ids.add(int(part))
        except ValueError:
            logger.warning("ADMIN_USER_IDS contiene un valor inválido: %s", part)
    return admin_ids


def is_rate_limited(user_id: int) -> tuple[bool, int]:
    now = time.time()
    requests = user_requests[user_id]

    while requests and now - requests[0] > RATE_LIMIT_WINDOW:
        requests.popleft()

    if len(requests) >= RATE_LIMIT_MAX_REQUESTS:
        retry_after = max(1, int(RATE_LIMIT_WINDOW - (now - requests[0])))
        return True, retry_after

    requests.append(now)
    return False, 0


def download_tiktok(url: str) -> tuple[Path, str, Path]:
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
        video_id = info.get("id") or file_path.stem

        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        saved_file = DOWNLOADS_DIR / f"{sanitize_filename(title)}_{video_id}{file_path.suffix.lower() or '.mp4'}"
        shutil.copy2(file_path, saved_file)

        return file_path, title, saved_file


def classify_download_error(exc: Exception) -> str:
    message = str(exc).lower()
    if "private" in message or "status code 10204" in message:
        return "Ese video parece privado o no está disponible públicamente."
    if "login" in message or "sign in" in message:
        return "Ese video pide inicio de sesión y ahora mismo no puedo descargarlo."
    if "unable to extract" in message or "unsupported url" in message:
        return "No pude leer ese enlace. Prueba con el link directo del video."
    if "http error 404" in message or "not found" in message:
        return "Ese enlace ya no existe o fue eliminado."
    return "No pude descargar ese video. Puede ser privado, restringido o requerir otro método."


def format_uptime(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h {minutes}m {secs}s"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Enviame un enlace de TikTok y voy a intentar devolverte el video en la mejor version disponible. "
        "Tambien voy a guardar una copia local en la carpeta downloads.\n\n"
        "Tip: también puedes usar /descargar seguido del link."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Uso:\n"
        "1. Copia el link del video de TikTok.\n"
        "2. Pegalo en este chat o usa /descargar LINK.\n"
        "3. Espero unos segundos y te envio el archivo.\n"
        "4. El video tambien queda guardado en la carpeta downloads.\n\n"
        "Límite actual: 5 descargas por minuto por usuario."
    )


async def process_download(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not update.message:
        return

    user = update.effective_user
    user_id = user.id if user else 0

    limited, retry_after = is_rate_limited(user_id)
    if limited:
        await update.message.reply_text(
            f"Vas muy rápido. Espera {retry_after} segundos y prueba otra vez."
        )
        return

    url = extract_url(text)
    if not url:
        await update.message.reply_text("No encontré un enlace válido en tu mensaje.")
        return

    if not is_tiktok_url(url):
        await update.message.reply_text("Por ahora este bot está configurado solo para enlaces de TikTok.")
        return

    stats["total_requests"] += 1
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)
    status = await update.message.reply_text("Procesando el enlace...")

    try:
        file_path, title, saved_file = await asyncio.to_thread(download_tiktok, url)
    except Exception as exc:
        logger.exception("No se pudo descargar el video")
        stats["failed_downloads"] += 1
        await status.edit_text(classify_download_error(exc))
        return

    try:
        with file_path.open("rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption=title[:1000],
                supports_streaming=True,
            )
        stats["successful_downloads"] += 1
        await status.edit_text(f"Listo. Tambien guardé una copia en:\n{saved_file}")
    except Exception:
        logger.exception("No se pudo enviar el video")
        stats["failed_downloads"] += 1
        await status.edit_text(
            f"Pude descargar el video y quedó guardado en:\n{saved_file}\n"
            "Pero falló el envío por Telegram. Puede ser por tamaño o por un error temporal."
        )
    finally:
        try:
            file_path.unlink(missing_ok=True)
            shutil.rmtree(file_path.parent, ignore_errors=True)
        except OSError:
            logger.warning("No se pudo limpiar el archivo temporal %s", file_path)


async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usa el comando así:\n/descargar https://www.tiktok.com/...")
        return

    await process_download(update, context, text)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    admin_ids = context.application.bot_data.get("admin_user_ids", set())
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("Ese comando es solo para admin.")
        return

    uptime = format_uptime(int(time.time() - stats["started_at"]))
    active_users = len(user_requests)
    await update.message.reply_text(
        "Estadísticas del bot:\n"
        f"- uptime: {uptime}\n"
        f"- solicitudes totales: {stats['total_requests']}\n"
        f"- descargas exitosas: {stats['successful_downloads']}\n"
        f"- descargas fallidas: {stats['failed_downloads']}\n"
        f"- usuarios registrados en memoria: {active_users}"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    await process_download(update, context, update.message.text)


def main() -> None:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Falta la variable de entorno TELEGRAM_BOT_TOKEN. Configúrala antes de iniciar el bot."
        )

    application = Application.builder().token(token).build()
    application.bot_data["admin_user_ids"] = parse_admin_ids()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("descargar", download_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot iniciado")
    application.run_polling()


if __name__ == "__main__":
    main()
