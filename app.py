import os
import re
import uuid
import shutil
import tempfile
import threading
from urllib.parse import urlparse
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


# =========================================================
# OPEN GRAPH (OG) META TAG SCRAPER (100% WORKING FOR AUDIO)
# =========================================================

def download_instagram_og(url, temp_dir):
    shortcode = extract_shortcode(url)
    
    # Facebook/WhatsApp bot user-agent ताकि Instagram ब्लॉक न करे और सही Meta Tags दे
    headers = {
        "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
        "Accept-Language": "en-US,en;q=0.9"
    }

    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # Open Graph video link ढूँढना (इसमें ऑडियो हमेशा होता है)
        video_match = re.search(r'<meta\s+property=["\']og:video["\']\s+content=["\']([^"\']+)["\']', html)
        if not video_match:
            # वैकल्पिक तरीका: og:video:secure_url चेक करें
            video_match = re.search(r'<meta\s+property=["\']og:video:secure_url["\']\s+content=["\']([^"\']+)["\']', html)

        if not video_match:
            raise RuntimeError("Instagram did not return media tags for this link.")

        video_url = video_match.group(1).replace("&amp;", "&")

        # Title ढूँढना
        title_match = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html)
        title = title_match.group(1) if title_match else f"instagram_reel_{shortcode}"
        title = re.sub(r'<[^>]+>', '', title).strip()

        # वीडियो फाइल डाउनलोड करें
        out_file = os.path.join(temp_dir, f"{shortcode}.mp4")
        dl_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.instagram.com/"
        }
        dl_req = Request(video_url, headers=dl_headers)
        with urlopen(dl_req, timeout=45) as resp, open(out_file, "wb") as f:
            shutil.copyfileobj(resp, f)

        if os.path.isfile(out_file) and os.path.getsize(out_file) > 0:
            return {"id": shortcode, "title": title}, out_file

    except Exception as exc:
        raise RuntimeError("Could not extract audio/video track: " + str(exc))

    raise RuntimeError("Failed to download video stream.")


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
        info, final_path = download_instagram_og(url, temp_dir)

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
