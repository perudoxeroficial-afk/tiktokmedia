import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from collections import Counter, defaultdict, deque

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import yt_dlp


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tiktok_bot")

URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)
BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DATA_DIR = BASE_DIR / "data"
CACHE_INDEX_FILE = DATA_DIR / "cache_index.json"
HISTORY_FILE = DATA_DIR / "download_history.jsonl"
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX_REQUESTS = 5
DOWNLOAD_RETRIES = 2

user_requests: dict[int, deque[float]] = defaultdict(deque)
stats = {
    "started_at": time.time(),
    "total_requests": 0,
    "successful_downloads": 0,
    "failed_downloads": 0,
    "cache_hits": 0,
}
cache_index: dict[str, dict[str, str]] = {}
QUICK_ACTIONS = {"descargar", "ayuda", "estado", "menu"}


def extract_url(text: str) -> str | None:
    match = URL_PATTERN.search(text or "")
    return match.group(0) if match else None


def is_supported_url(url: str) -> bool:
    lowered = url.lower()
    return (
        "tiktok.com/" in lowered
        or "vm.tiktok.com/" in lowered
        or "instagram.com/reel/" in lowered
        or "instagram.com/reels/" in lowered
    )


def detect_platform(url: str) -> str:
    lowered = url.lower()
    if "instagram.com/reel/" in lowered or "instagram.com/reels/" in lowered:
        return "Instagram Reel"
    return "TikTok"


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


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


def load_cache_index() -> dict[str, dict[str, str]]:
    if not CACHE_INDEX_FILE.exists():
        return {}

    try:
        data = json.loads(CACHE_INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("No se pudo leer cache_index.json, se recreará.")
        return {}

    if not isinstance(data, dict):
        return {}

    valid_cache: dict[str, dict[str, str]] = {}
    for url, entry in data.items():
        if not isinstance(entry, dict):
            continue
        path_value = entry.get("saved_file")
        if not isinstance(path_value, str):
            continue
        saved_path = Path(path_value)
        if saved_path.exists():
            valid_cache[url] = entry
    return valid_cache


def save_cache_index() -> None:
    CACHE_INDEX_FILE.write_text(
        json.dumps(cache_index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_history(entry: dict[str, object]) -> None:
    ensure_storage()
    with HISTORY_FILE.open("a", encoding="utf-8") as history_file:
        history_file.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_history(limit: int = 10, statuses: set[str] | None = None) -> list[dict[str, object]]:
    if not HISTORY_FILE.exists():
        return []

    entries: list[dict[str, object]] = []
    with HISTORY_FILE.open("r", encoding="utf-8") as history_file:
        for line in history_file:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if statuses and entry.get("status") not in statuses:
                continue
            entries.append(entry)

    return entries[-limit:]


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


def download_media(url: str) -> tuple[Path, str, Path]:
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

        saved_file = DOWNLOADS_DIR / f"{sanitize_filename(title)}_{video_id}{file_path.suffix.lower() or '.mp4'}"
        shutil.copy2(file_path, saved_file)

        return file_path, title, saved_file


def get_cached_file(url: str) -> tuple[Path, str] | None:
    cached_entry = cache_index.get(url)
    if not cached_entry:
        return None

    saved_path = Path(cached_entry["saved_file"])
    if not saved_path.exists():
        cache_index.pop(url, None)
        save_cache_index()
        return None

    title = str(cached_entry.get("title", saved_path.stem))
    return saved_path, title


def update_cache(url: str, title: str, saved_file: Path) -> None:
    cache_index[url] = {
        "title": title,
        "saved_file": str(saved_file),
        "updated_at": str(int(time.time())),
    }
    save_cache_index()


def download_with_retry(url: str) -> tuple[Path, str, Path]:
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            return download_media(url)
        except Exception as exc:
            last_error = exc
            logger.warning("Descarga falló en intento %s/%s: %s", attempt, DOWNLOAD_RETRIES, exc)
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(2)

    raise last_error if last_error else RuntimeError("No se pudo descargar el archivo.")


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


def format_file_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def format_timestamp(timestamp: object) -> str:
    if not isinstance(timestamp, int):
        return "sin fecha"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def build_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["Descargar", "Ayuda"], ["Estado", "Menu"]],
        resize_keyboard=True,
        input_field_placeholder="Pega un link o usa un botón",
    )


def build_inline_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Descargar", callback_data="menu_download"),
            InlineKeyboardButton("Ayuda", callback_data="menu_help"),
        ],
        [
            InlineKeyboardButton("Estado", callback_data="menu_status"),
            InlineKeyboardButton("Menu", callback_data="menu_home"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("Panel Admin", callback_data="menu_admin")])
    return InlineKeyboardMarkup(rows)


def is_admin_user(application: Application, user_id: int | None) -> bool:
    if user_id is None:
        return False
    admin_ids = application.bot_data.get("admin_user_ids", set())
    return user_id in admin_ids


def build_welcome_text(first_name: str | None) -> str:
    greeting_name = first_name or "bro"
    return (
        f"<b>TikSaveBot</b>\n"
        f"Hola, {greeting_name}.\n\n"
        f"Estoy online para descargar videos de <b>TikTok</b> e <b>Instagram Reels</b>.\n"
        f"Solo pega el link y yo me encargo del resto.\n\n"
        f"<b>Lo que hago:</b>\n"
        f"- Descarga rapida\n"
        f"- Cache para enlaces repetidos\n"
        f"- Reintento automatico si falla\n"
        f"- Envio como video o archivo si hace falta"
    )


def build_help_text() -> str:
    return (
        "<b>Como usar TikSaveBot</b>\n\n"
        "1. Toca <b>Descargar</b> o pega tu link directo.\n"
        "2. Espera unos segundos.\n"
        "3. Recibe el video en el chat.\n\n"
        "<b>Comandos:</b>\n"
        "/start\n"
        "/help\n"
        "/descargar LINK\n"
        "/estado\n"
        "/menu"
    )


def build_status_text() -> str:
    uptime = format_uptime(int(time.time() - stats["started_at"]))
    return (
        "<b>Estado del bot</b>\n\n"
        f"Online: si\n"
        f"Uptime: {uptime}\n"
        f"Solicitudes: {stats['total_requests']}\n"
        f"Exitosas: {stats['successful_downloads']}\n"
        f"Cache hits: {stats['cache_hits']}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        build_welcome_text(update.effective_user.first_name if update.effective_user else None),
        parse_mode="HTML",
        reply_markup=build_main_keyboard(),
    )
    await update.message.reply_text(
        "Elige una accion rapida:",
        reply_markup=build_inline_menu(is_admin_user(context.application, update.effective_user.id if update.effective_user else None)),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        build_help_text(),
        parse_mode="HTML",
        reply_markup=build_inline_menu(is_admin_user(context.application, update.effective_user.id if update.effective_user else None)),
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

    if not is_supported_url(url):
        await update.message.reply_text("Por ahora este bot acepta enlaces de TikTok e Instagram Reels.")
        return

    platform_name = detect_platform(url)
    stats["total_requests"] += 1
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)
    status = await update.message.reply_text(
        f"{platform_name} detectado.\nAnalizando link..."
    )

    cached_result = get_cached_file(url)
    source_label = "descarga nueva"
    try:
        if cached_result:
            saved_file, title = cached_result
            file_path = saved_file
            stats["cache_hits"] += 1
            source_label = "cache"
            await status.edit_text(
                f"{platform_name} detectado.\nAnalizando link...\nDescargando video...\nCache encontrada."
            )
        else:
            await status.edit_text(
                f"{platform_name} detectado.\nAnalizando link...\nDescargando video..."
            )
            file_path, title, saved_file = await asyncio.to_thread(download_with_retry, url)
            update_cache(url, title, saved_file)
    except Exception as exc:
        logger.exception("No se pudo descargar el video")
        stats["failed_downloads"] += 1
        write_history(
            {
                "timestamp": int(time.time()),
                "user_id": user_id,
                "url": url,
                "status": "download_failed",
                "error": str(exc),
            }
        )
        await status.edit_text(classify_download_error(exc))
        return

    file_size = format_file_size(file_path.stat().st_size)
    await status.edit_text(
        f"{platform_name} detectado.\nAnalizando link...\nDescargando video...\nEnviando archivo..."
    )
    try:
        with file_path.open("rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption=title[:1000],
                supports_streaming=True,
            )
        stats["successful_downloads"] += 1
        write_history(
            {
                "timestamp": int(time.time()),
                "user_id": user_id,
                "url": url,
                "status": "sent_video",
                "source": source_label,
                "saved_file": str(saved_file),
                "file_size": file_path.stat().st_size,
            }
        )
        await status.edit_text(
            f"Listo. Fuente: {source_label}.\nCopia local:\n{saved_file}\nTamaño: {file_size}"
        )
    except Exception:
        logger.exception("No se pudo enviar el video como video")
        try:
            with file_path.open("rb") as document_file:
                await update.message.reply_document(
                    document=document_file,
                    caption=title[:1000],
                )
            stats["successful_downloads"] += 1
            write_history(
                {
                    "timestamp": int(time.time()),
                    "user_id": user_id,
                    "url": url,
                    "status": "sent_document",
                    "source": source_label,
                    "saved_file": str(saved_file),
                    "file_size": file_path.stat().st_size,
                }
            )
            await status.edit_text(
                f"Listo. Fuente: {source_label}.\nTelegram no aceptó el MP4 como video, así que te lo mandé como archivo.\n"
                f"Copia local:\n{saved_file}\nTamaño: {file_size}"
            )
        except Exception:
            logger.exception("No se pudo enviar el archivo tampoco")
            stats["failed_downloads"] += 1
            write_history(
                {
                    "timestamp": int(time.time()),
                    "user_id": user_id,
                    "url": url,
                    "status": "send_failed",
                    "source": source_label,
                    "saved_file": str(saved_file),
                    "file_size": file_path.stat().st_size,
                }
            )
            await status.edit_text(
                f"Pude descargar el video y quedó guardado en:\n{saved_file}\n"
                f"Tamaño: {file_size}\n"
                "Pero Telegram rechazó el envío. Revisa los logs de Railway para ver si fue límite de tamaño, timeout o error temporal."
            )
    finally:
        if file_path != saved_file:
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


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        build_status_text(),
        parse_mode="HTML",
        reply_markup=build_inline_menu(is_admin_user(context.application, update.effective_user.id if update.effective_user else None)),
    )


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
        f"- aciertos de cache: {stats['cache_hits']}\n"
        f"- usuarios registrados en memoria: {active_users}"
    )


async def last_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    admin_ids = context.application.bot_data.get("admin_user_ids", set())
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("Ese comando es solo para admin.")
        return

    entries = read_history(limit=5)
    if not entries:
        await update.message.reply_text("Todavía no hay historial guardado.")
        return

    lines = ["Últimas descargas:"]
    for entry in reversed(entries):
        lines.append(
            f"- {format_timestamp(entry.get('timestamp'))} | {entry.get('status')} | {entry.get('source', 'sin fuente')} | {entry.get('url', 'sin url')}"
        )

    await update.message.reply_text("\n".join(lines))


async def errors_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    admin_ids = context.application.bot_data.get("admin_user_ids", set())
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("Ese comando es solo para admin.")
        return

    entries = read_history(limit=5, statuses={"download_failed", "send_failed"})
    if not entries:
        await update.message.reply_text("No hay errores recientes en el historial.")
        return

    lines = ["Últimos errores:"]
    for entry in reversed(entries):
        error_text = str(entry.get("error", "sin detalle"))
        lines.append(
            f"- {format_timestamp(entry.get('timestamp'))} | {entry.get('status')} | {error_text[:120]}"
        )

    await update.message.reply_text("\n".join(lines))


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    admin_ids = context.application.bot_data.get("admin_user_ids", set())
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("Ese comando es solo para admin.")
        return

    entries = read_history(limit=200)
    if not entries:
        await update.message.reply_text("Todavía no hay suficiente historial para calcular top.")
        return

    url_counter: Counter[str] = Counter()
    user_counter: Counter[str] = Counter()

    for entry in entries:
        url = str(entry.get("url", "")).strip()
        if url:
            url_counter[url] += 1

        user_id = entry.get("user_id")
        if isinstance(user_id, int):
            user_counter[str(user_id)] += 1

    top_urls = url_counter.most_common(3)
    top_users = user_counter.most_common(3)

    lines = ["Top reciente del bot:"]
    if top_urls:
        lines.append("Links más repetidos:")
        for url, count in top_urls:
            lines.append(f"- {count}x | {url}")

    if top_users:
        lines.append("Usuarios más activos:")
        for user_id, count in top_users:
            lines.append(f"- {count} solicitudes | user_id {user_id}")

    await update.message.reply_text("\n".join(lines))


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "Menu principal de TikSaveBot:",
        reply_markup=build_inline_menu(is_admin_user(context.application, update.effective_user.id if update.effective_user else None)),
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return

    await query.answer()
    admin = is_admin_user(context.application, update.effective_user.id if update.effective_user else None)

    if query.data == "menu_download":
        await query.message.reply_text(
            "Pega aqui tu link de TikTok o Instagram Reel y lo proceso.",
            reply_markup=build_main_keyboard(),
        )
        return

    if query.data == "menu_help":
        await query.message.reply_text(build_help_text(), parse_mode="HTML", reply_markup=build_inline_menu(admin))
        return

    if query.data == "menu_status":
        await query.message.reply_text(build_status_text(), parse_mode="HTML", reply_markup=build_inline_menu(admin))
        return

    if query.data == "menu_admin":
        if not admin:
            await query.message.reply_text("Ese panel es solo para admin.")
            return
        await query.message.reply_text(
            "<b>Panel Admin</b>\n\nUsa:\n/stats\n/last\n/errors\n/top",
            parse_mode="HTML",
            reply_markup=build_inline_menu(admin),
        )
        return

    await query.message.reply_text(
        build_welcome_text(update.effective_user.first_name if update.effective_user else None),
        parse_mode="HTML",
        reply_markup=build_inline_menu(admin),
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    lowered = update.message.text.strip().lower()
    if lowered == "descargar":
        await update.message.reply_text("Pega tu link de TikTok o Instagram Reel y lo descargo.")
        return
    if lowered == "ayuda":
        await help_command(update, context)
        return
    if lowered == "estado":
        await status_command(update, context)
        return
    if lowered == "menu":
        await menu_command(update, context)
        return

    await process_download(update, context, update.message.text)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Inicia TikSaveBot"),
            BotCommand("help", "Muestra la ayuda"),
            BotCommand("descargar", "Descarga un link"),
            BotCommand("estado", "Muestra si el bot está online"),
            BotCommand("menu", "Abre el menu rapido"),
            BotCommand("stats", "Estadisticas admin"),
            BotCommand("last", "Ultimas descargas admin"),
            BotCommand("errors", "Ultimos errores admin"),
            BotCommand("top", "Top reciente admin"),
        ]
    )


def main() -> None:
    load_dotenv()
    ensure_storage()
    cache_index.update(load_cache_index())
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Falta la variable de entorno TELEGRAM_BOT_TOKEN. Configúrala antes de iniciar el bot."
        )

    application = Application.builder().token(token).post_init(post_init).build()
    application.bot_data["admin_user_ids"] = parse_admin_ids()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("descargar", download_command))
    application.add_handler(CommandHandler("estado", status_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("last", last_command))
    application.add_handler(CommandHandler("errors", errors_command))
    application.add_handler(CommandHandler("top", top_command))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot iniciado")
    application.run_polling()


if __name__ == "__main__":
    main()
