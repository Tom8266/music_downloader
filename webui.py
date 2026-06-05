#!/home/tom/Desktop/project/python/music_downloader/.venv/bin/python3
"""GD Studio Music API Web UI"""

import os
import json
import threading
import requests
from flask import Flask, render_template, request, jsonify, send_file

from music_dl import search, get_song_url, get_lyric, get_pic, sanitize_filename, embed_metadata, fetch_cover, API_BASE, download_file, SOURCES, DEFAULT_SOURCE

app = Flask(__name__)
DOWNLOAD_DIR = os.environ.get("MUSIC_DOWNLOAD_DIR", os.path.join(os.path.expanduser("~"), "MusicDownloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

downloads_status = {}
downloads_lock = threading.Lock()


def resolve_outdir(outdir):
    if not outdir:
        return DOWNLOAD_DIR
    if os.path.isabs(outdir):
        return os.path.normpath(outdir)
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), outdir))


def validate_path(filepath, base_dir):
    real_fp = os.path.realpath(filepath)
    real_base = os.path.realpath(base_dir)
    if not real_fp.startswith(real_base + os.sep) and real_fp != real_base:
        return False
    return True


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
        return jsonify(result)
    except Exception as e:
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
        return jsonify({"error": str(e)}), 500


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json() or {}
    track_id = data.get("id", "").strip()
    source = data.get("source", DEFAULT_SOURCE)
    br = data.get("br", "320")
    download_pic = data.get("pic", False)
    name = data.get("name", track_id)
    artists = data.get("artist", "")
    pic_id = data.get("pic_id", "")

    if not track_id:
        return jsonify({"error": "缺少曲目 ID"}), 400

    try:
        result = get_song_url(track_id, source, br)
        url = result.get("url")
        if not url:
            return jsonify({"error": "未获取到下载链接", "detail": result}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    artist_str = ""
    if isinstance(artists, list) and artists:
        artist_str = " - " + " / ".join(artists)
    elif isinstance(artists, str) and artists.strip():
        artist_str = " - " + artists

    ext = ".mp3"
    if "flac" in url.lower():
        ext = ".flac"
    elif "m4a" in url.lower():
        ext = ".m4a"

    outdir = resolve_outdir(data.get("outdir", ""))
    os.makedirs(outdir, exist_ok=True)

    filename = sanitize_filename(f"{name}{artist_str}{ext}")
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
            "size": result.get("size", 0),
            "br": result.get("br", "?"),
            "filepath": filepath,
        }

    def do_download():
        try:
            download_file(url, filepath)

            status_update = {"status": "done", "progress": 100}

            pic_data = None

            if download_pic:
                try:
                    artist_name = ""
                    if isinstance(artists, list):
                        artist_name = artists[0] if artists else ""
                    elif isinstance(artists, str):
                        artist_name = artists
                    pic_data, _ = fetch_cover(name, artist_name, source)
                except Exception:
                    pass

            if ext == ".mp3" and pic_data:
                artist_display = ""
                if isinstance(artists, list):
                    artist_display = " / ".join(artists)
                elif isinstance(artists, str):
                    artist_display = artists
                try:
                    embed_metadata(
                        filepath,
                        title=name,
                        artist=artist_display,
                        album=data.get("album", ""),
                        pic_data=pic_data,
                    )
                except Exception:
                    pass

            with downloads_lock:
                downloads_status[track_id].update(status_update)
        except Exception as e:
            with downloads_lock:
                downloads_status[track_id] = {"status": "error", "error": str(e), "name": name}

    threading.Thread(target=do_download, daemon=True).start()
    return jsonify({"status": "started", "id": track_id, "filename": filename})


@app.route("/api/download/<track_id>/status")
def api_download_status(track_id):
    with downloads_lock:
        status = downloads_status.get(track_id, {"status": "unknown"})
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
    preferred_source = request.args.get("source", DEFAULT_SOURCE)
    if not name:
        return jsonify({"error": "缺少歌曲名称"}), 400
    for src in [preferred_source] + [s for s in SOURCES if s != preferred_source]:
        try:
            query = f"{name} {artist}".strip()
            r = search(query, src, count=5)
            if not isinstance(r, list) or not r:
                continue
            for item in r:
                item_artists = item.get("artist", [])
                if not isinstance(item_artists, list):
                    item_artists = [item_artists]
                if artist and not any(artist.lower() in str(a).lower() for a in item_artists):
                    continue
                pic_id = item.get("pic_id", "")
                if not pic_id:
                    continue
                pic = get_pic(pic_id, src, "300")
                pic_url = pic.get("url", "")
                if pic_url:
                    return jsonify({"url": pic_url, "source": src})
        except Exception:
            continue
    return jsonify({"url": ""})


def main():
    import argparse
    parser = argparse.ArgumentParser(description="GD Studio Music Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="监听端口 (默认: 8080)")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
