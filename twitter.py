import asyncio
import glob
import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from curl_cffi import requests as cffi_requests
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
        "DOWNLOAD_DIR": _env("DOWNLOAD_DIR", "/data/downloads"),
        "COOKIES_FILE": _env("COOKIES_FILE", "/secrets/cookies.txt"),
        "BOT_CLEANUP_MODE": cleanup,
        "LOG_FILE": _env("LOG_FILE", "/data/logs/bot.log"),
        "AUTO_LIKES_ENABLED": _env_bool("AUTO_LIKES_ENABLED", False),
        "AUTO_LIKES_TARGET_USER": _env("AUTO_LIKES_TARGET_USER"),
        "AUTO_LIKES_POLL_INTERVAL": max(_env_int("AUTO_LIKES_POLL_INTERVAL", 300), 30),
        "AUTO_LIKES_MAX_PER_ROUND": max(_env_int("AUTO_LIKES_MAX_PER_ROUND", 20), 1),
        "AUTO_LIKES_QUEUE_MAXSIZE": max(_env_int("AUTO_LIKES_QUEUE_MAXSIZE", 200), 10),
        "AUTO_LIKES_WORKERS": max(_env_int("AUTO_LIKES_WORKERS", 2), 1),
        "AUTO_LIKES_RETRY_MAX": max(_env_int("AUTO_LIKES_RETRY_MAX", 3), 0),
        "AUTO_LIKES_RETRY_BACKOFF_SEC": max(_env_int("AUTO_LIKES_RETRY_BACKOFF_SEC", 30), 1),
        "SQLITE_PATH": _env("SQLITE_PATH", "/data/state/bot.db"),
        "LIKES_GRAPHQL_QUERY_ID": _env("LIKES_GRAPHQL_QUERY_ID", "RozQdCp4CilQzrcuU0NY5w"),
        "USER_GRAPHQL_QUERY_ID": _env("USER_GRAPHQL_QUERY_ID", "IGgvgiOx4QZndDHuD3x9TQ"),
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


X_BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"

_KNOWN_FALSE_FEATURES = frozenset({
    "verified_phone_label_enabled",
    "rweb_video_screen_enabled",
    "responsive_web_profile_redirect_enabled",
    "rweb_tipjar_consumption_enabled",
    "premium_content_api_read_enabled",
    "responsive_web_grok_analyze_button_fetch_trends_enabled",
    "responsive_web_grok_analyze_post_followups_enabled",
    "responsive_web_grok_show_grok_translated_post",
    "responsive_web_grok_community_note_auto_translation_is_enabled",
    "tweet_awards_web_tipping_enabled",
    "post_ctas_fetch_enabled",
    "longform_notetweets_inline_media_enabled",
    "responsive_web_enhance_cards_enabled",
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled",
})


def _auto_discover_graphql() -> dict:
    """从 X 前端 JS bundle 中提取 GraphQL 端点的 queryId 和 featureSwitches。"""
    targets = {"Likes", "UserByScreenName"}
    result: dict[str, dict] = {}

    sess = cffi_requests.Session(impersonate="chrome")
    if CONFIG["HTTP_PROXY"]:
        sess.proxies = {"https": CONFIG["HTTP_PROXY"], "http": CONFIG["HTTP_PROXY"]}

    try:
        resp = sess.get("https://x.com", timeout=20, allow_redirects=True)
        html = resp.text
    except Exception as exc:
        logger.warning("自动发现: 无法访问 x.com: %s", exc)
        return result

    js_urls = re.findall(r'src="(https://abs\.twimg\.com/responsive-web/client-web[^"]*\.js)"', html)
    if not js_urls:
        js_urls = re.findall(r'src="(https://abs\.twimg\.com/[^"]*\.js)"', html)
    if not js_urls:
        logger.warning("自动发现: HTML 中未找到 JS bundle URL")
        return result

    logger.info("自动发现: 找到 %d 个 JS bundle，开始扫描...", len(js_urls))

    endpoint_pattern = re.compile(
        r'\{queryId:"([^"]{10,40})",operationName:"([^"]+)",operationType:"\w+"'
        r',metadata:\{featureSwitches:\[([^\]]*)\]'
    )
    qid_only_pattern = re.compile(
        r'queryId:"([^"]{10,40})",operationName:"(Likes|UserByScreenName)"'
    )

    for url in js_urls:
        if len(result) >= len(targets):
            break
        try:
            js_text = sess.get(url, timeout=15).text
        except Exception:
            continue

        for m in endpoint_pattern.finditer(js_text):
            qid, op_name, features_raw = m.group(1), m.group(2), m.group(3)
            if op_name in targets and op_name not in result:
                feature_keys = [k.strip().strip('"').strip("'") for k in features_raw.split(",") if k.strip()]
                features = {k: (k not in _KNOWN_FALSE_FEATURES) for k in feature_keys}
                result[op_name] = {"queryId": qid, "features": features}
                logger.info("自动发现: %s -> queryId=%s (%d features)", op_name, qid, len(features))

        if len(result) < len(targets):
            for m in qid_only_pattern.finditer(js_text):
                qid, op_name = m.group(1), m.group(2)
                if op_name not in result:
                    result[op_name] = {"queryId": qid}
                    logger.info("自动发现: %s -> queryId=%s (features 使用默认)", op_name, qid)

    sess.close()
    return result


GRAPHQL_FEATURES = {
    "rweb_video_screen_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": False,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "content_disclosure_indicator_enabled": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_enhance_cards_enabled": False,
}

_cached_user_id: dict[str, str] = {}


def _parse_cookies_from_netscape(cookie_path: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    try:
        with open(cookie_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain = parts[0].lower().lstrip(".")
                name, value = parts[5], parts[6]
                if domain in ("twitter.com", "x.com") or domain.endswith(".twitter.com") or domain.endswith(".x.com"):
                    cookies[name] = value
    except OSError:
        pass
    return cookies


def _build_graphql_session(cookies: dict[str, str]) -> cffi_requests.Session:
    ct0 = cookies.get("ct0", "")
    sess = cffi_requests.Session(impersonate="chrome")
    sess.headers.update({
        "authorization": f"Bearer {urllib.parse.unquote(X_BEARER_TOKEN)}",
        "x-csrf-token": ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "content-type": "application/json",
    })
    for name, value in cookies.items():
        sess.cookies.set(name, value, domain=".x.com")
    if CONFIG["HTTP_PROXY"]:
        sess.proxies = {"https": CONFIG["HTTP_PROXY"], "http": CONFIG["HTTP_PROXY"]}
    return sess


def _resolve_user_id(sess: cffi_requests.Session, screen_name: str) -> str | None:
    if screen_name in _cached_user_id:
        return _cached_user_id[screen_name]
    query_id = CONFIG["USER_GRAPHQL_QUERY_ID"]
    variables = json.dumps({"screen_name": screen_name, "withSafetyModeUserFields": True})
    features = json.dumps({
        "hidden_profile_subscriptions_enabled": True,
        "profile_label_improvements_pcf_label_in_post_enabled": True,
        "responsive_web_profile_redirect_enabled": False,
        "rweb_tipjar_consumption_enabled": False,
        "verified_phone_label_enabled": False,
        "subscriptions_verification_info_is_identity_verified_enabled": True,
        "subscriptions_verification_info_verified_since_enabled": True,
        "highlights_tweets_tab_ui_enabled": True,
        "responsive_web_twitter_article_notes_tab_enabled": True,
        "subscriptions_feature_can_gift_premium": True,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "responsive_web_graphql_timeline_navigation_enabled": True,
    })
    url = f"https://x.com/i/api/graphql/{query_id}/UserByScreenName?variables={urllib.parse.quote(variables)}&features={urllib.parse.quote(features)}"
    try:
        resp = sess.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        uid = data["data"]["user"]["result"]["rest_id"]
        _cached_user_id[screen_name] = uid
        logger.info("已解析 @%s -> userId=%s", screen_name, uid)
        return uid
    except Exception as exc:
        logger.error("解析 userId 失败 (@%s): %s", screen_name, exc)
        return None


def _extract_cursor_bottom(entries: list[dict]) -> str | None:
    for entry in reversed(entries):
        entry_id = entry.get("entryId", "")
        if entry_id.startswith("cursor-bottom-"):
            return entry.get("content", {}).get("value")
    return None


def _parse_tweet_entries(entries: list[dict]) -> list[str]:
    urls: list[str] = []
    for entry in entries:
        try:
            tweet_result = (
                entry.get("content", {})
                .get("itemContent", {})
                .get("tweet_results", {})
                .get("result", {})
            )
            if not tweet_result:
                continue
            tweet_id = tweet_result.get("rest_id")
            if not tweet_id:
                continue
            screen_name = (
                tweet_result.get("core", {})
                .get("user_results", {})
                .get("result", {})
                .get("legacy", {})
                .get("screen_name", "")
            )
            if screen_name:
                urls.append(f"https://x.com/{screen_name}/status/{tweet_id}")
            else:
                urls.append(f"https://x.com/i/status/{tweet_id}")
        except Exception:
            continue
    return urls


def _fetch_likes_graphql(sess: cffi_requests.Session, user_id: str, count: int) -> list[str]:
    query_id = CONFIG["LIKES_GRAPHQL_QUERY_ID"]
    features_str = json.dumps(GRAPHQL_FEATURES)
    all_urls: list[str] = []
    cursor: str | None = None
    page = 0
    max_pages = max(count // 20, 1) + 2

    while len(all_urls) < count and page < max_pages:
        page += 1
        variables: dict = {
            "userId": user_id,
            "count": min(count - len(all_urls), 100),
            "includePromotedContent": False,
        }
        if cursor:
            variables["cursor"] = cursor

        url = (
            f"https://x.com/i/api/graphql/{query_id}/Likes"
            f"?variables={urllib.parse.quote(json.dumps(variables))}"
            f"&features={urllib.parse.quote(features_str)}"
        )
        resp = sess.get(url, timeout=30)
        if resp.status_code in (401, 403):
            logger.error("GraphQL Likes 返回 %s: cookies 可能已过期，请重新导出。", resp.status_code)
            break
        if resp.status_code == 429:
            logger.warning("GraphQL Likes 触发限流 (429)，本轮跳过。")
            break
        resp.raise_for_status()
        data = resp.json()

        entries: list[dict] = []
        for instruction in (
            data.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        ):
            entries.extend(instruction.get("entries", []))

        page_urls = _parse_tweet_entries(entries)
        if not page_urls:
            break
        all_urls.extend(page_urls)
        logger.info("GraphQL Likes 第 %d 页获取 %d 条 (累计 %d)", page, len(page_urls), len(all_urls))

        new_cursor = _extract_cursor_bottom(entries)
        if not new_cursor or new_cursor == cursor:
            break
        cursor = new_cursor

    return all_urls


def fetch_liked_urls_sync(target_user: str, max_items: int) -> list[str]:
    if not os.path.isfile(COOKIES_PATH):
        logger.warning("未找到 cookies 文件: %s", COOKIES_PATH)
        return []
    cookies = _parse_cookies_from_netscape(COOKIES_PATH)
    if "auth_token" not in cookies or "ct0" not in cookies:
        logger.warning("cookies 中缺少 auth_token 或 ct0，无法调用 GraphQL API。")
        return []

    sess = _build_graphql_session(cookies)
    try:
        user_id = _resolve_user_id(sess, target_user)
        if not user_id:
            return []
        urls = _fetch_likes_graphql(sess, user_id, max_items)
        if urls:
            logger.info("GraphQL 获取到 %d 条点赞", len(urls))
        else:
            logger.warning("GraphQL 点赞列表为空（可能无新点赞或 queryId 已失效，当前: %s）。", CONFIG["LIKES_GRAPHQL_QUERY_ID"])
        return urls[:max_items]
    except Exception as exc:
        logger.error("GraphQL 点赞抓取失败: %s", exc)
        return []
    finally:
        sess.close()


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


def _apply_auto_discover() -> None:
    global GRAPHQL_FEATURES
    try:
        discovered = _auto_discover_graphql()
    except Exception as exc:
        logger.warning("自动发现 GraphQL 配置失败: %s，使用默认值", exc)
        return

    if "Likes" in discovered:
        CONFIG["LIKES_GRAPHQL_QUERY_ID"] = discovered["Likes"]["queryId"]
        if "features" in discovered["Likes"]:
            GRAPHQL_FEATURES = discovered["Likes"]["features"]
        logger.info("Likes queryId 已更新为: %s", CONFIG["LIKES_GRAPHQL_QUERY_ID"])
    else:
        logger.warning("自动发现未找到 Likes 端点，使用默认 queryId: %s", CONFIG["LIKES_GRAPHQL_QUERY_ID"])

    if "UserByScreenName" in discovered:
        CONFIG["USER_GRAPHQL_QUERY_ID"] = discovered["UserByScreenName"]["queryId"]
        logger.info("UserByScreenName queryId 已更新为: %s", CONFIG["USER_GRAPHQL_QUERY_ID"])
    else:
        logger.warning("自动发现未找到 UserByScreenName 端点，使用默认 queryId: %s", CONFIG["USER_GRAPHQL_QUERY_ID"])


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

    if CONFIG["AUTO_LIKES_ENABLED"]:
        _apply_auto_discover()

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
