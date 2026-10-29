import os
import re
import uuid
import shutil
import tempfile
import threading
import subprocess
from urllib.parse import urlparse

import yt_dlp
import imageio_ffmpeg
from flask import Flask, request, jsonify, Response, send_file

app = Flask(__name__)

ALLOWED_ORIGIN = "https://techbynehra22.blogspot.com"

DOWNLOADS = {}
DOWNLOADS_LOCK = threading.Lock()


# =========================================================
# FFMPEG PATH HELPER (WORKS ON RENDER WITHOUT APT.TXT)
# =========================================================

def get_ffmpeg_path():
    # पहले सिस्टम में चेक करें, न मिले तो imageio_ffmpeg से लें
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


# =========================================================
# CORS
# =========================================================

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


# =========================================================
# INSTAGRAM URL VALIDATION
# =========================================================

def is_valid_instagram_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        allowed_hosts = {
            "instagram.com",
            "www.instagram.com",
            "m.instagram.com"
        }

        allowed_paths = (
            "/reel/",
            "/reels/",
            "/p/",
            "/tv/"
        )

        return (
            parsed.scheme in ("http", "https")
            and host in allowed_hosts
            and parsed.path.startswith(allowed_paths)
        )

    except Exception:
        return False


# =========================================================
# MEDIA STREAM CHECK USING FFMPEG (NO FFPROBE NEEDED)
# =========================================================

def get_media_streams_info(file_path):
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg or not os.path.isfile(file_path):
        return ""

    try:
        # ffmpeg -i फाइल की स्ट्रीम जानकारी stderr में देता है
        result = subprocess.run(
            [ffmpeg, "- hide_banner", "-i", file_path],
            capture_output=True,
            text=True,
            timeout=20,
            check=False
        )
        return result.stderr or ""
    except Exception as exc:
        app.logger.warning("FFmpeg inspect error: %s", str(exc))
        return ""


def has_audio_stream(file_path):
    info = get_media_streams_info(file_path)
    return "Audio:" in info


def has_video_stream(file_path):
    info = get_media_streams_info(file_path)
    return "Video:" in info


# =========================================================
# FIND DOWNLOADED MEDIA
# =========================================================

def get_downloaded_files(temp_dir):
    result = []
    if not os.path.isdir(temp_dir):
        return result

    for name in os.listdir(temp_dir):
        path = os.path.join(temp_dir, name)
        if not os.path.isfile(path):
            continue

        if name.endswith((".part", ".ytdl", ".json", ".temp")):
            continue

        try:
            if os.path.getsize(path) > 0:
                result.append(path)
        except OSError:
            pass

    return result


# =========================================================
# FFMPEG MERGE & FINALIZE
# =========================================================

def merge_video_audio(video_file, audio_file, temp_dir):
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is not available.")

    output_file = os.path.join(temp_dir, "instagram_merged.mp4")

    command = [
        ffmpeg, "-y",
        "-i", video_file,
        "-i", audio_file,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output_file
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
        check=False
    )

    if result.returncode != 0 or not os.path.isfile(output_file):
        raise RuntimeError("FFmpeg could not merge video and audio.")

    return output_file


def finalize_video(input_file, temp_dir):
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return input_file

    output_file = os.path.join(temp_dir, "instagram_final.mp4")

    command = [
        ffmpeg, "-y",
        "-i", input_file,
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output_file
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
        check=False
    )

    if result.returncode == 0 and os.path.isfile(output_file) and os.path.getsize(output_file) > 0:
        return output_file

    return input_file


# =========================================================
# YT-DLP DOWNLOAD (FIXED FOR AUDIO)
# =========================================================

def download_instagram(url, temp_dir):
    output_template = os.path.join(temp_dir, "%(id)s.%(ext)s")
    ffmpeg_path = get_ffmpeg_path()

    if not ffmpeg_path:
        raise RuntimeError("FFmpeg binary is not available on server.")

    # बदलाव: सबसे पहले Video+Audio मर्ज करेगा या वही MP4 लेगा जिसमें ऑडियो मौजूद हो
    format_options = [
        "bv*+ba/best[ext=mp4][acodec!=none][vcodec!=none]/best[acodec!=none]",
        "bestvideo+bestaudio/best",
        "best[ext=mp4][acodec!=none]/best"
    ]

    last_error = None

    for attempt_number, format_selector in enumerate(format_options, start=1):
        # पुरानी फाइलें साफ करें
        for name in os.listdir(temp_dir):
            path = os.path.join(temp_dir, name)
            try:
                if os.path.isfile(path) or os.path.islink(path):
                    os.remove(path)
                elif os.path.isdir(path):
                    shutil.rmtree(path)
            except OSError:
                pass

        ydl_opts = {
            "quiet": False,
            "no_warnings": False,
            "noplaylist": True,
            "format": format_selector,
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "ffmpeg_location": ffmpeg_path,
            "retries": 5,
            "fragment_retries": 5,
            "extractor_retries": 3,
            "socket_timeout": 45,
            "overwrites": True,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9"
            }
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

            if not info:
                raise RuntimeError("Instagram did not return media information.")

            files = get_downloaded_files(temp_dir)
            if not files:
                raise RuntimeError("No downloaded media file was found.")

            # चेक करें कि किस फाइल में ऑडियो और वीडियो दोनों हैं
            for file_path in sorted(files, key=os.path.getsize, reverse=True):
                if has_video_stream(file_path) and has_audio_stream(file_path):
                    final_file = finalize_video(file_path, temp_dir)
                    return info, final_file

            # अगर अलग-अलग फाइलें डाउनलोड हुई हैं तो उन्हें मर्ज करें
            video_files = [p for p in files if has_video_stream(p)]
            audio_files = [p for p in files if has_audio_stream(p)]

            if video_files and audio_files:
                video_file = max(video_files, key=os.path.getsize)
                audio_file = max(audio_files, key=os.path.getsize)
                merged_file = merge_video_audio(video_file, audio_file, temp_dir)
                return info, merged_file

            raise RuntimeError("Downloaded stream did not contain an audio track.")

        except Exception as exc:
            last_error = exc
            app.logger.warning("Attempt %s failed: %s", attempt_number, str(exc))
            continue

    raise RuntimeError("All download attempts failed: " + str(last_error))


# =========================================================
# ROUTES (HOME, HEALTH, DOWNLOAD, FILE)
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return """
    <!doctype html>
    <html>
    <head>
        <title>Instagram Reel Downloader API</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
    </head>
    <body style="font-family:Arial; text-align:center; padding:40px">
        <h2>Instagram Reel Downloader API</h2>
        <p>Service is running with Audio Merge Support.</p>
        <p><a href="/health">Check API Health</a></p>
    </body>
    </html>
    """


@app.route("/health", methods=["GET"])
def health():
    ffmpeg_bin = get_ffmpeg_path()
    return jsonify({
        "status": "online",
        "message": "Instagram Downloader API is running",
        "ffmpeg_available": bool(ffmpeg_bin),
        "ffmpeg_path": ffmpeg_bin
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
            "error": "Please provide an Instagram URL."
        }), 400

    if not is_valid_instagram_url(url):
        return jsonify({
            "success": False,
            "error": "Please enter a valid Instagram Reel URL."
        }), 400

    if not get_ffmpeg_path():
        return jsonify({
            "success": False,
            "error": "FFmpeg is not available on Render."
        }), 503

    temp_dir = tempfile.mkdtemp(prefix="instagram_download_")

    try:
        info, final_path = download_instagram(url, temp_dir)

        if not final_path or not os.path.isfile(final_path):
            raise RuntimeError("Final video file does not exist.")

        file_size = os.path.getsize(final_path)
        if file_size <= 0:
            raise RuntimeError("Downloaded file is empty.")

        token = uuid.uuid4().hex
        title = info.get("title") or info.get("id") or "instagram_reel"

        with DOWNLOADS_LOCK:
            DOWNLOADS[token] = {
                "path": final_path,
                "temp_dir": temp_dir,
                "title": title
            }

        return jsonify({
            "success": True,
            "token": token,
            "title": title,
            "quality": "best",
            "audio_included": True,
            "download_url": "/file/" + token
        })

    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({
            "success": False,
            "error": "Could not download this Reel. " + str(exc)
        }), 502


@app.route("/file/<token>", methods=["GET"])
def get_file(token):
    with DOWNLOADS_LOCK:
        item = DOWNLOADS.get(token)

    if not item:
        return jsonify({
            "success": False,
            "error": "Download link expired or not found. Please try again."
        }), 404

    path = item["path"]
    if not os.path.isfile(path):
        return jsonify({
            "success": False,
            "error": "The downloaded file is no longer available."
        }), 404

    filename = re.sub(
        r"[^A-Za-z0-9._-]",
        "_",
        item.get("title", "instagram_reel")
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
            max_age=0
        )

        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"

        def cleanup():
            with DOWNLOADS_LOCK:
                DOWNLOADS.pop(token, None)
            shutil.rmtree(item["temp_dir"], ignore_errors=True)

        response.call_on_close(cleanup)
        return response

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": "Could not send the video. " + str(exc)
        }), 502


# =========================================================
# SERVER
# =========================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
