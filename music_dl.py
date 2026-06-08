#!/home/tom/Desktop/project/python/music_downloader/.venv/bin/python3
"""GD Studio Music API CLI — 搜索、下载、歌词、专辑图"""

import argparse
import logging
import os
import sys
import time
import urllib.parse
import requests
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, USLT, TIT2, TPE1, TALB, TCON, TYER, error as ID3Error
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, TransferSpeedColumn, DownloadColumn
from video_dl import get_video_info, download_video, QUALITY_PRESETS

logger = logging.getLogger("music_dl")

API_BASE = "https://music-api.gdstudio.xyz/api.php"
DEFAULT_SOURCE = "netease"
DEFAULT_BR = "320"
TIMEOUT = 30

SOURCES = ["netease", "kuwo", "joox", "bilibili"]

# 全局 Session — 复用 TCP 连接池，避免重复 DNS + TLS 握手
_session = requests.Session()
_session.headers.update({"User-Agent": "MusicDownloader/1.0"})
_adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=2)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


def setup_logging(level=logging.INFO, log_file=None):
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(fmt)
    root.addHandler(handler)
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            logger.warning("无法创建日志文件 %s: %s", log_file, e)


def search(name, source=DEFAULT_SOURCE, count=20, pages=1, album=False):
    s = source + "_album" if album else source
    params = {"types": "search", "source": s, "name": name, "count": count, "pages": pages}
    logger.debug("搜索: source=%s query=%r count=%d page=%d album=%s", s, name, count, pages, album)
    resp = _session.get(API_BASE, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    n = len(data) if isinstance(data, list) else 0
    logger.debug("搜索完成: %d 条结果", n)
    return data


def get_song_url(track_id, source=DEFAULT_SOURCE, br=DEFAULT_BR):
    params = {"types": "url", "source": source, "id": track_id, "br": br}
    logger.debug("获取下载链接: id=%s source=%s br=%s", track_id, source, br)
    resp = _session.get(API_BASE, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("url"):
        logger.info("获取下载链接成功: %s KB, %s", data.get("size", "?"), data.get("br", "?"))
    else:
        logger.warning("获取下载链接失败: %s", data)
    return data


def get_lyric(lyric_id, source=DEFAULT_SOURCE):
    params = {"types": "lyric", "source": source, "id": lyric_id}
    resp = _session.get(API_BASE, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_pic(pic_id, source=DEFAULT_SOURCE, size="500"):
    params = {"types": "pic", "source": source, "id": pic_id, "size": size}
    resp = _session.get(API_BASE, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def sanitize_filename(name):
    return "".join(c for c in name if c not in r'\/:*?"<>|').strip()


def format_artist_str(artists, separator=" / "):
    if isinstance(artists, list):
        return separator.join(artists) if artists else ""
    if isinstance(artists, str) and artists.strip():
        return artists.strip()
    return ""


def download_file(url, filepath, progress_callback=None, extra_headers=None, resume=True, max_retries=3):
    t0 = time.time()
    part_file = filepath + ".part"
    resumed = 0

    for attempt in range(max_retries):
        headers = extra_headers.copy() if extra_headers else {}
        if resume and os.path.exists(part_file):
            resumed = os.path.getsize(part_file)
            if resumed > 0:
                headers["Range"] = f"bytes={resumed}-"
                logger.info("续传: %s (已有 %.1f MB)", os.path.basename(filepath), resumed / 1048576)

        logger.info("开始下载: %s%s", os.path.basename(filepath),
                     f" (重试 {attempt + 1}/{max_retries})" if attempt > 0 else "")
        logger.debug("下载 URL: %s", url[:120])

        try:
            resp = _session.get(url, stream=True, timeout=(10, 120), headers=headers)
        except requests.RequestException:
            if attempt < max_retries - 1:
                logger.warning("连接失败，%ds 后重试...", (attempt + 1) * 2)
                time.sleep((attempt + 1) * 2)
                continue
            raise

        # Handle resume: 206 = server accepted range, 200 = server ignored range (restart)
        if resp.status_code == 206:
            mode = "ab"
            total = resumed + int(resp.headers.get("content-length", 0))
            logger.debug("续传模式: 已下载 %.1f MB, 剩余 %.1f MB", resumed / 1048576, (total - resumed) / 1048576)
        elif resp.status_code == 200:
            mode = "wb"
            total = int(resp.headers.get("content-length", 0))
            resumed = 0
            if os.path.exists(part_file):
                os.remove(part_file)
            if headers.get("Range"):
                logger.debug("服务器不支持 Range，重新下载")
        else:
            resp.raise_for_status()
            mode = "wb"
            total = int(resp.headers.get("content-length", 0))

        logger.debug("文件大小: %.1f MB", total / 1048576 if total else 0)

        progress_ctx = None
        task = None
        if not progress_callback:
            progress_ctx = Progress(
                TextColumn("  [bold green]Download[/bold green]"),
                BarColumn(),
                "[progress.percentage]{task.percentage:>3.0f}%",
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
            )
            progress_ctx.__enter__()
            task = progress_ctx.add_task("", total=total or None, completed=resumed)

        downloaded = resumed
        try:
            with open(part_file, mode) as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total, resumed)
                    elif task is not None:
                        progress_ctx.update(task, advance=len(chunk))
        except (requests.RequestException, ConnectionError) as e:
            if progress_ctx:
                progress_ctx.__exit__(None, None, None)
            if attempt < max_retries - 1:
                logger.warning("下载中断 (%s)，%ds 后重试续传...", e, (attempt + 1) * 2)
                time.sleep((attempt + 1) * 2)
                continue
            raise

        if progress_ctx:
            progress_ctx.__exit__(None, None, None)

        # Rename .part → final filename on success
        os.rename(part_file, filepath)
        break  # success — exit retry loop

    elapsed = time.time() - t0
    size_mb = os.path.getsize(filepath) / 1048576
    logger.info("下载完成: %s (%.1f MB, %.1fs)", os.path.basename(filepath), size_mb, elapsed)


def embed_metadata(filepath, title="", artist="", album="", lyric_text="", pic_data=None):
    ext = os.path.splitext(filepath)[1].lower()
    logger.debug("嵌入元数据: %s (fmt=%s)", os.path.basename(filepath), ext)

    if ext == ".mp3":
        return _embed_mp3(filepath, title, artist, album, lyric_text, pic_data)
    elif ext == ".flac":
        return _embed_flac(filepath, title, artist, album, pic_data)
    elif ext in (".m4a", ".mp4"):
        return _embed_mp4(filepath, title, artist, album, pic_data)
    else:
        logger.debug("不支持的文件格式: %s", ext)
        return None


def _embed_mp3(filepath, title, artist, album, lyric_text, pic_data):
    try:
        audio = MP3(filepath, ID3=ID3)
    except Exception as e:
        logger.warning("无法打开 MP3: %s — %s", filepath, e)
        return None
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if title:
        tags.add(TIT2(encoding=3, text=title))
    if artist:
        tags.add(TPE1(encoding=3, text=artist))
    if album:
        tags.add(TALB(encoding=3, text=album))
    if lyric_text:
        tags.delall("USLT")
        tags.add(USLT(encoding=3, lang="zho", desc="", text=lyric_text))
    if pic_data:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=pic_data))
    tags.save(filepath, v2_version=3)
    logger.info("MP3 元数据嵌入成功: title=%s artist=%s pic=%s",
                title or "-", artist or "-", "yes" if pic_data else "no")
    return filepath


def _embed_flac(filepath, title, artist, album, pic_data):
    try:
        audio = FLAC(filepath)
    except Exception as e:
        logger.warning("无法打开 FLAC: %s — %s", filepath, e)
        return None
    if title:
        audio["TITLE"] = title
    if artist:
        audio["ARTIST"] = artist
    if album:
        audio["ALBUM"] = album
    if pic_data:
        audio.clear_pictures()
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = pic_data
        audio.add_picture(pic)
    audio.save()
    logger.info("FLAC 元数据嵌入成功: title=%s artist=%s pic=%s",
                title or "-", artist or "-", "yes" if pic_data else "no")
    return filepath


def _embed_mp4(filepath, title, artist, album, pic_data):
    try:
        audio = MP4(filepath)
    except Exception as e:
        logger.warning("无法打开 M4A: %s — %s", filepath, e)
        return None
    if title:
        audio["\xa9nam"] = title
    if artist:
        audio["\xa9ART"] = artist
    if album:
        audio["\xa9alb"] = album
    if pic_data:
        cover = MP4Cover(pic_data, imageformat=MP4Cover.FORMAT_JPEG)
        audio["covr"] = [cover]
    audio.save()
    logger.info("M4A 元数据嵌入成功: title=%s artist=%s pic=%s",
                title or "-", artist or "-", "yes" if pic_data else "no")
    return filepath


def fetch_cover_url(name, artist="", preferred_source=DEFAULT_SOURCE, pic_id="", size="300"):
    """获取封面图片 URL（不下载图片本身）。

    优先使用 pic_id 直接获取，fallback 到搜索。
    对于 bilibili，pic_id 本身就是协议相对 URL。

    Returns:
        (url, source) tuple, 或 (None, None) 如果找不到。
    """
    sources = [preferred_source] + [s for s in SOURCES if s != preferred_source]

    # 优先用 pic_id 直接获取 URL
    if pic_id:
        # Bilibili: pic_id is already an image URL (protocol-relative)
        if preferred_source == "bilibili" and pic_id.startswith("//"):
            url = "https:" + pic_id
            logger.debug("封面URL(来自bilibili pic_id): %s", url)
            return url, preferred_source
        # 其他音源: 通过 get_pic API 获取 URL
        try:
            pic = get_pic(pic_id, preferred_source, size)
            pic_url = pic.get("url", "")
            if pic_url and pic_url.startswith(("http://", "https://")):
                # bilibili get_pic 可能返回假 URL (如 "https://BV...")
                if preferred_source == "bilibili" and "bv" in pic_url.lower() and not pic_url.startswith("https://i"):
                    pass  # skip — fake URL
                else:
                    logger.debug("封面URL(来自pic_id): %s", pic_url)
                    return pic_url, preferred_source
        except Exception:
            pass

    # fallback: 搜索歌曲获取封面 URL
    for src in sources:
        try:
            query = f"{name} {artist}".strip()
            logger.debug("封面URL搜索尝试: source=%s query=%r", src, query)
            r = search(query, src, count=5)
            if not isinstance(r, list) or not r:
                continue
            for item in r:
                item_artists = item.get("artist", [])
                if not isinstance(item_artists, list):
                    item_artists = [item_artists]
                if artist and not any(artist.lower() in str(a).lower() for a in item_artists):
                    continue
                item_pic_id = item.get("pic_id", "")
                if not item_pic_id:
                    continue
                if src == "bilibili" and item_pic_id.startswith("//"):
                    return "https:" + item_pic_id, src
                pic = get_pic(item_pic_id, src, size)
                pic_url = pic.get("url", "")
                if pic_url and pic_url.startswith(("http://", "https://")):
                    if src == "bilibili" and "bv" in pic_url.lower() and not pic_url.startswith("https://i"):
                        continue
                    logger.debug("封面URL(来自搜索): source=%s url=%s", src, pic_url)
                    return pic_url, src
        except Exception:
            continue

    logger.warning("封面URL获取失败: 所有音源均未找到")
    return None, None


def fetch_cover(name, artist="", preferred_source=DEFAULT_SOURCE):
    """获取封面图片数据（下载图片字节）。先获取 URL，再下载图片。"""
    url, src = fetch_cover_url(name, artist, preferred_source, size="500")
    if not url:
        return None, None
    try:
        pic_resp = _session.get(url, timeout=TIMEOUT)
        pic_resp.raise_for_status()
        logger.info("封面获取成功: source=%s size=%d", src, len(pic_resp.content))
        return pic_resp.content, src
    except Exception as e:
        logger.warning("封面图片下载失败: %s — %s", url, e)
        return None, None


def cmd_search(args):
    logger.info("CLI 搜索: %s", args.name)
    result = search(args.name, args.source, args.count, args.pages, args.album)
    if isinstance(result, list):
        print(f"\n找到 {len(result)} 首歌曲 ({args.source}, 第{args.pages}页):\n")
        for i, item in enumerate(result, 1):
            artists = item.get("artist", ["未知歌手"])
            artist_str = " / ".join(artists) if isinstance(artists, list) else str(artists)
            print(f"  [{i:2d}] {item.get('name', '?')}")
            print(f"       歌手: {artist_str}")
            print(f"       专辑: {item.get('album', '?')}")
            print(f"       ID: {item.get('id', '?')}  源: {item.get('source', '?')}")
            print()
    else:
        logger.error("搜索结果异常: %s", result)
        print(f"搜索结果异常: {result}")


def cmd_download(args):
    name = args.name or args.id
    artist_str = format_artist_str(args.artist)
    filename_suffix = f" - {args.artist}" if args.artist else ""

    result = get_song_url(args.id, args.source, args.br)
    url = result.get("url")
    if not url:
        logger.error("未获取到下载链接: id=%s result=%s", args.id, result)
        print(f"未获取到下载链接: {result}")
        return

    ext = ".mp3"
    if "flac" in url.lower():
        ext = ".flac"
    elif "m4a" in url.lower() or "m4s" in url.lower():
        ext = ".m4a"

    filename = sanitize_filename(f"{name}{filename_suffix}{ext}")
    filepath = os.path.join(args.outdir, filename)

    if os.path.exists(filepath):
        logger.info("文件已存在，跳过: %s", filepath)
        print(f"已存在，跳过: {filepath}")
        return

    os.makedirs(args.outdir, exist_ok=True)

    size_kb = result.get("size", "?")
    actual_br = result.get("br", "?")
    print(f"歌曲: {name}{filename_suffix}")
    print(f"音质: {actual_br}  大小: {size_kb} KB")

    dl_headers = {"Referer": "https://www.bilibili.com/", "User-Agent": "Mozilla/5.0"} if args.source == "bilibili" else None
    download_file(url, filepath, extra_headers=dl_headers)

    pic_data = None
    pic_src = None
    if not args.no_pic:
        pic_data, pic_src = fetch_cover(name, args.artist or "", args.source)

    try:
        embed_metadata(
            filepath,
            title=name,
            artist=args.artist or "",
            album=args.album or "",
            pic_data=pic_data,
        )
        if pic_data:
            print(f"  封面已嵌入{f' ({pic_src})' if pic_src else ''}")
    except Exception as e:
        logger.exception("嵌入元数据失败: %s", filepath)
        print(f"  嵌入元数据失败: {e}")

    print(f"\n下载完成: {filepath}")
    logger.info("CLI 下载完成: %s", filepath)


def cmd_lyric(args):
    result = get_lyric(args.id, args.source)
    lyric = result.get("lyric", "")
    tlyric = result.get("tlyric", "")

    if lyric:
        print(f"\n--- 歌词 (LRC) ---\n{lyric}")
        if args.save:
            filepath = f"{args.id}.lrc"
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(lyric)
            print(f"\n已保存: {filepath}")
    else:
        print("未获取到歌词")

    if tlyric and args.show_trans:
        print(f"\n--- 中文翻译 ---\n{tlyric}")


def cmd_pic(args):
    result = get_pic(args.id, args.source, args.size)
    pic_url = result.get("url")
    if pic_url:
        print(f"专辑图链接: {pic_url}")
        if args.save:
            ext = os.path.splitext(urllib.parse.urlparse(pic_url).path)[1] or ".jpg"
            filepath = f"cover_{args.id}{ext}"
            download_file(pic_url, filepath)
            print(f"已保存: {filepath}")
    else:
        print(f"未获取到专辑图: {result}")


def cmd_video(args):
    if args.action == "info":
        logger.info("获取视频信息: %s", args.url)
        info = get_video_info(args.url)
        print(f"\n{info['title']}")
        if info.get("uploader"):
            print(f"   上传者: {info['uploader']}")
        if info.get("duration"):
            m, s = divmod(info["duration"], 60)
            print(f"   时长: {m}:{s:02d}")
        if info.get("thumbnail"):
            print(f"   封面: {info['thumbnail']}")
        print(f"\n可用格式 ({len(info['formats'])} 个):\n")
        for f in info["formats"]:
            size_str = f"  {f['filesize'] / 1048576:.1f} MB" if f["filesize"] else ""
            type_str = "[V+A]" if f["has_video"] and f["has_audio"] else ("[V]" if f["has_video"] else "[A]")
            print(f"  {type_str} {f['resolution']:10s} {f['ext']:6s} {f['id']:12s}{size_str}")
        print()
        print("预设质量: " + ", ".join(QUALITY_PRESETS.keys()))
    elif args.action == "download":
        quality = args.quality or "best"
        outdir = args.outdir or os.path.join(os.path.expanduser("~"), "Videos")
        logger.info("视频下载: %s quality=%s outdir=%s", args.url, quality, outdir)
        print(f"\n下载视频: {args.url}")
        print(f"   质量: {quality}  输出: {outdir}\n")
        filepath = download_video(args.url, quality, outdir)
        if filepath:
            print(f"\n下载完成: {filepath}")
        else:
            print("\n下载失败")
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="GD Studio Music API CLI — 音乐搜索/下载/视频下载工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s search 周杰伦                          # 搜索歌曲
  %(prog)s search 周杰伦 -s kuwo                   # 指定音乐源搜索
  %(prog)s search 周杰伦 --album                   # 搜索专辑
  %(prog)s download abc123                         # 下载歌曲
  %(prog)s download abc123 --name 大鱼 --artist 周深 # 指定歌名艺术家
  %(prog)s download abc123 -b 999                  # 无损下载（自动嵌入封面）
  %(prog)s download abc123 --no-pic                # 不嵌入封面
  %(prog)s lyric abc123                            # 获取歌词
  %(prog)s pic abc123                              # 获取专辑图
  %(prog)s video info <url>                        # 查看视频信息
  %(prog)s video download <url> -q 720             # 下载视频
        """,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志输出 (DEBUG 级别)")
    parser.add_argument("--log-file", default=None, help="日志输出到文件")
    sub = parser.add_subparsers(dest="cmd", help="可用命令")

    p_search = sub.add_parser("search", help="搜索歌曲/专辑")
    p_search.add_argument("name", help="搜索关键字（曲目名/歌手/专辑）")
    p_search.add_argument("-s", "--source", default=DEFAULT_SOURCE, choices=SOURCES, help="音乐源 (默认: netease)")
    p_search.add_argument("-c", "--count", type=int, default=20, help="每页条数 (默认: 20)")
    p_search.add_argument("-p", "--pages", type=int, default=1, help="页码 (默认: 1)")
    p_search.add_argument("--album", action="store_true", help="搜索专辑")

    p_dl = sub.add_parser("download", help="下载歌曲")
    p_dl.add_argument("id", help="曲目 ID")
    p_dl.add_argument("-s", "--source", default=DEFAULT_SOURCE, choices=SOURCES, help="音乐源 (默认: netease)")
    p_dl.add_argument("-b", "--br", default=DEFAULT_BR, choices=["128", "192", "320", "740", "999"], help="音质 (默认: 320)")
    p_dl.add_argument("--no-pic", action="store_true", help="不嵌入封面")
    p_dl.add_argument("--name", default="", help="自定义文件名（省略时自动搜索）")
    p_dl.add_argument("--artist", default="", help="自定义艺术家名")
    p_dl.add_argument("--album", default="", help="自定义专辑名")
    p_dl.add_argument("-o", "--outdir", default="downloads", help="输出目录 (默认: downloads)")

    p_lyric = sub.add_parser("lyric", help="获取歌词")
    p_lyric.add_argument("id", help="歌词 ID")
    p_lyric.add_argument("-s", "--source", default=DEFAULT_SOURCE, choices=SOURCES, help="音乐源 (默认: netease)")
    p_lyric.add_argument("--save", action="store_true", help="保存为 .lrc 文件")
    p_lyric.add_argument("--show-trans", action="store_true", help="显示中文翻译")

    p_pic = sub.add_parser("pic", help="获取专辑图")
    p_pic.add_argument("id", help="专辑图 ID")
    p_pic.add_argument("-s", "--source", default=DEFAULT_SOURCE, choices=SOURCES, help="音乐源 (默认: netease)")
    p_pic.add_argument("--size", choices=["300", "500"], default="500", help="图片尺寸 (默认: 500)")
    p_pic.add_argument("--save", action="store_true", help="保存图片")

    p_video = sub.add_parser("video", help="视频下载 (YouTube, Bilibili 等)")
    p_video.add_argument("action", choices=["info", "download"], help="info=查看信息, download=下载")
    p_video.add_argument("url", help="视频链接")
    p_video.add_argument("-q", "--quality", choices=list(QUALITY_PRESETS.keys()), default="best", help="下载质量 (默认: best)")
    p_video.add_argument("-o", "--outdir", default=None, help="输出目录 (默认: ~/Videos)")

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(level=level, log_file=args.log_file)

    if not args.cmd:
        parser.print_help()
        return

    try:
        {
            "search": lambda: cmd_search(args),
            "download": lambda: cmd_download(args),
            "lyric": lambda: cmd_lyric(args),
            "pic": lambda: cmd_pic(args),
            "video": lambda: cmd_video(args),
        }[args.cmd]()
    except requests.exceptions.RequestException as e:
        print(f"网络错误: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
