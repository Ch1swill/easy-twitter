import os
import re
import glob
import asyncio
import logging
import logging.handlers
import threading
import time
import sys
import shutil
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

# --- Configuration (environment variables) ---
# Required: BOT_TOKEN, DOWNLOAD_DIR
# Optional: HTTP_PROXY, HTTPS_PROXY, ALLOWED_USERS, COOKIES_FILE, BOT_CLEANUP_MODE

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def load_config() -> dict:
    http_proxy = _env("HTTP_PROXY") or _env("HTTPS_PROXY")
    cookies_file = _env("COOKIES_FILE", "cookies.txt")
    cleanup = _env("BOT_CLEANUP_MODE", "minimal").lower()
    if cleanup not in ("minimal", "delete"):
        cleanup = "minimal"
    return {
        "BOT_TOKEN": _env("BOT_TOKEN"),
        "HTTP_PROXY": http_proxy,
        "ALLOWED_USERS": _env("ALLOWED_USERS"),
        "DOWNLOAD_DIR": _env("DOWNLOAD_DIR"),
        "COOKIES_FILE": cookies_file,
        "BOT_CLEANUP_MODE": cleanup,
        "LOG_FILE": _env("LOG_FILE"),
    }


CONFIG = load_config()

if os.path.isabs(CONFIG["DOWNLOAD_DIR"]):
    DOWNLOAD_PATH = CONFIG["DOWNLOAD_DIR"]
else:
    DOWNLOAD_PATH = os.path.join(BASE_DIR, CONFIG["DOWNLOAD_DIR"])

_cookies = CONFIG["COOKIES_FILE"]
if os.path.isabs(_cookies):
    COOKIES_PATH = _cookies
else:
    COOKIES_PATH = os.path.join(BASE_DIR, _cookies)

os.makedirs(DOWNLOAD_PATH, exist_ok=True)

_log_handlers: list = [logging.StreamHandler(sys.stdout)]
if CONFIG["LOG_FILE"]:
    os.makedirs(os.path.dirname(CONFIG["LOG_FILE"]), exist_ok=True)
    _log_handlers.append(
        logging.handlers.RotatingFileHandler(
            CONFIG["LOG_FILE"],
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    )
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=_log_handlers,
)
logger = logging.getLogger("XDT-Bot")

TWITTER_PATTERNS = [
    r"https?://(?:www\.)?twitter\.com/\w+/status/\d+",
    r"https?://(?:www\.)?x\.com/\w+/status/\d+",
    r"https?://(?:mobile\.)?twitter\.com/\w+/status/\d+",
]


def is_allowed_user(user_id: int) -> bool:
    if not CONFIG["ALLOWED_USERS"]:
        return True
    allowed_ids = []
    for uid in str(CONFIG["ALLOWED_USERS"]).split(","):
        uid = uid.strip()
        if not uid:
            continue
        try:
            allowed_ids.append(int(uid))
        except ValueError:
            logger.warning("ALLOWED_USERS 包含无效 ID，已跳过: %r", uid)
    return user_id in allowed_ids


def extract_twitter_url(text: str) -> str | None:
    for pattern in TWITTER_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return None


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\n\r\t/]', "", name)
    name = re.sub(r'\.{2,}', "_", name)
    return name


def process_tweet_title(title: str) -> str:
    if not title:
        return "Video"

    title = re.sub(r"@[A-Za-z0-9_]+", "", title)
    title = re.sub(r"https?://\S+", "", title)
    title = re.sub(r"#[^\s#]+", "", title)
    title = re.sub(r"\s+", " ", title).strip()

    if not title:
        title = "Video"

    if len(title) > 30:
        title = title[:30].strip()

    return sanitize_filename(title)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *您好！我是您的 XDT 下载助手* 🎬\n\n"
        "发送 Twitter/X 链接给我，我就能为您下载视频！🚀",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *使用说明：*\n\n"
        "直接发送链接即可，例如：\n"
        "👉 `https://x.com/user/status/123456`\n\n"
        "📂 视频文件保存在服务端配置的下载目录（由管理员设置）。\n",
        parse_mode="Markdown",
    )


async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update.effective_user.id):
        return

    try:
        files = glob.glob(os.path.join(DOWNLOAD_PATH, "*"))
        video_files = [f for f in files if f.endswith((".mp4", ".webm", ".mkv", ".mov"))]

        if not video_files:
            await update.message.reply_text("📭 *空空如也！* 暂时还没有下载视频。")
            return

        try:
            video_files.sort(key=os.path.getmtime, reverse=True)
        except OSError:
            pass

        msg_list = []
        for f in video_files[:10]:
            name = os.path.basename(f)
            try:
                size_mb = os.path.getsize(f) / (1024 * 1024)
            except OSError:
                size_mb = 0.0
            msg_list.append(f"📹 `{name}` (*{size_mb:.1f} MB*)")

        await update.message.reply_text(
            f"📂 *最近下载：*\n\n" + "\n".join(msg_list),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("列出文件失败: %s", e)
        await update.message.reply_text("❌ *出错了！* 列出文件失败，请稍后重试。", parse_mode="Markdown")


def _progress_future_done(fut) -> None:
    try:
        exc = fut.exception()
        if exc:
            logger.debug("Progress message edit failed: %s", exc)
    except Exception:
        pass


class TelegramProgressHook:
    def __init__(self, status_msg, loop):
        self.status_msg = status_msg
        self.loop = loop
        self.last_update_time = 0
        self.last_percent = 0
        self.last_text = ""
        self.downloaded_files: list = []
        self._lock = threading.Lock()

    async def _safe_edit_progress(self, text: str) -> None:
        try:
            await self.status_msg.edit_text(text, parse_mode="Markdown")
        except Exception as e:
            logger.debug("Progress edit skipped: %s", e)

    def progress_hook(self, d):
        if d["status"] == "finished":
            filename = d.get("filename")
            if filename:
                with self._lock:
                    self.downloaded_files.append(os.path.basename(filename))
            return

        if d["status"] != "downloading":
            return

        now = time.time()
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes", 0)
        percent_raw = 0.0
        if total:
            percent_raw = downloaded / total * 100

        # 无锁快速过滤
        if not ((now - self.last_update_time > 2.5) or (percent_raw - self.last_percent > 5)):
            return

        percent_str = f"{percent_raw:.1f}%"
        bar_len = 10
        filled = int(percent_raw / 100 * bar_len)
        bar = "▰" * filled + "▱" * (bar_len - filled)

        speed = d.get("_speed_str", "N/A").strip().replace("KiB", "K").replace("MiB", "M")
        eta = d.get("_eta_str", "N/A").strip()
        if "Unknown" in eta:
            eta = "..."

        total_str = "N/A"
        if total:
            total_mb = total / (1024 * 1024)
            total_str = f"{total_mb:.1f}MB"

        playlist_index = d.get("playlist_index")
        n_entries = d.get("n_entries")
        playlist_info = ""
        if playlist_index and n_entries:
            playlist_info = f" `({playlist_index}/{n_entries})`"

        text = (
            f"📥 *下载中...*{playlist_info}\n"
            f"`{bar}` *{percent_str}*\n"
            f"📦 `{total_str}` | ⚡ `{speed}/s` | ⏳ `{eta}`"
        )

        if text == self.last_text:
            return

        # 持锁原子化"检查-更新"
        with self._lock:
            if not ((now - self.last_update_time > 2.5) or (percent_raw - self.last_percent > 5)):
                return
            if text == self.last_text:
                return
            self.last_update_time = now
            self.last_percent = percent_raw
            self.last_text = text

        fut = asyncio.run_coroutine_threadsafe(
            self._safe_edit_progress(text),
            self.loop,
        )
        fut.add_done_callback(_progress_future_done)


async def apply_success_cleanup(status_msg, recent_files: list, url: str) -> None:
    mode = CONFIG["BOT_CLEANUP_MODE"]
    logger.info("下载成功: %s -> %s", url, recent_files)

    if mode == "delete":
        try:
            await status_msg.delete()
        except Exception as e:
            logger.debug("Could not delete status message: %s", e)
            try:
                await status_msg.edit_text("✅ 下载已完成。", parse_mode=None)
            except Exception:
                pass
        return

    # minimal
    try:
        await status_msg.edit_text("✅ 下载已完成。", parse_mode=None)
    except Exception as e:
        logger.debug("Success message edit failed: %s", e)


async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update.effective_user.id):
        await update.message.reply_text("🚫 *访问拒绝* 您没有权限使用此机器人。")
        return

    text = update.message.text
    url = extract_twitter_url(text)

    if not url:
        return

    status_msg = await update.message.reply_text(
        "🔍 *正在解析链接...* 请稍候 ☕️",
        parse_mode="Markdown",
    )

    try:
        date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        loop = asyncio.get_running_loop()
        progress_tracker = TelegramProgressHook(status_msg, loop)

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "best[ext=mp4]/best",
            "noplaylist": False,
            "retries": 5,
            "progress_hooks": [progress_tracker.progress_hook],
        }

        if CONFIG["HTTP_PROXY"]:
            ydl_opts["proxy"] = CONFIG["HTTP_PROXY"]

        if os.path.exists(COOKIES_PATH):
            ydl_opts["cookiefile"] = COOKIES_PATH

        uploader = "Unknown"
        clean_title = "Video"

        def run_extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            info = await loop.run_in_executor(None, run_extract)
        except Exception as e:
            await status_msg.edit_text(f"❌ 获取信息失败:\n{e}", parse_mode=None)
            return

        uploader = sanitize_filename(info.get("uploader", "Unknown")) or "Unknown"
        raw_title = info.get("title", "")
        clean_title = process_tweet_title(raw_title)

        if uploader and clean_title.startswith(uploader):
            clean_title = clean_title[len(uploader) :].lstrip(" -_")

        if not clean_title:
            clean_title = "Video"

        is_playlist = "entries" in info and info["entries"] is not None
        suffix_part = "_%(playlist_index)s" if is_playlist else ""

        out_filename = (
            f"{uploader}_{clean_title}_{date_str}{suffix_part}.%(ext)s"
        )
        outtmpl_path = os.path.join(DOWNLOAD_PATH, out_filename)
        if not os.path.normpath(outtmpl_path).startswith(
            os.path.normpath(DOWNLOAD_PATH)
        ):
            logger.error("路径遍历检测：拒绝 outtmpl=%s", outtmpl_path)
            await status_msg.edit_text("❌ 处理请求时出错，请稍后重试。", parse_mode=None)
            return
        ydl_opts["outtmpl"] = outtmpl_path

        await status_msg.edit_text(
            f"⬇️ 开始下载...\n\n🎬 {clean_title}\n👤 {uploader}",
            parse_mode=None,
        )

        def run_download():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                return None
            except Exception as e:
                return e

        download_error = await loop.run_in_executor(None, run_download)

        if download_error is not None:
            logger.error("yt-dlp 下载错误: %s", download_error)
            await status_msg.edit_text("❌ 下载失败，请稍后重试。", parse_mode=None)
            return

        recent_files = progress_tracker.downloaded_files

        if recent_files:
            await apply_success_cleanup(status_msg, recent_files, url)
        else:
            await status_msg.edit_text(
                "⚠️ 下载流程结束，但未检测到新文件。(可能是覆盖了旧文件？)",
                parse_mode=None,
            )

    except Exception as e:
        logger.exception("下载错误: %s", e)
        try:
            await status_msg.edit_text(
                "❌ 处理请求时出错，请稍后重试。",
                parse_mode=None,
            )
        except Exception:
            pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception in handler", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ 处理请求时出错，请稍后重试。",
                parse_mode=None,
            )
        except Exception:
            pass


def main() -> None:
    if not CONFIG["BOT_TOKEN"]:
        print("❌ Error: set BOT_TOKEN in the environment.")
        sys.exit(1)
    if not CONFIG["DOWNLOAD_DIR"]:
        print("❌ Error: set DOWNLOAD_DIR in the environment.")
        sys.exit(1)

    if shutil.which("ffmpeg") is None:
        print("⚠️ Warning: 'ffmpeg' not found in PATH. Merging video+audio might fail.")

    print("🚀 Starting XDT Bot...")
    print(f"📂 Downloads: {DOWNLOAD_PATH}")
    print(f"🍪 Cookies: {'Found' if os.path.exists(COOKIES_PATH) else 'Not Found (Optional)'}")
    print(f"🌐 Proxy: {CONFIG['HTTP_PROXY'] or 'None'}")
    print(f"🧹 BOT_CLEANUP_MODE: {CONFIG['BOT_CLEANUP_MODE']}")

    builder = Application.builder().token(CONFIG["BOT_TOKEN"])

    if CONFIG["HTTP_PROXY"]:
        builder = builder.proxy(CONFIG["HTTP_PROXY"]).get_updates_proxy(
            CONFIG["HTTP_PROXY"]
        )

    app = builder.build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("list", list_files))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
