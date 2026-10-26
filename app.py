```python
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


def has_stream(path, stream_type):
    ffprobe = shutil.which("ffprobe")

    if not ffprobe or not os.path.isfile(path):
        return False

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-select_streams", stream_type,
                "-show_entries", "stream=codec_type",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )

        expected = "video" if stream_type.startswith("v") else "audio"

        return (
            result.returncode == 0
            and expected in result.stdout.lower()
        )

    except (OSError, subprocess.SubprocessError):
        return False


def has_audio_stream(path):
    return has_stream(path, "a:0")


def has_video_stream(path):
    return has_stream(path, "v:0")


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

    # Only accept a file containing BOTH video and audio.
    # This prevents an audio-only file from being sent as a video.
    complete_files = [
        path for path in candidates
        if has_video_stream(path) and has_audio_stream(path)
    ]

    if complete_files:
        final_path = max(complete_files, key=os.path.getsize)
        return info, final_path

    # If separate video/audio files remain, yt-dlp/FFmpeg may not
    # have merged them successfully. Report the real issue in logs.
    app.logger.warning(
        "No merged file with both video and audio was found. Files: %s",
        [os.path.basename(p) for p in candidates],
    )

    raise RuntimeError(
        "No downloaded file contains both video and audio streams."
    )


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

    # Diagnostic logs: never log cookies or request headers.
    app.logger.warning("DOWNLOAD REQUEST RECEIVED")
    app.logger.warning(
        "Instagram URL received: %s",
        url.split("?")[0],
    )

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

    # Try alternate format selectors if the first attempt fails.
    format_options = [
        "bv*+ba/b",
        "bestvideo+bestaudio/best",
        "best",
    ]

    last_error = None
    info = None
    final_path = None

    try:
        for index, format_selector in enumerate(format_options, start=1):

            # Clear files left by a previous failed attempt.
            for name in os.listdir(temp_dir):
                path = os.path.join(temp_dir, name)

                try:
                    if os.path.isfile(path) or os.path.islink(path):
                        os.remove(path)
                    elif os.path.isdir(path):
                        shutil.rmtree(path)
                except OSError:
                    app.logger.warning(
                        "Could not clear retry file: %s",
                        path,
                    )

            try:
                app.logger.warning(
                    "Download attempt %s using format: %s",
                    index,
                    format_selector,
                )

                info, final_path = download_with_options(
                    url,
                    temp_dir,
                    format_selector,
                )

                if (
                    final_path
                    and os.path.isfile(final_path)
                    and has_video_stream(final_path)
                    and has_audio_stream(final_path)
                ):
                    break

                last_error = RuntimeError(
                    "Final file did not contain both video and audio."
                )
                final_path = None

            except Exception as exc:
                last_error = exc
                final_path = None

                app.logger.exception(
                    "Download attempt %s failed",
                    index,
                )

        if not final_path or not os.path.isfile(final_path):
            raise RuntimeError(
                "All format attempts failed. "
                + (
                    str(last_error)
                    if last_error
                    else "No valid output file was produced."
                )
            )

        if os.path.getsize(final_path) == 0:
            raise RuntimeError("Downloaded file is empty.")

        if not has_video_stream(final_path):
            raise RuntimeError("Downloaded file has no video stream.")

        if not has_audio_stream(final_path):
            raise RuntimeError("Downloaded file has no audio stream.")

        token = uuid.uuid4().hex
        title = (info or {}).get("title") or "instagram_reel"

        with DOWNLOADS_LOCK:
            DOWNLOADS[token] = {
                "path": final_path,
                "temp_dir": temp_dir,
                "title": title,
            }

        app.logger.warning(
            "Download successful; video and audio streams verified."
        )

        return jsonify({
            "success": True,
            "token": token,
            "title": title,
            "quality": "best",
            "audio_included": True,
            "download_url": f"/file/{token}",
        })

    except Exception:
        app.logger.exception("Instagram download failed")

        shutil.rmtree(temp_dir, ignore_errors=True)

        return jsonify({
            "success": False,
            "error": (
                "Could not download this Reel with audio. "
                "Check the latest Render logs for the actual format or "
                "FFmpeg error, then try again."
            ),
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

    if not has_video_stream(path) or not has_audio_stream(path):
        return jsonify({
            "success": False,
            "error": "The video or audio stream is missing; the file was not sent.",
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
```
