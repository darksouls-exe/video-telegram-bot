import telebot
import yt_dlp
import os
import time
import threading
from flask import Flask, send_file
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from urllib.parse import urlparse, unquote
import requests

TOKEN = os.getenv("BOT_TOKEN", "7953484219:AAEGvUwwb-OH4ixVAvI4NPUzTU27L47EI9E")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

video_files = {}
pending_urls = {}


def clean_url(url):
    for _ in range(3):
        url = unquote(url)
    try:
        r = requests.head(url, allow_redirects=True, timeout=10,
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        url = r.url
    except:
        pass
    if "facebook.com" in url:
        parsed = urlparse(url)
        url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return url


def delete_file_later(name, filename, delay=3600):
    def delete():
        time.sleep(delay)
        if os.path.exists(filename):
            os.remove(filename)
        if name in video_files:
            del video_files[name]
    threading.Thread(target=delete, daemon=True).start()


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
        "retries": 10,
        "socket_timeout": 30,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
    }
    if url:
        cookiefile = get_cookiefile(url)
        if cookiefile and os.path.exists(cookiefile):
            opts["cookiefile"] = cookiefile
    return opts


def get_resolutions(url):
    ydl_opts = base_ydl_opts(url)
    ydl_opts["skip_download"] = True
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    formats = info.get("formats", [])
    resolutions = set()
    for f in formats:
        if f.get("vcodec") not in (None, "none"):
            height = f.get("height")
            if height:
                resolutions.add(height)
    return sorted(resolutions)


def download_video(url, height):
    filename = f"video_{int(time.time())}.mp4"
    ydl_opts = base_ydl_opts(url)
    ydl_opts.update({
        "outtmpl": filename,
        "format": f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
        "merge_output_format": "mp4",
    })
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return filename


@app.route("/")
def home():
    return "Bot is running"

@app.route("/status")
def status():
    files = os.listdir(".")
    cookies = {
        "youtube": os.path.exists("cookies_youtube.txt"),
        "facebook": os.path.exists("cookies_facebook.txt"),
        "tiktok": os.path.exists("cookies_tiktok.txt")
    }
    return f"Files: {files}<br><br>Cookies found: {cookies}"

@app.route("/video/<name>")
def serve_video(name):
    if name in video_files and os.path.exists(video_files[name]):
        return send_file(video_files[name])
    return "File not found"


@bot.message_handler(content_types=["text"])
def handle(message):
    url = message.text.strip()
    url = clean_url(url)
    if not url.startswith("http"):
        bot.reply_to(message, "❌ Gửi link video hợp lệ")
        return
    key = str(message.chat.id)
    pending_urls[key] = url
    try:
        bot.reply_to(message, "🔍 Đang đọc video...")
        resolutions = get_resolutions(url)
        if not resolutions:
            markup = InlineKeyboardMarkup()
            markup.row(
                InlineKeyboardButton("360p", callback_data="res_360"),
                InlineKeyboardButton("480p", callback_data="res_480")
            )
            markup.row(
                InlineKeyboardButton("720p", callback_data="res_720"),
                InlineKeyboardButton("1080p", callback_data="res_1080")
            )
            bot.send_message(message.chat.id, "🎬 Chọn độ phân giải:", reply_markup=markup)
            return
    except Exception as e:
        bot.reply_to(message, f"❌ Không đọc được video\n\n{e}")
        return
    markup = InlineKeyboardMarkup()
    for r in sorted(resolutions)[:6]:
        markup.add(InlineKeyboardButton(f"{r}p", callback_data=f"res_{r}"))
    bot.send_message(message.chat.id, "🎬 Chọn độ phân giải:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("res_"))
def handle_resolution(call):
    key = str(call.message.chat.id)
    if key not in pending_urls:
        bot.answer_callback_query(call.id, "❌ Link hết hạn, gửi lại link mới")
        return
    height = int(call.data.split("_")[1])
    url = pending_urls.pop(key)
    bot.answer_callback_query(call.id)
    bot.edit_message_text(f"⏳ Đang tải {height}p...", call.message.chat.id, call.message.message_id)
    try:
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
            bot.send_message(call.message.chat.id, f"📥 Video lớn\n\nTải tại:\n{link}\n\n⏳ Link tồn tại 1 giờ")
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Lỗi tải video\n\n{e}")


def run_bot():
    print("BOT STARTED")
    bot.remove_webhook()
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            print("Bot restart:", e)
            time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    print("SERVER STARTED")
    app.run(host="0.0.0.0", port=port, threaded=True)
