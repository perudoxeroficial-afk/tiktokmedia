import asyncio
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from collections import Counter, defaultdict, deque
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, ReplyKeyboardMarkup, Update
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
PHOTO_GALLERY_MAX_IMAGES = 8
PHOTO_GALLERY_DOWNLOAD_TIMEOUT = 45
PHOTO_GALLERY_SEND_TIMEOUT = 30
PHOTO_PREVIEW_SEND_TIMEOUT = 20
PHOTO_CONVERSION_TIMEOUT = 25
PHOTO_WORKER_TIMEOUT = 120

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


def is_tiktok_photo_url(url: str) -> bool:
    parsed = urlparse(url)
    return "tiktok.com" in parsed.netloc.lower() and "/photo/" in parsed.path.lower()


def fetch_html(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="ignore")


def resolve_tiktok_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "tiktok.com" not in host:
        return url

    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        return response.geturl() or url


def collect_photo_urls_from_obj(obj: object, found: list[str], seen: set[str]) -> None:
    if isinstance(obj, dict):
        for value in obj.values():
            collect_photo_urls_from_obj(value, found, seen)
        return

    if isinstance(obj, list):
        for value in obj:
            collect_photo_urls_from_obj(value, found, seen)
        return

    if isinstance(obj, str) and "muscdn.com" in obj and "~noop.webp" in obj:
        normalized = obj.replace("http://", "https://")
        if normalized not in seen:
            seen.add(normalized)
            found.append(normalized)


def collect_audio_urls_from_obj(obj: object, found: list[str], seen: set[str]) -> None:
    if isinstance(obj, dict):
        for value in obj.values():
            collect_audio_urls_from_obj(value, found, seen)
        return

    if isinstance(obj, list):
        for value in obj:
            collect_audio_urls_from_obj(value, found, seen)
        return

    if not isinstance(obj, str):
        return

    lowered = obj.lower()
    if "muscdn.com" not in lowered:
        return

    if not any(ext in lowered for ext in (".mp3", ".m4a", ".aac", ".wav")):
        return

    normalized = obj.replace("http://", "https://")
    if normalized not in seen:
        seen.add(normalized)
        found.append(normalized)


def download_binary_file(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=12) as response, destination.open("wb") as output_file:
        shutil.copyfileobj(response, output_file)


def download_photo_gallery(photo_urls: list[str]) -> tuple[list[Path], Path]:
    temp_dir = Path(tempfile.mkdtemp(prefix="tiktok_photo_gallery_"))
    image_paths: list[Path] = []
    for index, photo_url in enumerate(photo_urls[:PHOTO_GALLERY_MAX_IMAGES], start=1):
        image_path = temp_dir / f"photo_{index:03d}.webp"
        download_binary_file(photo_url, image_path)
        image_paths.append(image_path)
    return image_paths, temp_dir


def download_single_photo(photo_url: str) -> tuple[Path, Path]:
    temp_dir = Path(tempfile.mkdtemp(prefix="tiktok_photo_preview_"))
    image_path = temp_dir / "preview.webp"
    download_binary_file(photo_url, image_path)
    return image_path, temp_dir


def build_photo_video(
    photo_urls: list[str],
    audio_url: str | None,
    title: str,
    video_id: str,
) -> tuple[Path, Path, bool]:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg no está disponible para convertir la publicación de fotos en video.")

    temp_dir = Path(tempfile.mkdtemp(prefix="tiktok_photo_"))
    image_paths: list[Path] = []
    for index, photo_url in enumerate(photo_urls, start=1):
        image_path = temp_dir / f"frame_{index:03d}.webp"
        download_binary_file(photo_url, image_path)
        image_paths.append(image_path)

    audio_path: Path | None = None
    has_audio = False
    if audio_url:
        try:
            audio_path = temp_dir / "audio_track.m4a"
            download_binary_file(audio_url, audio_path)
            has_audio = True
        except Exception:
            logger.warning("No se pudo descargar el audio del post de fotos, se enviará sin sonido.")
            audio_path = None

    output_path = temp_dir / "photo_post.mp4"
    safe_name = sanitize_filename(title)
    saved_file = DOWNLOADS_DIR / f"{safe_name}_{video_id}.mp4"

    command = ["ffmpeg", "-y"]
    per_image_duration = 1.8
    for image_path in image_paths:
        command.extend(["-loop", "1", "-t", str(per_image_duration), "-i", str(image_path)])

    filter_parts = []
    concat_inputs = []
    for index in range(len(image_paths)):
        filter_parts.append(
            f"[{index}:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps=30,format=yuv420p[v{index}]"
        )
        concat_inputs.append(f"[v{index}]")

    filter_complex = ";".join(filter_parts + [f"{''.join(concat_inputs)}concat=n={len(image_paths)}:v=1:a=0[vout]"])
    command.extend(["-filter_complex", filter_complex, "-map", "[vout]"])

    if audio_path:
        command.extend(["-i", str(audio_path), "-map", f"{len(image_paths)}:a", "-shortest"])

    command.extend(
        [
            "-movflags",
            "+faststart",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            str(output_path),
        ]
    )

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"No se pudo convertir la publicación de fotos en video: {result.stderr.strip()[:300]}")

    shutil.copy2(output_path, saved_file)
    return output_path, saved_file, has_audio


def extract_tiktok_photo_post(url: str) -> tuple[list[str], str, str | None, str]:
    html = fetch_html(url)
    script_match = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">(.*?)</script>',
        html,
    )
    if not script_match:
        raise RuntimeError("No se pudo leer el contenido de la publicación de fotos.")

    data = json.loads(script_match.group(1))
    root = data.get("__DEFAULT_SCOPE__", {})
    photo_urls: list[str] = []
    collect_photo_urls_from_obj(root, photo_urls, set())
    audio_urls: list[str] = []
    collect_audio_urls_from_obj(root, audio_urls, set())

    if not photo_urls:
        raise RuntimeError("No encontré fotos disponibles en esa publicación.")

    title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = "Post de fotos de TikTok"
    if title_match:
        title = re.sub(r"\s+", " ", title_match.group(1)).strip()[:1000] or title

    if title.lower() == "tiktok - make your day":
        url_match = re.search(r"tiktok\.com/@([^/]+)/photo/(\d+)", url, re.IGNORECASE)
        if url_match:
            username = url_match.group(1)
            title = f"Publicacion de fotos de @{username}"
            video_id = url_match.group(2)
        else:
            video_id = str(int(time.time()))
    else:
        url_match = re.search(r"/photo/(\d+)", url, re.IGNORECASE)
        video_id = url_match.group(1) if url_match else str(int(time.time()))

    audio_url = audio_urls[0] if audio_urls else None
    return photo_urls, title, audio_url, video_id


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


def get_cached_file(url: str) -> tuple[Path, str, bool | None] | None:
    cached_entry = cache_index.get(url)
    if not cached_entry:
        return None

    saved_path = Path(cached_entry["saved_file"])
    if not saved_path.exists():
        cache_index.pop(url, None)
        save_cache_index()
        return None

    title = str(cached_entry.get("title", saved_path.stem))
    has_audio = cached_entry.get("has_audio")
    if not isinstance(has_audio, bool):
        has_audio = None
    return saved_path, title, has_audio


def update_cache(url: str, title: str, saved_file: Path, has_audio: bool | None = None) -> None:
    cache_index[url] = {
        "title": title,
        "saved_file": str(saved_file),
        "updated_at": str(int(time.time())),
    }
    if has_audio is not None:
        cache_index[url]["has_audio"] = has_audio
    save_cache_index()


def run_photo_worker(url: str) -> dict[str, object]:
    worker_path = BASE_DIR / "photo_worker.py"
    if not worker_path.exists():
        raise RuntimeError("No se encontró photo_worker.py en el proyecto.")

    result = subprocess.run(
        [sys.executable, str(worker_path), url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(BASE_DIR),
        timeout=PHOTO_WORKER_TIMEOUT,
    )

    payload = (result.stdout or result.stderr or "").strip()
    if not payload:
        raise RuntimeError("El photo worker no devolvió salida.")

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"El photo worker devolvió una respuesta inválida: {payload[:300]}") from exc

    if data.get("status") != "ok":
        raise RuntimeError(str(data.get("error", "El photo worker no pudo procesar la publicación.")))

    return data


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
    if "unsupported url" in message and "/photo/" in message:
        return "Ese enlace corresponde a una publicación de fotos de TikTok. Detecté el carrusel, pero no pude convertirlo o entregarlo correctamente."
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


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


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
        [["Nueva entrega", "Asistencia"], ["Estado del servicio", "Centro"]],
        resize_keyboard=True,
        input_field_placeholder="Pega un link o usa un botón",
    )


def build_inline_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Nueva entrega", callback_data="menu_download"),
            InlineKeyboardButton("Asistencia", callback_data="menu_help"),
        ],
        [
            InlineKeyboardButton("Estado del servicio", callback_data="menu_status"),
            InlineKeyboardButton("Centro", callback_data="menu_home"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("Panel ejecutivo", callback_data="menu_admin")])
    return InlineKeyboardMarkup(rows)


def build_post_download_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Nueva entrega", callback_data="menu_download"),
                InlineKeyboardButton("Estado del servicio", callback_data="menu_status"),
            ],
            [
                InlineKeyboardButton("Asistencia", callback_data="menu_help"),
                InlineKeyboardButton("Centro", callback_data="menu_home"),
            ],
        ]
    )


def is_admin_user(application: Application, user_id: int | None) -> bool:
    if user_id is None:
        return False
    admin_ids = application.bot_data.get("admin_user_ids", set())
    return user_id in admin_ids


def build_welcome_text(first_name: str | None) -> str:
    greeting_name = first_name or "bro"
    return (
        f"━━━━ <b>TikSaveBot</b> ━━━━\n"
        f"Bienvenido, <b>{greeting_name}</b>.\n\n"
        f"Tu asistente para descargar videos de <b>TikTok</b> e <b>Instagram Reels</b> con una experiencia rápida y cuidada.\n\n"
        f"<b>Servicios disponibles</b>\n"
        f"• Descarga de video optimizada\n"
        f"• Conversion de publicaciones photo de TikTok a video\n"
        f"• Cache inteligente para enlaces repetidos\n"
        f"• Reintento automático ante fallos temporales\n"
        f"• Entrega como video o archivo según disponibilidad"
    )


def build_help_text() -> str:
    return (
        "━━━━ <b>Asistencia</b> ━━━━\n\n"
        "1. Selecciona <b>Descargar</b> o pega tu enlace directo.\n"
        "2. Espera unos segundos mientras proceso tu solicitud.\n"
        "3. Recibe el resultado en este mismo chat.\n\n"
        "Tambien puedo convertir publicaciones <b>photo</b> de TikTok en video para una entrega mas comoda.\n\n"
        "<b>Comandos principales</b>\n"
        "/start\n"
        "/help\n"
        "/descargar LINK\n"
        "/estado\n"
        "/menu"
    )


def build_status_text() -> str:
    uptime = format_uptime(int(time.time() - stats["started_at"]))
    ffmpeg_status = "disponible" if ffmpeg_available() else "no disponible"
    return (
        "━━━━ <b>Estado del servicio</b> ━━━━\n\n"
        f"• Disponibilidad: <b>online</b>\n"
        f"• Tiempo activo: <b>{uptime}</b>\n"
        f"• FFmpeg: <b>{ffmpeg_status}</b>\n"
        f"• Solicitudes procesadas: <b>{stats['total_requests']}</b>\n"
        f"• Entregas exitosas: <b>{stats['successful_downloads']}</b>\n"
        f"• Cache hits: <b>{stats['cache_hits']}</b>"
    )


def build_success_text(platform_name: str, source_label: str, saved_file: Path, file_size: str) -> str:
    return (
        f"━━━━ <b>Entrega completada</b> ━━━━\n\n"
        f"• Plataforma: <b>{platform_name}</b>\n"
        f"• Origen: <b>{source_label}</b>\n"
        f"• Tamaño: <b>{file_size}</b>\n"
        f"• Estado: <b>entregado</b>\n"
        f"• Archivo:\n<code>{saved_file}</code>"
    )


def build_document_success_text(platform_name: str, source_label: str, saved_file: Path, file_size: str) -> str:
    return (
        f"━━━━ <b>Entrega completada</b> ━━━━\n\n"
        f"• Plataforma: <b>{platform_name}</b>\n"
        f"• Origen: <b>{source_label}</b>\n"
        f"• Tamaño: <b>{file_size}</b>\n"
        f"• Estado: <b>enviado como archivo</b>\n"
        f"Telegram no aceptó el MP4 como video, por lo que fue enviado como archivo.\n"
        f"• Archivo:\n<code>{saved_file}</code>"
    )


def describe_photo_delivery_source(source_label: str, has_audio: bool | None) -> str:
    if has_audio is True:
        return f"{source_label} con audio"
    if has_audio is False:
        return f"{source_label} sin audio"
    return f"{source_label} con audio no disponible"


def infer_media_kind(path: Path, explicit_kind: object = None) -> str:
    if isinstance(explicit_kind, str) and explicit_kind in {"video", "image"}:
        return explicit_kind
    return "video" if path.suffix.lower() == ".mp4" else "image"


def build_progress_text(platform_name: str, stage: str, source_label: str | None = None) -> str:
    steps = {
        "analyzing": (
            "◇",
            "Analizando enlace",
            "● ○ ○",
            "Revisando la solicitud y detectando la plataforma..."
        ),
        "downloading": (
            "◇",
            "Preparando descarga",
            "● ● ○",
            "Obteniendo la mejor versión disponible del archivo..."
        ),
        "cached": (
            "◇",
            "Recuperando desde cache",
            "● ● ○",
            "Se encontró una copia local disponible para entrega inmediata..."
        ),
        "sending": (
            "◇",
            "Entregando archivo",
            "● ● ●",
            "Finalizando el proceso y enviando el resultado al chat..."
        ),
    }
    icon, title, bar, subtitle = steps[stage]
    extra = f"\nFuente detectada: <b>{source_label}</b>" if source_label else ""
    return (
        f"━━━━ <b>Entrega en proceso</b> ━━━━\n\n"
        f"{icon} <b>{platform_name} detectado</b>\n"
        f"<b>{title}</b>\n"
        f"<code>{bar}</code>\n"
        f"{subtitle}{extra}"
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
        "Centro de control:",
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
            f"◆ <b>Límite temporal alcanzado</b>\n\nEspera <b>{retry_after}</b> segundos antes de realizar una nueva solicitud.",
            parse_mode="HTML",
        )
        return

    url = extract_url(text)
    if not url:
        await update.message.reply_text(
            "◆ <b>Enlace no detectado</b>\n\nPega un enlace directo de TikTok o Instagram Reel para continuar.",
            parse_mode="HTML",
            reply_markup=build_inline_menu(is_admin_user(context.application, update.effective_user.id if update.effective_user else None)),
        )
        return

    if not is_supported_url(url):
        await update.message.reply_text(
            "◆ <b>Formato no compatible</b>\n\nActualmente este bot acepta enlaces de <b>TikTok</b> e <b>Instagram Reels</b>.",
            parse_mode="HTML",
            reply_markup=build_inline_menu(is_admin_user(context.application, update.effective_user.id if update.effective_user else None)),
        )
        return

    original_url = url
    try:
        url = await asyncio.to_thread(resolve_tiktok_url, url)
    except Exception:
        logger.warning("No se pudo resolver el enlace original, se usará la URL recibida: %s", original_url)
        url = original_url

    if is_tiktok_photo_url(url):
        await process_tiktok_photo_post(update, context, url, user_id)
        return

    platform_name = detect_platform(url)

    stats["total_requests"] += 1
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)
    status = await update.message.reply_text(
        build_progress_text(platform_name, "analyzing"),
        parse_mode="HTML",
    )

    cached_result = get_cached_file(url)
    source_label = "descarga nueva"
    try:
        if cached_result:
            saved_file, title, _ = cached_result
            file_path = saved_file
            stats["cache_hits"] += 1
            source_label = "cache"
            await status.edit_text(
                build_progress_text(platform_name, "cached", source_label),
                parse_mode="HTML",
            )
        else:
            await status.edit_text(
                build_progress_text(platform_name, "downloading", source_label),
                parse_mode="HTML",
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
        await status.edit_text(
            f"◆ <b>No fue posible completar la descarga</b>\n\n{classify_download_error(exc)}",
            parse_mode="HTML",
            reply_markup=build_post_download_menu(),
        )
        return

    file_size = format_file_size(file_path.stat().st_size)
    await status.edit_text(
        build_progress_text(platform_name, "sending", source_label),
        parse_mode="HTML",
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
            build_success_text(platform_name, source_label, saved_file, file_size),
            parse_mode="HTML",
            reply_markup=build_post_download_menu(),
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
                build_document_success_text(platform_name, source_label, saved_file, file_size),
                parse_mode="HTML",
                reply_markup=build_post_download_menu(),
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
                f"◆ <b>Descarga completada, entrega pendiente</b>\n\n"
                f"El archivo fue guardado correctamente en:\n<code>{saved_file}</code>\n"
                f"Tamaño del archivo: <b>{file_size}</b>\n\n"
                "Telegram rechazó el envío. Revisa los logs de Railway para confirmar si fue un límite de tamaño, timeout o un fallo temporal.",
                parse_mode="HTML",
                reply_markup=build_post_download_menu(),
            )
    finally:
        if file_path != saved_file:
            try:
                file_path.unlink(missing_ok=True)
                shutil.rmtree(file_path.parent, ignore_errors=True)
            except OSError:
                logger.warning("No se pudo limpiar el archivo temporal %s", file_path)


async def process_tiktok_photo_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    user_id: int,
) -> None:
    if not update.message:
        return

    platform_name = "TikTok Photo"
    stats["total_requests"] += 1
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)
    status = await update.message.reply_text(
        build_progress_text(platform_name, "analyzing"),
        parse_mode="HTML",
    )
    cached_result = get_cached_file(url)
    source_label = "photo worker"

    try:
        if cached_result:
            saved_file, title, has_audio = cached_result
            file_path = saved_file
            media_kind = infer_media_kind(saved_file)
            source_label = "cache"
            stats["cache_hits"] += 1
            await status.edit_text(
                build_progress_text(platform_name, "cached", source_label),
                parse_mode="HTML",
            )
        else:
            await status.edit_text(
                build_progress_text(platform_name, "downloading", "photo worker"),
                parse_mode="HTML",
            )
            worker_result = await asyncio.to_thread(run_photo_worker, url)
            saved_file = Path(str(worker_result["saved_file"]))
            title = str(worker_result.get("title") or saved_file.stem)
            has_audio = bool(worker_result.get("has_audio"))
            media_kind = infer_media_kind(saved_file, worker_result.get("media_kind"))
            file_path = saved_file
            update_cache(url, title, saved_file, has_audio)
    except Exception as exc:
        logger.exception("No se pudo procesar la publicación photo con el worker")
        stats["failed_downloads"] += 1
        write_history(
            {
                "timestamp": int(time.time()),
                "user_id": user_id,
                "url": url,
                "status": "photo_worker_failed",
                "error": str(exc),
            }
        )
        await status.edit_text(
            "◆ <b>No fue posible completar la publicacion de fotos</b>\n\n"
            f"{html.escape(str(exc))}",
            parse_mode="HTML",
            reply_markup=build_post_download_menu(),
        )
        return

    await status.edit_text(
        build_progress_text(platform_name, "sending", source_label),
        parse_mode="HTML",
    )

    try:
        file_size = format_file_size(file_path.stat().st_size)
        if media_kind == "video":
            with file_path.open("rb") as video_file:
                await update.message.reply_video(
                    video=video_file,
                    caption=title[:1000],
                    supports_streaming=True,
                )
        else:
            with file_path.open("rb") as image_file:
                await update.message.reply_photo(
                    photo=image_file,
                    caption=title[:1000],
                )
        stats["successful_downloads"] += 1
        write_history(
            {
                "timestamp": int(time.time()),
                "user_id": user_id,
                "url": url,
                "status": "sent_photo_video" if media_kind == "video" else "sent_photo_image",
                "source": source_label,
                "saved_file": str(saved_file),
                "file_size": file_path.stat().st_size,
                "has_audio": has_audio,
            }
        )
        await status.edit_text(
            build_success_text(
                platform_name,
                describe_photo_delivery_source(source_label, has_audio)
                if media_kind == "video"
                else f"{source_label} en imagen",
                saved_file,
                file_size,
            ),
            parse_mode="HTML",
            reply_markup=build_post_download_menu(),
        )
    except Exception as exc:
        logger.exception("No se pudo enviar la publicación photo como video")
        try:
            file_size = format_file_size(file_path.stat().st_size)
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
                    "status": "sent_photo_document" if media_kind == "video" else "sent_photo_image_document",
                    "source": source_label,
                    "saved_file": str(saved_file),
                    "file_size": file_path.stat().st_size,
                    "has_audio": has_audio,
                }
            )
            await status.edit_text(
                build_document_success_text(
                    platform_name,
                    describe_photo_delivery_source(source_label, has_audio)
                    if media_kind == "video"
                    else f"{source_label} en imagen",
                    saved_file,
                    file_size,
                ),
                parse_mode="HTML",
                reply_markup=build_post_download_menu(),
            )
        except Exception:
            stats["failed_downloads"] += 1
            write_history(
                {
                    "timestamp": int(time.time()),
                    "user_id": user_id,
                    "url": url,
                    "status": "photo_send_failed",
                    "error": str(exc),
                }
            )
            await status.edit_text(
                "◆ <b>La publicacion photo fue procesada, pero no pude entregarla</b>\n\n"
                "Telegram rechazó el archivo generado por el worker.",
                parse_mode="HTML",
                reply_markup=build_post_download_menu(),
            )


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
        "━━━━ <b>Resumen del servicio</b> ━━━━\n\n"
        f"• Tiempo activo: <b>{uptime}</b>\n"
        f"• Solicitudes totales: <b>{stats['total_requests']}</b>\n"
        f"• Entregas exitosas: <b>{stats['successful_downloads']}</b>\n"
        f"• Entregas fallidas: <b>{stats['failed_downloads']}</b>\n"
        f"• Cache hits: <b>{stats['cache_hits']}</b>\n"
        f"• Usuarios en memoria: <b>{active_users}</b>",
        parse_mode="HTML",
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
        await update.message.reply_text("━━━━ <b>Actividad reciente</b> ━━━━\n\nAún no hay historial disponible.", parse_mode="HTML")
        return

    lines = ["━━━━ <b>Actividad reciente</b> ━━━━", ""]
    for entry in reversed(entries):
        lines.append(
            f"• <b>{format_timestamp(entry.get('timestamp'))}</b>\n"
            f"Estado: {entry.get('status')}\n"
            f"Origen: {entry.get('source', 'sin fuente')}\n"
            f"URL: {entry.get('url', 'sin url')}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


async def errors_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    admin_ids = context.application.bot_data.get("admin_user_ids", set())
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("Ese comando es solo para admin.")
        return

    entries = read_history(limit=5, statuses={"download_failed", "send_failed"})
    if not entries:
        await update.message.reply_text("━━━━ <b>Incidencias</b> ━━━━\n\nNo hay incidencias recientes en el historial.", parse_mode="HTML")
        return

    lines = ["━━━━ <b>Incidencias</b> ━━━━", ""]
    for entry in reversed(entries):
        error_text = str(entry.get("error", "sin detalle"))
        lines.append(
            f"• <b>{format_timestamp(entry.get('timestamp'))}</b>\n"
            f"Tipo: {entry.get('status')}\n"
            f"Detalle: {error_text[:120]}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    admin_ids = context.application.bot_data.get("admin_user_ids", set())
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("Ese comando es solo para admin.")
        return

    entries = read_history(limit=200)
    if not entries:
        await update.message.reply_text("━━━━ <b>Rendimiento</b> ━━━━\n\nAún no hay suficiente historial para generar métricas.", parse_mode="HTML")
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

    lines = ["━━━━ <b>Rendimiento</b> ━━━━", ""]
    if top_urls:
        lines.append("<b>Enlaces más recurrentes</b>")
        for url, count in top_urls:
            lines.append(f"• <b>{count}x</b> | {url}")

    if top_users:
        lines.append("<b>Usuarios con mayor actividad</b>")
        for user_id, count in top_users:
            lines.append(f"• <b>{count}</b> solicitudes | user_id {user_id}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "━━━━ <b>Centro de control</b> ━━━━",
        parse_mode="HTML",
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
            "Comparte tu enlace de TikTok o Instagram Reel para iniciar una nueva entrega.",
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
            "━━━━ <b>Panel ejecutivo</b> ━━━━\n\n"
            "• <b>Resumen del servicio</b>: /stats\n"
            "• <b>Actividad reciente</b>: /last\n"
            "• <b>Incidencias</b>: /errors\n"
            "• <b>Rendimiento</b>: /top",
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
        await update.message.reply_text(
            "━━━━ <b>Nueva entrega</b> ━━━━\n\nComparte tu enlace de TikTok o Instagram Reel para comenzar.",
            parse_mode="HTML",
            reply_markup=build_main_keyboard(),
        )
        return
    if lowered == "nueva entrega":
        await update.message.reply_text(
            "━━━━ <b>Nueva entrega</b> ━━━━\n\nComparte tu enlace de TikTok o Instagram Reel para comenzar.",
            parse_mode="HTML",
            reply_markup=build_main_keyboard(),
        )
        return
    if lowered == "ayuda":
        await help_command(update, context)
        return
    if lowered == "asistencia":
        await help_command(update, context)
        return
    if lowered == "estado":
        await status_command(update, context)
        return
    if lowered == "estado del servicio":
        await status_command(update, context)
        return
    if lowered == "menu":
        await menu_command(update, context)
        return
    if lowered == "centro":
        await menu_command(update, context)
        return

    await process_download(update, context, update.message.text)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Inicia TikSaveBot"),
            BotCommand("help", "Asistencia"),
            BotCommand("descargar", "Nueva entrega"),
            BotCommand("estado", "Estado del servicio"),
            BotCommand("menu", "Centro de control"),
            BotCommand("stats", "Resumen del servicio"),
            BotCommand("last", "Actividad reciente"),
            BotCommand("errors", "Incidencias"),
            BotCommand("top", "Rendimiento"),
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
