import os
import glob
import time
import threading
from uuid import uuid4
from urllib.parse import unquote, urlsplit

import requests
import telebot
import yt_dlp
from flask import Flask, send_file
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


# =========================
# CONFIG
# =========================

TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

pending = {}
large_files = {}
lock = threading.Lock()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)


# =========================
# URL
# =========================

def clean_url(text):
    text = unquote((text or "").strip())

    # Nếu người dùng gửi thêm chữ trước/sau URL
    parts = text.split()

    url = next(
        (x for x in parts if x.startswith(("http://", "https://"))),
        text
    )

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    return url.strip("<>()[]{}\"'.,;")


def is_facebook(url):
    try:
        host = urlsplit(url).netloc.lower().split(":")[0]

        return (
            host == "facebook.com"
            or host.endswith(".facebook.com")
            or host in ("fb.watch", "fb.gg")
        )

    except Exception:
        return False


# =========================
# YT-DLP
# =========================

def ydl_options(url, height=None, output=None):

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,

        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 3,

        "socket_timeout": 60,

        "http_headers": {
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if output:
        opts["outtmpl"] = output

    if height:
        opts["format"] = (
            f"best[height<={height}][ext=mp4]/"
            f"best[height<={height}]/"
            f"best[ext=mp4]/"
            f"best"
        )

    return opts


def cleanup(prefix):

    for f in glob.glob(prefix + "*"):

        try:
            os.remove(f)

        except OSError:
            pass


def download_video(url, height):

    prefix = f"video_{uuid4().hex}"

    formats = [

        # Ưu tiên MP4 có sẵn
        f"best[height<={height}][ext=mp4]/"
        f"best[height<={height}]/"
        f"best[ext=mp4]/best",

        # Fallback
        f"best[height<={height}]/best",

        "best",
    ]

    last_error = None

    try:

        for fmt in formats:

            try:

                options = ydl_options(
                    url,
                    output=f"{prefix}.%(ext)s"
                )

                options["format"] = fmt

                print(
                    f"[YTDLP] url={url} "
                    f"format={fmt}"
                )

                with yt_dlp.YoutubeDL(options) as ydl:

                    ydl.download([url])

                files = [
                    f for f in glob.glob(prefix + ".*")
                    if not f.endswith(".part")
                ]

                if not files:
                    continue

                filename = max(
                    files,
                    key=os.path.getsize
                )

                if os.path.getsize(filename) > 0:
                    return filename

            except Exception as e:

                last_error = e

                print(
                    "[FORMAT ERROR]",
                    repr(e)
                )

        raise last_error or Exception(
            "yt-dlp không tạo được file."
        )

    except Exception:

        cleanup(prefix)

        raise


# =========================
# BUTTON
# =========================

def resolution_buttons():

    kb = InlineKeyboardMarkup()

    kb.row(
        InlineKeyboardButton(
            "360p",
            callback_data="res:360"
        ),

        InlineKeyboardButton(
            "480p",
            callback_data="res:480"
        )
    )

    kb.row(
        InlineKeyboardButton(
            "720p",
            callback_data="res:720"
        ),

        InlineKeyboardButton(
            "1080p",
            callback_data="res:1080"
        )
    )

    return kb


# =========================
# DELETE OLD FILE
# =========================

def delete_later(name, filename):

    time.sleep(3600)

    large_files.pop(name, None)

    try:
        os.remove(filename)

    except OSError:
        pass


# =========================
# FLASK
# =========================

@app.get("/")
def home():

    return "Bot is running", 200


@app.get("/health")
def health():

    return "OK", 200


@app.get("/video/<name>")
def serve_video(name):

    filename = large_files.get(name)

    if filename and os.path.exists(filename):

        return send_file(filename)

    return "Not found", 404


# =========================
# TELEGRAM
# =========================

@bot.message_handler(
    content_types=["text"]
)
def receive(message):

    try:

        url = clean_url(message.text)

        if not url.startswith(
            ("http://", "https://")
        ):

            bot.reply_to(
                message,
                "❌ Hãy gửi link video."
            )

            return

        chat_id = str(
            message.chat.id
        )

        with lock:

            pending[chat_id] = url

        print(
            f"[REQUEST] "
            f"chat={chat_id} "
            f"url={url}"
        )

        bot.reply_to(
            message,
            "🎬 Chọn độ phân giải:",
            reply_markup=resolution_buttons()
        )

    except Exception as e:

        print(
            "[MESSAGE ERROR]",
            repr(e)
        )

        bot.reply_to(
            message,
            "❌ Không đọc được link."
        )


# =========================
# DOWNLOAD CALLBACK
# =========================

@bot.callback_query_handler(
    func=lambda c:
        c.data.startswith("res:")
)
def choose_resolution(call):

    filename = None

    error_id = uuid4().hex[:8]

    chat_id = str(
        call.message.chat.id
    )

    try:

        with lock:

            url = pending.pop(
                chat_id,
                None
            )

        if not url:

            bot.answer_callback_query(
                call.id,
                "Link đã hết hạn."
            )

            return

        height = int(
            call.data.split(":")[1]
        )

        bot.answer_callback_query(
            call.id
        )

        bot.edit_message_text(
            f"⏳ Đang tải {height}p...",
            chat_id,
            call.message.message_id
        )

        print(
            f"[DOWNLOAD {error_id}] "
            f"chat={chat_id} "
            f"url={url} "
            f"height={height}"
        )

        filename = download_video(
            url,
            height
        )

        size = os.path.getsize(
            filename
        )

        # Telegram gửi trực tiếp
        if size <= 50_000_000:

            with open(
                filename,
                "rb"
            ) as f:

                if filename.lower().endswith(
                    ".mp4"
                ):

                    bot.send_video(
                        chat_id,
                        f,
                        supports_streaming=True
                    )

                else:

                    bot.send_document(
                        chat_id,
                        f
                    )

            os.remove(filename)

            print(
                f"[DONE {error_id}] "
                f"{size} bytes"
            )

            return

        # File lớn
        name = uuid4().hex

        large_files[name] = filename

        threading.Thread(
            target=delete_later,
            args=(name, filename),
            daemon=True
        ).start()

        base = os.getenv(
            "RENDER_EXTERNAL_URL",
            ""
        ).rstrip("/")

        if not base:

            base = (
                "https://your-service.onrender.com"
            )

        bot.send_message(
            chat_id,

            "📥 Video lớn hơn 50MB:\n\n"
            f"{base}/video/{name}\n\n"
            "⏳ Link hết hạn sau 1 giờ."
        )

    except Exception as e:

        print(
            f"[DOWNLOAD ERROR {error_id}]"
        )

        print(
            repr(e)
        )

        if filename and os.path.exists(
            filename
        ):

            try:
                os.remove(filename)

            except OSError:
                pass

        err = str(e).lower()

        if (
            "cannot parse data" in err
        ):

            msg = (
                "❌ Facebook không trả dữ liệu "
                "video cho yt-dlp.\n\n"
                "Mã lỗi: " + error_id
            )

        elif (
            "private" in err
            or "login" in err
            or "sign in" in err
        ):

            msg = (
                "❌ Video yêu cầu đăng nhập "
                "hoặc không công khai."
            )

        elif (
            "unsupported url" in err
        ):

            msg = (
                "❌ Link không được yt-dlp "
                "hỗ trợ."
            )

        elif (
            "timeout" in err
            or "timed out" in err
        ):

            msg = (
                "❌ Server bị timeout khi "
                "tải video."
            )

        elif (
            "403" in err
            or "forbidden" in err
        ):

            msg = (
                "❌ Server bị nguồn video "
                "từ chối truy cập."
            )

        else:

            msg = (
                "❌ Tải video thất bại.\n\n"
                f"Mã lỗi: {error_id}"
            )

        bot.send_message(
            chat_id,
            msg
        )


# =========================
# START BOT
# =========================

def run_bot():

    while True:

        try:

            bot.remove_webhook()

            print(
                "[BOT] polling started"
            )

            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=60,
                skip_pending=True
            )

        except Exception as e:

            print(
                "[BOT ERROR]",
                repr(e)
            )

            time.sleep(5)


# =========================
# MAIN
# =========================

if __name__ == "__main__":

    print(
        "[YT-DLP]",
        yt_dlp.version.__version__
    )

    threading.Thread(
        target=run_bot,
        daemon=True
    ).start()

    port = int(
        os.getenv("PORT", "10000")
    )

    print(
        f"[SERVER] port={port}"
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
