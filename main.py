import telebot, yt_dlp, os, re, time, threading, subprocess, sys
from flask import Flask, send_file, request, redirect
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from urllib.parse import unquote
import requests

TOKEN = os.getenv("BOT_TOKEN", "")
bot   = telebot.TeleBot(TOKEN)
app   = Flask(__name__)

video_files  = {}
pending_urls = {}

# ── Auto-update yt-dlp ────────────────────────────────────────────────────────
def update_ytdlp():
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--upgrade", "yt-dlp"],
                       capture_output=True, timeout=120)
        import importlib, yt_dlp as _y; importlib.reload(_y)
        print("[yt-dlp] updated:", _y.version.__version__)
    except Exception as e:
        print("[yt-dlp] update failed:", e)

# ── Facebook URL normaliser ───────────────────────────────────────────────────
_FB_ID_PATTERNS = [
    r'facebook\.com/reel/(\d+)',
    r'facebook\.com/share/[vr]/(\d+)',
    r'facebook\.com/watch\?.*?v=(\d+)',
    r'facebook\.com/video/(\d+)',
    r'facebook\.com/[\w.]+/videos/(\d+)',
]

def normalize_fb(url):
    for pat in _FB_ID_PATTERNS:
        m = re.search(pat, url)
        if m:
            return f"https://m.facebook.com/watch/?v={m.group(1)}"
    # /share/r/CODE (alphanumeric)
    m = re.search(r'facebook\.com/share/r/([^/?&#]+)', url)
    if m:
        return f"https://www.facebook.com/reel/{m.group(1)}"
    return url

def clean_url(url):
    for _ in range(3):
        url = unquote(url)
    url = url.strip()

    if any(d in url for d in ("facebook.com", "fb.watch", "fb.gg")):
        url = url.replace("//web.facebook.com", "//www.facebook.com") \
                 .replace("//m.facebook.com", "//www.facebook.com")
        if "fb.watch" in url or "fb.gg" in url:
            try:
                r = requests.get(url, allow_redirects=True, timeout=12,
                                 headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0)"})
                url = r.url if r.url.startswith("http") else url
            except Exception: pass
        return normalize_fb(url)

    # Other shortlinks
    if any(d in url for d in ("youtu.be", "vt.tiktok.com", "vm.tiktok.com",
                               "t.co", "bit.ly", "tinyurl.com")):
        try:
            r = requests.get(url, allow_redirects=True, timeout=12,
                             headers={"User-Agent": "Mozilla/5.0"})
            url = r.url if r.url.startswith("http") else url
        except Exception: pass
    return url

def is_fb(url): return any(d in url for d in ("facebook.com", "fb.watch", "fb.gg"))

# ── yt-dlp options ────────────────────────────────────────────────────────────
def ydl_opts(url=None, extra=None):
    opts = {
        "quiet": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 60,
        "noplaylist": True,
        "geo_bypass": True,
        "geo_bypass_country": "US",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if url and is_fb(url):
        opts["extractor_args"] = {"facebook": {"webpage_download_timeout": ["60"]}}
        cookie = "cookies_facebook.txt"
        if os.path.exists(cookie):
            opts["cookiefile"] = cookie
    elif url and "youtube.com" in url:
        if os.path.exists("cookies_youtube.txt"):
            opts["cookiefile"] = "cookies_youtube.txt"
    elif url and "tiktok.com" in url:
        if os.path.exists("cookies_tiktok.txt"):
            opts["cookiefile"] = "cookies_tiktok.txt"
    if extra:
        opts.update(extra)
    return opts

# ── Download with format fallback ─────────────────────────────────────────────
def download_video(url, height):
    fn = f"video_{int(time.time())}.mp4"
    base = ydl_opts(url)
    formats = [
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={height}]+bestaudio/best[height<={height}]",
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "best",
    ]
    last_err = None
    for fmt in formats:
        try:
            with yt_dlp.YoutubeDL({**base, "outtmpl": fn, "format": fmt, "merge_output_format": "mp4"}) as ydl:
                ydl.download([url])
            if os.path.exists(fn) and os.path.getsize(fn) > 0:
                return fn
        except Exception as e:
            last_err = e
            if any(k in str(e).lower() for k in ("cannot parse", "unsupported url", "please report")):
                update_ytdlp()   # self-heal rồi thử lại
                try:
                    with yt_dlp.YoutubeDL({**base, "outtmpl": fn, "format": fmt, "merge_output_format": "mp4"}) as ydl:
                        ydl.download([url])
                    if os.path.exists(fn) and os.path.getsize(fn) > 0:
                        return fn
                except Exception as e2:
                    last_err = e2
    raise last_err or Exception("Tải thất bại")

def delete_later(name, fn, delay=3600):
    def _del():
        time.sleep(delay)
        try: os.remove(fn)
        except Exception: pass
        video_files.pop(name, None)
    threading.Thread(target=_del, daemon=True).start()

def markup():
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("360p", callback_data="res_360"),
          InlineKeyboardButton("480p", callback_data="res_480"))
    m.row(InlineKeyboardButton("720p", callback_data="res_720"),
          InlineKeyboardButton("1080p", callback_data="res_1080"))
    return m

# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/")
def home(): return "Bot is running", 200

@app.route("/health")
def health(): return "OK", 200

@app.route("/video/<name>")
def serve_video(name):
    if name in video_files and os.path.exists(video_files[name]):
        return send_file(video_files[name])
    return "Not found", 404

@app.route("/upload-cookie", methods=["GET", "POST"])
def upload_cookie():
    """Upload Facebook cookies để bypass IP block của Render."""
    if request.method == "POST":
        cookie_text = request.form.get("cookie", "").strip()
        platform    = request.form.get("platform", "facebook")
        fname = {"facebook": "cookies_facebook.txt",
                 "youtube":  "cookies_youtube.txt",
                 "tiktok":   "cookies_tiktok.txt"}.get(platform, "cookies_facebook.txt")
        if cookie_text:
            with open(fname, "w", encoding="utf-8") as f:
                f.write(cookie_text)
            return f"<h2>✅ Đã lưu {fname}</h2><a href='/upload-cookie'>Upload thêm</a>"
        return "<h2>❌ Cookie rỗng</h2><a href='/upload-cookie'>Thử lại</a>"

    return """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Upload Cookie</title>
<style>body{font-family:sans-serif;max-width:700px;margin:40px auto;padding:0 20px}
textarea{width:100%;height:200px;font-size:12px}
select,button{margin-top:10px;padding:8px 16px;font-size:14px}
button{background:#0088cc;color:white;border:none;border-radius:6px;cursor:pointer}</style>
</head><body>
<h2>🍪 Upload Cookies</h2>
<p>Dùng extension <b>Get cookies.txt LOCALLY</b> → Export cookies từ facebook.com → Paste vào đây</p>
<form method="POST">
<select name="platform">
  <option value="facebook">Facebook</option>
  <option value="youtube">YouTube</option>
  <option value="tiktok">TikTok</option>
</select><br>
<textarea name="cookie" placeholder="Paste nội dung file cookies.txt vào đây..."></textarea><br>
<button type="submit">💾 Lưu Cookie</button>
</form></body></html>"""

# ── Bot handlers ──────────────────────────────────────────────────────────────
@bot.message_handler(content_types=["text"])
def handle(message):
    try:
        url = clean_url(message.text.strip())
        if not url.startswith("http"):
            bot.reply_to(message, "❌ Gửi link video hợp lệ"); return

        pending_urls[str(message.chat.id)] = url

        if is_fb(url):
            bot.reply_to(message, "🎬 Chọn độ phân giải:", reply_markup=markup()); return

        bot.reply_to(message, "🔍 Đang kiểm tra link...")
        try:
            with yt_dlp.YoutubeDL(ydl_opts(url, {"skip_download": True})) as ydl:
                if not ydl.extract_info(url, download=False):
                    raise Exception("Không lấy được thông tin video")
            bot.send_message(message.chat.id, "🎬 Chọn độ phân giải:", reply_markup=markup())
        except Exception as e:
            bot.reply_to(message, f"❌ Không đọc được video\n\n{e}")
    except Exception as e:
        try: bot.reply_to(message, f"❌ Lỗi: {e}")
        except Exception: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("res_"))
def handle_res(call):
    try:
        key = str(call.message.chat.id)
        if key not in pending_urls:
            bot.answer_callback_query(call.id, "❌ Link hết hạn, gửi lại"); return

        height = int(call.data.split("_")[1])
        url    = pending_urls.pop(key)
        bot.answer_callback_query(call.id)
        bot.edit_message_text(f"⏳ Đang tải {height}p...", call.message.chat.id, call.message.message_id)

        fn   = download_video(url, height)
        size = os.path.getsize(fn)

        if size <= 50_000_000:
            with open(fn, "rb") as f: bot.send_video(call.message.chat.id, f)
            os.remove(fn)
        else:
            name = str(int(time.time()))
            video_files[name] = fn
            delete_later(name, fn)
            base = os.getenv("RENDER_EXTERNAL_URL", "https://video-telegram-bot.onrender.com")
            bot.send_message(call.message.chat.id,
                             f"📥 Video >50MB — tải tại:\n{base}/video/{name}\n\n⏳ Link hết hạn sau 1 giờ")

    except Exception as e:
        err = str(e).lower()
        if any(k in err for k in ("cannot parse", "please report", "unsupported")):
            msg = ("❌ Facebook chặn server (IP bị block)\n\n"
                   f"👉 Truy cập để upload cookie:\n"
                   f"{os.getenv('RENDER_EXTERNAL_URL','https://video-telegram-bot.onrender.com')}/upload-cookie\n\n"
                   "Dùng extension 'Get cookies.txt LOCALLY' → Export từ facebook.com → Paste vào link trên")
        elif any(k in err for k in ("timed out", "timeout", "connection")):
            msg = ("❌ Kết nối bị timeout\n\n"
                   f"👉 Upload cookie tại:\n"
                   f"{os.getenv('RENDER_EXTERNAL_URL','https://video-telegram-bot.onrender.com')}/upload-cookie")
        elif any(k in err for k in ("login", "sign in", "private")):
            msg = "❌ Video riêng tư hoặc yêu cầu đăng nhập"
        else:
            msg = f"❌ Lỗi tải video\n\n{e}"
        try: bot.send_message(call.message.chat.id, msg)
        except Exception: pass

# ── Start ─────────────────────────────────────────────────────────────────────
def run_bot():
    bot.remove_webhook()
    while True:
        try: bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        except Exception as e: print("bot restart:", e); time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=update_ytdlp, daemon=True).start()
    threading.Thread(target=run_bot,      daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    print(f"SERVER on port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
