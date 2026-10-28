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
# INSTAGRAM URL CHECK
# =========================================================

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
            and parsed.path.startswith(
                ("/reel/", "/reels/", "/p/", "/tv/")
            )
        )

    except Exception:
        return False


# =========================================================
# FFPROBE STREAM CHECK
# =========================================================

def get_stream_info(path):
    ffprobe = shutil.which("ffprobe")

    if not ffprobe or not os.path.isfile(path):
        return {
            "video": False,
            "audio": False,
        }

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        output = result.stdout.lower()

        return {
            "video": "video" in output,
            "audio": "audio" in output,
        }

    except Exception as exc:
        app.logger.warning(
            "FFprobe check failed: %s",
            str(exc)
        )

        return {
            "video": False,
            "audio": False,
        }


def has_audio_stream(path):
    return get_stream_info(path)["audio"]


def has_video_stream(path):
    return get_stream_info(path)["video"]


# =========================================================
# FILE LIST
# =========================================================

def get_downloaded_files(temp_dir):
    files = []

    if not os.path.isdir(temp_dir):
        return files

    for name in os.listdir(temp_dir):
        path = os.path.join(temp_dir, name)

        if not os.path.isfile(path):
            continue

        if name.endswith(
            (".part", ".ytdl", ".json", ".temp")
        ):
            continue

        try:
            if os.path.getsize(path) > 0:
                files.append(path)
        except OSError:
            pass

    return files


# =========================================================
# FORMAT LOGGING
# =========================================================

def log_available_formats(info):
    formats = info.get("formats") or []

    app.logger.warning(
        "Instagram returned %s available formats.",
        len(formats)
    )

    for fmt in formats:
        format_id = fmt.get("format_id", "?")
        ext = fmt.get("ext", "?")
        resolution = fmt.get("resolution") or "?"
        acodec = fmt.get("acodec") or "none"
        vcodec = fmt.get("vcodec") or "none"
        abr = fmt.get("abr") or "?"
        tbr = fmt.get("tbr") or "?"

        app.logger.warning(
            "FORMAT id=%s ext=%s resolution=%s "
            "vcodec=%s acodec=%s abr=%s tbr=%s",
            format_id,
            ext,
            resolution,
            vcodec,
            acodec,
            abr,
            tbr,
        )


# =========================================================
# CLEAN TEMP DIRECTORY
# =========================================================

def clear_temp_directory(temp_dir):
    if not os.path.isdir(temp_dir):
        return

    for name in os.listdir(temp_dir):
        path = os.path.join(temp_dir, name)

        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)

            elif os.path.isdir(path):
                shutil.rmtree(path)

        except OSError as exc:
            app.logger.warning(
                "Could not remove retry file %s: %s",
                path,
                str(exc)
            )


# =========================================================
# DOWNLOAD USING YT-DLP
# =========================================================

def download_instagram(url, temp_dir):
    output_template = os.path.join(
        temp_dir,
        "%(id)s.%(ext)s"
    )

    ffmpeg_path = shutil.which("ffmpeg")

    if not ffmpeg_path:
        raise RuntimeError(
            "FFmpeg is not available."
        )

    ydl_opts = {
        "quiet": False,
        "no_warnings": False,
        "verbose": True,

        "noplaylist": True,

        # Prefer separate best video + best audio.
        # Fall back to best combined stream.
        "format": (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo+bestaudio/"
            "best[ext=mp4]/"
            "best"
        ),

        # Force final container to MP4.
        "merge_output_format": "mp4",

        "outtmpl": output_template,

        "ffmpeg_location": ffmpeg_path,

        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,

        "socket_timeout": 45,

        "overwrites": True,

        # Keep partial files if a network issue happens.
        "continuedl": True,

        # Do not download playlists.
        "noplaylist": True,

        # Instagram extractor.
        "extractor_args": {
            "instagram": {
                "api": ["web"],
            }
        },
    }

    app.logger.warning(
        "Starting Instagram extraction..."
    )

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:

        # First extract information without downloading.
        info = ydl.extract_info(
            url,
            download=False
        )

        if not info:
            raise RuntimeError(
                "Instagram did not return video information."
            )

        log_available_formats(info)

        app.logger.warning(
            "Video ID: %s",
            info.get("id", "unknown")
        )

        app.logger.warning(
            "Title: %s",
            info.get("title", "instagram_reel")
        )

        app.logger.warning(
            "Extractor: %s",
            info.get("extractor", "unknown")
        )

        formats = info.get("formats") or []

        video_formats = []
        audio_formats = []
        combined_formats = []

        for fmt in formats:

            vcodec = fmt.get("vcodec")
            acodec = fmt.get("acodec")

            if vcodec and vcodec != "none":
                video_formats.append(fmt)

            if acodec and acodec != "none":
                audio_formats.append(fmt)

            if (
                vcodec
                and vcodec != "none"
                and acodec
                and acodec != "none"
            ):
                combined_formats.append(fmt)

        app.logger.warning(
            "Video-only formats: %s",
            len(video_formats)
        )

        app.logger.warning(
            "Audio formats: %s",
            len(audio_formats)
        )

        app.logger.warning(
            "Combined video+audio formats: %s",
            len(combined_formats)
        )

        if not audio_formats and not combined_formats:
            raise RuntimeError(
                "Instagram did not expose an audio stream."
            )

        # Actual download.
        ydl.download([url])

    # Find output files.
    candidates = get_downloaded_files(
        temp_dir
    )

    app.logger.warning(
        "Downloaded files: %s",
        [
            os.path.basename(path)
            for path in candidates
        ]
    )

    if not candidates:
        raise RuntimeError(
            "No downloaded file was produced."
        )

    # Check every resulting file.
    valid_files = []

    for path in candidates:

        info_stream = get_stream_info(path)

        app.logger.warning(
            "FILE CHECK: %s | video=%s | audio=%s | size=%s",
            os.path.basename(path),
            info_stream["video"],
            info_stream["audio"],
            os.path.getsize(path),
        )

        if (
            info_stream["video"]
            and info_stream["audio"]
        ):
            valid_files.append(path)

    if not valid_files:

        raise RuntimeError(
            "No downloaded file contains both "
            "video and audio streams."
        )

    # Select largest valid file.
    final_path = max(
        valid_files,
        key=os.path.getsize
    )

    return info, final_path


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():

    return """
    <!doctype html>
    <html>
    <head>
        <title>Instagram Reel Downloader API</title>

        <meta
            name="viewport"
            content="width=device-width, initial-scale=1"
        >
    </head>

    <body
        style="
            font-family:Arial;
            text-align:center;
            padding:40px;
        "
    >

        <h2>
            Instagram Reel Downloader API
        </h2>

        <p>
            Service is running.
        </p>

        <p>
            <a href="/health">
                Check API Health
            </a>
        </p>

    </body>
    </html>
    """


# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "online",

        "message":
            "Instagram Downloader API is running",

        "ffmpeg_available":
            bool(shutil.which("ffmpeg")),

        "ffprobe_available":
            bool(shutil.which("ffprobe")),
    })


# =========================================================
# OPTIONS / CORS PREFLIGHT
# =========================================================

@app.route(
    "/download",
    methods=["OPTIONS"]
)
def download_options():

    return Response(
        "",
        status=204
    )


# =========================================================
# DOWNLOAD API
# =========================================================

@app.route(
    "/download",
    methods=["POST"]
)
def download():

    data = request.get_json(
        silent=True
    ) or {}

    url = (
        data.get("url") or ""
    ).strip()

    app.logger.warning(
        "=========================================="
    )

    app.logger.warning(
        "DOWNLOAD REQUEST RECEIVED"
    )

    app.logger.warning(
        "Instagram URL: %s",
        url.split("?")[0]
    )

    # -----------------------------------------------------
    # URL CHECK
    # -----------------------------------------------------

    if not url:

        return jsonify({
            "success": False,
            "error":
                "Please provide an Instagram URL."
        }), 400

    if not is_valid_instagram_url(url):

        return jsonify({
            "success": False,
            "error":
                "Please enter a valid Instagram Reel or post URL."
        }), 400

    # -----------------------------------------------------
    # FFmpeg CHECK
    # -----------------------------------------------------

    ffmpeg = shutil.which(
        "ffmpeg"
    )

    ffprobe = shutil.which(
        "ffprobe"
    )

    if not ffmpeg or not ffprobe:

        return jsonify({
            "success": False,
            "error":
                "FFmpeg or FFprobe is unavailable. "
                "Check apt.txt and redeploy."
        }), 503

    app.logger.warning(
        "FFmpeg: %s",
        ffmpeg
    )

    app.logger.warning(
        "FFprobe: %s",
        ffprobe
    )

    # -----------------------------------------------------
    # TEMP DIRECTORY
    # -----------------------------------------------------

    temp_dir = tempfile.mkdtemp(
        prefix="instagram_download_"
    )

    try:

        info, final_path = download_instagram(
            url,
            temp_dir
        )

        # -------------------------------------------------
        # FINAL VALIDATION
        # -------------------------------------------------

        if not os.path.isfile(
            final_path
        ):

            raise RuntimeError(
                "Downloaded file was not found."
            )

        file_size = os.path.getsize(
            final_path
        )

        if file_size <= 0:

            raise RuntimeError(
                "Downloaded file is empty."
            )

        streams = get_stream_info(
            final_path
        )

        if not streams["video"]:

            raise RuntimeError(
                "Downloaded file has no video stream."
            )

        if not streams["audio"]:

            raise RuntimeError(
                "Downloaded file has no audio stream."
            )

        app.logger.warning(
            "FINAL FILE: %s",
            os.path.basename(
                final_path
            )
        )

        app.logger.warning(
            "FINAL SIZE: %s bytes",
            file_size
        )

        app.logger.warning(
            "FINAL VIDEO: %s",
            streams["video"]
        )

        app.logger.warning(
            "FINAL AUDIO: %s",
            streams["audio"]
        )

        # -------------------------------------------------
        # TOKEN
        # -------------------------------------------------

        token = uuid.uuid4().hex

        title = (
            info.get("title")
            or "instagram_reel"
        )

        with DOWNLOADS_LOCK:

            DOWNLOADS[token] = {
                "path": final_path,
                "temp_dir": temp_dir,
                "title": title,
            }

        app.logger.warning(
            "DOWNLOAD READY: %s",
            token
        )

        return jsonify({

            "success": True,

            "token": token,

            "title": title,

            "quality": "best",

            "audio_included": True,

            "video_included": True,

            "download_url":
                f"/file/{token}",
        })

    except Exception as exc:

        app.logger.exception(
            "Instagram download failed"
        )

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        error_text = str(
            exc
        )

        lower_error = error_text.lower()

        if (
            "audio" in lower_error
            and (
                "stream" in lower_error
                or "expose" in lower_error
                or "contains" in lower_error
            )
        ):

            message = (
                "Instagram did not provide a usable "
                "audio stream for this Reel. "
                "Try another public Reel."
            )

        else:

            message = (
                "Could not download this Reel. "
                "Please try again with another public Reel."
            )

        return jsonify({

            "success": False,

            "error": message,

        }), 502


# =========================================================
# SEND FILE
# =========================================================

@app.route(
    "/file/<token>",
    methods=["GET"]
)
def get_file(token):

    with DOWNLOADS_LOCK:

        item = DOWNLOADS.get(
            token
        )

    if not item:

        return jsonify({
            "success": False,
            "error":
                "Download link expired or not found. "
                "Please try again."
        }), 404

    path = item["path"]

    if not os.path.isfile(path):

        return jsonify({
            "success": False,
            "error":
                "The downloaded file is no longer available."
        }), 404

    streams = get_stream_info(
        path
    )

    if not streams["video"]:

        return jsonify({
            "success": False,
            "error":
                "Video stream was not found."
        }), 502

    if not streams["audio"]:

        return jsonify({
            "success": False,
            "error":
                "Audio stream was not found."
        }), 502

    filename = re.sub(
        r"[^A-Za-z0-9._-]",
        "_",
        item.get(
            "title",
            "instagram_reel"
        )
    )[:100]

    if not filename.lower().endswith(
        ".mp4"
    ):

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

        response.headers[
            "Cache-Control"
        ] = "no-store"

        response.headers[
            "X-Content-Type-Options"
        ] = "nosniff"

        def cleanup():

            with DOWNLOADS_LOCK:

                DOWNLOADS.pop(
                    token,
                    None
                )

            shutil.rmtree(
                item["temp_dir"],
                ignore_errors=True
            )

        response.call_on_close(
            cleanup
        )

        return response

    except Exception:

        app.logger.exception(
            "File delivery failed"
        )

        return jsonify({
            "success": False,
            "error":
                "Could not send the video. "
                "Please try again."
        }), 502


# =========================================================
# START SERVER
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
