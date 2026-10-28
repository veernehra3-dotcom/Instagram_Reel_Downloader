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
# FFPROBE MEDIA INFORMATION
# =========================================================

def get_media_info(file_path):
    ffprobe = shutil.which("ffprobe")

    if not ffprobe:
        return None

    if not os.path.isfile(file_path):
        return None

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_type,codec_name",
                "-of",
                "json",
                file_path
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False
        )

        if result.returncode != 0:
            return None

        return result.stdout

    except Exception as exc:
        app.logger.warning(
            "FFprobe error: %s",
            str(exc)
        )
        return None


def has_audio_stream(file_path):
    info = get_media_info(file_path)

    if not info:
        return False

    return '"codec_type": "audio"' in info


def has_video_stream(file_path):
    info = get_media_info(file_path)

    if not info:
        return False

    return '"codec_type": "video"' in info


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

        if name.endswith(
            (
                ".part",
                ".ytdl",
                ".json",
                ".temp"
            )
        ):
            continue

        try:
            size = os.path.getsize(path)

            if size > 0:
                result.append(path)

        except OSError:
            pass

    return result


# =========================================================
# FFMPEG MERGE VIDEO + AUDIO
# =========================================================

def merge_video_audio(video_file, audio_file, temp_dir):

    ffmpeg = shutil.which("ffmpeg")

    if not ffmpeg:
        raise RuntimeError(
            "FFmpeg is not available on Render."
        )

    output_file = os.path.join(
        temp_dir,
        "instagram_merged.mp4"
    )

    command = [
        ffmpeg,
        "-y",

        "-i",
        video_file,

        "-i",
        audio_file,

        "-map",
        "0:v:0",

        "-map",
        "1:a:0",

        "-c:v",
        "copy",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-movflags",
        "+faststart",

        output_file
    ]

    app.logger.info(
        "FFmpeg: merging video and audio..."
    )

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
        check=False
    )

    if result.returncode != 0:

        app.logger.error(
            "FFmpeg merge error:\n%s",
            result.stderr[-8000:]
        )

        raise RuntimeError(
            "FFmpeg could not merge video and audio."
        )

    if not os.path.isfile(output_file):
        raise RuntimeError(
            "FFmpeg did not create merged MP4."
        )

    if os.path.getsize(output_file) <= 0:
        raise RuntimeError(
            "Merged MP4 is empty."
        )

    if not has_video_stream(output_file):
        raise RuntimeError(
            "Merged MP4 has no video stream."
        )

    if not has_audio_stream(output_file):
        raise RuntimeError(
            "Merged MP4 has no audio stream."
        )

    return output_file


# =========================================================
# FFMPEG RE-MUX / FINALIZE
# =========================================================

def finalize_video(input_file, temp_dir):

    ffmpeg = shutil.which("ffmpeg")

    if not ffmpeg:
        raise RuntimeError(
            "FFmpeg is not available on Render."
        )

    output_file = os.path.join(
        temp_dir,
        "instagram_final.mp4"
    )

    command = [
        ffmpeg,
        "-y",

        "-i",
        input_file,

        "-map",
        "0:v:0",

        "-map",
        "0:a:0?",

        "-c:v",
        "copy",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-movflags",
        "+faststart",

        output_file
    ]

    app.logger.info(
        "FFmpeg: finalizing downloaded video..."
    )

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
        check=False
    )

    if result.returncode != 0:

        app.logger.error(
            "FFmpeg finalization error:\n%s",
            result.stderr[-8000:]
        )

        raise RuntimeError(
            "FFmpeg could not finalize the MP4."
        )

    if not os.path.isfile(output_file):
        raise RuntimeError(
            "Final MP4 was not created."
        )

    if os.path.getsize(output_file) <= 0:
        raise RuntimeError(
            "Final MP4 is empty."
        )

    return output_file


# =========================================================
# YT-DLP DOWNLOAD
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

    # Important:
    # First try combined streams.
    # Then try video + audio.
    # Then best available format.
    format_options = [
        "best[ext=mp4]/best",
        "bv*+ba/b",
        "bestvideo*+bestaudio/best",
        "best"
    ]

    last_error = None

    for attempt_number, format_selector in enumerate(
        format_options,
        start=1
    ):

        app.logger.info(
            "================================================="
        )

        app.logger.info(
            "Instagram download attempt %s",
            attempt_number
        )

        app.logger.info(
            "Format selector: %s",
            format_selector
        )

        app.logger.info(
            "================================================="
        )

        # Clean previous attempt files
        for name in os.listdir(temp_dir):

            path = os.path.join(
                temp_dir,
                name
            )

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

        ydl_opts = {

            "quiet": False,

            "no_warnings": False,

            "verbose": True,

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

            "continuedl": False,

            "concurrent_fragment_downloads": 1,

            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/155.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9"
            },

            "extractor_args": {
                "instagram": {
                    "api": ["web"]
                }
            }
        }

        try:

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:

                info = ydl.extract_info(
                    url,
                    download=True
                )

            if not info:
                raise RuntimeError(
                    "Instagram did not return media information."
                )

            files = get_downloaded_files(
                temp_dir
            )

            if not files:
                raise RuntimeError(
                    "No downloaded media file was found."
                )

            app.logger.info(
                "Files downloaded: %s",
                [
                    os.path.basename(x)
                    for x in files
                ]
            )

            # -------------------------------------------------
            # CHECK EVERY FILE
            # -------------------------------------------------

            for file_path in sorted(
                files,
                key=os.path.getsize,
                reverse=True
            ):

                video = has_video_stream(
                    file_path
                )

                audio = has_audio_stream(
                    file_path
                )

                app.logger.info(
                    "Media check: %s | video=%s | audio=%s | size=%s",
                    os.path.basename(file_path),
                    video,
                    audio,
                    os.path.getsize(file_path)
                )

                # Combined video + audio
                if video and audio:

                    app.logger.info(
                        "SUCCESS: Combined video + audio found."
                    )

                    final_file = finalize_video(
                        file_path,
                        temp_dir
                    )

                    if has_audio_stream(final_file):

                        return info, final_file

            # -------------------------------------------------
            # SEPARATE VIDEO + AUDIO
            # -------------------------------------------------

            video_files = [
                path
                for path in files
                if has_video_stream(path)
            ]

            audio_files = [
                path
                for path in files
                if has_audio_stream(path)
            ]

            app.logger.info(
                "Video files: %s",
                len(video_files)
            )

            app.logger.info(
                "Audio files: %s",
                len(audio_files)
            )

            if video_files and audio_files:

                video_file = max(
                    video_files,
                    key=os.path.getsize
                )

                audio_file = max(
                    audio_files,
                    key=os.path.getsize
                )

                app.logger.info(
                    "Separate video stream found: %s",
                    os.path.basename(video_file)
                )

                app.logger.info(
                    "Separate audio stream found: %s",
                    os.path.basename(audio_file)
                )

                merged_file = merge_video_audio(
                    video_file,
                    audio_file,
                    temp_dir
                )

                return info, merged_file

            # -------------------------------------------------
            # VIDEO ONLY
            # -------------------------------------------------

            if video_files:

                largest_video = max(
                    video_files,
                    key=os.path.getsize
                )

                app.logger.warning(
                    "Video downloaded but NO audio stream was exposed."
                )

                app.logger.warning(
                    "Video-only file: %s",
                    os.path.basename(largest_video)
                )

                raise RuntimeError(
                    "Instagram returned a video-only stream "
                    "without an audio track."
                )

            raise RuntimeError(
                "Instagram returned no usable video stream."
            )

        except Exception as exc:

            last_error = exc

            app.logger.warning(
                "Attempt %s failed: %s",
                attempt_number,
                str(exc)
            )

            continue

    if last_error:

        raise RuntimeError(
            "All Instagram download attempts failed. "
            + str(last_error)
        )

    raise RuntimeError(
        "Instagram download failed."
    )


# =========================================================
# HOME
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return """
    <!doctype html>

    <html>

    <head>

        <title>
            Instagram Reel Downloader API
        </title>

        <meta
            name="viewport"
            content="width=device-width, initial-scale=1"
        >

    </head>

    <body
        style="
        font-family:Arial;
        text-align:center;
        padding:40px
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

@app.route("/health", methods=["GET"])
def health():

    return jsonify({

        "status": "online",

        "message":
            "Instagram Downloader API is running",

        "ffmpeg_available":
            bool(shutil.which("ffmpeg")),

        "ffprobe_available":
            bool(shutil.which("ffprobe"))

    })


# =========================================================
# OPTIONS / PREFLIGHT
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
# DOWNLOAD
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
        "=============================================="
    )

    app.logger.warning(
        "DOWNLOAD REQUEST RECEIVED"
    )

    app.logger.warning(
        "Instagram URL: %s",
        url.split("?")[0]
    )

    app.logger.warning(
        "=============================================="
    )

    # -------------------------------------------------
    # URL CHECK
    # -------------------------------------------------

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
                "Please enter a valid Instagram Reel URL."

        }), 400

    # -------------------------------------------------
    # FFMPEG CHECK
    # -------------------------------------------------

    if not shutil.which("ffmpeg"):

        return jsonify({

            "success": False,

            "error":
                "FFmpeg is not available on Render."

        }), 503

    # -------------------------------------------------
    # TEMP DIRECTORY
    # -------------------------------------------------

    temp_dir = tempfile.mkdtemp(
        prefix="instagram_download_"
    )

    try:

        info, final_path = download_instagram(
            url,
            temp_dir
        )

        if not final_path:

            raise RuntimeError(
                "No final video was created."
            )

        if not os.path.isfile(final_path):

            raise RuntimeError(
                "Final video file does not exist."
            )

        file_size = os.path.getsize(
            final_path
        )

        if file_size <= 0:

            raise RuntimeError(
                "Downloaded file is empty."
            )

        if not has_video_stream(
            final_path
        ):

            raise RuntimeError(
                "Final file does not contain video."
            )

        if not has_audio_stream(
            final_path
        ):

            raise RuntimeError(
                "Final file does not contain audio."
            )

        token = uuid.uuid4().hex

        title = (
            info.get("title")
            or info.get("id")
            or "instagram_reel"
        )

        with DOWNLOADS_LOCK:

            DOWNLOADS[token] = {

                "path":
                    final_path,

                "temp_dir":
                    temp_dir,

                "title":
                    title

            }

        app.logger.info(
            "DOWNLOAD READY: %s bytes",
            file_size
        )

        return jsonify({

            "success": True,

            "token":
                token,

            "title":
                title,

            "quality":
                "best",

            "audio_included":
                True,

            "download_url":
                "/file/" + token

        })

    except Exception as exc:

        app.logger.exception(
            "Instagram download failed"
        )

        error_text = str(
            exc
        )

        app.logger.error(
            "FINAL ERROR: %s",
            error_text
        )

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        return jsonify({

            "success": False,

            "error":
                "Could not download this Reel. "
                + error_text

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

    # -------------------------------------------------
    # FINAL AUDIO CHECK
    # -------------------------------------------------

    if not has_video_stream(path):

        return jsonify({

            "success": False,

            "error":
                "Video stream was not found."

        }), 502

    if not has_audio_stream(path):

        return jsonify({

            "success": False,

            "error":
                "Audio stream was not found."

        }), 502

    # -------------------------------------------------
    # FILE NAME
    # -------------------------------------------------

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

            max_age=0

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

    except Exception as exc:

        app.logger.exception(
            "File delivery failed"
        )

        return jsonify({

            "success": False,

            "error":
                "Could not send the video. "
                + str(exc)

        }), 502


# =========================================================
# SERVER
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
