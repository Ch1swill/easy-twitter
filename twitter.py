import asyncio
import glob
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone

import httpx
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TWITTER_PATTERNS = [
    r"https?://(?:www\.)?twitter\.com/\w+/status/\d+",
    r"https?://(?:www\.)?x\.com/\w+/status/\d+",
    r"https?://(?:mobile\.)?twitter\.com/\w+/status/\d+",
]
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def load_config() -> dict:
    cleanup = _env("BOT_CLEANUP_MODE", "minimal").lower()
    if cleanup not in ("minimal", "delete"):
        cleanup = "minimal"
    return {
        "BOT_TOKEN": _env("BOT_TOKEN"),
        "HTTP_PROXY": _env("HTTP_PROXY") or _env("HTTPS_PROXY"),
        "ALLOWED_USERS": _env("ALLOWED_USERS"),
        "DOWNLOAD_DIR": _env("DOWNLOAD_DIR"),
        "COOKIES_FILE": _env("COOKIES_FILE", "cookies.txt"),
        "BOT_CLEANUP_MODE": cleanup,
        "LOG_FILE": _env("LOG_FILE"),
        "AUTO_LIKES_ENABLED": _env_bool("AUTO_LIKES_ENABLED", False),
        "AUTO_LIKES_TARGET_USER": _env("AUTO_LIKES_TARGET_USER"),
        "AUTO_LIKES_POLL_INTERVAL": max(_env_int("AUTO_LIKES_POLL_INTERVAL", 300), 30),
        "AUTO_LIKES_MAX_PER_ROUND": max(_env_int("AUTO_LIKES_MAX_PER_ROUND", 20), 1),
        "AUTO_LIKES_QUEUE_MAXSIZE": max(_env_int("AUTO_LIKES_QUEUE_MAXSIZE", 200), 10),
        "AUTO_LIKES_WORKERS": max(_env_int("AUTO_LIKES_WORKERS", 2), 1),
        "AUTO_LIKES_RETRY_MAX": max(_env_int("AUTO_LIKES_RETRY_MAX", 3), 0),
        "AUTO_LIKES_RETRY_BACKOFF_SEC": max(_env_int("AUTO_LIKES_RETRY_BACKOFF_SEC", 30), 1),
        "SQLITE_PATH": _env("SQLITE_PATH", "/data/state/bot.db"),
    }


CONFIG = load_config()
DOWNLOAD_PATH = CONFIG["DOWNLOAD_DIR"] if os.path.isabs(CONFIG["DOWNLOAD_DIR"]) else os.path.join(BASE_DIR, CONFIG["DOWNLOAD_DIR"])
COOKIES_PATH = CONFIG["COOKIES_FILE"] if os.path.isabs(CONFIG["COOKIES_FILE"]) else os.path.join(BASE_DIR, CONFIG["COOKIES_FILE"])
SQLITE_PATH = CONFIG["SQLITE_PATH"] if os.path.isabs(CONFIG["SQLITE_PATH"]) else os.path.join(BASE_DIR, CONFIG["SQLITE_PATH"])
os.makedirs(DOWNLOAD_PATH, exist_ok=True)
os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)

_handlers: list = [logging.StreamHandler(sys.stdout)]
if CONFIG["LOG_FILE"]:
    os.makedirs(os.path.dirname(CONFIG["LOG_FILE"]), exist_ok=True)
    _handlers.append(logging.handlers.RotatingFileHandler(CONFIG["LOG_FILE"], maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"))
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO, handlers=_handlers)
logger = logging.getLogger("XDT-Bot")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\n\r\t/]', "", name)
    return re.sub(r"\.{2,}", "_", name)


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


def extract_twitter_url(text: str) -> str | None:
    for pattern in TWITTER_PATTERNS:
        matched = re.search(pattern, text)
        if matched:
            return matched.group(0)
    return None


def extract_tweet_id(url: str) -> str | None:
    m = re.search(r"/status/(\d+)", url)
    return m.group(1) if m else None


def is_allowed_user(user_id: int) -> bool:
    if not CONFIG["ALLOWED_USERS"]:
        return True
    ids = []
    for raw in CONFIG["ALLOWED_USERS"].split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ids.append(int(raw))
        except ValueError:
            logger.warning("ALLOWED_USERS contains invalid user id: %s", raw)
    return user_id in ids


class BotDatabase:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.init_schema()

    def init_schema(self) -> None:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS liked_items (
                    tweet_id TEXT PRIMARY KEY,
                    tweet_url TEXT NOT NULL,
                    liked_at TEXT,
                    discovered_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS download_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tweet_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tweet_id, status)
                )
                """
            )
            self.conn.commit()

    def upsert_liked_item(self, tweet_id: str, tweet_url: str, liked_at: str | None = None) -> bool:
        ts = now_iso()
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("INSERT OR IGNORE INTO liked_items(tweet_id, tweet_url, liked_at, discovered_at, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (tweet_id, tweet_url, liked_at, ts, STATUS_PENDING, ts, ts))
            inserted = cur.rowcount > 0
            if not inserted:
                cur.execute("UPDATE liked_items SET updated_at=? WHERE tweet_id=?", (ts, tweet_id))
            self.conn.commit()
            return inserted

    def enqueue_job_if_needed(self, tweet_id: str) -> bool:
        ts = now_iso()
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT status FROM liked_items WHERE tweet_id=?", (tweet_id,))
            row = cur.fetchone()
            if not row or row["status"] == STATUS_DONE:
                return False
            cur.execute("SELECT 1 FROM download_jobs WHERE tweet_id=? AND status IN (?, ?)", (tweet_id, STATUS_PENDING, STATUS_RUNNING))
            if cur.fetchone():
                return False
            cur.execute("INSERT INTO download_jobs(tweet_id, status, attempts, created_at, updated_at) VALUES (?, ?, 0, ?, ?)", (tweet_id, STATUS_PENDING, ts, ts))
            self.conn.commit()
            return True

    def mark_job_running(self, tweet_id: str) -> int:
        ts = now_iso()
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT id, attempts FROM download_jobs WHERE tweet_id=? AND status=? ORDER BY id ASC LIMIT 1", (tweet_id, STATUS_PENDING))
            row = cur.fetchone()
            if not row:
                return 0
            attempts = int(row["attempts"]) + 1
            cur.execute("UPDATE download_jobs SET status=?, attempts=?, updated_at=? WHERE id=?", (STATUS_RUNNING, attempts, ts, row["id"]))
            cur.execute("UPDATE liked_items SET status=?, updated_at=? WHERE tweet_id=?", (STATUS_RUNNING, ts, tweet_id))
            self.conn.commit()
            return attempts

    def mark_job_success(self, tweet_id: str) -> None:
        ts = now_iso()
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("UPDATE download_jobs SET status=?, updated_at=?, error_message=NULL WHERE tweet_id=? AND status IN (?, ?)", (STATUS_DONE, ts, tweet_id, STATUS_PENDING, STATUS_RUNNING))
            cur.execute("UPDATE liked_items SET status=?, updated_at=?, error_message=NULL WHERE tweet_id=?", (STATUS_DONE, ts, tweet_id))
            self.conn.commit()

    def mark_job_failed(self, tweet_id: str, err: str, should_retry: bool, backoff_sec: int) -> None:
        ts = now_iso()
        with self.lock:
            cur = self.conn.cursor()
            if should_retry:
                next_retry = datetime.fromtimestamp(time.time() + backoff_sec, timezone.utc).isoformat()
                cur.execute("UPDATE download_jobs SET status=?, next_retry_at=?, error_message=?, updated_at=? WHERE tweet_id=? AND status=?", (STATUS_PENDING, next_retry, err[:500], ts, tweet_id, STATUS_RUNNING))
                cur.execute("UPDATE liked_items SET status=?, error_message=?, updated_at=? WHERE tweet_id=?", (STATUS_PENDING, err[:500], ts, tweet_id))
            else:
                cur.execute("UPDATE download_jobs SET status=?, error_message=?, updated_at=? WHERE tweet_id=? AND status=?", (STATUS_FAILED, err[:500], ts, tweet_id, STATUS_RUNNING))
                cur.execute("UPDATE liked_items SET status=?, error_message=?, updated_at=? WHERE tweet_id=?", (STATUS_FAILED, err[:500], ts, tweet_id))
            self.conn.commit()

    def get_tweet_url(self, tweet_id: str) -> str | None:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT tweet_url FROM liked_items WHERE tweet_id=?", (tweet_id,))
            row = cur.fetchone()
            return row["tweet_url"] if row else None

    def get_pending_retry_job_ids(self, limit: int) -> list[str]:
        ts = now_iso()
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT tweet_id FROM download_jobs WHERE status=? AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY id ASC LIMIT ?", (STATUS_PENDING, ts, limit))
            return [r["tweet_id"] for r in cur.fetchall()]

    def summary(self) -> dict:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT status, COUNT(*) AS c FROM liked_items GROUP BY status")
            data = {row["status"]: row["c"] for row in cur.fetchall()}
            return {
                "pending": data.get(STATUS_PENDING, 0),
                "running": data.get(STATUS_RUNNING, 0),
                "done": data.get(STATUS_DONE, 0),
                "failed": data.get(STATUS_FAILED, 0),
            }


class RuntimeState:
    def __init__(self, db: BotDatabase):
        self.db = db
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=CONFIG["AUTO_LIKES_QUEUE_MAXSIZE"])
        self.worker_tasks: list[asyncio.Task] = []
        self.poller_task: asyncio.Task | None = None
        self.auto_paused = False
        self.last_poll_at = ""
        self.last_poll_error = ""


RUNTIME = RuntimeState(BotDatabase(SQLITE_PATH))


def _progress_future_done(fut) -> None:
    try:
        _ = fut.exception()
    except Exception:
        pass


class TelegramProgressHook:
    def __init__(self, status_msg, loop):
        self.status_msg = status_msg
        self.loop = loop
        self.last_update_time = 0.0
        self.last_percent = 0.0
        self.last_text = ""
        self.downloaded_files: list[str] = []

    async def _safe_edit(self, text: str) -> None:
        if self.status_msg is None:
            return
        try:
            await self.status_msg.edit_text(text, parse_mode="Markdown")
        except Exception:
            return

    def progress_hook(self, d: dict) -> None:
        if d["status"] == "finished":
            if d.get("filename"):
                self.downloaded_files.append(os.path.basename(d["filename"]))
            return
        if d["status"] != "downloading":
            return
        now = time.time()
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes", 0)
        percent_raw = (downloaded / total * 100) if total else 0.0
        if not ((now - self.last_update_time > 2.5) or (percent_raw - self.last_percent > 5)):
            return
        text = f"📥 *下载中...*\n`{percent_raw:.1f}%`"
        if text == self.last_text:
            return
        self.last_update_time = now
        self.last_percent = percent_raw
        self.last_text = text
        fut = asyncio.run_coroutine_threadsafe(self._safe_edit(text), self.loop)
        fut.add_done_callback(_progress_future_done)


async def apply_success_cleanup(status_msg, recent_files: list[str], url: str) -> None:
    logger.info("下载成功: %s -> %s", url, recent_files)
    if status_msg is None:
        return
    if CONFIG["BOT_CLEANUP_MODE"] == "delete":
        try:
            await status_msg.delete()
            return
        except Exception:
            pass
    try:
        await status_msg.edit_text("✅ 下载已完成。", parse_mode=None)
    except Exception:
        pass


def build_ydl_opts(progress_hook) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "best[ext=mp4]/best",
        "noplaylist": False,
        "retries": 5,
        "progress_hooks": [progress_hook] if progress_hook else [],
    }
    if CONFIG["HTTP_PROXY"]:
        opts["proxy"] = CONFIG["HTTP_PROXY"]
    if os.path.isfile(COOKIES_PATH):
        opts["cookiefile"] = COOKIES_PATH
    return opts


async def download_tweet_url(url: str, source: str, status_msg=None) -> tuple[bool, str]:
    loop = asyncio.get_running_loop()
    progress = TelegramProgressHook(status_msg, loop)
    ydl_opts = build_ydl_opts(progress.progress_hook)
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    def run_extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await loop.run_in_executor(None, run_extract)
    except Exception as exc:
        return False, f"extract_failed: {exc}"

    uploader = sanitize_filename(info.get("uploader", "Unknown")) or "Unknown"
    title = process_tweet_title(info.get("title", ""))
    is_playlist = "entries" in info and info["entries"] is not None
    suffix_part = "_%(playlist_index)s" if is_playlist else ""
    out_filename = f"{uploader}_{title}_{date_str}{suffix_part}.%(ext)s"
    outtmpl_path = os.path.join(DOWNLOAD_PATH, out_filename)
    ydl_opts["outtmpl"] = outtmpl_path

    if status_msg is not None:
        try:
            await status_msg.edit_text(f"⬇️ 开始下载...\n\n🎬 {title}\n👤 {uploader}", parse_mode=None)
        except Exception:
            pass

    def run_download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    try:
        await loop.run_in_executor(None, run_download)
    except Exception as exc:
        return False, f"download_failed: {exc}"

    if progress.downloaded_files and status_msg is not None:
        await apply_success_cleanup(status_msg, progress.downloaded_files, url)
    logger.info("下载完成 source=%s url=%s", source, url)
    return True, "ok"


def _netscape_cookie_header(cookie_path: str) -> str:
    pairs: list[str] = []
    try:
        with open(cookie_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _flag, _path, _secure, _expiry, name, value = parts[:7]
                d = domain.lower().lstrip(".")
                if d in ("twitter.com", "x.com") or d.endswith(".twitter.com") or d.endswith(".x.com"):
                    pairs.append(f"{name}={value}")
    except OSError:
        return ""
    return "; ".join(pairs)


def fetch_liked_urls_sync(target_user: str, max_items: int) -> list[str]:
    """仅用 httpx 拉取点赞页 HTML，正则提取推文链接；下载仍由 yt-dlp 处理单条 status URL。"""
    if not os.path.isfile(COOKIES_PATH):
        logger.warning("未找到 cookies 文件，无法抓取点赞页: %s", COOKIES_PATH)
        return []
    header = _netscape_cookie_header(COOKIES_PATH)
    if not header:
        logger.warning("cookies 文件中未解析到 twitter/x 域的条目: %s", COOKIES_PATH)
        return []

    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cookie": header,
    }
    pattern = re.compile(
        r"https://(?:twitter\.com|x\.com)/[A-Za-z0-9_]{1,30}/status/(\d+)",
        re.IGNORECASE,
    )
    seen: set[str] = set()
    out: list[str] = []
    proxy = CONFIG["HTTP_PROXY"] or None

    for page_url in (
        f"https://twitter.com/{target_user}/likes",
        f"https://x.com/{target_user}/likes",
    ):
        if len(out) >= max_items:
            break
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True, proxy=proxy) as client:
                resp = client.get(page_url, headers=req_headers)
                resp.raise_for_status()
                text = resp.text
        except Exception as exc:
            logger.warning("HTTP 抓取点赞页失败 %s: %s", page_url, exc)
            continue

        for m in pattern.finditer(text):
            tid = m.group(1)
            if tid in seen:
                continue
            seen.add(tid)
            out.append(m.group(0))
            if len(out) >= max_items:
                break

    if not out:
        logger.warning(
            "点赞页未解析到任何 status 链接（页面可能为纯前端渲染）。可检查 cookies 或改用手动发链接。"
        )
    return out


async def likes_poller_loop() -> None:
    while True:
        try:
            if RUNTIME.auto_paused or not CONFIG["AUTO_LIKES_ENABLED"] or not CONFIG["AUTO_LIKES_TARGET_USER"]:
                await asyncio.sleep(5)
                continue
            RUNTIME.last_poll_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            liked_urls = await asyncio.get_running_loop().run_in_executor(None, fetch_liked_urls_sync, CONFIG["AUTO_LIKES_TARGET_USER"], CONFIG["AUTO_LIKES_MAX_PER_ROUND"])
            for url in liked_urls:
                tweet_id = extract_tweet_id(url)
                if not tweet_id:
                    continue
                inserted = RUNTIME.db.upsert_liked_item(tweet_id=tweet_id, tweet_url=url)
                queued = inserted and RUNTIME.db.enqueue_job_if_needed(tweet_id)
                if queued and not RUNTIME.queue.full():
                    await RUNTIME.queue.put(tweet_id)
            # 补偿重试任务
            for tweet_id in RUNTIME.db.get_pending_retry_job_ids(10):
                if RUNTIME.queue.full():
                    break
                await RUNTIME.queue.put(tweet_id)
            RUNTIME.last_poll_error = ""
        except Exception as exc:
            RUNTIME.last_poll_error = str(exc)
            logger.error("点赞轮询失败: %s", exc)
        await asyncio.sleep(CONFIG["AUTO_LIKES_POLL_INTERVAL"])


async def worker_loop(worker_name: str) -> None:
    while True:
        tweet_id = await RUNTIME.queue.get()
        try:
            attempts = RUNTIME.db.mark_job_running(tweet_id)
            if attempts == 0:
                continue
            tweet_url = RUNTIME.db.get_tweet_url(tweet_id)
            if not tweet_url:
                RUNTIME.db.mark_job_failed(tweet_id, "missing tweet_url", False, 0)
                continue
            ok, msg = await download_tweet_url(tweet_url, source=f"auto:{worker_name}", status_msg=None)
            if ok:
                RUNTIME.db.mark_job_success(tweet_id)
            else:
                should_retry = attempts <= CONFIG["AUTO_LIKES_RETRY_MAX"]
                backoff = CONFIG["AUTO_LIKES_RETRY_BACKOFF_SEC"] * attempts
                RUNTIME.db.mark_job_failed(tweet_id, msg, should_retry, backoff)
                if should_retry and not RUNTIME.queue.full():
                    await asyncio.sleep(backoff)
                    await RUNTIME.queue.put(tweet_id)
        except Exception as exc:
            logger.exception("worker %s 崩溃: %s", worker_name, exc)
        finally:
            RUNTIME.queue.task_done()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("👋 您好！发送 X/Twitter 链接即可下载。", parse_mode=None)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("发送链接下载视频。\n/list 查看最近下载。\n/autostatus 查看自动任务状态。", parse_mode=None)


async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update.effective_user.id):
        return
    files = glob.glob(os.path.join(DOWNLOAD_PATH, "*"))
    video_files = [f for f in files if f.endswith((".mp4", ".webm", ".mkv", ".mov"))]
    if not video_files:
        await update.message.reply_text("暂无下载文件。", parse_mode=None)
        return
    video_files.sort(key=os.path.getmtime, reverse=True)
    lines = []
    for f in video_files[:10]:
        size_mb = os.path.getsize(f) / (1024 * 1024)
        lines.append(f"• {os.path.basename(f)} ({size_mb:.1f} MB)")
    await update.message.reply_text("最近下载:\n" + "\n".join(lines), parse_mode=None)


async def autostatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update.effective_user.id):
        return
    s = RUNTIME.db.summary()
    await update.message.reply_text(
        (
            f"AUTO_LIKES_ENABLED={CONFIG['AUTO_LIKES_ENABLED']}\n"
            f"TARGET={CONFIG['AUTO_LIKES_TARGET_USER'] or '-'}\n"
            f"POLL_INTERVAL={CONFIG['AUTO_LIKES_POLL_INTERVAL']}s\n"
            f"QUEUE={RUNTIME.queue.qsize()}/{CONFIG['AUTO_LIKES_QUEUE_MAXSIZE']}\n"
            f"paused={RUNTIME.auto_paused}\n"
            f"last_poll_at={RUNTIME.last_poll_at or '-'}\n"
            f"last_poll_error={RUNTIME.last_poll_error or '-'}\n"
            f"jobs pending={s['pending']} running={s['running']} done={s['done']} failed={s['failed']}"
        ),
        parse_mode=None,
    )


async def autopause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    RUNTIME.auto_paused = True
    await update.message.reply_text("自动点赞下载已暂停。", parse_mode=None)


async def autoresume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    RUNTIME.auto_paused = False
    await update.message.reply_text("自动点赞下载已恢复。", parse_mode=None)


async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update.effective_user.id):
        await update.message.reply_text("访问拒绝。", parse_mode=None)
        return
    text = update.message.text or ""
    url = extract_twitter_url(text)
    if not url:
        return
    status_msg = await update.message.reply_text("正在处理链接...", parse_mode=None)
    ok, msg = await download_tweet_url(url, source="manual", status_msg=status_msg)
    if not ok:
        await status_msg.edit_text(f"下载失败: {msg}", parse_mode=None)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception in handler", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("处理请求时出错，请稍后重试。", parse_mode=None)
        except Exception:
            pass


async def on_startup(app: Application) -> None:
    if CONFIG["AUTO_LIKES_ENABLED"]:
        RUNTIME.poller_task = asyncio.create_task(likes_poller_loop(), name="likes_poller")
        for i in range(CONFIG["AUTO_LIKES_WORKERS"]):
            RUNTIME.worker_tasks.append(asyncio.create_task(worker_loop(f"worker-{i+1}"), name=f"download_worker_{i+1}"))
        logger.info("自动点赞下载已启动: workers=%s interval=%ss", CONFIG["AUTO_LIKES_WORKERS"], CONFIG["AUTO_LIKES_POLL_INTERVAL"])


async def on_shutdown(app: Application) -> None:
    tasks = [t for t in [RUNTIME.poller_task, *RUNTIME.worker_tasks] if t is not None]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    if not CONFIG["BOT_TOKEN"]:
        print("Error: set BOT_TOKEN in environment.")
        sys.exit(1)
    if not CONFIG["DOWNLOAD_DIR"]:
        print("Error: set DOWNLOAD_DIR in environment.")
        sys.exit(1)
    if shutil.which("ffmpeg") is None:
        print("Warning: ffmpeg not found in PATH. Merge may fail.")

    print("Starting XDT Bot...")
    print(f"Downloads: {DOWNLOAD_PATH}")
    print(f"Cookies: {'Found' if os.path.exists(COOKIES_PATH) else 'Not Found (Optional)'}")
    print(f"SQLite: {SQLITE_PATH}")
    print(f"Auto likes: {CONFIG['AUTO_LIKES_ENABLED']} target={CONFIG['AUTO_LIKES_TARGET_USER'] or '-'}")

    builder = Application.builder().token(CONFIG["BOT_TOKEN"]).post_init(on_startup).post_shutdown(on_shutdown)
    if CONFIG["HTTP_PROXY"]:
        builder = builder.proxy(CONFIG["HTTP_PROXY"]).get_updates_proxy(CONFIG["HTTP_PROXY"])
    app = builder.build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("list", list_files))
    app.add_handler(CommandHandler("autostatus", autostatus))
    app.add_handler(CommandHandler("autopause", autopause))
    app.add_handler(CommandHandler("autoresume", autoresume))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
