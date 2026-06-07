# Music Downloader

Music search, streaming, and download tool with a Material Design 3 Web UI. Also supports video downloading via yt-dlp.

Built on the [GD Studio Music API](https://music-api.gdstudio.xyz/api.php).

## Features

**Music**
- Multi-source search (Netease, Kuwo, JOOX, Bilibili)
- Streaming playback with player bar
- Download with metadata embedding (MP3 / FLAC / M4A, cover art, ID3 tags)
- Cover art auto-fetch with multi-source fallback

**Video**
- yt-dlp powered — YouTube, Bilibili, and hundreds more
- Quality presets (best, 1080p, 720p, 480p, audio only)
- Playlist / collection support with per-part download
- Netscape-format cookie support for login-required content

**Web UI**
- Material Design 3 — dark theme, Roboto + Material Symbols
- Music / Video mode toggle
- Per-mode settings (output directory, source, quality)
- Download history with delete

**CLI**
- Search, download, lyrics, and cover art
- Video info and download subcommands
- Rich progress bars with speed and ETA

## Install

```bash
git clone https://github.com/Tom8266/music_downloader.git
cd music_downloader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

### Web UI

```bash
./webui.py                         # http://127.0.0.1:8080
./webui.py --port 9090 --debug     # dev server with hot reload
./webui.py --verbose --log-file app.log
```

### CLI

```bash
# Music
./music_dl.py search 周杰伦
./music_dl.py search 周杰伦 -s bilibili --album
./music_dl.py download <id> --name 大鱼 --artist 周深
./music_dl.py download <id> -b 999 -o ~/Music
./music_dl.py lyric <id> --save
./music_dl.py pic <id> --save

# Video
./music_dl.py video info <url>
./music_dl.py video download <url> -q 1080
./music_dl.py video download <url> -q audio
```

## Dependencies

- Python 3.10+
- `requests` `flask` `mutagen` `rich` `waitress` `yt-dlp`

## Disclaimer

This project uses a publicly available third-party API. All music resources belong to their respective copyright holders. For personal learning use only. Do not use commercially.

## License

[CC BY-NC 4.0](LICENSE)
