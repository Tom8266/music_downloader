#!/home/tom/Desktop/project/python/music_downloader/.venv/bin/python3
"""GD Studio Music API Web UI"""

import argparse
import logging
import os
import time
import json
import threading
import requests
from flask import Flask, render_template, request, jsonify, send_file, Response

from music_dl import search, get_song_url, get_lyric, get_pic, sanitize_filename, embed_metadata, fetch_cover, API_BASE, download_file, SOURCES, DEFAULT_SOURCE, setup_logging, format_artist_str
from video_dl import get_video_info, download_video

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

video_downloads_status = {}
video_downloads_lock = threading.Lock()


def _download_cover(pic_id, name, artists, source):
    """获取封面: 优先用 pic_id，fallback 到搜索。"""
    if pic_id:
        # Bilibili's pic_id is already an image URL (protocol-relative)
        if source == "bilibili" and pic_id.startswith("//"):
            try:
                pic_url = "https:" + pic_id
                pic_resp = requests.get(pic_url, timeout=30)
                pic_resp.raise_for_status()
                logger.debug("封面(来自bilibili pic_id): %d bytes", len(pic_resp.content))
                return pic_resp.content
            except Exception:
                pass
        else:
            try:
                pic = get_pic(pic_id, source, "500")
                pic_url = pic.get("url", "")
                if pic_url and (pic_url.startswith("http://") or pic_url.startswith("https://")):
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
    if outdir:
        outdir = os.path.expanduser(outdir)
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


@app.route("/api/stream")
def api_stream():
    """代理音频流 (用于 bilibili 等需要 Referer 的音源)。"""
    track_id = request.args.get("id", "").strip()
    source = request.args.get("source", DEFAULT_SOURCE)
    br = request.args.get("br", "320")
    if not track_id:
        return jsonify({"error": "缺少曲目 ID"}), 400

    try:
        result = get_song_url(track_id, source, br)
        url = result.get("url")
        if not url:
            return jsonify({"error": "未获取到音频链接"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    range_header = request.headers.get("Range", "")
    headers = {
        "Referer": "https://www.bilibili.com/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    }
    if range_header:
        headers["Range"] = range_header

    try:
        upstream = requests.get(url, headers=headers, stream=True, timeout=120)
        upstream.raise_for_status()
    except Exception as e:
        logger.exception("代理流失败: id=%s", track_id)
        return jsonify({"error": str(e)}), 502

    status = 206 if range_header and upstream.status_code == 206 else upstream.status_code
    resp_headers = {
        "Content-Type": upstream.headers.get("Content-Type", "audio/mp4"),
        "Accept-Ranges": "bytes",
    }
    if "Content-Length" in upstream.headers:
        resp_headers["Content-Length"] = upstream.headers["Content-Length"]
    if "Content-Range" in upstream.headers:
        resp_headers["Content-Range"] = upstream.headers["Content-Range"]

    def generate():
        for chunk in upstream.iter_content(chunk_size=65536):
            yield chunk
        upstream.close()

    return Response(generate(), status=status, headers=resp_headers)


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
        url = result.get("url", "")
        # Validate: bilibili API returns fake URLs like "https://BV..."
        if url and not (url.startswith("http://") or url.startswith("https://")):
            url = ""
        elif url and "bv" in url.lower() and not url.startswith("https://i"):
            # bilibili get_pic returns "https://BV..." — not a real URL
            url = ""
        result["url"] = url
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
    elif "m4a" in url.lower() or "m4s" in url.lower():
        ext = ".m4a"

    outdir = resolve_outdir(data.get("outdir", ""))
    os.makedirs(outdir, exist_ok=True)

    filename = sanitize_filename(f"{name}{filename_suffix}{ext}")
    filepath = os.path.join(outdir, filename)

    if not validate_path(filepath, outdir):
        return jsonify({"error": "非法下载路径"}), 400

    if os.path.exists(filepath):
        return jsonify({"status": "exists", "filename": filename})

    part_file = filepath + ".part"
    initial_bytes = os.path.getsize(part_file) if os.path.exists(part_file) else 0

    with downloads_lock:
        downloads_status[track_id] = {
            "status": "downloading",
            "name": name,
            "progress": 0,
            "downloaded_bytes": initial_bytes,
            "total_bytes": 0,
            "resumed": initial_bytes,
            "size": result.get("size", 0),
            "br": result.get("br", "?"),
            "filepath": filepath,
        }

    def do_download():
        try:
            last_pct = [0]
            last_bytes = [0]
            logger.info("下载线程启动: %s → %s", name, filename)

            def on_progress(downloaded, total, resumed=0):
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
                        downloads_status[track_id]["resumed"] = resumed

            dl_headers = {"Referer": "https://www.bilibili.com/", "User-Agent": "Mozilla/5.0"} if source == "bilibili" else None
            download_file(url, filepath, progress_callback=on_progress, extra_headers=dl_headers)

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


@app.route("/api/file/delete", methods=["POST"])
def api_delete_file():
    data = request.get_json() or {}
    outdir = resolve_outdir(data.get("outdir", ""))
    filename = data.get("filename", "").strip()
    if not filename:
        return jsonify({"error": "缺少文件名"}), 400
    filepath = os.path.join(outdir, filename)
    if not validate_path(filepath, outdir):
        return jsonify({"error": "非法文件路径"}), 400
    if not os.path.isfile(filepath):
        return jsonify({"error": "文件不存在"}), 404
    try:
        os.remove(filepath)
        logger.info("已删除文件: %s", filename)
        return jsonify({"status": "deleted", "filename": filename})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


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
            # Bilibili's pic_id is already an image URL
            if source == "bilibili" and pic_id.startswith("//"):
                return "https:" + pic_id
            pic = get_pic(pic_id, source, "300")
            pic_url = pic.get("url", "")
            if pic_url and (pic_url.startswith("http://") or pic_url.startswith("https://")):
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


# ── Video Download Routes ──────────────────────────────────────────────

import hashlib


@app.route("/api/video/info", methods=["POST"])
def api_video_info():
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    cookie = data.get("cookie", "").strip() or None
    if not url:
        return jsonify({"error": "缺少视频链接"}), 400
    try:
        info = get_video_info(url, cookies=cookie)
        logger.info("获取视频信息: %s", info.get("title", "?"))
        return jsonify(info)
    except Exception as e:
        logger.exception("获取视频信息失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/video/download", methods=["POST"])
def api_video_download():
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    quality = data.get("quality", "best")
    cookie = data.get("cookie", "").strip() or None
    if not url:
        return jsonify({"error": "缺少视频链接"}), 400

    vid = hashlib.md5(url.encode()).hexdigest()[:12]

    try:
        info = get_video_info(url, cookies=cookie)
        title = info.get("title", url[:60])
    except Exception as e:
        return jsonify({"error": f"无法获取视频信息: {e}"}), 400

    outdir = data.get("outdir", "").strip()
    if outdir:
        outdir = resolve_outdir(outdir)
    else:
        outdir = os.path.join(os.path.expanduser("~"), "Videos")
    os.makedirs(outdir, exist_ok=True)

    with video_downloads_lock:
        video_downloads_status[vid] = {
            "status": "downloading",
            "title": title,
            "progress": 0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "speed": 0,
            "eta": 0,
        }

    def do_video_download():
        try:
            last_update = [0]

            def on_progress(downloaded, total, speed, eta):
                now = time.time()
                if now - last_update[0] < 0.5 and total and downloaded < total:
                    return
                last_update[0] = now
                pct = int(downloaded / total * 100) if total else 0
                with video_downloads_lock:
                    if vid in video_downloads_status:
                        video_downloads_status[vid]["progress"] = pct
                        video_downloads_status[vid]["downloaded_bytes"] = downloaded
                        video_downloads_status[vid]["total_bytes"] = total
                        video_downloads_status[vid]["speed"] = speed
                        video_downloads_status[vid]["eta"] = eta

            filepath = download_video(url, quality, outdir, progress_callback=on_progress, cookies=cookie)
            with video_downloads_lock:
                video_downloads_status[vid]["status"] = "done"
                video_downloads_status[vid]["progress"] = 100
                video_downloads_status[vid]["filepath"] = filepath or ""
                video_downloads_status[vid]["_finished_at"] = time.time()
            logger.info("视频下载线程完成: %s", title)
        except Exception as e:
            logger.exception("视频下载线程失败: %s", title)
            with video_downloads_lock:
                video_downloads_status[vid] = {
                    "status": "error", "error": str(e), "title": title, "_finished_at": time.time()
                }

    threading.Thread(target=do_video_download, daemon=True).start()
    return jsonify({"status": "started", "id": vid, "title": title})


@app.route("/api/video/<vid>/status")
def api_video_download_status(vid):
    with video_downloads_lock:
        status = video_downloads_status.get(vid, {"status": "unknown"})
    # Cleanup old entries
    now = time.time()
    with video_downloads_lock:
        expired = [
            k for k, st in video_downloads_status.items()
            if st.get("status") in ("done", "error")
            and now - st.get("_finished_at", now) > 300
        ]
        for k in expired:
            del video_downloads_status[k]
    return jsonify(status)


@app.route("/api/video/downloaded")
def api_video_downloaded():
    outdir = request.args.get("outdir", "").strip()
    if outdir:
        outdir = resolve_outdir(outdir)
    else:
        outdir = os.path.join(os.path.expanduser("~"), "Videos")
    os.makedirs(outdir, exist_ok=True)
    files = []
    if os.path.isdir(outdir):
        for f in sorted(os.listdir(outdir), key=lambda x: os.path.getmtime(os.path.join(outdir, x)), reverse=True):
            fpath = os.path.join(outdir, f)
            if os.path.isfile(fpath) and os.path.splitext(f)[1].lower() in (".mp4", ".mkv", ".webm", ".m4a"):
                files.append({
                    "name": f,
                    "size": os.path.getsize(fpath),
                    "mtime": int(os.path.getmtime(fpath)),
                    "ext": os.path.splitext(f)[1].lower(),
                })
    return jsonify(files)


@app.route("/api/video/file/<path:filename>")
def serve_video_file(filename):
    outdir = resolve_outdir(request.args.get("outdir", ""))
    if outdir == DOWNLOAD_DIR:
        outdir = os.path.join(os.path.expanduser("~"), "Videos")
    filepath = os.path.join(outdir, filename)
    if not validate_path(filepath, outdir):
        return jsonify({"error": "非法文件路径"}), 400
    if not os.path.isfile(filepath):
        return jsonify({"error": "文件不存在"}), 404
    return send_file(filepath, as_attachment=True)


@app.route("/api/video/file/delete", methods=["POST"])
def api_video_delete_file():
    data = request.get_json() or {}
    outdir = resolve_outdir(data.get("outdir", ""))
    # If no explicit outdir, video downloads default to ~/Videos
    if not data.get("outdir", "").strip():
        outdir = os.path.join(os.path.expanduser("~"), "Videos")
    filename = data.get("filename", "").strip()
    if not filename:
        return jsonify({"error": "缺少文件名"}), 400
    filepath = os.path.join(outdir, filename)
    if not validate_path(filepath, outdir):
        return jsonify({"error": "非法文件路径"}), 400
    if not os.path.isfile(filepath):
        return jsonify({"error": "文件不存在"}), 404
    try:
        os.remove(filepath)
        logger.info("已删除视频文件: %s", filename)
        return jsonify({"status": "deleted", "filename": filename})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


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
