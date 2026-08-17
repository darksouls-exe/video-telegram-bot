import glob
import html
import os
import re
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import requests
import telebot
import yt_dlp
from flask import Flask, request, send_file
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup


TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN chưa được cấu hình trên Render.")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
pending, large_files, lock = {}, {}, threading.Lock()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.getenv(
    "FACEBOOK_COOKIE_FILE",
    os.path.join(BASE_DIR, "cookies_facebook.txt"),
)
if not os.path.isabs(COOKIE_FILE):
    COOKIE_FILE = os.path.join(BASE_DIR, COOKIE_FILE)
DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "telegram-video-bot")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138 Safari/537.36"


def is_facebook(url):
    try:
        host = urlsplit(url).netloc.lower().split(":")[0]
        return host == "facebook.com" or host.endswith(".facebook.com") or host in {"fb.watch", "fb.gg"}
    except ValueError:
        return False


def save_cookie(cookie):
    cookie = (cookie or "").strip().lstrip("\ufeff")
    if not cookie.startswith("# Netscape HTTP Cookie File"):
        raise ValueError("Cookie phải là định dạng Netscape.")
    with open(COOKIE_FILE, "w", encoding="utf-8") as file:
        file.write(cookie + "\n")
    try:
        os.chmod(COOKIE_FILE, 0o600)
    except OSError:
        pass


if os.getenv("FACEBOOK_COOKIES", "").strip():
    try:
        save_cookie(os.getenv("FACEBOOK_COOKIES"))
        print("[facebook] cookie restored from environment")
    except ValueError as error:
        print("[facebook] invalid FACEBOOK_COOKIES:", error)


def clean_url(value):
    value = unquote((value or "").strip())
    match = re.search(r"https?://\S+", value, re.I)
    value = (match.group(0) if match else value).strip("<>()[]{}\"'.,;")
    if not re.match(r"https?://", value, re.I):
        value = "https://" + value

    parsed = urlsplit(value)
    host, path = parsed.netloc.lower().split(":")[0], parsed.path.lower()
    if host in {"fb.watch", "fb.gg"} or path.startswith("/share/"):
        try:
            result = requests.get(
                value, headers={"User-Agent": UA},
                allow_redirects=True, timeout=15,
            )
            if result.url.startswith("http"):
                value = result.url
        except requests.RequestException:
            pass

    patterns = (
        r"facebook\.com/reel/(\d+)",
        r"facebook\.com/share/[vr]/(\d+)",
        r"facebook\.com/watch[^#]*[?&]v=(\d+)",
        r"facebook\.com/video/(\d+)",
        r"facebook\.com/[\w.]+/videos/(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.I)
        if match:
            return f"https://m.facebook.com/watch/?v={match.group(1)}"
    return value


def ydl_options(url, **extra):
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 60,
        "http_headers": {
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if is_facebook(url) and os.path.isfile(COOKIE_FILE):
        options["cookiefile"] = COOKIE_FILE
        print("[facebook] server cookie is being used")
    options.update(extra)
    return options


def remove_files(prefix):
    for path in glob.glob(prefix + "*"):
        try:
            os.remove(path)
        except OSError:
            pass


def download_video(url, height):
    prefix = os.path.join(DOWNLOAD_DIR, f"video_{uuid4().hex}")
    formats = (
        f"best[height<={height}][ext=mp4]/best[height<={height}]/best[ext=mp4]/best",
        "best[ext=mp4]/best",
    )
    last_error = RuntimeError("yt-dlp không tạo được file.")
    try:
        for selected_format in formats:
            remove_files(prefix)
            try:
                options = ydl_options(
                    url, format=selected_format,
                    outtmpl=f"{prefix}.%(ext)s",
                )
                with yt_dlp.YoutubeDL(options) as ydl:
                    ydl.download([url])
                files = [
                    path for path in glob.glob(prefix + ".*")
                    if not path.endswith(".part")
                    and os.path.isfile(path)
                    and os.path.getsize(path) > 0
                ]
                if files:
                    return max(files, key=os.path.getsize)
            except Exception as error:
                last_error = error
        raise last_error
    except Exception:
        remove_files(prefix)
        raise


def resolution_buttons():
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton("360p", callback_data="res:360"),
        InlineKeyboardButton("480p", callback_data="res:480"),
    )
    keyboard.row(
        InlineKeyboardButton("720p", callback_data="res:720"),
        InlineKeyboardButton("1080p", callback_data="res:1080"),
    )
    return keyboard


def delete_later(name, filename):
    time.sleep(3600)
    large_files.pop(name, None)
    try:
        os.remove(filename)
    except OSError:
        pass


@app.get("/")
def home():
    return "Bot is running", 200


@app.get("/health")
def health():
    return "OK", 200


@app.get("/video/<name>")
def video(name):
    filename = large_files.get(name)
    return send_file(filename) if filename and os.path.isfile(filename) else ("Not found", 404)


@app.route("/upload-cookie", methods=["GET", "POST"])
def upload_cookie():
    key = os.getenv("COOKIE_UPLOAD_KEY", "").strip()
    supplied = request.args.get("key", "").strip() or request.form.get("key", "").strip()
    if not key or supplied != key:
        return "Not found", 404
    if request.method == "POST":
        try:
            save_cookie(request.form.get("cookie", ""))
        except ValueError:
            return "Invalid cookies.txt format", 400
        return "Facebook cookie saved on server."
    return f"""
    <meta charset="utf-8"><h2>Facebook cookie</h2>
    <p>Chỉ chủ bot dùng trang này.</p>
    <form method="post">
      <input type="hidden" name="key" value="{html.escape(supplied)}">
      <textarea name="cookie" rows="20" cols="90"></textarea><br>
      <button>Lưu cookie</button>
    </form>
    """


@app.get("/cookie-status")
def cookie_status():
    key = os.getenv("COOKIE_UPLOAD_KEY", "").strip()
    if not key or request.args.get("key", "").strip() != key:
        return "Not found", 404
    if not os.path.isfile(COOKIE_FILE):
        return "NO_COOKIE_FILE", 404
    try:
        with open(COOKIE_FILE, encoding="utf-8") as file:
            lines = file.readlines()
        valid = bool(lines) and lines[0].lstrip("\ufeff").startswith(
            "# Netscape HTTP Cookie File"
        )
        count = sum(
            1 for line in lines
            if line.strip() and not line.startswith("#") and len(line.split("\t")) >= 7
        )
        return {
            "file": "present",
            "format": "netscape" if valid else "unknown",
            "lines": len(lines),
            "cookie_count": count,
        }
    except OSError:
        return "COOKIE_FILE_READ_ERROR", 500


@bot.message_handler(content_types=["text"])
def receive(message):
    try:
        url = clean_url(message.text)
        if not url.startswith("http"):
            bot.reply_to(message, "❌ Hãy gửi link video.")
            return
        with lock:
            pending[str(message.chat.id)] = url

        # Facebook không extract_info trước vì dễ bị Facebook chặn.
        if is_facebook(url):
            bot.reply_to(
                message, "🎬 Chọn độ phân giải:",
                reply_markup=resolution_buttons(),
            )
            return

        with yt_dlp.YoutubeDL(ydl_options(url, skip_download=True)) as ydl:
            ydl.extract_info(url, download=False)
        bot.reply_to(
            message, "🎬 Chọn độ phân giải:",
            reply_markup=resolution_buttons(),
        )
    except Exception as error:
        print("[message] error:", repr(error))
        bot.reply_to(message, "❌ Link không hợp lệ hoặc video không thể truy cập.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("res:"))
def choose_resolution(call):
    filename = None
    url = ""
    try:
        with lock:
            url = pending.pop(str(call.message.chat.id), None)
        if not url:
            bot.answer_callback_query(call.id, "Link đã hết hạn, hãy gửi lại.")
            return

        height = int(call.data.split(":")[1])
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"⏳ Đang tải {height}p...",
            call.message.chat.id,
            call.message.message_id,
        )
        print(f"[DOWNLOAD] {url} | {height}p")
        filename = download_video(url, height)

        if os.path.getsize(filename) <= 50_000_000:
            with open(filename, "rb") as file:
                if filename.lower().endswith(".mp4"):
                    bot.send_video(
                        call.message.chat.id, file,
                        supports_streaming=True,
                    )
                else:
                    bot.send_document(call.message.chat.id, file)
            os.remove(filename)
            return

        name = uuid4().hex
        large_files[name] = filename
        threading.Thread(
            target=delete_later,
            args=(name, filename),
            daemon=True,
        ).start()
        base = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
        base = base or "https://your-service.onrender.com"
        bot.send_message(
            call.message.chat.id,
            f"📥 Video lớn hơn 50 MB:\n{base}/video/{name}\n\n"
            "Link hết hạn sau 1 giờ.",
        )
    except Exception as error:
        print("[download] error:", repr(error))
        if filename and os.path.exists(filename):
            try:
                os.remove(filename)
            except OSError:
                pass

        text = str(error).lower()
        if any(word in text for word in ("private", "login", "sign in")):
            reply = "❌ Video riêng tư hoặc yêu cầu đăng nhập Facebook."
        elif is_facebook(url):
            reply = "❌ Facebook đang chặn máy chủ tải. Hãy kiểm tra cookie Render."
        else:
            reply = "❌ Không tải được video. Hãy thử lại."
        bot.send_message(call.message.chat.id, reply)


def run_bot():
    while True:
        try:
            bot.remove_webhook()
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=60,
                skip_pending=True,
            )
        except Exception as error:
            print("[telegram] restart:", repr(error))
            time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        threaded=True,
    )
