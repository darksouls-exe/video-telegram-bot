import telebot, yt_dlp, os, re, time, threading, subprocess, sys
from flask import Flask, send_file, request
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from urllib.parse import unquote
import requests
from bs4 import BeautifulSoup

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
        if os.path.exists("cookies_facebook.txt"):
            opts["cookiefile"] = "cookies_facebook.txt"
    elif url and "youtube.com" in url:
        if os.path.exists("cookies_youtube.txt"):
            opts["cookiefile"] = "cookies_youtube.txt"
    elif url and "tiktok.com" in url:
        if os.path.exists("cookies_tiktok.txt"):
            opts["cookiefile"] = "cookies_tiktok.txt"
    if extra:
        opts.update(extra)
    return opts

# ── Fallback: tải qua snapsave.app ───────────────────────────────────────────
def download_fb_via_snapsave(url, height):
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"})

    r = session.get("https://snapsave.app/", timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")
    token_input = soup.find("input", {"name": "token"})
    token = token_input["value"] if token_input else ""

    resp = session.post("https://snapsave.app/action.php", data={"url": url, "token": token},
                        headers={"Referer": "https://snapsave.app/"}, timeout=20)
    soup2 = BeautifulSoup(resp.text, "html.parser")

    links = []
    for a in soup2.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http") and any(x in href for x in ("fbcdn", "facebook", "cdninstagram", "video")):
            label = a.get_text(strip=True).lower()
            quality = 1080 if "hd" in label else 480 if "sd" in label else 360
            links.append((quality, href))

    if not links:
        raise Exception("snapsave không trả về link")

    links.sort(key=lambda x: abs(x[0] - height))
    chosen = links[0][1]

    fn = f"video_{int(time.time())}.mp4"
    with session.get(chosen, stream=True, timeout=60) as dl:
        dl.raise_for_status()
        with open(fn, "wb") as f:
            for chunk in dl.iter_content(chunk_size=1024*1024):
                f.write(chunk)

    if os.path.exists(fn) and os.path.getsize(fn) > 0:
        return fn
    raise Exception("File tải về rỗng")

# ── Download ──────────────────────────────────────────────────────────────────
_FB_BLOCK_KEYWORDS = ("cannot parse", "unsupported url", "please report",
                      "login", "sign in", "checkpoint", "blocked", "403", "429")

def download_video(url, height):
    fn = f"video_{int(time.time())}.mp4"
    base = ydl_opts(url)
    formats = [
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={height}]+bestaudio/best[height<={height}]",
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "best",
    ]
    last_err = None
    fb_blocked = False

    for fmt in formats:
        try:
            with yt_dlp.YoutubeDL({**base, "outtmpl": fn, "format": fmt, "merge_output_format": "mp4"}) as ydl:
                ydl.download([url])
            if os.path.exists(fn) and os.path.getsize(fn) > 0:
                return fn
        except Exception as e:
            last_err = e
            err_lower = str(e).lower()
            if any(k in err_lower for k in ("cannot parse", "unsupported url", "please report")):
                update_ytdlp()
                try:
                    with yt_dlp.YoutubeDL({**base, "outtmpl": fn, "format": fmt, "merge_output_format": "mp4"}) as ydl:
                        ydl.download([url])
                    if os.path.exists(fn) and os.path.getsize(fn) > 0:
                        return fn
                except Exception as e2:
                    last_err = e2
                    err_lower = str(e2).lower()
            if is_fb(url) and any(k in err_lower for k in _FB_BLOCK_KEYWORDS):
                fb_blocked = True
                break

    if fb_blocked or (is_fb(url) and last_err):
        try:
            return download_fb_via_snapsave(url, height)
        except Exception as snap_err:
            raise Exception(f"Không tải được video Facebook\nLỗi 1: {last_err}\nLỗi 2: {snap_err}")

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
        if any(k in err for k in ("login", "sign in", "private", "riêng tư")):
            msg = "❌ Video riêng tư hoặc yêu cầu đăng nhập, không thể tải"
        elif any(k in err for k in ("timed out", "timeout", "connection")):
            msg = "❌ Kết nối bị timeout, thử lại sau ít phút"
        else:
            msg = f"❌ Không tải được video, thử lại hoặc dùng link khác\n\n🔧 {e}"
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
