
from flask import Flask, request, jsonify, render_template_string
import yt_dlp
import re

app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tech by Nehra | Instagram Downloader</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#10111b;color:white;font-family:Arial,sans-serif;padding:22px}
.container{max-width:650px;margin:35px auto;background:#1c1d2c;padding:25px;border-radius:20px}
h1{text-align:center;color:#ff729e}
p{text-align:center;color:#c7c7d8}
.categories{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:25px 0}
.cat{background:#292a3d;border:1px solid #44465e;border-radius:12px;padding:13px 3px;text-align:center;cursor:pointer;font-size:12px}
.cat.active{border-color:#ff729e;background:#48283d}
.emoji{display:block;font-size:24px;margin-bottom:8px}
input,select,button{width:100%;padding:15px;margin-top:12px;border-radius:10px;border:1px solid #45465b;font-size:15px}
input,select{background:#10111b;color:white}
button{background:#ff397b;color:white;font-weight:bold;border:0;cursor:pointer}
#status{overflow-wrap:anywhere;margin-top:18px;line-height:1.6}
a{color:#70e5b1}
.small{font-size:12px;color:#aaa;text-align:center}
</style>
</head>
<body>
<div class="container">
<h1>Tech by Nehra</h1>
<p>Instagram Downloader</p>
<div class="categories">
<div class="cat active" data-type="reel"><span class="emoji">🎬</span>Reels</div>
<div class="cat" data-type="post"><span class="emoji">🖼️</span>Posts</div>
<div class="cat" data-type="story"><span class="emoji">⭕</span>Stories</div>
<div class="cat" data-type="highlight"><span class="emoji">✨</span>Highlights</div>
<div class="cat" data-type="video"><span class="emoji">📹</span>Videos</div>
</div>
<form id="form">
<input type="hidden" id="type" name="type" value="reel">
<input type="url" id="url" name="url" placeholder="Paste public Instagram link here" required>
<select id="quality" name="quality">
<option value="best">Highest Available Quality</option>
<option value="1080">1080p</option>
<option value="720">720p</option>
<option value="480">480p</option>
</select>
<button type="submit">Get Download Link</button>
</form>
<div id="status"></div>
<p class="small">Use only public content you have permission to download.</p>
</div>
<script>
document.querySelectorAll('.cat').forEach(el=>{
el.addEventListener('click',()=>{
document.querySelectorAll('.cat').forEach(x=>x.classList.remove('active'));
el.classList.add('active');
document.getElementById('type').value=el.dataset.type;
});
});
document.getElementById('form').addEventListener('submit',async e=>{
e.preventDefault();
const status=document.getElementById('status');
status.textContent='Processing link...';
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
if(!r.ok)throw new Error(d.error||'Download failed');
status.innerHTML='';
(d.formats||[]).forEach(f=>{
const a=document.createElement('a');
a.href=f.url;
a.textContent='Download '+f.quality;
a.target='_blank';
a.rel='noopener noreferrer';
status.appendChild(a);
status.appendChild(document.createElement('br'));
});
if(d.formats&&d.formats.length===0)status.textContent='No downloadable formats found.';
}catch(err){status.textContent=err.message}
});
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

    if not re.match(r"^https?://(www\.)?instagram\.com/", url):
        return jsonify({"error": "Please enter a valid Instagram URL."}), 400

    if quality not in ("best", "1080", "720", "480"):
        quality = "best"

    if quality == "best":
        fmt = "best"
    else:
        fmt = f"best[height<={quality}]"

    try:
        opts = {
            "format": fmt,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": 20,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = []
        if info.get("url"):
            formats.append({
                "quality": str(info.get("height") or "Available"),
                "url": info["url"]
            })
        else:
            for f in info.get("formats", []):
                if f.get("url") and f.get("vcodec") != "none":
                    height = f.get("height") or 0
                    if quality == "best" or height <= int(quality):
                        formats.append({
                            "quality": f"{height}p" if height else "Available",
                            "url": f["url"]
                        })

        unique = {}
        for f in formats:
            unique[f["quality"]] = f
        return jsonify({"formats": list(unique.values())})

    except Exception:
        return jsonify({
            "error": "Could not access this content. It may be private, restricted, or unsupported. Please try a public Instagram link."
        }), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
