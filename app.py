def download_instagram(url, temp_dir):
    output_template = os.path.join(temp_dir, "%(id)s.%(ext)s")
    ffmpeg_path = get_ffmpeg_path()

    # यह सेटिंग्स Instagram की लाइब्रेरी/ट्रेंडिंग ऑडियो और ओरिजिनल आवाज़ दोनों को पकड़कर मर्ज करती है
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'outtmpl': output_template,
        'merge_output_format': 'mp4',
        'ffmpeg_location': ffmpeg_path,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 30,
        # यह लाइन Instagram के नए बदलावों के बावजूद ऑडियो और वीडियो को आपस में जोड़ने के लिए सबसे ज़रूरी है
        'extractor_args': {
            'instagram': {
                'api': ['web', 'graphql']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            # फोल्डर में बनी हुई फाइनल वीडियो फाइल ढूँढना
            files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.endswith('.mp4')]
            if files:
                best_file = max(files, key=os.path.getsize)
                if os.path.getsize(best_file) > 10000:
                    return info, best_file

    except Exception as exc:
        raise RuntimeError(str(exc))

    raise RuntimeError("Could not download this Reel with audio.")
