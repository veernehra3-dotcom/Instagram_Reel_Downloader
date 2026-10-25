
import os
import re
import uuid
import threading
import requests
import yt_dlp

from flask import Flask, request, jsonify, Response
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


# Get video URL
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

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "best[ext=mp4]/best",
        "extractor_args": {
            "instagram": {
                "api": ["web"]
            }
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return jsonify({
                "success": False,
                "error": "Video information could not be retrieved."
            }), 502

        video_url = info.get("url")

        if not video_url and info.get("formats"):
            formats = [
                f for f in info["formats"]
                if f.get("url") and f.get("vcodec") != "none"
            ]
            if formats:
                video_url = formats[-1]["url"]

        if not video_url:
            return jsonify({
                "success": False,
                "error": "Video URL not found. The post may be private or unsupported."
            }), 502

        token = uuid.uuid4().hex

        with DOWNLOADS_LOCK:
            DOWNLOADS[token] = {
                "url": video_url,
                "title": info.get("title") or "instagram_video",
                "headers": info.get("http_headers") or {}
            }

        return jsonify({
            "success": True,
            "token": token,
            "title": info.get("title") or "instagram_video",
            "quality": "best"
        })

    except Exception as e:
        app.logger.exception("Instagram extraction failed")
        return jsonify({
            "success": False,
            "error": "Could not retrieve the video. It may be private, unavailable, or Instagram may have restricted access."
        }), 502


# Stream video to the user's browser
@app.route("/file/<token>", methods=["GET"])
def get_file(token):
    with DOWNLOADS_LOCK:
        item = DOWNLOADS.get(token)

    if not item:
        return jsonify({
            "success": False,
            "error": "Download link expired or not found. Please try again."
        }), 404

    try:
        headers = dict(item.get("headers") or {})
        headers["User-Agent"] = headers.get(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
        )

        upstream = requests.get(
            item["url"],
            headers=headers,
            stream=True,
            timeout=(15, 60),
            allow_redirects=True
        )
        upstream.raise_for_status()

        content_type = upstream.headers.get(
            "Content-Type", "video/mp4"
        )

        filename = re.sub(
            r"[^A-Za-z0-9._-]",
            "_",
            item.get("title", "instagram_video")
        )[:100]

        if not filename.lower().endswith(".mp4"):
            filename += ".mp4"

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        response = Response(
            generate(),
            content_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store"
            }
        )

        return response

    except Exception:
        app.logger.exception("Video streaming failed")
        return jsonify({
            "success": False,
            "error": "Could not download the video file. Please try again."
        }), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
