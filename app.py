            raise RuntimeError("Downloaded file is empty.")

        if not has_video_stream(final_path):
            raise RuntimeError("Downloaded file has no video stream.")

        if not has_audio_stream(final_path):
            raise RuntimeError("Downloaded file has no audio stream.")

        token = uuid.uuid4().hex
        title = (info or {}).get("title") or "instagram_reel"

        with DOWNLOADS_LOCK:
            DOWNLOADS[token] = {
                "path": final_path,
                "temp_dir": temp_dir,
                "title": title,
            }

        app.logger.warning(
            "Download successful; video and audio streams verified."
        )

        return jsonify({
            "success": True,
            "token": token,
            "title": title,
            "quality": "best",
            "audio_included": True,
            "download_url": f"/file/{token}",
        })

    except Exception:
        app.logger.exception("Instagram download failed")

        shutil.rmtree(temp_dir, ignore_errors=True)

        return jsonify({
            "success": False,
            "error": (
                "Could not download this Reel with audio. "
                "Check the latest Render logs for the actual format or "
                "FFmpeg error, then try again."
            ),
        }), 502


@app.route("/file/<token>", methods=["GET"])
def get_file(token):
    with DOWNLOADS_LOCK:
        item = DOWNLOADS.get(token)

    if not item:
        return jsonify({
            "success": False,
            "error": "Download link expired or not found. Please try again.",
        }), 404

    path = item["path"]

    if not os.path.isfile(path):
        return jsonify({
            "success": False,
            "error": "The downloaded file is no longer available.",
        }), 404

    if not has_video_stream(path) or not has_audio_stream(path):
        return jsonify({
            "success": False,
            "error": "The video or audio stream is missing; the file was not sent.",
        }), 502

    filename = re.sub(
        r"[^A-Za-z0-9._-]",
        "_",
        item.get("title", "instagram_reel"),
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
            max_age=0,
        )

        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"

        def cleanup():
            with DOWNLOADS_LOCK:
                DOWNLOADS.pop(token, None)

            shutil.rmtree(item["temp_dir"], ignore_errors=True)

        response.call_on_close(cleanup)
        return response

    except Exception:
        app.logger.exception("File delivery failed")

        return jsonify({
            "success": False,
            "error": "Could not send the video. Please try again.",
        }), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
