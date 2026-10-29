import os
import re
import json
import uuid
import shutil
import tempfile
import threading
from urllib.parse import urlparse, quote
from urllib.request import Request, urlopen

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
    return match.group(1) if match else "reel"


def download_file_from_url(media_url, output_path):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.instagram.com/"
    }
    req = Request(media_url, headers=headers)
    with urlopen(req, timeout=45) as resp, open(output_path, "wb") as out_file:
        shutil.copyfileobj(resp, out_file)
    return output_path


# =========================================================
# BULLETPROOF DOWNLOADER (API + SCRAPE HYBRID)
# =========================================================

def download_instagram(url, temp_dir):
    shortcode = extract_shortcode(url)
    last_error = None

    # तरीका 1: स्टेबल पब्लिक डाउनलोडर API (Instagram के ब्लॉक को आसानी से पार कर लेता है)
    try:
        api_url = "https://vkrdownloader.xyz/server?vkr=" + quote(url)
        req = Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=25) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            data = res_data.get("data", {})
            downloads = data.get("downloads", [])
            for item in downloads:
                dl_url = item.get("url")
                if dl_url:
                    out_file = os.path.join(temp_dir, f"{shortcode}_api.mp4")
                    download_file_from_url(dl_url, out_file)
                    if os.path.isfile(out_file) and os.path.getsize(out_file) > 10000:
                        title = data.get("title") or f"instagram_reel_{shortcode}"
                        return {"id": shortcode, "title": title}, out_file
    except Exception as exc:
        last_error = str(exc)

    # तरीका 2: यदि API फेल हो, तो मोबाइल ऐप यूजर-एजेंट के साथ डायरेक्ट मेटा टैग स्क्रैपिंग ट्राई करें
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
            "Accept-Language": "en-US,en;q=0.9"
        }
        req = Request(url, headers=headers)
        with urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        video_match = re.search(r'<meta\s+property=["\']og:video["\']\s+content=["\']([^"\']+)["\']', html)
        if video_match:
            video_url = video_match.group(1).replace("&amp;", "&")
            title_match = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html)
            title = title_match.group(1) if title_match else f"instagram_reel_{shortcode}"
            
            out_file = os.path.join(temp_dir, f"{shortcode}_og.mp4")
            download_file_from_url(video_url, out_file)
            if os.path.isfile(out_file) and os.path.getsize(out_file) > 10000:
                return {"id": shortcode, "title": title}, out_file
    except Exception as exc:
        last_error = str(exc)

    raise RuntimeError(f"Instagram blocked direct access. Error: {last_error or 'Unknown'}")


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
        title = info.get("title") or "instagram_reel"

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
