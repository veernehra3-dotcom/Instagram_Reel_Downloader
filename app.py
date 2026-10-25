from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

@app.route("/")
def home():
    return render_template_string("""
    <h1>Tech by Nehra</h1>
    <h2>Instagram Reel Downloader</h2>
    <form action="/download" method="post">
        <input type="url" name="url" placeholder="Paste Instagram Reel URL" required>
        <button type="submit">Download Reel</button>
    </form>
    """)

@app.route("/download", methods=["POST"])
def download():
    url = request.form.get("url", "")
    return jsonify({"message": "URL received", "url": url})

@app.route("/health")
def health():
    return jsonify({"status": "online"})

if __name__ == "__main__":
    app.run()
