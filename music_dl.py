#!/home/tom/Desktop/project/python/music_downloader/.venv/bin/python3
"""GD Studio Music API CLI — 搜索、下载、歌词、专辑图"""

import argparse
import os
import sys
import urllib.parse
import requests
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, USLT, TIT2, TPE1, TALB, TCON, TYER, error as ID3Error
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, TransferSpeedColumn, DownloadColumn

API_BASE = "https://music-api.gdstudio.xyz/api.php"
DEFAULT_SOURCE = "netease"
DEFAULT_BR = "320"
TIMEOUT = 30

SOURCES = ["netease", "kuwo", "joox"]
STABLE_SOURCES = ["netease", "kuwo", "joox"]


def search(name, source=DEFAULT_SOURCE, count=20, pages=1, album=False):
    s = source + "_album" if album else source
    params = {"types": "search", "source": s, "name": name, "count": count, "pages": pages}
    resp = requests.get(API_BASE, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_song_url(track_id, source=DEFAULT_SOURCE, br=DEFAULT_BR):
    params = {"types": "url", "source": source, "id": track_id, "br": br}
    resp = requests.get(API_BASE, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_lyric(lyric_id, source=DEFAULT_SOURCE):
    params = {"types": "lyric", "source": source, "id": lyric_id}
    resp = requests.get(API_BASE, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_pic(pic_id, source=DEFAULT_SOURCE, size="500"):
    params = {"types": "pic", "source": source, "id": pic_id, "size": size}
    resp = requests.get(API_BASE, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def sanitize_filename(name):
    return "".join(c for c in name if c not in r'\/:*?"<>|').strip()


def download_file(url, filepath):
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))

    with Progress(
        TextColumn("  [bold green]⬇[/bold green]"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task = progress.add_task("", total=total or None)
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                progress.update(task, advance=len(chunk))


def embed_metadata(filepath, title="", artist="", album="", lyric_text="", pic_data=None):
    try:
        audio = MP3(filepath, ID3=ID3)
    except Exception:
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
    return filepath


def fetch_cover(name, artist="", preferred_source=DEFAULT_SOURCE):
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
                pic = get_pic(pic_id, src, "500")
                pic_url = pic.get("url", "")
                if pic_url:
                    pic_resp = requests.get(pic_url, timeout=TIMEOUT)
                    pic_resp.raise_for_status()
                    return pic_resp.content, src
        except Exception:
            continue
    return None, None


def cmd_search(args):
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
        print(f"搜索结果异常: {result}")


def cmd_download(args):
    artist_str = ""
    if args.artist:
        artist_str = " - " + args.artist
    elif not args.name:
        artist_str = ""

    name = args.name or args.id

    result = get_song_url(args.id, args.source, args.br)
    url = result.get("url")
    if not url:
        print(f"未获取到下载链接: {result}")
        return

    ext = ".mp3"
    if "flac" in url.lower():
        ext = ".flac"
    elif "m4a" in url.lower():
        ext = ".m4a"

    filename = sanitize_filename(f"{name}{artist_str}{ext}")
    filepath = os.path.join(args.outdir, filename)

    if os.path.exists(filepath):
        print(f"已存在，跳过: {filepath}")
        return

    os.makedirs(args.outdir, exist_ok=True)

    size_kb = result.get("size", "?")
    actual_br = result.get("br", "?")
    print(f"歌曲: {name}{artist_str}")
    print(f"音质: {actual_br}  大小: {size_kb} KB")

    download_file(url, filepath)

    pic_data = None
    if args.pic:
        pic_data, pic_src = fetch_cover(name, args.artist or "", args.source)

    if ext == ".mp3" and pic_data:
        try:
            embed_metadata(
                filepath,
                title=name,
                artist=args.artist or "",
                pic_data=pic_data,
            )
            print(f"  封面已嵌入{f' ({pic_src})' if pic_src else ''}")
        except Exception as e:
            print(f"  嵌入元数据失败: {e}")

    print(f"\n✅ 下载完成: {filepath}")


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


def main():
    parser = argparse.ArgumentParser(
        description="GD Studio Music API CLI — 音乐搜索/下载工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s search 周杰伦                          # 搜索歌曲
  %(prog)s search 周杰伦 -s kuwo                   # 指定音乐源搜索
  %(prog)s search 周杰伦 --album                   # 搜索专辑
  %(prog)s download abc123                         # 下载歌曲
  %(prog)s download abc123 --name 大鱼 --artist 周深 # 指定歌名艺术家
  %(prog)s download abc123 --pic -b 999            # 无损+封面嵌入
  %(prog)s lyric abc123                            # 获取歌词
  %(prog)s pic abc123                              # 获取专辑图
        """,
    )
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
    p_dl.add_argument("--pic", action="store_true", help="嵌入专辑封面到 MP3（自动 fallback 音源）")
    p_dl.add_argument("--name", default="", help="自定义文件名（省略时自动搜索）")
    p_dl.add_argument("--artist", default="", help="自定义艺术家名")
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

    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        return

    try:
        {
            "search": lambda: cmd_search(args),
            "download": lambda: cmd_download(args),
            "lyric": lambda: cmd_lyric(args),
            "pic": lambda: cmd_pic(args),
        }[args.cmd]()
    except requests.exceptions.RequestException as e:
        print(f"网络错误: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
