import telebot
import yt_dlp
import os
import time
import threading
from flask import Flask, send_file
from threading import Thread
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = os.getenv("BOT_TOKEN")

bot = telebot.TeleBot(TOKEN)

app = Flask(__name__)

video_files = {}
pending_urls = {}

# ================= DELETE FILE =================
def delete_file_later(name, filename, delay=3600):

    def delete():

        time.sleep(delay)

        if os.path.exists(filename):
            os.remove(filename)

        if name in video_files:
            del video_files[name]

    threading.Thread(target=delete, daemon=True).start()


# ================= COOKIES =================
def get_cookiefile(url):

    if "youtube.com" in url or "youtu.be" in url:
        return "cookies_youtube.txt"

    elif "facebook.com" in url or "fb.watch" in url:
        return "cookies_facebook.txt"

    elif "tiktok.com" in url:
        return "cookies_tiktok.txt"

    return None


# ================= GET RESOLUTIONS =================
def get_resolutions(url):

    ydl_opts = {
        "quiet": True,
        "skip_download": True
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats", [])

    resolutions = set()

    for f in formats:

        height = f.get("height")

        if height:
            resolutions.add(height)

    return sorted(resolutions)


# ================= DOWNLOAD VIDEO =================
def download_video(url, height):

    filename = f"video_{int(time.time())}.mp4"

    cookiefile = get_cookiefile(url)

    ydl_opts = {

        "outtmpl": filename,

        "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",

        "merge_output_format": "mp4",

        "quiet": True,

        "concurrent_fragment_downloads": 5,

        "socket_timeout": 30,

        "noplaylist": True,

        "http_headers": {
            "User-Agent": "Mozilla/5.0"
        }

    }

    if cookiefile and os.path.exists(cookiefile):
        ydl_opts["cookiefile"] = cookiefile

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    return filename


# ================= FLASK =================
@app.route("/")
def home():
    return "Bot is running"


@app.route("/video/<name>")
def serve_video(name):

    if name in video_files and os.path.exists(video_files[name]):
        return send_file(video_files[name])

    return "File not found"


# ================= HANDLE MESSAGE =================
@bot.message_handler(func=lambda message: True)
def handle(message):

    url = message.text.strip()

    if "http" not in url:
        bot.reply_to(message, "❌ Link không hợp lệ")
        return

    key = str(message.chat.id)

    pending_urls[key] = url

    try:

        resolutions = get_resolutions(url)

        if not resolutions:
            bot.reply_to(message, "❌ Không đọc được độ phân giải video")
            return

    except Exception as e:

        bot.reply_to(message, f"❌ Không đọc được video\n{e}")
        return

    markup = InlineKeyboardMarkup()

    for r in resolutions[:6]:
        markup.add(InlineKeyboardButton(f"{r}p", callback_data=f"res_{r}"))

    bot.send_message(message.chat.id, "🎬 Chọn độ phân giải:", reply_markup=markup)


# ================= HANDLE RESOLUTION =================
@bot.callback_query_handler(func=lambda call: call.data.startswith("res_"))
def handle_resolution(call):

    key = str(call.message.chat.id)

    if key not in pending_urls:

        bot.answer_callback_query(call.id, "❌ Link hết hạn")
        return

    height = call.data.split("_")[1]

    url = pending_urls.pop(key)

    bot.edit_message_text(

        f"⏳ Đang tải video {height}p...",

        call.message.chat.id,

        call.message.message_id

    )

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

            link = f"https://video-telegram-bot.onrender.com/video/{name}"

            bot.send_message(

                call.message.chat.id,

                f"📥 Video lớn.\n\nTải tại:\n{link}\n\n⏳ Link tồn tại 1 giờ"

            )

    except Exception as e:

        bot.send_message(call.message.chat.id, f"❌ Lỗi tải video\n\n{e}")


# ================= RUN SERVER =================
def run():

    port = int(os.environ.get("PORT", 5000))

    app.run(host="0.0.0.0", port=port)


def keep_alive():

    t = Thread(target=run)

    t.start()


keep_alive()


bot.delete_webhook(drop_pending_updates=True)

# restart polling nếu lỗi
while True:

    try:

        bot.infinity_polling(skip_pending=True)

    except Exception as e:

        print("Bot restart:", e)

        time.sleep(5)
