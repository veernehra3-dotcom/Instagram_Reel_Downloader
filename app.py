
from flask import Flask, request, jsonify, render_template_string, Response
import yt_dlp
import re
import requests
from urllib.parse import urlparse

app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tech by Nehra Downloader</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#10111b;color:#fff;font-family:Arial;padding:20px}
.container{max-width:650px;margin:30px auto;background:#1c1d2c;padding:24px;border-radius:20px}
h1{text-align:center;color:#ff729e}
p{text-align:center;color:#ccc}
.categories{display:grid;grid-template-columns:repeat(5,1fr);gap:7px;margin:25px 0}
.cat{padding:12px 3px;text-align:center;background:#292a3d;border:1px solid #45465b;border-radius:12px;cursor:pointer;font-size:12px}
.cat.active{border-color:#ff729e;background:#48283d}
.emoji{display:block;font-size:23px;margin-bottom:7px}
input,select,button{width:100%;padding:15px;margin-top:12px;border-radius:10px;font-size:15px}
input,select{background:#10111b;color:white;border:1px solid #45465b}
button{background:#ff397b;color:white;border:0;font-weight:bold;cursor:pointer}
#status{margin-top:18px;text-align:center;overflow-wrap:anywhere;line-height:1.7}
.small{font-size:12px;color:#aaa}
</style>
</head>
<body>
<div class="container">
<h1>Tech by Nehra</h1>
<p>Instagram Video Downloader</p>
<div class="categories">
<div class="cat active" data-type="reel"><span class="emoji">🎬</span>Reels</div>
<div class="cat" data-type="post"><span class="emoji">🖼️</span>Posts</div>
<div class="cat" data-type="story"><span class="emoji">⭕</span>Stories</div>
<div class="cat" data-type="highlight"><span class="emoji">✨</span>Highlights</div>
<div class="cat" data-type="video"><span class="emoji">📹</span>Videos</div>
</div>
<form id="form">
<input type="hidden" id="type" value="reel">
<input id="url" type="url" placeholder="Paste public Instagram link here" required>
<select id="quality">
<option value="best">Highest Available Quality</option>
<option value="1080">1080p</option>
<option value="720">720p</option>
<option value="480">480p</option>
</select>
<button id="submit" type="submit">Get Download Link</button>
</form>
<div id="status"></div>
<p class="small">Download only content you own or have permission to save.</p>
</div>
<script>
document.querySelectorAll('.cat').forEach(el=>{
el.onclick=()=>{
document.querySelectorAll('.cat').forEach(x=>x.classList.remove('active'));
el.classList.add('active');
document.getElementById('type').value=el.dataset.type;
};
});
document.getElementById('form').onsubmit=async e=>{
e.preventDefault();
const status=document.getElementById('status');
const btn=document.getElementById('submit');
btn.disabled=true;
status.textContent='Processing... Please wait.';
try{
const r=await fetch('/download',{
method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({
url:document.getElementById('url').value,
quality:document.getElementById('quality').value,
type:document.getElementById('type').value
})
});
const d=await r.json();
if(!r.ok)throw Error(d.error||'Download failed');
status.replaceChildren();
if(!d.token)throw Error('No downloadable video found.');
const a=document.createElement('a');
a.href='/file/'+encodeURIComponent(d.token);
a.textContent='⬇ Download Video ('+d.quality+')';
a.style.cssText='display:block;background:#20a875;color:white;padding:15px;border-radius:10px;text-decoration:none;font-weight:bold;margin-top:12px';
status.appendChild(a);
a.click();
}catch(err){
status.textContent=err.message;
}finally{
btn.disabled=false;
}
};
</script>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/health")
def health():
    return jsonify({"status": "online"})

@app.route("/download", methods=["POST"])
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = data.get("quality", "best")

    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ("instagram.com", "www.instagram.com"):
            return jsonify({"error": "Please enter a valid Instagram URL."}), 400

        if quality not in ("best", "1080", "720", "480"):
            quality = "best"

        fmt = "best[ext=mp4]/best" if quality == "best" else f"best[height<={quality}][ext=mp4]/best[height<={quality}]/best"

        opts = {
            "format": fmt,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 25,
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        video_url = info.get("url")
        if not video_url:
            formats = [f for f in info.get("formats", []) if f.get("url") and f.get("vcodec") != "none"]
            if not formats:
                return jsonify({"error": "No downloadable video found."}), 400
            formats.sort(key=lambda f: f.get("height") or 0, reverse=True)
            video_url = formats[0]["url"]

        token = str(abs(hash(video_url)))
        app.config.setdefault("DOWNLOADS", {})[token] = {
            "url": video_url,
            "quality": info.get("height") or quality
        }

        return jsonify({"token": token, "quality": str(info.get("height") or quality) + "p"})

    except Exception:
        return jsonify({"error": "Could not access this video. Check that it is public and try again."}), 400

@app.route("/file/<token>")
def get_file(token):
    item = app.config.get("DOWNLOADS", {}).get(token)
    if not item:
        return "Download link expired. Please try again.", 404

    try:
        response = requests.get(
            item["url"],
            headers={"User-Agent": "Mozilla/5.0"},
            stream=True,
            timeout=30
        )
        response.raise_for_status()

        def generate():
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    yield chunk

        return Response(
            generate(),
            content_type="video/mp4",
            headers={"Content-Disposition": 'attachment; filename="Tech_by_Nehra.mp4"'}
        )
    except Exception:
        return "Could not download the video. Please try again.", 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
