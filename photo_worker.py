import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
PHOTO_WORKER_DIR = BASE_DIR / "photo_worker_output"
TOBY_LAB_DIR = BASE_DIR / "toby_lab"
TOBY_FETCH_SCRIPT = TOBY_LAB_DIR / "fetch_photo_metadata.js"
USER_AGENT = "Mozilla/5.0"


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or "tiktok_photo_post"


def ensure_worker_dependencies() -> None:
    if not TOBY_LAB_DIR.exists():
        raise RuntimeError("No existe la carpeta toby_lab. Ejecuta primero la instalacion del laboratorio.")
    if not TOBY_FETCH_SCRIPT.exists():
        raise RuntimeError("No existe fetch_photo_metadata.js en toby_lab.")
    if not (TOBY_LAB_DIR / "node_modules").exists():
        raise RuntimeError("Faltan dependencias en toby_lab. Ejecuta npm install dentro de esa carpeta.")


def fetch_photo_metadata(url: str) -> dict[str, object]:
    ensure_worker_dependencies()
    result = subprocess.run(
        ["node", str(TOBY_FETCH_SCRIPT), url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(TOBY_LAB_DIR),
    )

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    payload = stdout or stderr
    if not payload:
        raise RuntimeError("El worker Node no devolvio salida.")

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"El worker Node devolvio un JSON invalido: {payload[:300]}") from exc

    if data.get("status") != "success":
        raise RuntimeError(str(data.get("error", "No se pudo obtener metadata del post photo.")))

    result_data = data.get("result")
    if not isinstance(result_data, dict):
        raise RuntimeError("La respuesta del worker Node no trae un objeto result valido.")

    if result_data.get("type") != "image":
        raise RuntimeError("La URL no fue identificada como publicacion photo.")

    return result_data


def download_binary_file(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=20) as response, destination.open("wb") as output_file:
        shutil.copyfileobj(response, output_file)


def normalize_audio_url(metadata: dict[str, object]) -> str | None:
    music = metadata.get("music")
    if not isinstance(music, dict):
        return None

    play_urls = music.get("playUrl")
    if isinstance(play_urls, list):
        for item in play_urls:
            if isinstance(item, str) and item.startswith("http"):
                return item
    if isinstance(play_urls, str) and play_urls.startswith("http"):
        return play_urls
    return None


def build_photo_video(metadata: dict[str, object]) -> dict[str, object]:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg no esta disponible en este entorno.")

    photo_urls = metadata.get("images")
    if not isinstance(photo_urls, list) or not photo_urls:
        raise RuntimeError("La metadata no trae imagenes utilizables.")

    video_id = str(metadata.get("id") or "photo_post")
    title = str(metadata.get("desc") or f"Post de fotos {video_id}")
    audio_url = normalize_audio_url(metadata)

    PHOTO_WORKER_DIR.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="photo_worker_"))
    image_paths: list[Path] = []

    try:
        for index, photo_url in enumerate(photo_urls, start=1):
            image_path = temp_dir / f"frame_{index:03d}.webp"
            download_binary_file(str(photo_url), image_path)
            image_paths.append(image_path)

        audio_path: Path | None = None
        has_audio = False
        if audio_url:
            try:
                audio_path = temp_dir / "audio_track.mp3"
                download_binary_file(audio_url, audio_path)
                has_audio = True
            except Exception:
                audio_path = None

        output_path = temp_dir / "photo_post.mp4"
        saved_file = PHOTO_WORKER_DIR / f"{sanitize_filename(title)}_{video_id}.mp4"

        command = ["ffmpeg", "-y"]
        per_image_duration = 2.0
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
        command.extend(["-filter_complex", filter_complex])

        if audio_path:
            command.extend(["-i", str(audio_path)])

        command.extend(
            [
                "-map",
                "[vout]",
            ]
        )

        if audio_path:
            command.extend(
                [
                    "-map",
                    f"{len(image_paths)}:a",
                    "-shortest",
                ]
            )

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

        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[:500] or "FFmpeg devolvio un error.")

        shutil.copy2(output_path, saved_file)
        return {
            "status": "ok",
            "title": title,
            "photo_count": len(image_paths),
            "has_audio": has_audio,
            "saved_file": str(saved_file),
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_photo_job(url: str) -> dict[str, object]:
    metadata = fetch_photo_metadata(url)
    return build_photo_video(metadata)


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"status": "error", "error": "Uso: python photo_worker.py <url>"}))
        return 1

    url = sys.argv[1].strip()
    try:
        result = run_photo_job(url)
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
