#!/home/tom/Desktop/project/python/music_downloader/.venv/bin/python3
"""GD Studio Music API Web UI"""

import argparse
import logging
import os
import time
import json
import threading
import requests
from flask import Flask, render_template, request, jsonify, send_file

from music_dl import search, get_song_url, get_lyric, get_pic, sanitize_filename, embed_metadata, fetch_cover, API_BASE, download_file, SOURCES, DEFAULT_SOURCE, setup_logging, format_artist_str

logger = logging.getLogger("webui")
app = Flask(__name__)

# 关闭 Flask 默认的 werkzeug 请求日志，用我们自己的
log_werkzeug = logging.getLogger("werkzeug")
log_werkzeug.disabled = True


@app.after_request
def log_request(response):
    logger.info("%s %s → %s (%d B)",
                request.method,
                request.full_path if request.query_string else request.path,
                response.status,
                response.content_length or 0)
    return response


DOWNLOAD_DIR = os.environ.get("MUSIC_DOWNLOAD_DIR", os.path.join(os.path.expanduser("~"), "MusicDownloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

downloads_status = {}
downloads_lock = threading.Lock()


def _download_cover(pic_id, name, artists, source):
    """获取封面: 优先用 pic_id，fallback 到搜索。"""
    if pic_id:
        try:
            pic = get_pic(pic_id, source, "500")
            pic_url = pic.get("url", "")
            if pic_url:
                pic_resp = requests.get(pic_url, timeout=30)
                pic_resp.raise_for_status()
                logger.debug("封面(来自pic_id): %d bytes", len(pic_resp.content))
                return pic_resp.content
        except Exception:
            pass
    artist_name = format_artist_str(artists)
    if isinstance(artists, list) and artists:
        artist_name = artists[0]
    try:
        pic_data, _ = fetch_cover(name, artist_name, source)
        return pic_data
    except Exception:
        return None


def _embed_track_metadata(filepath, name, artists, album, pic_data):
    """嵌入元数据到音频文件。"""
    artist_display = format_artist_str(artists)
    try:
        embed_metadata(
            filepath,
            title=name,
            artist=artist_display,
            album=album or "",
            pic_data=pic_data,
        )
    except Exception:
        pass


def resolve_outdir(outdir):
    if not outdir:
        return DOWNLOAD_DIR
    if os.path.isabs(outdir):
        return os.path.normpath(outdir)
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), outdir))


def validate_path(filepath, base_dir):
    try:
        return os.path.commonpath([os.path.realpath(filepath), os.path.realpath(base_dir)]) == os.path.realpath(base_dir)
    except ValueError:
        return False


@app.route("/")
def index():
    return render_template("index.html", sources=SOURCES, default_source=DEFAULT_SOURCE)


@app.route("/api/search")
def api_search():
    name = request.args.get("name", "").strip()
    source = request.args.get("source", DEFAULT_SOURCE)
    count = int(request.args.get("count", 20))
    pages = int(request.args.get("pages", 1))
    if not name:
        return jsonify({"error": "请输入搜索关键字"}), 400
    try:
        result = search(name, source, count, pages)
        n = len(result) if isinstance(result, list) else 0
        logger.info("Web 搜索: %s → %d 条结果", name, n)
        return jsonify(result)
    except Exception as e:
        logger.exception("搜索失败: %s", name)
        return jsonify({"error": str(e)}), 500


@app.route("/api/url")
def api_url():
    track_id = request.args.get("id", "").strip()
    source = request.args.get("source", DEFAULT_SOURCE)
    br = request.args.get("br", "320")
    if not track_id:
        return jsonify({"error": "缺少曲目 ID"}), 400
    try:
        result = get_song_url(track_id, source, br)
        return jsonify(result)
    except Exception as e:
        logger.exception("获取下载链接失败: id=%s", track_id)
        return jsonify({"error": str(e)}), 500


@app.route("/api/lyric")
def api_lyric():
    lyric_id = request.args.get("id", "").strip()
    source = request.args.get("source", DEFAULT_SOURCE)
    if not lyric_id:
        return jsonify({"error": "缺少歌词 ID"}), 400
    try:
        result = get_lyric(lyric_id, source)
        return jsonify(result)
    except Exception as e:
        logger.exception("获取歌词失败: id=%s", lyric_id)
        return jsonify({"error": str(e)}), 500


@app.route("/api/pic")
def api_pic():
    pic_id = request.args.get("id", "").strip()
    source = request.args.get("source", DEFAULT_SOURCE)
    size = request.args.get("size", "500")
    if not pic_id:
        return jsonify({"error": "缺少专辑图 ID"}), 400
    try:
        result = get_pic(pic_id, source, size)
        return jsonify(result)
    except Exception as e:
        logger.exception("获取专辑图失败: id=%s", pic_id)
        return jsonify({"error": str(e)}), 500


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json() or {}
    track_id = data.get("id", "").strip()
    source = data.get("source", DEFAULT_SOURCE)
    br = data.get("br", "320")
    pic_id = data.get("pic_id", "").strip()
    name = data.get("name", track_id)
    artists = data.get("artist", "")

    if not track_id:
        return jsonify({"error": "缺少曲目 ID"}), 400

    try:
        result = get_song_url(track_id, source, br)
        url = result.get("url")
        if not url:
            logger.error("下载请求: 未获取到链接 id=%s", track_id)
            return jsonify({"error": "未获取到下载链接", "detail": result}), 400
    except Exception as e:
        logger.exception("下载请求失败: id=%s", track_id)
        return jsonify({"error": str(e)}), 500

    artist_str = format_artist_str(artists)
    filename_suffix = f" - {artist_str}" if artist_str else ""

    ext = ".mp3"
    if "flac" in url.lower():
        ext = ".flac"
    elif "m4a" in url.lower():
        ext = ".m4a"

    outdir = resolve_outdir(data.get("outdir", ""))
    os.makedirs(outdir, exist_ok=True)

    filename = sanitize_filename(f"{name}{filename_suffix}{ext}")
    filepath = os.path.join(outdir, filename)

    if not validate_path(filepath, outdir):
        return jsonify({"error": "非法下载路径"}), 400

    if os.path.exists(filepath):
        return jsonify({"status": "exists", "filename": filename})

    with downloads_lock:
        downloads_status[track_id] = {
            "status": "downloading",
            "name": name,
            "progress": 0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "size": result.get("size", 0),
            "br": result.get("br", "?"),
            "filepath": filepath,
        }

    def do_download():
        try:
            last_pct = [0]
            last_bytes = [0]
            logger.info("下载线程启动: %s → %s", name, filename)

            def on_progress(downloaded, total):
                if total:
                    pct = int(downloaded / total * 100)
                    if pct == last_pct[0]:
                        return
                    last_pct[0] = pct
                else:
                    # CDN 未返回文件大小，每 100KB 更新一次字节数
                    if downloaded - last_bytes[0] < 102400:
                        return
                    last_bytes[0] = downloaded
                    pct = 0
                if pct % 20 == 0:
                    logger.debug("下载进度: %s %d%%", name, pct)
                with downloads_lock:
                    if track_id in downloads_status:
                        downloads_status[track_id]["progress"] = pct
                        downloads_status[track_id]["downloaded_bytes"] = downloaded
                        downloads_status[track_id]["total_bytes"] = total

            download_file(url, filepath, progress_callback=on_progress)

            pic_data = _download_cover(pic_id, name, artists, source)
            _embed_track_metadata(filepath, name, artists, data.get("album", ""), pic_data)

            with downloads_lock:
                downloads_status[track_id]["status"] = "done"
                downloads_status[track_id]["progress"] = 100
                downloads_status[track_id]["_finished_at"] = time.time()
            logger.info("下载线程完成: %s", filename)
        except Exception as e:
            logger.exception("下载线程失败: %s", name)
            with downloads_lock:
                downloads_status[track_id] = {"status": "error", "error": str(e), "name": name, "_finished_at": time.time()}

    threading.Thread(target=do_download, daemon=True).start()
    return jsonify({"status": "started", "id": track_id, "filename": filename})


def _cleanup_old_downloads():
    """清理超过 5 分钟的已完成/失败下载状态。"""
    now = time.time()
    with downloads_lock:
        expired = [
            tid for tid, st in downloads_status.items()
            if st.get("status") in ("done", "error")
            and now - st.get("_finished_at", now) > 300
        ]
        for tid in expired:
            del downloads_status[tid]


@app.route("/api/download/<track_id>/status")
def api_download_status(track_id):
    with downloads_lock:
        status = downloads_status.get(track_id, {"status": "unknown"})
    _cleanup_old_downloads()
    return jsonify(status)


@app.route("/api/downloaded")
def api_downloaded():
    outdir = resolve_outdir(request.args.get("outdir", ""))
    os.makedirs(outdir, exist_ok=True)
    files = []
    if os.path.isdir(outdir):
        for f in sorted(os.listdir(outdir), key=lambda x: os.path.getmtime(os.path.join(outdir, x)), reverse=True):
            fpath = os.path.join(outdir, f)
            if os.path.isfile(fpath):
                files.append({
                    "name": f,
                    "size": os.path.getsize(fpath),
                    "mtime": int(os.path.getmtime(fpath)),
                    "ext": os.path.splitext(f)[1].lower(),
                })
    return jsonify(files)


@app.route("/api/file/<path:filename>")
def serve_file(filename):
    outdir = resolve_outdir(request.args.get("outdir", ""))
    filepath = os.path.join(outdir, filename)
    if not validate_path(filepath, outdir):
        return jsonify({"error": "非法文件路径"}), 400
    if not os.path.isfile(filepath):
        return jsonify({"error": "文件不存在"}), 404
    return send_file(filepath, as_attachment=True)


@app.route("/api/cover")
def api_cover():
    name = request.args.get("name", "").strip()
    artist = request.args.get("artist", "").strip()
    source = request.args.get("source", DEFAULT_SOURCE)
    if not name:
        return jsonify({"error": "缺少歌曲名称"}), 400
    url = _fetch_cover_url(name, artist, source)
    if not url:
        # fallback 到其他音源
        for src in [s for s in SOURCES if s != source]:
            url = _fetch_cover_url(name, artist, src)
            if url:
                source = src
                break
    return jsonify({"url": url or "", "source": source})


def _fetch_cover_url(name, artist, source):
    """获取封面 URL（不下载图片）。"""
    try:
        query = f"{name} {artist}".strip()
        r = search(query, source, count=5)
        if not isinstance(r, list) or not r:
            return None
        for item in r:
            item_artists = item.get("artist", [])
            if not isinstance(item_artists, list):
                item_artists = [item_artists]
            if artist and not any(artist.lower() in str(a).lower() for a in item_artists):
                continue
            pic_id = item.get("pic_id", "")
            if not pic_id:
                continue
            pic = get_pic(pic_id, source, "300")
            pic_url = pic.get("url", "")
            if pic_url:
                return pic_url
    except Exception:
        pass
    return None


@app.route("/api/dirs")
def api_dirs():
    path = request.args.get("path", "").strip()
    if not path:
        path = os.path.expanduser("~")
    path = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return jsonify({"error": "目录不存在"}), 404
    parent = os.path.dirname(path)
    if parent == path:
        parent = None
    try:
        entries = []
        for name in sorted(os.listdir(path), key=lambda n: n.lower()):
            full = os.path.join(path, name)
            if os.path.isdir(full) and not name.startswith('.'):
                entries.append({"name": name, "path": full})
    except PermissionError:
        logger.warning("目录浏览无权限: %s", path)
        return jsonify({"error": "无权限访问"}), 403
    return jsonify({
        "current": path,
        "parent": parent,
        "entries": entries,
        "home": os.path.expanduser("~"),
    })


def main():
    parser = argparse.ArgumentParser(description="GD Studio Music Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="监听端口 (默认: 8080)")
    parser.add_argument("--debug", action="store_true", help="使用 Flask 开发服务器 (调试模式)")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志输出 (DEBUG 级别)")
    parser.add_argument("--log-file", default=None, help="日志输出到文件")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(level=level, log_file=args.log_file)

    if args.debug:
        app.run(host=args.host, port=args.port, debug=True)
    else:
        from waitress import serve
        logger.info("启动 Web UI: http://%s:%s", args.host, args.port)
        print(f"  ➜  http://{args.host}:{args.port}")
        serve(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
