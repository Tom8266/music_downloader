# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A music search/stream/download tool using the [GD Studio Music API](https://music-api.gdstudio.xyz/api.php). Supports multiple sources (Netease, Kuwo, JOOX), MP3 metadata embedding, cover art fetching, and a Web UI.

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
```

No test suite or linter is configured.

## Architecture

**Two entry points, one shared module:**

- `music_dl.py` — the "library": API client functions (`search`, `get_song_url`, `get_lyric`, `get_pic`, `download_file`, `embed_metadata`, `fetch_cover`) plus a CLI (`argparse` subcommands). The `main()` block at the bottom is the CLI entry point.
- `webui.py` — Flask app that `from music_dl import ...` all the API client functions. Uses `waitress` in production, Flask's built-in server with `--debug`. Server-renders `templates/index.html` on `/`, everything else is JSON API routes under `/api/`.

**Data flow (Web UI):**
1. Single-page HTML (`templates/index.html`) with vanilla JS and Material Design 3 styling (Roboto + Material Symbols from Google Fonts CDN, CSS custom properties for design tokens).
2. Search → `/api/search` → returns JSON from GD Studio API.
3. Play → `/api/url` → returns a streaming URL the browser `<audio>` element plays directly.
4. Download → `POST /api/download` → spawns a `threading.Thread` that downloads server-side, updates an in-memory `downloads_status` dict (no DB/persistence). Frontend polls `/api/download/<id>/status` every 800ms for progress.
5. Downloaded files served via `/api/file/<filename>` with path-traversal protection (`validate_path`).

**Cover art:** `fetch_cover()` searches the preferred source first, then falls back to other sources. If the track has a `pic_id` it fetches directly; otherwise it searches by name+artist.

**Settings:** Stored in browser `localStorage` under key `md_settings` (`{br, source, outdir, pic}`). Read by JS helpers (`getBr()`, `getOutdir()`, etc.) and sent as params with each API call. The download directory can also be set via `MUSIC_DOWNLOAD_DIR` env var on the server.

**Static files:** `static/icon.svg` (favicon), `static/wallpaper.webp` (background). The old `electron/` directory and `package.json`/`package-lock.json` have been removed (see git history).

## Key constraints

- The GD Studio API returns JSON arrays for search results — but may return error objects on rate-limiting. The Web UI handles this; the CLI prints raw JSON.
- Downloads go to `~/MusicDownloads` by default (set in `webui.py` `DOWNLOAD_DIR`). The CLI defaults to `./downloads/`.
- `validate_path()` prevents path traversal out of the resolved output directory.
- No authentication, no database, no session state — all state is ephemeral (in-memory dict + browser localStorage).
