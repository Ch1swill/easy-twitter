# CLAUDE.md
始终用中文对话
This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A Telegram bot that downloads videos from Twitter/X links and saves them locally. The entire application is a single file: [twitter.py](twitter.py).

## Running & Deployment

```bash
# Setup
cp .env.example .env
# Edit .env to configure BOT_TOKEN, DOWNLOAD_DIR, and optional settings

# Start (recommended)
docker-compose up -d

# Logs
docker-compose logs -f xdt-bot

# Local dev (no Docker)
pip install -r requirements.txt
BOT_TOKEN=your_token DOWNLOAD_DIR=./downloads python twitter.py
```

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Required | Notes |
|---|---|---|
| `BOT_TOKEN` | Yes | Telegram Bot API token |
| `DOWNLOAD_DIR` | Yes | Path where videos are saved |
| `ALLOWED_USERS` | No | Comma-separated Telegram user IDs; empty = allow all |
| `HTTP_PROXY` / `HTTPS_PROXY` | No | Applied to both Telegram API and yt-dlp |
| `COOKIES_FILE` | No | Netscape-format cookies for authenticated X/Twitter access |
| `BOT_CLEANUP_MODE` | No | `minimal` (replace status with "done") or `delete` (remove status message) |

Docker Compose mounts `./data/downloads` → `/data/downloads` and `./secrets` → `/secrets` (read-only, for cookies).

## Architecture

Single-file async Python app using `python-telegram-bot` 21.x with `asyncio`.

**Request flow:**
1. Telegram message arrives → handler routes based on command or text
2. `extract_twitter_url()` validates URL against twitter.com / x.com patterns
3. `is_allowed_user()` checks ACL if `ALLOWED_USERS` is set
4. `download_video()` calls yt-dlp via `run_in_executor()` (thread pool, non-blocking)
5. `TelegramProgressHook` sends progress updates back to the async loop via `run_coroutine_threadsafe()`
6. Downloaded files are detected by modification time and renamed via `process_tweet_title()`
7. `apply_success_cleanup()` handles post-download message behavior

**Handlers:**
- `/start` → greeting
- `/help` → usage
- `/list` → 10 most recent downloads with file sizes
- Any text → `download_video()` (main logic)

**Threading model:** yt-dlp runs in a thread pool executor. The `TelegramProgressHook` class bridges the synchronous yt-dlp callback into the async Telegram event loop.

## Key Implementation Notes

- All user-facing messages are in Chinese (Simplified)
- yt-dlp is configured with `merge_output_format: mp4` and ffmpeg for audio+video merging
- File naming pattern: `{uploader}_{clean_title}_{date_timestamp}[_{playlist_index}].{ext}`
- Supported extensions detected post-download: `.mp4`, `.webm`, `.mkv`, `.mov`
- ffmpeg is installed in the Docker image (required by yt-dlp for format merging)
