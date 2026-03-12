import telebot
import yt_dlp
import os
import time
import threading
from flask import Flask, send_file
from threading import Thread
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = "7953484219:AAEGvUwwb-OH4ixVAvI4NPUzTU27L47EI9E"

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
video_files = {}
pending_urls = {}

def delete_file_later(name, filename, delay=3600):
    def delete():
        time.sleep(delay)
        if os.path.exists(filename):
            os.remove(filename)
        if name in video_files:
            del video_files[name]
    threading.Thread(target=delete, daemon=True).start()

def download_video(url, height):
    filename = f"video_{int(time.time())}.mp4"
    ydl_opts = {
        'outtmpl': filename,
        'format': f'bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={height}]+bestaudio/best[height<={height}]',
        'merge_output_format': 'mp4',
        'quiet': True,
        'concurrent_fragment_downloads': 5,
        'noplaylist': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return filename

@app.route('/video/<name>')
def serve_video(name):
    if name in video_files and os.path.exists(video_files[name]):
        return send_file(video_files[name])
    return "File not found"

@bot.message_handler(func=lambda message: True)
def handle(message):
    url = message.text.strip()
    if "http" not in url:
        bot.reply_to(message, "❌ Gửi link video hợp lệ")
        return
    key = str(message.chat.id)
    pending_urls[key] = url
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

@bot.callback_query_handler(func=lambda call: call.data.startswith("res_"))
def handle_resolution(call):
    key = str(call.message.chat.id)
    if key not in pending_urls:
        bot.answer_callback_query(call.id, "❌ Link đã hết hạn, gửi lại link mới")
        return
    height = call.data.split("_")[1]
    url = pending_urls.pop(key)
    bot.answer_callback_query(call.id)
    bot.edit_message_text(f"⏳ Đang tải video {height}p...", call.message.chat.id, call.message.message_id)
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
            delete_file_later(name, filename, 3600)
            download_link = f"https://video-telegram-bot.onrender.com/video/{name}"
            bot.send_message(call.message.chat.id, f"📥 Video lớn.\n\nTải tại:\n{download_link}\n\n⏳ Link tồn tại 1 giờ")
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Không tải được video\n\n{e}")

def run():
    app.run(host="0.0.0.0", port=5000)

def keep_alive():
    t = Thread(target=run)
    t.start()

keep_alive()

bot.infinity_polling()
