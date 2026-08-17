import os, time, glob, threading
from uuid import uuid4
from urllib.parse import unquote, urlsplit

import requests
import telebot
import yt_dlp
from flask import Flask, send_file
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ================= CONFIG =================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN chưa được cấu hình.")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

pending = {}
large_files = {}
lock = threading.Lock()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0 Safari/537.36"
)

# ================= URL =================

def is_facebook(url):
    try:
        host = urlsplit(url).netloc.lower().split(":")[0]
        return host == "facebook.com" or host.endswith(".facebook.com") or host in {
            "fb.watch", "fb.gg"
        }
    except:
        return False


def clean_url(url):
    url = unquote(url.strip())

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Lấy đúng URL nếu người dùng gửi thêm text
    if " " in url:
        parts = url.split()
        url = next((x for x in parts if x.startswith("http")), url)

    # Resolve fb.watch / fb.gg
    host = urlsplit(url).netloc.lower()

    if host in ("fb.watch", "fb.gg"):
        try:
            r = requests.get(
                url,
                headers={"User-Agent": UA},
                allow_redirects=True,
                timeout=15
            )
            if r.url.startswith("http"):
                url = r.url
        except requests.RequestException:
            pass

    return url.strip("<>()[]{}\"'.,;")


# ================= YT-DLP =================

def ydl_options(url, height=None, check=False):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 60,
        "http_headers": {
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9"
        }
    }

    if check:
        opts["skip_download"] = True

    if height:
        opts["format"] = (
            f"best[height<={height}][ext=mp4]/"
            f"best[height<={height}]/best[ext=mp4]/best"
        )

    return opts


def remove_files(prefix):
    for f in glob.glob(prefix + "*"):
        try:
            os.remove(f)
        except:
            pass


def download_video(url, height):
    prefix = f"video_{uuid4().hex}"
    filename = None

    try:
        options = ydl_options(url, height)

        # Ưu tiên MP4 có sẵn, tránh phải ghép audio/video
        with yt_dlp.YoutubeDL({
            **options,
            "outtmpl": f"{prefix}.%(ext)s"
        }) as ydl:
            ydl.download([url])

        files = [
            f for f in glob.glob(prefix + ".*")
            if not f.endswith(".part")
        ]

        if not files:
            raise RuntimeError("yt-dlp không tạo được file.")

        filename = max(files, key=os.path.getsize)

        if os.path.getsize(filename) == 0:
            raise RuntimeError("File video rỗng.")

        return filename

    except Exception:
        remove_files(prefix)
        raise


# ================= BUTTON =================

def buttons():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("360p", callback_data="res:360"),
        InlineKeyboardButton("480p", callback_data="res:480")
    )
    kb.row(
        InlineKeyboardButton("720p", callback_data="res:720"),
        InlineKeyboardButton("1080p", callback_data="res:1080")
    )
    return kb


# ================= FILE CLEANUP =================

def delete_later(name, filename):
    time.sleep(3600)

    large_files.pop(name, None)

    try:
        os.remove(filename)
    except:
        pass


# ================= FLASK =================

@app.get("/")
def home():
    return "Bot is running", 200


@app.get("/health")
def health():
    return "OK", 200


@app.get("/video/<name>")
def video(name):
    filename = large_files.get(name)

    if filename and os.path.exists(filename):
        return send_file(filename)

    return "Not found", 404


# ================= TELEGRAM =================

@bot.message_handler(content_types=["text"])
def receive(message):
    try:
        url = clean_url(message.text)

        if not url.startswith("http"):
            bot.reply_to(message, "❌ Hãy gửi link video.")
            return

        chat_id = str(message.chat.id)

        with lock:
            pending[chat_id] = url

        bot.reply_to(
            message,
            "🎬 Chọn độ phân giải:",
            reply_markup=buttons()
        )

    except Exception as e:
        print("[message]", e)
        bot.reply_to(message, "❌ Link không hợp lệ.")


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("res:")
)
def choose_resolution(call):

    filename = None

    try:
        chat_id = str(call.message.chat.id)

        with lock:
            url = pending.pop(chat_id, None)

        if not url:
            bot.answer_callback_query(
                call.id,
                "Link đã hết hạn, hãy gửi lại."
            )
            return

        height = int(call.data.split(":")[1])

        bot.answer_callback_query(call.id)

        bot.edit_message_text(
            f"⏳ Đang tải {height}p...",
            call.message.chat.id,
            call.message.message_id
        )

        print(f"[DOWNLOAD] {url} | {height}p")

        filename = download_video(url, height)
        size = os.path.getsize(filename)

        # Telegram bot giới hạn gửi trực tiếp khoảng 50 MB
        if size <= 50_000_000:

            with open(filename, "rb") as f:

                if filename.lower().endswith(".mp4"):
                    bot.send_video(
                        call.message.chat.id,
                        f,
                        supports_streaming=True
                    )
                else:
                    bot.send_document(
                        call.message.chat.id,
                        f
                    )

            os.remove(filename)
            return

        # File lớn -> link server
        name = uuid4().hex
        large_files[name] = filename

        threading.Thread(
            target=delete_later,
            args=(name, filename),
            daemon=True
        ).start()

        base = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

        if not base:
            base = "https://your-service.onrender.com"

        bot.send_message(
            call.message.chat.id,
            f"📥 Video lớn hơn 50MB:\n"
            f"{base}/video/{name}\n\n"
            f"⏳ Link hết hạn sau 1 giờ."
        )

    except Exception as e:

        print("[DOWNLOAD ERROR]", repr(e))

        if filename and os.path.exists(filename):
            try:
                os.remove(filename)
            except:
                pass

        error = str(e).lower()

        if "private" in error or "login" in error or "sign in" in error:
            msg = (
                "❌ Video riêng tư hoặc yêu cầu đăng nhập.\n"
                "Hãy thử một video công khai."
            )

        elif "unsupported url" in error:
            msg = "❌ Link này không được yt-dlp hỗ trợ."

        elif "timeout" in error or "timed out" in error:
            msg = "❌ Kết nối tải video bị timeout. Hãy thử lại."

        else:
            msg = (
                "❌ Không tải được video.\n"
                "Hãy thử lại với một link công khai khác."
            )

        bot.send_message(
            call.message.chat.id,
            msg
        )


# ================= BOT =================

def run_bot():

    while True:

        try:
            bot.remove_webhook()

            print("[BOT] Starting...")

            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=60,
                skip_pending=True
            )

        except Exception as e:

            print("[BOT ERROR]", repr(e))
            time.sleep(5)


# ================= START =================

if __name__ == "__main__":

    threading.Thread(
        target=run_bot,
        daemon=True
    ).start()

    port = int(os.getenv("PORT", "10000"))

    print(f"[SERVER] Port: {port}")

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
