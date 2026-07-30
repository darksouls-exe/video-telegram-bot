import telebot
import yt_dlp
import os
import time
import threading
import subprocess
import sys
from flask import Flask, send_file
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from urllib.parse import unquote
import requests

TOKEN = os.getenv("BOT_TOKEN", "7953484219:AAEGvUwwb-OH4ixVAvI4NPUzTU27L47EI9E")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

video_files = {}
pending_urls = {}


# ================= AUTO-UPDATE yt-dlp =================
def update_ytdlp():
    """Tự động cập nhật yt-dlp mỗi khi bot khởi động."""
    try:
        print("[yt-dlp] Đang kiểm tra bản cập nhật...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "--upgrade", "yt-dlp"],
            capture_output=True, text=True, timeout=120
        )
        import importlib
        import yt_dlp as _ydl
        importlib.reload(_ydl)
        print(f"[yt-dlp] Version hiện tại: {_ydl.version.__version__}")
    except Exception as e:
        print(f"[yt-dlp] Cập nhật thất bại (dùng version cũ): {e}")


# ================= CLEAN URL =================
SHORT_DOMAINS = (
    "fb.watch", "fb.gg",
    "m.facebook.com/share", "www.facebook.com/share",
    "vt.tiktok.com", "vm.tiktok.com",
    "youtu.be", "t.co", "bit.ly", "tinyurl.com"
)


def clean_url(url):
    for _ in range(3):
        url = unquote(url)
    url = url.strip()
    if any(d in url for d in SHORT_DOMAINS):
        try:
            r = requests.get(
                url, allow_redirects=True, timeout=15,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                        "AppleWebKit/605.1.15"
                    )
                }
            )
            if r.url and r.url.startswith("http"):
                url = r.url
        except Exception as e:
            print(f"Redirect resolve error: {e}")
    if "facebook.com" in url:
        url = url.replace("m.facebook.com", "www.facebook.com")
        url = url.replace("//web.facebook.com", "//www.facebook.com")
    return url


def is_facebook_url(url):
    return "facebook.com" in url or "fb.watch" in url or "fb.gg" in url


# ================= DELETE FILE =================
def delete_file_later(name, filename, delay=3600):
    def delete():
        time.sleep(delay)
        if os.path.exists(filename):
            os.remove(filename)
        if name in video_files:
            del video_files[name]
    threading.Thread(target=delete, daemon=True).start()


# ================= YTDLP OPTIONS =================
def get_cookiefile(url):
    if "youtube.com" in url or "youtu.be" in url:
        return "cookies_youtube.txt"
    elif "facebook.com" in url or "fb.watch" in url:
        return "cookies_facebook.txt"
    elif "tiktok.com" in url:
        return "cookies_tiktok.txt"
    return None


def base_ydl_opts(url=None):
    opts = {
        "quiet": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 60,
        "noplaylist": True,
        "geo_bypass": True,
        "geo_bypass_country": "US",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Fetch-Mode": "navigate",
        }
    }
    if url and is_facebook_url(url):
        opts["extractor_args"] = {
            "facebook": {"webpage_download_timeout": ["60"]}
        }
    if url:
        cookiefile = get_cookiefile(url)
        if cookiefile and os.path.exists(cookiefile):
            opts["cookiefile"] = cookiefile
    return opts


# ================= DOWNLOAD VIDEO (với fallback) =================
# Thứ tự thử format: ưu tiên mp4 gộp sẵn, sau đó ghép video+audio
FORMAT_CHAINS = [
    # Thử 1: mp4 gộp sẵn (phổ biến trên Facebook/TikTok)
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
    # Thử 2: theo chiều cao tối đa
    "bestvideo+bestaudio/best",
    # Thử 3: đơn giản nhất, tải bất cứ thứ gì có
    "best",
]


def download_video(url, height):
    filename = f"video_{int(time.time())}.mp4"
    base_opts = base_ydl_opts(url)

    # Tạo danh sách format theo height rồi thêm fallback không giới hạn
    formats = [
        (
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={height}]+bestaudio"
            f"/best[height<={height}][ext=mp4]/best[height<={height}]"
        ),
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best",
        "best",
    ]

    last_error = None
    for fmt in formats:
        try:
            opts = {**base_opts}
            opts.update({
                "outtmpl": filename,
                "format": fmt,
                "merge_output_format": "mp4",
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            if os.path.exists(filename) and os.path.getsize(filename) > 0:
                return filename
        except Exception as e:
            last_error = e
            print(f"[download] format={fmt!r} failed: {e}")
            # Nếu là lỗi parse/unsupported → thử update yt-dlp rồi retry 1 lần
            err = str(e).lower()
            if "cannot parse" in err or "unsupported url" in err or "please report" in err:
                print("[download] Phát hiện yt-dlp lỗi thời, đang tự cập nhật...")
                update_ytdlp()
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([url])
                    if os.path.exists(filename) and os.path.getsize(filename) > 0:
                        return filename
                except Exception as e2:
                    last_error = e2
                    print(f"[download] Retry sau update cũng thất bại: {e2}")
            continue

    raise last_error or Exception("Tải thất bại với mọi format thử")


# ================= RESOLUTION MARKUP =================
def resolution_markup():
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("360p", callback_data="res_360"),
        InlineKeyboardButton("480p", callback_data="res_480")
    )
    markup.row(
        InlineKeyboardButton("720p", callback_data="res_720"),
        InlineKeyboardButton("1080p", callback_data="res_1080")
    )
    return markup


# ================= FLASK =================
@app.route("/")
def home():
    return "Bot is running", 200

@app.route("/health")
def health():
    return "OK", 200

@app.route("/status")
def status():
    try:
        import yt_dlp as _ydl
        ver = _ydl.version.__version__
    except Exception:
        ver = "unknown"
    cookies = {
        "youtube": os.path.exists("cookies_youtube.txt"),
        "facebook": os.path.exists("cookies_facebook.txt"),
        "tiktok": os.path.exists("cookies_tiktok.txt"),
    }
    return f"Bot running<br>yt-dlp: {ver}<br>Cookies: {cookies}", 200

@app.route("/video/<name>")
def serve_video(name):
    if name in video_files and os.path.exists(video_files[name]):
        return send_file(video_files[name])
    return "File not found", 404


# ================= HANDLE MESSAGE =================
@bot.message_handler(content_types=["text"])
def handle(message):
    try:
        url = message.text.strip()
        if not url.startswith("http"):
            bot.reply_to(message, "❌ Gửi link video hợp lệ")
            return

        url = clean_url(url)
        key = str(message.chat.id)
        pending_urls[key] = url

        # Facebook: bỏ qua validate, hiển thị nút ngay
        if is_facebook_url(url):
            bot.reply_to(message, "🎬 Chọn độ phân giải:", reply_markup=resolution_markup())
            return

        # Các nền tảng khác: validate trước
        bot.reply_to(message, "🔍 Đang kiểm tra link...")
        try:
            ydl_opts = base_ydl_opts(url)
            ydl_opts["skip_download"] = True
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    raise Exception("Không lấy được thông tin video")
        except Exception as e:
            bot.reply_to(message, f"❌ Không đọc được video\n\n{e}")
            return

        bot.send_message(message.chat.id, "🎬 Chọn độ phân giải:", reply_markup=resolution_markup())

    except Exception as e:
        print(f"[handle] Unexpected error: {e}")
        try:
            bot.reply_to(message, f"❌ Lỗi không xác định\n\n{e}")
        except Exception:
            pass


# ================= HANDLE RESOLUTION =================
@bot.callback_query_handler(func=lambda call: call.data.startswith("res_"))
def handle_resolution(call):
    try:
        key = str(call.message.chat.id)
        if key not in pending_urls:
            bot.answer_callback_query(call.id, "❌ Link hết hạn, gửi lại link mới")
            return

        height = int(call.data.split("_")[1])
        url = pending_urls.pop(key)

        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"⏳ Đang tải {height}p...",
            call.message.chat.id,
            call.message.message_id
        )

        filename = download_video(url, height)
        size = os.path.getsize(filename)

        if size <= 50000000:
            with open(filename, "rb") as video:
                bot.send_video(call.message.chat.id, video)
            os.remove(filename)
        else:
            name = str(int(time.time()))
            video_files[name] = filename
            delete_file_later(name, filename)
            base_url = os.getenv("RENDER_EXTERNAL_URL", "https://video-telegram-bot.onrender.com")
            link = f"{base_url}/video/{name}"
            bot.send_message(
                call.message.chat.id,
                f"📥 Video lớn hơn 50MB\n\nTải tại:\n{link}\n\n⏳ Link tồn tại 1 giờ"
            )

    except Exception as e:
        print(f"[handle_resolution] Error: {e}")
        err = str(e).lower()
        if "timed out" in err or "timeout" in err or "connection" in err:
            msg = (
                "❌ Server bị Facebook chặn kết nối\n\n"
                "Cách khắc phục: thêm file cookies_facebook.txt vào Render\n"
                "(Export bằng extension 'Get cookies.txt LOCALLY')"
            )
        elif "cannot parse" in err or "please report" in err or "unsupported url" in err:
            msg = (
                "❌ yt-dlp không đọc được link này\n\n"
                "Bot đang tự cập nhật, vui lòng thử lại sau 1 phút"
            )
            # Tự update nền
            threading.Thread(target=update_ytdlp, daemon=True).start()
        elif "login" in err or "sign in" in err or "private" in err:
            msg = "❌ Video này ở chế độ riêng tư hoặc yêu cầu đăng nhập"
        else:
            msg = f"❌ Lỗi tải video\n\n{e}"
        try:
            bot.send_message(call.message.chat.id, msg)
        except Exception:
            pass


# ================= RUN BOT =================
def run_bot():
    print("BOT STARTED")
    bot.remove_webhook()
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            print(f"Bot restart: {e}")
            time.sleep(5)


# ================= START =================
if __name__ == "__main__":
    # Cập nhật yt-dlp ngay khi khởi động (chạy nền, không block)
    threading.Thread(target=update_ytdlp, daemon=True).start()

    threading.Thread(target=run_bot, daemon=True).start()

    port = int(os.environ.get("PORT", 10000))
    print(f"SERVER STARTED on port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
