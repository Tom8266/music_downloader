# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A music search/stream/download tool using the [GD Studio Music API](https://music-api.gdstudio.xyz/api.php). Supports multiple sources (Netease, Kuwo, JOOX, Bilibili), MP3/FLAC/M4A metadata embedding, cover art fetching, and a Web UI. Also includes video downloading via yt-dlp (YouTube, Bilibili, etc.).

## Commands

```bash
# Web UI (production — Waitress)
./webui.py                           # http://127.0.0.1:8080
./webui.py --port 9090 --debug       # Flask dev server with hot reload

# CLI
./music_dl.py search <keyword> [-s kuwo|joox] [--album] [-c 20] [-p 1]
./music_dl.py download <id> [--name ...] [--artist ...] [--pic] [-b 320|740|999] [-o dir]
./music_dl.py lyric <id>
./music_dl.py pic <id> [--save]
./music_dl.py video info <url>
./music_dl.py video download <url> [-q best|1080|720|480|audio] [-o dir]
```

No test suite or linter is configured. Both entry points share `setup_logging(level, log_file)` (structured format with timestamps) — pass `--verbose` for DEBUG, `--log-file <path>` for file logging. Dependencies: `requests`, `flask`, `mutagen`, `rich`, `waitress`, `yt-dlp` (see `requirements.txt`).

## Architecture

**Two entry points, one shared module:**

- `music_dl.py` — the "library": API client functions (`search`, `get_song_url`, `get_lyric`, `get_pic`, `download_file`, `embed_metadata`, `fetch_cover`) plus a CLI (`argparse` subcommands: search, download, lyric, pic, video). The `main()` block at the bottom is the CLI entry point.
- `webui.py` — Flask app that `from music_dl import ...` all the API client functions. Uses `waitress` in production, Flask's built-in server with `--debug`. Server-renders `templates/index.html` on `/`, everything else is JSON API routes under `/api/`.
- `video_dl.py` — yt-dlp wrapper: `get_video_info(url, cookies)` extracts metadata (auto-detects playlists, uses `extract_flat` for speed); `download_video(url, format_id, output_dir, progress_callback, cookies)` downloads with progress. Cookies are written to temp files and passed as yt-dlp `cookiefile`. `QUALITY_PRESETS` maps friendly names (`best`, `1080`, `720`, `480`, `audio`) to yt-dlp format strings. URL tracking parameters are stripped via `_clean_url()`.

**Shared utilities in `music_dl.py`:**
- `sanitize_filename(name)` — strips `\/:*?"<>|` from filenames
- `format_artist_str(artists, separator=" / ")` — normalizes API artist data (list or string) to display form
- `setup_logging(level, log_file)` — configures root logger, used by both entry points

**CLI progress bars:** `download_file()` uses `rich.progress.Progress` (with bar, speed, ETA columns) when no `progress_callback` is passed (CLI mode). The Web UI passes a callback that updates the in-memory `downloads_status` dict instead.

**Data flow (Web UI):**
1. Single-page HTML (`templates/index.html`) with vanilla JS and Material Design 3 styling (Roboto + Material Symbols from Google Fonts CDN, CSS custom properties for design tokens).
2. Search → `/api/search` → returns JSON from GD Studio API.
3. Play → `/api/url` → returns a streaming URL the browser `<audio>` element plays directly.
4. Download → `POST /api/download` → spawns a `threading.Thread` that downloads server-side, updates an in-memory `downloads_status` dict (no DB/persistence). Frontend polls `/api/download/<id>/status` every 800ms for progress.
5. Downloaded files served via `/api/file/<filename>` with path-traversal protection (`validate_path`).
6. `/api/downloaded` → lists audio files in the output directory (sorted by mtime, newest first).
7. `/api/cover` → returns a cover art URL for thumbnails (searches multiple sources, but returns URL only — no image download).
8. `/api/dirs` → directory browser for the settings panel (returns parent, home, and subdirectory entries).
9. `/api/download/<id>/status` → polled every 800ms by the frontend for download progress. Completed/failed entries are auto-cleaned after 5 minutes by `_cleanup_old_downloads()`.

**Video download routes** (under `/api/video/`):
- `POST /api/video/info` → `{url}` → returns video metadata (title, thumbnail, formats) via yt-dlp
- `POST /api/video/download` → `{url, quality}` → spawns download thread, returns `{status, id, title}`
- `GET /api/video/<vid>/status` → polls progress (same 800ms pattern as music downloads)
- `GET /api/video/file/<filename>` → serves downloaded video files

**Download lifecycle:** Each download spawns a daemon `threading.Thread`. Status is tracked in an in-memory `downloads_status` dict keyed by track ID (no DB/persistence). Entries with status `"done"` or `"error"` are removed after 5 minutes (checked on each status poll). The frontend fades out completed items after 2 seconds and error items after 3 seconds.

**Cover art:** `fetch_cover()` searches the preferred source first, then falls back to other sources. If the track has a `pic_id` it fetches directly; otherwise it searches by name+artist.

**Settings:** Music settings stored in browser `localStorage` under key `md_settings` (`{br, source, outdir}`). Video settings under `md_video_settings` (`{outdir, cookie}`). JS helpers: `getBr()`, `getOutdir()`, `getVideoOutdir()`, `getVideoCookie()`. Default values centralized in `const D`. The music download directory can also be set via `MUSIC_DOWNLOAD_DIR` env var on the server. Directory browser is unified — `_browseDir`/`_loadDir`/`_renderDirBrowser` serve both music and video via a `prefix` parameter.

**Frontend architecture:**
- Mode toggle (`[音乐] [视频]` segmented buttons) — `switchMode()` shows/hides `#musicContent` / `#videoContent`, persists to localStorage `md_mode`.
- Polling: download progress polls (800ms intervals) are tracked via `_trackInterval()` and cleaned on mode switch by `_clearPollTimers()`.
- Playlist handling: `renderVideoInfo()` detects `is_playlist` and renders a part selector dropdown instead of quality options. Download uses the selected part URL.
- Bilibili-specific: cover thumbnails use `referrerpolicy="no-referrer"`, audio streaming goes through `/api/stream` proxy, downloads include `Referer` header.

**Additional API routes:**
- `/api/stream` → proxies bilibili audio with `Referer` header (avoids 403)
- `/api/file/delete` (POST) → deletes a downloaded file (with path validation)
- `/api/video/downloaded` → lists video files in output directory

**Static files:** `static/icon.svg` (favicon), `static/wallpaper.webp` (background). The old `electron/` directory and `package.json`/`package-lock.json` have been removed (see git history).

## Key constraints

- The GD Studio API returns JSON arrays for search results — but may return error objects on rate-limiting. The Web UI handles this; the CLI prints raw JSON.
- Downloads go to `~/MusicDownloads` by default (set in `webui.py` `DOWNLOAD_DIR`). The CLI defaults to `./downloads/`.
- `validate_path()` prevents path traversal out of the resolved output directory.
- No authentication, no database, no session state — all state is ephemeral (in-memory dict + browser localStorage).
- Shebangs in `music_dl.py` and `webui.py` are hardcoded to `.venv/bin/python3` — they only work inside that venv. Use `./webui.py` directly (executable bit) or `python3 webui.py`.
- The project uses Python 3.14. `mutagen` can have compatibility issues on bleeding-edge Python — if metadata embedding breaks, check mutagen's Python version support first.
- Bilibili CDN requires `Referer: https://www.bilibili.com/` header on audio/video requests (returns 403 without it). Both `/api/stream` proxy and `download_file()` pass this header for bilibili source. Images need `referrerpolicy="no-referrer"`.
- Bilibili `pic_id` in search results is already an image URL (`//i*.hdslb.com/...`). Using `get_pic()` with a BV number returns a fake URL — always use `pic_id` directly (prepending `https:`) for bilibili cover art.
- yt-dlp's bilibili extractor needs Chrome UA + Referer headers (set in `_build_opts()`). Playlist/collection detection uses `extract_flat` mode (fast) first, then falls back to one-item extraction for cover images.
