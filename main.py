import glob
import html
import os
import re
import subprocess
import sys
import tempfile
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

COOKIE_FILE = os.getenv(
    "FACEBOOK_COOKIE_FILE",
    os.path.join(BASE_DIR, "cookies_facebook.txt"),
)

if not os.path.isabs(COOKIE_FILE):
    COOKIE_FILE = os.path.join(BASE_DIR, COOKIE_FILE)

DOWNLOAD_DIR = os.path.join(
    tempfile.gettempdir(),
    "telegram-video-bot",
)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0 Safari/537.36"
)


# ================= FACEBOOK / URL =================

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


def save_cookie_text(cookie):
    """
    Lưu cookie Netscape vào server.
    Không in và không trả nội dung cookie ra ngoài.
    """

    cookie = (cookie or "").strip().lstrip("\ufeff")

    if not cookie.startswith("# Netscape HTTP Cookie File"):
        raise ValueError(
            "Cookie phải là định dạng Netscape cookies.txt."
        )

    with open(COOKIE_FILE, "w", encoding="utf-8") as output:
        output.write(cookie + "\n")

    try:
        os.chmod(COOKIE_FILE, 0o600)
    except OSError:
        pass


def restore_cookie_from_environment():
    """
    Khôi phục cookie sau khi Render restart.

    Có thể lưu toàn bộ nội dung cookies.txt trong biến bí mật:
    FACEBOOK_COOKIES
    """

    cookie = os.getenv("FACEBOOK_COOKIES", "")

    if not cookie.strip():
        return

    try:
        save_cookie_text(cookie)
        print("[facebook] cookie restored from FACEBOOK_COOKIES")

    except ValueError as error:
        print("[facebook] invalid FACEBOOK_COOKIES:", error)


restore_cookie_from_environment()


def clean_url(value):
    """
    Làm sạch link người dùng gửi.

    Hỗ trợ:
    - facebook.com/watch
    - facebook.com/reel
    - facebook.com/video
    - facebook.com/share
    - m.facebook.com
    - fb.watch
    - fb.gg
    """

    value = unquote((value or "").strip())

    # Nếu tin nhắn có thêm chữ, lấy phần bắt đầu bằng http
    match = re.search(r"https?://\S+", value, re.I)

    if match:
        value = match.group(0)

    value = value.strip("<>()[]{}\"'.,;")

    if not re.match(r"https?://", value, re.I):
        value = "https://" + value

    parsed = urlsplit(value)
    host = parsed.netloc.lower().split(":")[0]
    path = parsed.path.lower()

    # Resolve link rút gọn hoặc link share nếu Facebook cho redirect
    if host in {"fb.watch", "fb.gg"} or path.startswith("/share/"):
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

    # Chuẩn hóa những link có ID số
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
            return (
                "https://m.facebook.com/watch/"
                f"?v={match.group(1)}"
            )

    # Không chuyển đổi share/r có mã chữ và số
    return value


# ================= YT-DLP =================

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

    # Cookie được dùng server-side, người dùng Telegram không cần gửi cookie
    if is_facebook(url) and os.path.isfile(COOKIE_FILE):
        options["cookiefile"] = COOKIE_FILE
        print(f"[facebook] using cookie file: {COOKIE_FILE}")

    options.update(extra)

    return options


def update_ytdlp():
    """
    Tự cập nhật yt-dlp khi Facebook thay đổi hoặc yt-dlp báo lỗi parser.
    """

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
    """
    Tải video bằng yt-dlp.

    Ưu tiên format MP4 progressive có sẵn cả audio/video
    để không phụ thuộc ffmpeg trên Render.
    """

    prefix = os.path.join(
        DOWNLOAD_DIR,
        f"video_{uuid4().hex}",
    )

    last_error = RuntimeError(
        "yt-dlp không tạo được file video"
    )

    formats = (
        (
            f"best[height<={height}][ext=mp4]/"
            f"best[height<={height}]/"
            "best"
        ),
        "best[ext=mp4]/best",
    )

    try:
        for selected_format in formats:
            remove_files(prefix)

            try:
                options = ydl_options(
                    url,
                    outtmpl=f"{prefix}.%(ext)s",
                    format=selected_format,
                )

                with yt_dlp.YoutubeDL(options) as ydl:
                    ydl.download([url])

                files = [
                    path
                    for path in glob.glob(prefix + ".*")
                    if not path.endswith(".part")
                    and os.path.isfile(path)
                ]

                if files:
                    filename = max(
                        files,
                        key=os.path.getsize,
                    )

                    if os.path.getsize(filename) > 0:
                        return filename

                    remove_files(prefix)

            except Exception as error:
                last_error = error

                error_text = str(error).lower()

                parse_error = any(
                    word in error_text
                    for word in (
                        "cannot parse",
                        "unsupported url",
                        "please report",
                    )
                )

                if parse_error:
                    update_ytdlp()
                    continue

        raise last_error

    except Exception:
        remove_files(prefix)
        raise


# ================= SNAPSave FALLBACK =================

def download_file(session, url, filename):
    with session.get(
        url,
        headers={
            "Referer": "https://snapsave.app/",
        },
        stream=True,
        timeout=90,
    ) as response:

        response.raise_for_status()

        content_type = response.headers.get(
            "content-type",
            "",
        ).lower()

        if "text/html" in content_type:
            raise RuntimeError(
                "dịch vụ trung gian không trả về video"
            )

        with open(filename, "wb") as output:
            for chunk in response.iter_content(
                1024 * 1024
            ):
                if chunk:
                    output.write(chunk)

    if (
        not os.path.exists(filename)
        or os.path.getsize(filename) == 0
    ):
        raise RuntimeError("file video rỗng")

    return filename


def download_via_snapsave(url, height):
    """
    Fallback cho video Facebook công khai
    khi yt-dlp bị Facebook chặn.
    """

    session = requests.Session()
    session.headers["User-Agent"] = UA

    home = session.get(
        "https://snapsave.app/",
        timeout=20,
    )

    home.raise_for_status()

    soup = BeautifulSoup(
        home.text,
        "html.parser",
    )

    token_element = soup.find(
        "input",
        {"name": "token"},
    )

    token = (
        token_element.get("value", "")
        if token_element
        else ""
    )

    result = session.post(
        "https://snapsave.app/action.php",
        data={
            "url": url,
            "token": token,
        },
        headers={
            "Referer": "https://snapsave.app/",
        },
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
        label = anchor.get_text(
            " ",
            strip=True,
        ).lower()

        is_video_link = (
            any(
                word in href.lower()
                for word in (
                    "fbcdn",
                    "facebook",
                    "video",
                )
            )
            or "download" in label
        )

        if href.startswith("http") and is_video_link:
            quality = (
                1080
                if "hd" in label
                else 480
                if "sd" in label
                else 360
            )

            links.append(
                (
                    abs(quality - height),
                    href,
                )
            )

    if not links:
        raise RuntimeError(
            "SnapSave không trả về link video"
        )

    _, video_url = min(links)

    filename = os.path.join(
        DOWNLOAD_DIR,
        f"video_{uuid4().hex}.mp4",
    )

    return download_file(
        session,
        video_url,
        filename,
    )


def download_video(url, height):
    """
    Thử yt-dlp trước, sau đó thử SnapSave nếu là Facebook.
    """

    try:
        return download_with_ytdlp(
            url,
            height,
        )

    except Exception as first_error:
        print(
            "[yt-dlp] download failed:",
            first_error,
        )

        if not is_facebook(url):
            raise

        try:
            print("[fallback] trying SnapSave")

            return download_via_snapsave(
                url,
                height,
            )

        except Exception as second_error:
            print(
                "[fallback] failed:",
                second_error,
            )

            error_text = (
                f"{first_error} "
                f"{second_error}"
            ).lower()

            if any(
                word in error_text
                for word in (
                    "private",
                    "login",
                    "sign in",
                )
            ):
                raise RuntimeError(
                    "FACEBOOK_PRIVATE"
                )

            raise RuntimeError(
                "FACEBOOK_SERVER_BLOCKED"
            )


# ================= TELEGRAM BUTTONS =================

def buttons():
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton(
            "360p",
            callback_data="res:360",
        ),
        InlineKeyboardButton(
            "480p",
            callback_data="res:480",
        ),
    )

    keyboard.row(
        InlineKeyboardButton(
            "720p",
            callback_data="res:720",
        ),
        InlineKeyboardButton(
            "1080p",
            callback_data="res:1080",
        ),
    )

    return keyboard


# ================= FILE CLEANUP =================

def delete_later(name, filename):
    time.sleep(3600)

    large_files.pop(name, None)

    try:
        os.remove(filename)
    except OSError:
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


# ================= COOKIE MANAGEMENT =================

@app.route("/upload-cookie", methods=["GET", "POST"])
def upload_cookie():
    """
    Trang chỉ dành cho chủ bot.
    Telegram user không cần thao tác tại đây.
    """

    key = os.getenv(
        "COOKIE_UPLOAD_KEY",
        "",
    ).strip()

    supplied = (
        request.args.get("key", "").strip()
        or request.form.get("key", "").strip()
    )

    if not key or supplied != key:
        return "Not found", 404

    if request.method == "POST":
        cookie = (
            request.form.get("cookie", "")
            .strip()
            .lstrip("\ufeff")
        )

        try:
            save_cookie_text(cookie)

        except ValueError:
            return "Invalid cookies.txt format", 400

        return (
            "Facebook cookie saved on server. "
            f"Cookie lines: {len(cookie.splitlines())}"
        )

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
        ></textarea>

        <br>
        <button>Lưu cookie</button>
    </form>
    """


@app.get("/cookie-status")
def cookie_status():
    """
    Kiểm tra trạng thái cookie nhưng không bao giờ trả nội dung cookie.
    """

    key = os.getenv(
        "COOKIE_UPLOAD_KEY",
        "",
    ).strip()

    supplied = request.args.get(
        "key",
        "",
    ).strip()

    if not key or supplied != key:
        return "Not found", 404

    if not os.path.isfile(COOKIE_FILE):
        return "NO_COOKIE_FILE", 404

    try:
        with open(
            COOKIE_FILE,
            encoding="utf-8",
        ) as source:
            lines = source.readlines()

        valid_header = (
            bool(lines)
            and lines[0]
            .lstrip("\ufeff")
            .startswith(
                "# Netscape HTTP Cookie File"
            )
        )

        cookie_count = sum(
            1
            for line in lines
            if (
                line.strip()
                and not line.startswith("#")
                and len(line.split("\t")) >= 7
            )
        )

        return {
            "file": "present",
            "format": (
                "netscape"
                if valid_header
                else "unknown"
            ),
            "lines": len(lines),
            "cookie_count": cookie_count,
        }

    except OSError:
        return "COOKIE_FILE_READ_ERROR", 500


# ================= TELEGRAM RECEIVE =================

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

        chat_id = str(message.chat.id)

        with lock:
            pending[chat_id] = url

        # Facebook không kiểm tra extract_info trước
        # vì Facebook thường chặn bước kiểm tra này.
        if is_facebook(url):
            bot.reply_to(
                message,
                "🎬 Chọn độ phân giải:",
                reply_markup=buttons(),
            )
            return

        # Các nền tảng khác vẫn kiểm tra link trước
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
            ydl.extract_info(
                url,
                download=False,
            )

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


# ================= TELEGRAM DOWNLOAD =================

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
                "Link đã hết hạn, hãy gửi lại.",
            )
            return

        height = int(
            call.data.split(":")[1]
        )

        bot.answer_callback_query(call.id)

        bot.edit_message_text(
            f"⏳ Đang tải {height}p...",
            call.message.chat.id,
            call.message.message_id,
        )

        print(
            f"[DOWNLOAD] {url} | {height}p"
        )

        filename = download_video(
            url,
            height,
        )

        size = os.path.getsize(filename)

        # Telegram Bot API giới hạn gửi trực tiếp khoảng 50 MB
        if size <= 50_000_000:
            with open(
                filename,
                "rb",
            ) as video_file:

                if filename.lower().endswith(".mp4"):
                    bot.send_video(
                        call.message.chat.id,
                        video_file,
                        supports_streaming=True,
                    )
                else:
                    bot.send_document(
                        call.message.chat.id,
                        video_file,
                    )

            os.remove(filename)
            return

        # File lớn: gửi link tải từ server
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
                "📥 Video lớn hơn 50 MB.\n"
                f"{base}/video/{name}\n\n"
                "Link hết hạn sau 1 giờ."
            ),
        )

    except Exception as error:
        print(
            "[download] error:",
            repr(error),
        )

        if filename and os.path.exists(filename):
            try:
                os.remove(filename)
            except OSError:
                pass

        code = str(error)

        if code == "FACEBOOK_PRIVATE":
            message = (
                "❌ Video này riêng tư hoặc yêu cầu "
                "đăng nhập Facebook.\n"
                "Hãy thử một video công khai khác."
            )

        elif code == "FACEBOOK_SERVER_BLOCKED":
            message = (
                "❌ Facebook đang chặn máy chủ tải.\n\n"
                "Người dùng không cần thao tác trên "
                "thiết bị của mình. Chủ bot cần kiểm tra "
                "cookie Facebook trên server Render."
            )

        else:
            message = (
                "❌ Không tải được video.\n"
                "Hãy thử lại với link công khai khác."
            )

        bot.send_message(
            call.message.chat.id,
            message,
        )


# ================= BOT LOOP =================

def run_bot():
    while True:
        try:
            bot.remove_webhook()

            print("[BOT] Starting...")

            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=60,
                skip_pending=True,
            )

        except Exception as error:
            print(
                "[telegram] restart:",
                repr(error),
            )
            time.sleep(5)


# ================= START SERVER =================

if __name__ == "__main__":
    threading.Thread(
        target=run_bot,
        daemon=True,
    ).start()

    port = int(
        os.getenv("PORT", "5000")
    )

    print(
        f"Server running on port {port}"
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True,
    )
