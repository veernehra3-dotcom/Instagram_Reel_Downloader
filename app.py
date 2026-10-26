
import os
import re
import uuid
import shutil
import tempfile
import threading

import yt_dlp

from flask import Flask, request, jsonify, Response, send_file
from urllib.parse import urlparse

app = Flask(__name__)

# Blogger website URL
ALLOWED_ORIGIN = "https://techbynehra22.blogspot.com"

# Temporary download storage
DOWNLOADS = {}
DOWNLOADS_LOCK = threading.Lock()


# CORS settings for Blogger
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


# Check Instagram URL
def is_valid_instagram_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        return (
            parsed.scheme in ("http", "https")
            and host in (
                "instagram.com",
                "www.instagram.com",
                "m.instagram.com"
            )
        )
    except Exception:
        return False


# Home page
@app.route("/", methods=["GET"])
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Instagram Reel Downloader API</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
    </head>
    <body style="font-family:Arial;text-align:center;padding:40px">
        <h2>Instagram Reel Downloader API</h2>
        <p>Service is running.</p>
        <p><a href="/health">Check API Health</a></p>
    </body>
    </html>
    """


# Health check
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "online",
        "message": "Instagram Downloader API is running"
    })


# Handle browser preflight requests
@app.route("/download", methods=["OPTIONS"])
def download_options():
    return Response("", status=204)


# Download video AND audio, merging when required
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
            "error": "Please enter a valid Instagram URL."
        }), 400

    temp_dir = tempfile.mkdtemp(prefix="instagram_download_")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,

        # Prefer video + audio; fall back to a combined stream
        "format": "bestvideo*+bestaudio/best",

        # Merge separate streams into an MP4 when needed
        "merge_output_format": "mp4",

        "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),

        "extractor_args": {
            "instagram": {
                "api": ["web"]
            }
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            if not info:
                raise RuntimeError("Video information could not be retrieved.")

            prepared_path = ydl.prepare_filename(info)

        # FFmpeg normally creates this MP4 when merging streams
        mp4_path = os.path.splitext(prepared_path)[0] + ".mp4"

        if os.path.isfile(mp4_path):
            final_path = mp4_path
        elif os.path.isfile(prepared_path):
            final_path = prepared_path
        else:
            # Find the actual downloaded output if its extension changed
            candidates = [
                os.path.join(temp_dir, name)
                for name in os.listdir(temp_dir)
                if os.path.isfile(os.path.join(temp_dir, name))
                and not name.endswith((".part", ".ytdl"))
            ]

            if not candidates:
                raise RuntimeError("The downloaded video file was not found.")

            final_path = candidates[0]

        token = uuid.uuid4().hex
        title = info.get("title") or "instagram_video"

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
            "audio_included": True
        })

    except Exception:
        app.logger.exception("Instagram video/audio download failed")
        shutil.rmtree(temp_dir, ignore_errors=True)

        return jsonify({
            "success": False,
            "error": (
                "Could not download the video with audio. "
                "Check that FFmpeg is installed on the server and "
                "that the Instagram post is publicly accessible."
            )
        }), 502


# Send the completed video file to the browser
@app.route("/file/<token>", methods=["GET"])
def get_file(token):
    with DOWNLOADS_LOCK:
        item = DOWNLOADS.get(token)

    if not item:
        return jsonify({
            "success": False,
            "error": "Download link expired or not found. Please try again."
        }), 404

    file_path = item["path"]

    if not os.path.isfile(file_path):
        return jsonify({
            "success": False,
            "error": "The downloaded file is no longer available."
        }), 404

    filename = re.sub(
        r"[^A-Za-z0-9._-]",
        "_",
        item.get("title", "instagram_video")
    )[:100]

    if not filename.lower().endswith(".mp4"):
        filename += ".mp4"

    try:
        response = send_file(
            file_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=filename,
            conditional=False,
            max_age=0
        )

        response.headers["Cache-Control"] = "no-store"

        def cleanup():
            with DOWNLOADS_LOCK:
                DOWNLOADS.pop(token, None)

            shutil.rmtree(item["temp_dir"], ignore_errors=True)

        response.call_on_close(cleanup)
        return response

    except Exception:
        app.logger.exception("Video file delivery failed")
        return jsonify({
            "success": False,
            "error": "Could not send the downloaded video. Please try again."
        }), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
