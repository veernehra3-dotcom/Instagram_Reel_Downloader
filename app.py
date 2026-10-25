rom flask import Flask, request, jsonify
app = Flask(name)
@app.route("/")
def home():
return jsonify({"status": "online", "message": "Tech by Nehra Downloader API"})
