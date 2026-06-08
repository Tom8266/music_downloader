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

## 免责声明

**首先，看清楚了：**

本项目是一个**免费、开源**的技术学习项目，源码完整公开在 GitHub。它调用公开的第三方 API，本身不存储、不提供任何版权内容。所有音乐/视频资源的版权归原权利人所有。

**本项目仅限用于：**
- 学习 Python Web 开发（Flask / requests / yt-dlp / mutagen）
- 学习前端设计（Material Design 3 / vanilla JS）
- 个人对已拥有正版内容的合理使用

**严禁用于：**
- 任何商业用途
- 侵犯他人著作权
- 分发/传播版权内容

**关于法律责任：**

本项目采用 **GNU AGPLv3** 协议。你下载、修改、运行、传播本代码，即表示你同意：因你使用本代码产生的一切法律后果，由你自行承担，与作者无关。作者不提供任何担保，也不对任何损失负责。

如果你版权方认为本项目侵犯了你的权益，请通过 GitHub Issue 联系，我会及时处理。

---

## License

[GNU AGPLv3](LICENSE) — 你可以自由使用、修改、分发，但你必须：
1. **保持开源** — 修改后的版本也必须以 AGPLv3 开源
2. **保留版权声明** — 不能说是你写的
3. **网络服务也要开源** — 即使只在服务器上跑，用户有权获取源码
4. **不提供担保** — 作者不承担任何责任
