import glob
import html
import os
import re
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import requests
import telebot
import yt_dlp
from bs4 import BeautifulSoup
from flask import Flask, request, send_file
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup


TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not TOKEN:
    print("ERROR: Render Environment phải có BOT_TOKEN.")
    sys.exit(1)

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

pending = {}
large_files = {}
lock = threading.Lock()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/124 Safari/537.36"
)


def is_facebook(url):
    try:
        host = urlsplit(url).netloc.lower().split(":")[0]
        return (
            host == "facebook.com"
            or host.endswith(".facebook.com")
            or host in {"fb.watch", "fb.gg"}
        )
    except ValueError:
        return False


def clean_url(value):
    value = unquote(value.strip())

    match = re.search(r"https?://\S+", value, re.I)
    value = match.group(0) if match else value
    value = value.strip("<>()[]{}\"'.,;")

    if not re.match(r"https?://", value, re.I):
        value = "https://" + value

    host = urlsplit(value).netloc.lower().split(":")[0]

    if host in {"fb.watch", "fb.gg"}:
        try:
            response = requests.get(
                value,
                headers={"User-Agent": UA},
                allow_redirects=True,
                timeout=15,
            )
            if response.url.startswith("http"):
                value = response.url
        except requests.RequestException:
            pass

    patterns = (
        r"facebook\.com/reel/(\d+)",
        r"facebook\.com/share/[vr]/(\d+)",
        r"facebook\.com/watch[^?]*\?.*?[?&]v=(\d+)",
        r"facebook\.com/video/(\d+)",
        r"facebook\.com/[\w.]+/videos/(\d+)",
    )

    for pattern in patterns:
        match = re.search(pattern, value, re.I)
        if match:
            return f"https://m.facebook.com/watch/?v={match.group(1)}"

    # Giữ nguyên link share/r vì mã của link này không phải ID số
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

    # Cookie chỉ dùng trên server, người dùng Telegram không cần cookie
    if is_facebook(url) and os.path.exists("cookies_facebook.txt"):
        options["cookiefile"] = "cookies_facebook.txt"

    options.update(extra)
    return options


def update_ytdlp():
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-q",
                "--upgrade",
                "yt-dlp",
            ],
            timeout=120,
            check=False,
            capture_output=True,
        )

        import importlib

        importlib.reload(yt_dlp)
        print("[yt-dlp] updated")

    except Exception as error:
        print("[yt-dlp] update failed:", error)


def remove_files(prefix):
    for path in glob.glob(prefix + "*"):
        try:
            os.remove(path)
        except OSError:
            pass


def download_with_ytdlp(url, height):
    prefix = f"video_{uuid4().hex}"
    last_error = RuntimeError("yt-dlp không tạo được file video")

    # Ưu tiên MP4 có sẵn, không cần ffmpeg trên Render
    formats = (
        f"best[height<={height}][ext=mp4]/best[height<={height}]/best",
        "best[ext=mp4]/best",
    )

    try:
        for fmt in formats:
            try:
                options = ydl_options(
                    url,
                    outtmpl=f"{prefix}.%(ext)s",
                    format=fmt,
                )

                with yt_dlp.YoutubeDL(options) as ydl:
                    ydl.download([url])

                files = [
                    path
                    for path in glob.glob(prefix + ".*")
                    if not path.endswith(".part")
                ]

                if files:
                    return max(files, key=os.path.getsize)

            except Exception as error:
                last_error = error
                error_text = str(error).lower()

                if any(
                    word in error_text
                    for word in (
                        "cannot parse",
                        "unsupported url",
                        "please report",
                    )
                ):
                    update_ytdlp()
                    continue

        raise last_error

    except Exception:
        remove_files(prefix)
        raise


def download_file(session, url, filename):
    with session.get(
        url,
        headers={"Referer": "https://snapsave.app/"},
        stream=True,
        timeout=90,
    ) as response:
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()

        if "text/html" in content_type:
            raise RuntimeError(
                "dịch vụ trung gian không trả về video"
            )

        with open(filename, "wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    output.write(chunk)

    if not os.path.exists(filename):
        raise RuntimeError("file video không tồn tại")

    if os.path.getsize(filename) == 0:
        raise RuntimeError("file video rỗng")

    return filename


def download_via_snapsave(url, height):
    session = requests.Session()
    session.headers["User-Agent"] = UA

    home = session.get("https://snapsave.app/", timeout=20)
    home.raise_for_status()

    soup = BeautifulSoup(home.text, "html.parser")
    token_input = soup.find("input", {"name": "token"})
    token = token_input.get("value", "") if token_input else ""

    result = session.post(
        "https://snapsave.app/action.php",
        data={
            "url": url,
            "token": token,
        },
        headers={"Referer": "https://snapsave.app/"},
        timeout=30,
    )
    result.raise_for_status()

    result_soup = BeautifulSoup(
        html.unescape(result.text),
        "html.parser",
    )

    links = []

    for anchor in result_soup.select("a[href]"):
        href = anchor.get("href", "")
        label = anchor.get_text(" ", strip=True).lower()

        valid_link = (
            any(
                word in href.lower()
                for word in ("fbcdn", "facebook", "video")
            )
            or "download" in label
        )

        if href.startswith("http") and valid_link:
            quality = (
                1080
                if "hd" in label
                else 480
                if "sd" in label
                else 360
            )
            links.append((abs(quality - height), href))

    if not links:
        raise RuntimeError(
            "SnapSave không trả về link video"
        )

    _, video_url = min(links)

    return download_file(
        session,
        video_url,
        f"video_{uuid4().hex}.mp4",
    )


def download_video(url, height):
    try:
        return download_with_ytdlp(url, height)

    except Exception as first_error:
        print("[yt-dlp] download failed:", first_error)

        if not is_facebook(url):
            raise

        try:
            print("[fallback] trying SnapSave")
            return download_via_snapsave(url, height)

        except Exception as second_error:
            print("[fallback] failed:", second_error)

            error_text = f"{first_error} {second_error}".lower()

            if any(
                word in error_text
                for word in ("private", "login", "sign in")
            ):
                raise RuntimeError("FACEBOOK_PRIVATE")

            raise RuntimeError("FACEBOOK_SERVER_BLOCKED")


def buttons():
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

    if filename and os.path.exists(filename):
        return send_file(filename)

    return "Not found", 404


@app.route("/upload-cookie", methods=["GET", "POST"])
def upload_cookie():
    # Chỉ chủ bot dùng trang này
    key = os.getenv("COOKIE_UPLOAD_KEY", "").strip()

    supplied = (
        request.args.get("key", "").strip()
        or request.form.get("key", "").strip()
    )

    if not key or supplied != key:
        return "Not found", 404

    if request.method == "POST":
        cookie = request.form.get("cookie", "").strip()

        if not cookie.startswith("# Netscape HTTP Cookie File"):
            return "Invalid cookies.txt format", 400

        with open(
            "cookies_facebook.txt",
            "w",
            encoding="utf-8",
        ) as output:
            output.write(cookie)

        return "Facebook cookie saved on server."

    return f"""
    <meta charset="utf-8">
    <h2>Facebook cookie</h2>
    <p>Chỉ chủ bot dùng trang này.</p>

    <form method="post">
        <input
            type="hidden"
            name="key"
            value="{html.escape(supplied)}"
        >

        <textarea
            name="cookie"
            rows="20"
            cols="90"
            placeholder="Dán nội dung cookies.txt tại đây"
        ></textarea>

        <br>
        <button>Lưu cookie</button>
    </form>
    """


@bot.message_handler(content_types=["text"])
def receive(message):
    try:
        url = clean_url(message.text)

        if not url.startswith("http"):
            bot.reply_to(
                message,
                "❌ Hãy gửi link video.",
            )
            return

        with lock:
            pending[str(message.chat.id)] = url

        if is_facebook(url):
            bot.reply_to(
                message,
                "🎬 Chọn độ phân giải:",
                reply_markup=buttons(),
            )
            return

        bot.reply_to(
            message,
            "🔍 Đang kiểm tra link...",
        )

        with yt_dlp.YoutubeDL(
            ydl_options(
                url,
                skip_download=True,
            )
        ) as ydl:
            ydl.extract_info(url, download=False)

        bot.send_message(
            message.chat.id,
            "🎬 Chọn độ phân giải:",
            reply_markup=buttons(),
        )

    except Exception as error:
        print("[message] error:", error)

        bot.reply_to(
            message,
            "❌ Link không hợp lệ hoặc video không thể truy cập.",
        )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("res:")
)
def choose_resolution(call):
    try:
        with lock:
            url = pending.pop(
                str(call.message.chat.id),
                None,
            )

        if not url:
            bot.answer_callback_query(
                call.id,
                "Link đã hết hạn, hãy gửi lại.",
            )
            return

        height = int(call.data.split(":")[1])

        bot.answer_callback_query(call.id)

        bot.edit_message_text(
            f"⏳ Đang tải {height}p...",
            call.message.chat.id,
            call.message.message_id,
        )

        filename = download_video(url, height)
        file_size = os.path.getsize(filename)

        if file_size <= 50_000_000:
            with open(filename, "rb") as video_file:
                if filename.lower().endswith(".mp4"):
                    bot.send_video(
                        call.message.chat.id,
                        video_file,
                    )
                else:
                    bot.send_document(
                        call.message.chat.id,
                        video_file,
                    )

            os.remove(filename)
            return

        name = uuid4().hex
        large_files[name] = filename

        threading.Thread(
            target=delete_later,
            args=(name, filename),
            daemon=True,
        ).start()

        base = os.getenv(
            "RENDER_EXTERNAL_URL",
            "",
        ).rstrip("/")

        if not base:
            base = "https://your-service.onrender.com"

        bot.send_message(
            call.message.chat.id,
            (
                "📥 Video lớn, tải tại:\n"
                f"{base}/video/{name}\n\n"
                "Link hết hạn sau 1 giờ"
            ),
        )

    except Exception as error:
        print("[download] error:", error)

        code = str(error)

        if code == "FACEBOOK_PRIVATE":
            message = (
                "❌ Video này riêng tư hoặc yêu cầu đăng nhập Facebook.\n"
                "Hãy thử một video công khai khác."
            )

        elif code == "FACEBOOK_SERVER_BLOCKED":
            message = (
                "❌ Facebook đang chặn máy chủ tải.\n\n"
                "Người dùng không cần thao tác trên thiết bị của mình. "
                "Chủ bot cần cấu hình cookie Facebook một lần trên server Render."
            )

        else:
            message = (
                "❌ Không tải được video. "
                "Hãy thử lại với link công khai khác."
            )

        bot.send_message(
            call.message.chat.id,
            message,
        )


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
            print("[telegram] restart:", error)
            time.sleep(5)


if __name__ == "__main__":
    threading.Thread(
        target=run_bot,
        daemon=True,
    ).start()

    port = int(os.getenv("PORT", "5000"))

    print(f"Server running on port {port}")

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True,
    )
