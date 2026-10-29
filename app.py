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


# =========================================================
# FFMPEG PATH HELPER
# =========================================================

def get_ffmpeg_path():
    return shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


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
# INSTAGRAM URL VALIDATION & SHORTCODE
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


def extract_shortcode(url):
    match = re.search(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
    return match.group(1) if match else None


# =========================================================
# MEDIA STREAM CHECK USING FFMPEG
# =========================================================

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
    info = get_media_streams_info(file_path)
    return "Audio:" in info


def has_video_stream(file_path):
    info = get_media_streams_info(file_path)
    return "Video:" in info


# =========================================================
# BUILT-IN HTTP HELPERS (NO 'requests' MODULE NEEDED)
# =========================================================

def http_get_text(url, headers=None, timeout=20):
    req_headers = headers or {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    req = Request(url, headers=req_headers)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def download_file_from_url(media_url, output_path):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Referer": "https://www.instagram.com/"
    }
    req = Request(media_url, headers=headers)
    with urlopen(req, timeout=45) as resp, open(output_path, "wb") as out_file:
        shutil.copyfileobj(resp, out_file)
    return output_path


# =========================================================
# METHOD 1: INSTAGRAM GRAPHQL & EMBED (COMBINED AUDIO+VIDEO)
# =========================================================

def try_graphql_or_embed(url, temp_dir):
    shortcode = extract_shortcode(url)
    if not shortcode:
        return None

    # 1A: Instagram GraphQL Endpoint
    try:
        graphql_url = (
            "https://www.instagram.com/graphql/query/"
            "?doc_id=8845758582119845&variables="
            + quote(json.dumps({"shortcode": shortcode}))
        )
        gql_headers = {
            "User-Agent": (
                "Instagram 289.0.0.77.109 Android "
                "(33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100; en_US; 482924614)"
            ),
            "Accept": "*/*",
            "X-IG-App-ID": "936619743392459"
        }
        raw_json = http_get_text(graphql_url, headers=gql_headers, timeout=20)
        data = json.loads(raw_json)
        media = (
            data.get("data", {}).get("xdt_shortcode_media")
            or data.get("data", {}).get("shortcode_media")
            or {}
        )
        video_url = media.get("video_url")
        if video_url:
            out_file = os.path.join(temp_dir, shortcode + "_gql.mp4")
            download_file_from_url(video_url, out_file)
            if has_video_stream(out_file) and has_audio_stream(out_file):
                return {"id": shortcode, "title": "reel_" + shortcode}, out_file
    except Exception as exc:
        app.logger.warning("GraphQL fallback failed: %s", str(exc))

    # 1B: Instagram Captioned Embed Page
    try:
        embed_url = "https://www.instagram.com/p/" + shortcode + "/embed/captioned/"
        html = http_get_text(embed_url, timeout=20)
        html_clean = html.replace("\\/", "/")
        matches = re.findall(r'"video_url"\s*:\s*"([^"]+)"', html_clean)
        for match in matches:
            clean_url = match.encode("utf-8").decode("unicode_escape")
            out_file = os.path.join(temp_dir, shortcode + "_embed.mp4")
            download_file_from_url(clean_url, out_file)
            if has_video_stream(out_file) and has_audio_stream(out_file):
                return {"id": shortcode, "title": "reel_" + shortcode}, out_file
    except Exception as exc:
        app.logger.warning("Embed fallback failed: %s", str(exc))

    return None


# =========================================================
# METHOD 2: PUBLIC API FALLBACK
# =========================================================

def try_public_api_fallbacks(url, temp_dir):
    shortcode = extract_shortcode(url) or "reel"
    try:
        api_url = "https://vkrdownloader.xyz/server?vkr=" + quote(url)
        raw_json = http_get_text(api_url, timeout=25)
        data = json.loads(raw_json).get("data", {})
        downloads = data.get("downloads", [])
        for item in downloads:
            dl_url = item.get("url")
            if dl_url:
                out_file = os.path.join(temp_dir, shortcode + "_api.mp4")
                download_file_from_url(dl_url, out_file)
                if has_video_stream(out_file) and has_audio_stream(out_file):
                    return {"id": shortcode, "title": data.get("title") or ("reel_" + shortcode)}, out_file
    except Exception as exc:
        app.logger.warning("Public API fallback failed: %s", str(exc))

    return None


# =========================================================
# FFMPEG MERGE & FINALIZE
# =========================================================

def merge_video_audio(video_file, audio_file, temp_dir):
    ffmpeg = get_ffmpeg_path()
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
# MASTER DOWNLOADER
# =========================================================

def download_instagram(url, temp_dir):
    ffmpeg_path = get_ffmpeg_path()

    # 1. Try GraphQL or Embed first (Always has Audio)
    gql_result = try_graphql_or_embed(url, temp_dir)
    if gql_result:
        info, file_path = gql_result
        return info, finalize_video(file_path, temp_dir)

    # 2. Try yt-dlp format extraction & stream merging
    try:
        ydl_info_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "ffmpeg_location": ffmpeg_path,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
                )
            }
        }

        with yt_dlp.YoutubeDL(ydl_info_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info:
            formats = info.get("formats") or []
            shortcode = info.get("id") or extract_shortcode(url) or "reel"

            # Check combined formats
            combined_formats = [
                f for f in formats
                if f.get("url") and f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")
            ]
            if combined_formats:
                best_combined = combined_formats[-1]["url"]
                out_path = os.path.join(temp_dir, shortcode + "_combined.mp4")
                download_file_from_url(best_combined, out_path)
                if has_video_stream(out_path) and has_audio_stream(out_path):
                    return info, finalize_video(out_path, temp_dir)

            # Check separate video and audio formats
            video_formats = [
                f for f in formats
                if f.get("url") and f.get("vcodec") not in (None, "none")
            ]
            audio_formats = [
                f for f in formats
                if f.get("url") and f.get("acodec") not in (None, "none")
            ]

            if video_formats and audio_formats:
                v_url = video_formats[-1]["url"]
                a_url = audio_formats[-1]["url"]

                v_path = os.path.join(temp_dir, shortcode + "_v.mp4")
                a_path = os.path.join(temp_dir, shortcode + "_a.m4a")

                download_file_from_url(v_url, v_path)
                download_file_from_url(a_url, a_path)

                merged = merge_video_audio(v_path, a_path, temp_dir)
                if has_audio_stream(merged):
                    return info, merged

    except Exception as exc:
        app.logger.warning("yt-dlp extraction failed: %s", str(exc))

    # 3. Try Public API fallback
    api_result = try_public_api_fallbacks(url, temp_dir)
    if api_result:
        info, file_path = api_result
        return info, finalize_video(file_path, temp_dir)

    raise RuntimeError("Could not extract audio track for this Reel.")


# =========================================================
# ROUTES
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
        <p>Service is running.</p>
        <p><a href="/health">Check API Health</a></p>
    </body>
    </html>
    """


@app.route("/health", methods=["GET"])
def health():
    ffmpeg_bin = shutil.which("ffmpeg")
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
