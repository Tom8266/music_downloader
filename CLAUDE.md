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

No test suite or linter is configured. Both entry points share `setup_logging(level, log_file)` (structured format with timestamps) — pass `--verbose` for DEBUG, `--log-file <path>` for file logging. Dependencies: `requests`, `flask`, `mutagen`, `rich`, `waitress` (see `requirements.txt`).

## Architecture

**Two entry points, one shared module:**

- `music_dl.py` — the "library": API client functions (`search`, `get_song_url`, `get_lyric`, `get_pic`, `download_file`, `embed_metadata`, `fetch_cover`) plus a CLI (`argparse` subcommands: search, download, lyric, pic, video). The `main()` block at the bottom is the CLI entry point.
- `webui.py` — Flask app that `from music_dl import ...` all the API client functions. Uses `waitress` in production, Flask's built-in server with `--debug`. Server-renders `templates/index.html` on `/`, everything else is JSON API routes under `/api/`.
- `video_dl.py` — yt-dlp wrapper: `get_video_info(url)` extracts metadata; `download_video(url, format_id, output_dir, progress_callback)` downloads with progress. `QUALITY_PRESETS` maps friendly names to yt-dlp format strings.

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

**Settings:** Stored in browser `localStorage` under key `md_settings` (`{br, source, outdir, pic}`). Read by JS helpers (`getBr()`, `getOutdir()`, etc.) and sent as params with each API call. The download directory can also be set via `MUSIC_DOWNLOAD_DIR` env var on the server.

**Static files:** `static/icon.svg` (favicon), `static/wallpaper.webp` (background). The old `electron/` directory and `package.json`/`package-lock.json` have been removed (see git history).

## Key constraints

- The GD Studio API returns JSON arrays for search results — but may return error objects on rate-limiting. The Web UI handles this; the CLI prints raw JSON.
- Downloads go to `~/MusicDownloads` by default (set in `webui.py` `DOWNLOAD_DIR`). The CLI defaults to `./downloads/`.
- `validate_path()` prevents path traversal out of the resolved output directory.
- No authentication, no database, no session state — all state is ephemeral (in-memory dict + browser localStorage).
- Shebangs in `music_dl.py` and `webui.py` are hardcoded to `.venv/bin/python3` — they only work inside that venv. Use `./webui.py` directly (executable bit) or `python3 webui.py`.
- The project uses Python 3.14. `mutagen` can have compatibility issues on bleeding-edge Python — if metadata embedding breaks, check mutagen's Python version support first.
