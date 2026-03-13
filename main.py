import telebot
import yt_dlp
import os
import time
import threading
from flask import Flask, send_file
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from urllib.parse import urlparse, parse_qs, unquote

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise Exception("BOT_TOKEN not found")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

video_files = {}
pending_urls = {}

# ================= CLEAN URL =================
def clean_url(url):

    for _ in range(5):
        url = unquote(url)

    while True:

        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        if "facebook.com/login" in url and "next" in query:
            url = query["next"][0]
            continue

        if "share_url" in query:
            url = query["share_url"][0]
            continue

        break

    if "www.facebook.com" in url:
        url = url.replace("www.facebook.com", "m.facebook.com")

    return url


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
def base_ydl_opts():

    return {
        "quiet": True,
        "retries": 10,
        "socket_timeout": 30,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.facebook.com/"
        },
        "extractor_args": {
            "facebook": {
                "allow_unavailable_formats": True
            }
        }
    }


# ================= GET RESOLUTIONS =================
def get_resolutions(url):

    ydl_opts = base_ydl_opts()
    ydl_opts["skip_download"] = True

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats", [])

    resolutions = set()

    for f in formats:

        if f.get("vcodec") != "none":

            height = f.get("height")

            if height:
                resolutions.add(height)

    return sorted(resolutions)


# ================= DOWNLOAD VIDEO =================
def download_video(url, height):

    filename = f"video_{int(time.time())}.mp4"

    ydl_opts = base_ydl_opts()

    ydl_opts.update({
        "outtmpl": filename,
        "format": f"bestvideo[height<={height}]+bestaudio/best",
        "merge_output_format": "mp4",
        "ffmpeg_location": "/usr/bin/ffmpeg"
    })

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

    url = clean_url(url)

    if not url.startswith("http"):
        bot.reply_to(message, "❌ Link không hợp lệ")
        return

    key = str(message.chat.id)

    pending_urls[key] = url

    try:

        bot.reply_to(message, "🔍 Đang đọc video...")

        resolutions = get_resolutions(url)

        if not resolutions:
            bot.reply_to(message, "❌ Không đọc được độ phân giải")
            return

    except Exception as e:

        bot.reply_to(message, f"❌ Không đọc được video\n{e}")
        return

    markup = InlineKeyboardMarkup()

    for r in resolutions[:6]:

        markup.add(
            InlineKeyboardButton(
                f"{r}p",
                callback_data=f"res_{r}"
            )
        )

    bot.send_message(
        message.chat.id,
        "🎬 Chọn độ phân giải:",
        reply_markup=markup
    )


# ================= HANDLE RESOLUTION =================
@bot.callback_query_handler(func=lambda call: call.data.startswith("res_"))
def handle_resolution(call):

    key = str(call.message.chat.id)

    if key not in pending_urls:

        bot.answer_callback_query(call.id, "❌ Link hết hạn")
        return

    height = int(call.data.split("_")[1])

    url = pending_urls.pop(key)

    bot.edit_message_text(
        f"⏳ Đang tải {height}p...",
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

            base_url = os.getenv("RENDER_EXTERNAL_URL")

            link = f"{base_url}/video/{name}"

            bot.send_message(
                call.message.chat.id,
                f"📥 Video lớn\n\nTải tại:\n{link}\n\n⏳ Link tồn tại 1 giờ"
            )

    except Exception as e:

        bot.send_message(
            call.message.chat.id,
            f"❌ Lỗi tải video\n{e}"
        )


# ================= RUN BOT =================
def run_bot():

    bot.remove_webhook()

    while True:

        try:

            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=60,
                skip_pending=True
            )

        except Exception as e:

            print("Bot restart:", e)

            time.sleep(5)


# ================= START =================
if __name__ == "__main__":

    threading.Thread(target=run_bot).start()

    port = int(os.environ.get("PORT", 10000))

    app.run(host="0.0.0.0", port=port)
