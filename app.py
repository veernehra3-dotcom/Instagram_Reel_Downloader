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


def get_ffmpeg_path():
    return shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


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
        allowed_hosts = {"instagram.com", "www.instagram.com", "m.instagram.com"}
        allowed_paths = ("/reel/", "/reels/", "/p/", "/tv/")
        return (
            parsed.scheme in ("http", "https")
            and host in allowed_hosts
            and parsed.path.startswith(allowed_paths)
        )
    except Exception:
        return False


def get_media_streams_info(file_path):
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg or not os.path.isfile(file_path):
        return ""
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", file_path],
            capture_output=True,
            text=True,
            timeout=15,
            check=False
        )
        return result.stderr or ""
    except Exception:
        return ""


def has_audio_stream(file_path):
    return "Audio:" in get_media_streams_info(file_path)


# =========================================================
# YT-DLP MASTER DOWNLOADER (WITH INSTAGRAM WEB API BYPASS)
# =========================================================

def download_instagram(url, temp_dir):
    output_template = os.path.join(temp_dir, "%(id)s.%(ext)s")
    ffmpeg_path = get_ffmpeg_path()

    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'outtmpl': output_template,
        'merge_output_format': 'mp4',
        'ffmpeg_location': ffmpeg_path,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 30,
        # यह लाइन Instagram के नए ऑडियो सिस्टम को बाईपास करने के लिए सबसे ज़रूरी है
        'extractor_args': {
            'instagram': {
                'api': ['web', 'graphql']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
        files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.endswith('.mp4')]
        if files:
            best_file = max(files, key=os.path.getsize)
            if os.path.getsize(best_file) > 10000 and has_audio_stream(best_file):
                return info, best_file

    except Exception as exc:
        raise RuntimeError(str(exc))

    raise RuntimeError("Could not extract audio track for this reel. Instagram updated security.")


@app.route("/", methods=["GET"])
def home():
    return "Instagram Reel Downloader API is Running!"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "online", "ffmpeg_available": bool(get_ffmpeg_path())})


@app.route("/download", methods=["OPTIONS"])
def download_options():
    return Response("", status=204)


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url or not is_valid_instagram_url(url):
        return jsonify({"success": False, "error": "Please provide a valid Instagram URL."}), 400

    temp_dir = tempfile.mkdtemp(prefix="instagram_download_")

    try:
        info, final_path = download_instagram(url, temp_dir)

        token = uuid.uuid4().hex
        title = info.get("title") or info.get("id") or "instagram_reel"

        with DOWNLOADS_LOCK:
            DOWNLOADS[token] = {"path": final_path, "temp_dir": temp_dir, "title": title}

        return jsonify({
            "success": True,
            "token": token,
            "title": title,
            "download_url": "/file/" + token
        })
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({"success": False, "error": str(exc)}), 502


@app.route("/file/<token>", methods=["GET"])
def get_file(token):
    with DOWNLOADS_LOCK:
        item = DOWNLOADS.get(token)

    if not item or not os.path.isfile(item["path"]):
        return jsonify({"success": False, "error": "File not found or expired."}), 404

    try:
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", item.get("title", "reel"))[:80] + ".mp4"
        response = send_file(item["path"], mimetype="video/mp4", as_attachment=True, download_name=filename)
        
        def cleanup():
            with DOWNLOADS_LOCK:
                DOWNLOADS.pop(token, None)
            shutil.rmtree(item["temp_dir"], ignore_errors=True)
            
        response.call_on_close(cleanup)
        return response
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
