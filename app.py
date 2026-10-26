
import os
import re
import uuid
import shutil
import tempfile
import threading
import subprocess
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, request, jsonify, Response, send_file

app = Flask(__name__)

ALLOWED_ORIGIN = "https://techbynehra22.blogspot.com"
DOWNLOADS = {}
DOWNLOADS_LOCK = threading.Lock()


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin == ALLOWED_ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Max-Age"] = "86400"
        response.headers["Vary"] = "Origin"
    return response


def is_valid_instagram_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in ("http", "https")
            and host in {
                "instagram.com",
                "www.instagram.com",
                "m.instagram.com",
            }
            and parsed.path.startswith(("/reel/", "/reels/", "/p/", "/tv/"))
        )
    except Exception:
        return False


def has_audio_stream(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not os.path.isfile(path):
        return False

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_type",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        return result.returncode == 0 and "audio" in result.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def get_video_files(temp_dir):
    files = []

    for name in os.listdir(temp_dir):
        path = os.path.join(temp_dir, name)

        if not os.path.isfile(path):
            continue

        if name.endswith((".part", ".ytdl", ".json")):
            continue

        if os.path.getsize(path) > 0:
            files.append(path)

    return files


def download_with_options(url, temp_dir, format_selector):
    output_template = os.path.join(temp_dir, "%(id)s.%(ext)s")

    ydl_opts = {
        "quiet": False,
"no_warnings": False,
"verbose": True,
        "noplaylist": True,
        "format": format_selector,
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "ffmpeg_location": shutil.which("ffmpeg"),
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "socket_timeout": 30,
        "overwrites": True,
        "extractor_args": {
            "instagram": {
                "api": ["web"],
            }
        },
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if not info:
        raise RuntimeError("Instagram did not return video information.")

    candidates = get_video_files(temp_dir)

    if not candidates:
        raise RuntimeError("Downloaded video file was not found.")

    # Prefer a video file that actually contains an audio stream.
    audio_files = [
        path for path in candidates
        if has_audio_stream(path)
    ]

    if audio_files:
        final_path = max(audio_files, key=os.path.getsize)
        return info, final_path

    raise RuntimeError("Downloaded video has no detectable audio stream.")


@app.route("/")
def home():
    return """
    <!doctype html>
    <html>
    <head>
      <title>Instagram Reel Downloader API</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
    </head>
    <body style="font-family:Arial;text-align:center;padding:40px">
      <h2>Instagram Reel Downloader API</h2>
      <p>Service is running.</p>
      <a href="/health">Check API Health</a>
    </body>
    </html>
    """


@app.route("/health")
def health():
    return jsonify({
        "status": "online",
        "message": "Instagram Downloader API is running",
        "ffmpeg_available": bool(shutil.which("ffmpeg")),
        "ffprobe_available": bool(shutil.which("ffprobe")),
    })


@app.route("/download", methods=["OPTIONS"])
def download_options():
    return Response("", status=204)


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({
            "success": False,
            "error": "Please provide an Instagram URL.",
        }), 400

    if not is_valid_instagram_url(url):
        return jsonify({
            "success": False,
            "error": "Please enter a valid Instagram Reel or post URL.",
        }), 400

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return jsonify({
            "success": False,
            "error": "FFmpeg or FFprobe is unavailable. Check apt.txt and redeploy.",
        }), 503

    temp_dir = tempfile.mkdtemp(prefix="instagram_download_")

    # Try a format that prefers a video stream with audio first,
    # then separate video + audio, then a combined/best fallback.
    format_options = [
        "bv*+ba/b",
        "bestvideo+bestaudio/best",
        "best",
    ]

    last_error = None
    info = None
    final_path = None

    try:
        for index, format_selector in enumerate(format_options):
            # Use a clean directory for each attempt.
            for name in os.listdir(temp_dir):
                path = os.path.join(temp_dir, name)
                try:
                    if os.path.isfile(path) or os.path.islink(path):
                        os.remove(path)
                    elif os.path.isdir(path):
                        shutil.rmtree(path)
                except OSError:
                    app.logger.warning("Could not clear retry file: %s", path)

            try:
                app.logger.info(
                    "Instagram download attempt %s using format: %s",
                    index + 1,
                    format_selector,
                )

                info, final_path = download_with_options(
                    url,
                    temp_dir,
                    format_selector,
                )

                if final_path and has_audio_stream(final_path):
                    break

                last_error = RuntimeError(
                    "Downloaded file has no detectable audio stream."
                )
                final_path = None

            except Exception as exc:
                last_error = exc
                app.logger.warning(
                    "Download attempt %s failed: %s",
                    index + 1,
                    str(exc),
                )

        if not final_path or not os.path.isfile(final_path):
            raise RuntimeError(
                "All download formats failed. "
                + (str(last_error) if last_error else "No valid output file.")
            )

        if os.path.getsize(final_path) == 0:
            raise RuntimeError("Downloaded file is empty.")

        if not has_audio_stream(final_path):
            raise RuntimeError("Downloaded file has no detectable audio stream.")

        token = uuid.uuid4().hex
        title = (info or {}).get("title") or "instagram_reel"

        with DOWNLOADS_LOCK:
            DOWNLOADS[token] = {
                "path": final_path,
                "temp_dir": temp_dir,
                "title": title,
            }

        return jsonify({
            "success": True,
            "token": token,
            "title": title,
            "quality": "best",
            "audio_included": True,
            "download_url": f"/file/{token}",
        })

    except Exception as exc:
        app.logger.exception("Instagram download failed")

        shutil.rmtree(temp_dir, ignore_errors=True)

        error_text = str(exc).lower()

        if "no audio" in error_text or "audio stream" in error_text:
            message = (
                "Audio could not be found in the available Instagram streams. "
                "This Reel may not expose an audio stream to the downloader. "
                "Try another public Reel or retry later."
            )
        else:
            message = (
                "Could not download this Reel. Instagram may have restricted "
                "access, or the available formats may have changed. "
                "Please retry with another public Reel."
            )

        return jsonify({
            "success": False,
            "error": message,
        }), 502


@app.route("/file/<token>", methods=["GET"])
def get_file(token):
    with DOWNLOADS_LOCK:
        item = DOWNLOADS.get(token)

    if not item:
        return jsonify({
            "success": False,
            "error": "Download link expired or not found. Please try again.",
        }), 404

    path = item["path"]

    if not os.path.isfile(path):
        return jsonify({
            "success": False,
            "error": "The downloaded file is no longer available.",
        }), 404

    if not has_audio_stream(path):
        return jsonify({
            "success": False,
            "error": "Audio was not detected; the file was not sent.",
        }), 502

    filename = re.sub(
        r"[^A-Za-z0-9._-]",
        "_",
        item.get("title", "instagram_reel"),
    )[:100]

    if not filename.lower().endswith(".mp4"):
        filename += ".mp4"

    try:
        response = send_file(
            path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=filename,
            conditional=False,
            max_age=0,
        )

        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"

        def cleanup():
            with DOWNLOADS_LOCK:
                DOWNLOADS.pop(token, None)
            shutil.rmtree(item["temp_dir"], ignore_errors=True)

        response.call_on_close(cleanup)
        return response

    except Exception:
        app.logger.exception("File delivery failed")
        return jsonify({
            "success": False,
            "error": "Could not send the video. Please try again.",
        }), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
