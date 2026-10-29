import os
import re
import json
import uuid
import shutil
import tempfile
import threading
import subprocess
from urllib.parse import urlparse, quote
from urllib.request import Request, urlopen

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


def extract_shortcode(url):
    match = re.search(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
    return match.group(1) if match else None


def get_media_streams_info(file_path):
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg or not os.path.isfile(file_path):
        return ""
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", file_path],
            capture_output=True,
            text=True,
            timeout=20,
            check=False
        )
        return result.stderr or ""
    except Exception:
        return ""


def has_audio_stream(file_path):
    return "Audio:" in get_media_streams_info(file_path)


def has_video_stream(file_path):
    return "Video:" in get_media_streams_info(file_path)


def download_file_from_url(media_url, output_path):
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        "Referer": "https://www.instagram.com/"
    }
    req = Request(media_url, headers=headers)
    with urlopen(req, timeout=45) as resp, open(output_path, "wb") as out_file:
        shutil.copyfileobj(resp, out_file)
    return output_path


# =========================================================
# RELIABLE PUBLIC API & YT-DLP HYBRID DOWNLOADER
# =========================================================

def download_instagram(url, temp_dir):
    shortcode = extract_shortcode(url) or "reel"
    
    # तरीका 1: Public JSON API (Render IP ब्लॉक से बचने का सबसे पक्का तरीका)
    try:
        api_url = "https://vkrdownloader.xyz/server?vkr=" + quote(url)
        req = Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8")).get("data", {})
            downloads = data.get("downloads", [])
            for item in downloads:
                dl_url = item.get("url")
                if dl_url:
                    out_file = os.path.join(temp_dir, shortcode + "_api.mp4")
                    download_file_from_url(dl_url, out_file)
                    if has_video_stream(out_file) and has_audio_stream(out_file):
                        return {"id": shortcode, "title": data.get("title") or ("reel_" + shortcode)}, out_file
    except Exception as exc:
        app.logger.warning("API fallback failed: %s", str(exc))

    # तरीका 2: yt-dlp Fallback
    try:
        ffmpeg_path = get_ffmpeg_path()
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "ffmpeg_location": ffmpeg_path,
            "format": "best/bestvideo+bestaudio",
            "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s")
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.endswith(".mp4")]
            for f_path in files:
                if has_video_stream(f_path) and has_audio_stream(f_path):
                    return info, f_path
    except Exception as exc:
        app.logger.warning("yt-dlp download failed: %s", str(exc))

    raise RuntimeError("Could not extract audio track for this reel. Try another link.")


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
        if not final_path or not os.path.isfile(final_path) or os.path.getsize(final_path) <= 0:
            raise RuntimeError("Downloaded file is invalid.")

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
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", item.get("title", "reel")) + ".mp4"
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
