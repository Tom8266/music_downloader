"""Video download module — wraps yt-dlp for info extraction and download."""

import logging
import os
import tempfile
import time

import yt_dlp

logger = logging.getLogger("video_dl")

QUALITY_PRESETS = {
    "best": "bestvideo+bestaudio/best",
    "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "360": "bestvideo[height<=360]+bestaudio/best[height<=360]",
    "audio": "bestaudio/best",
}


def _make_cookiefile(cookies):
    """Write cookie string to a temp file for yt-dlp. Returns path or None."""
    if not cookies or not cookies.strip():
        return None
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    tmp.write(cookies.strip())
    tmp.close()
    return tmp.name


def _clean_url(url):
    """Strip tracking parameters that can confuse yt-dlp."""
    import re
    # Remove common tracking params from B站 URLs
    TRACK_PARAMS = (
        "spm_id_from|vd_source|share_source|share_medium|share_plat|"
        "share_session_id|share_tag|timestamp|unique_k|up_id|from_source|"
        "from_spmid|plat_id|session_id|trackid"
    )
    url = re.sub(rf'[?&]({TRACK_PARAMS})=[^&]*', '', url)
    # Fix edge case: if the first tracked param after ? was removed but
    # untracked ones remain, the URL may have & as the first separator.
    # e.g. /video/BV123/&legit_param=1 → /video/BV123/?legit_param=1
    if '&' in url and '?' not in url:
        url = url.replace('&', '?', 1)
    return url.rstrip('?')


def _build_opts(extra=None, cookies=None):
    """Build yt-dlp options dict with common headers + optional cookiefile."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "playlist_items": "1:30",  # 合集最多取前30个，防止超时
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        },
    }
    if extra:
        opts.update(extra)
    cookie_file = _make_cookiefile(cookies)
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts, cookie_file


def _get_bilibili_collection(bvid):
    """Query Bilibili API for ugc_season (合集) metadata.

    Many B站 videos belong to a 合集 (collection) that yt-dlp doesn't expose.
    Returns None if the video isn't in a collection, or a dict with:
      title, thumbnail, is_playlist, playlist_count, playlist_items
    where playlist_items use av{aid} URLs since the API returns aids.
    """
    import requests
    api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    try:
        resp = requests.get(api_url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        }, timeout=10)
        data = resp.json()
    except Exception:
        return None

    ugc = (data.get("data") or {}).get("ugc_season")
    if not ugc:
        return None

    items = []
    for section in ugc.get("sections", []):
        for ep in section.get("episodes", []):
            arc = ep.get("arc", {})
            aid = arc.get("aid")
            title = arc.get("title") or ep.get("title") or "?"
            if aid:
                items.append({
                    "id": str(aid),
                    "url": f"https://www.bilibili.com/video/av{aid}/",
                    "title": title,
                    "display_title": title,
                })

    if not items:
        return None

    cover = ugc.get("cover", "")
    return {
        "title": ugc.get("title", ""),
        "thumbnail": cover,
        "is_playlist": True,
        "playlist_count": len(items),
        "playlist_items": items,
    }


def _get_bilibili_bangumi_season(season_id):
    """Query Bilibili PGC API for full season episode list.

    Handles bangumi/episode URLs (e.g. /bangumi/play/ep779777).
    Filters to main-section episodes only (section_type == 0).
    Returns collection-format dict or None.
    """
    import requests
    api_url = f"https://api.bilibili.com/pgc/view/web/season?season_id={season_id}"
    try:
        resp = requests.get(api_url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        }, timeout=10)
        data = resp.json()
    except Exception:
        return None

    result = data.get("result") or {}
    episodes = result.get("episodes", [])
    if not episodes:
        return None

    items = []
    for ep in episodes:
        # section_type 0 = main episodes, 1 = previews/extras — skip non-main
        if ep.get("section_type") != 0:
            continue
        link = ep.get("link") or ep.get("share_url") or ""
        # long_title has the real episode name; title is just the number
        ep_title = ep.get("long_title") or ep.get("title") or "?"
        items.append({
            "id": str(ep.get("id") or ep.get("aid", "")),
            "url": link,
            "title": ep_title,
            "display_title": ep_title,
        })

    if not items:
        return None

    return {
        "title": result.get("title") or result.get("season_title", ""),
        "thumbnail": result.get("cover", ""),
        "is_playlist": True,
        "playlist_count": len(items),
        "playlist_items": items,
    }


def get_video_info(url, cookies=None):
    """Extract video metadata using yt-dlp.

    Args:
        url: Video URL.
        cookies: Optional Netscape-format cookie string.

    Returns dict with: title, thumbnail, duration, uploader, formats
    """
    import re

    url = _clean_url(url)
    opts, cookie_file = _build_opts(cookies=cookies)
    # Use extract_flat for fast playlist handling
    opts["extract_flat"] = True
    opts["playlist_items"] = "1:50"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    finally:
        if cookie_file:
            os.unlink(cookie_file)

    # If playlist/collection, return flat summary with URLs
    entries = info.get("entries")
    if entries:
        playlist_count = info.get("playlist_count") or info.get("n_entries") or len(entries)
        items = []
        for i, e in enumerate(entries):
            if e:
                real_title = e.get("title") or ""
                items.append({
                    "id": e.get("id", ""),
                    "url": e.get("url") or "",
                    "title": real_title or f"Part {i + 1}",
                    "display_title": real_title or f"P{i + 1}",
                })
        # Quick extraction of first video to get cover image
        thumbnail = info.get("thumbnail", "")
        if not thumbnail and items:
            try:
                cover_opts, cover_cf = _build_opts(cookies=cookies)
                cover_opts["playlist_items"] = "1:1"
                with yt_dlp.YoutubeDL(cover_opts) as ydl:
                    cover_info = ydl.extract_info(url, download=False)
                first_entry = (cover_info.get("entries") or [{}])[0]
                thumbnail = first_entry.get("thumbnail", "")
                if cover_cf:
                    os.unlink(cover_cf)
            except Exception:
                pass
        # If titles are all placeholder "Part N", try to enrich from PGC API
        # (yt-dlp extract_flat doesn't return episode titles for bangumi seasons)
        if items and all(
            t["title"] == f"Part {i + 1}" for i, t in enumerate(items)
        ):
            m = re.search(r'bilibili\.com/bangumi/play/ss(\d+)', url)
            if m:
                season = _get_bilibili_bangumi_season(m.group(1))
                if season:
                    title_map = {}
                    for ep in season["playlist_items"]:
                        title_map[ep["id"]] = ep["title"]
                    for item in items:
                        if item["id"] in title_map:
                            item["title"] = title_map[item["id"]]
                            item["display_title"] = title_map[item["id"]]
                    playlist_count = season["playlist_count"]
                    if not thumbnail:
                        thumbnail = season.get("thumbnail", "")
        return {
            "title": info.get("title", ""),
            "thumbnail": thumbnail,
            "uploader": info.get("uploader", ""),
            "webpage_url": info.get("webpage_url", url),
            "is_playlist": True,
            "playlist_count": playlist_count,
            "playlist_items": items,
        }

    # Not a yt-dlp playlist — check if this is a bilibili video with a wider collection
    # 1) UGC 合集 (single /video/BVxxx that belongs to a user collection)
    m = re.search(r'bilibili\.com/video/([A-Za-z0-9]+)', url)
    if m:
        collection = _get_bilibili_collection(m.group(1))
        if collection:
            return collection

    # 2) Bangumi / PGC season (single episode URL e.g. /bangumi/play/ep779777)
    sid = info.get("season_id")
    if sid:
        collection = _get_bilibili_bangumi_season(sid)
        if collection:
            return collection

    # Single video: do a full extraction to get formats
    opts2, cookie_file2 = _build_opts(cookies=cookies)
    try:
        with yt_dlp.YoutubeDL(opts2) as ydl:
            info = ydl.extract_info(url, download=False)
    finally:
        if cookie_file2:
            os.unlink(cookie_file2)

    # Filter to useful formats (skip storyboards, mhtml, etc.)
    formats = []
    for f in info.get("formats", []):
        fid = f.get("format_id", "")
        ext = f.get("ext", "?")
        res = f.get("resolution") or f.get("format_note") or (
            f"{f.get('width', '?')}x{f.get('height', '?')}" if f.get("height") else None
        ) or "?"
        filesize = f.get("filesize") or f.get("filesize_approx") or 0
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        is_video = vcodec and vcodec != "none"
        is_audio = acodec and acodec != "none"

        if "storyboard" in (f.get("format_note") or "").lower():
            continue
        if ext == "mhtml":
            continue

        formats.append({
            "id": fid,
            "ext": ext,
            "resolution": str(res),
            "filesize": filesize,
            "has_video": is_video,
            "has_audio": is_audio,
            "tbr": f.get("tbr") or 0,  # total bitrate
        })

    # Sort: video+audio formats first, then by quality descending
    def _sort_key(f):
        score = 0
        if f["has_video"] and f["has_audio"]:
            score += 1000
        elif f["has_video"]:
            score += 500
        elif f["has_audio"]:
            score += 200
        score += int(f.get("tbr", 0) or 0)
        return -score

    formats.sort(key=_sort_key)

    # Deduplicate by resolution+ext (keep first/best)
    seen = set()
    unique = []
    for f in formats:
        key = (f["resolution"], f["ext"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)

    return {
        "title": info.get("title", ""),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration") or 0,
        "uploader": info.get("uploader", ""),
        "webpage_url": info.get("webpage_url", url),
        "formats": unique,
    }


def download_video(url, format_id="best", output_dir=None, progress_callback=None, cookies=None):
    """Download a video using yt-dlp.

    Args:
        url: Video URL
        format_id: Format preset key (see QUALITY_PRESETS) or yt-dlp format string
        output_dir: Directory to save to (default: ~/Videos)
        progress_callback: Called with (downloaded_bytes, total_bytes, speed, eta)
        cookies: Optional Netscape-format cookie string

    Returns:
        Path to the downloaded file.
    """
    if output_dir is None:
        output_dir = os.path.join(os.path.expanduser("~"), "Videos")
    os.makedirs(output_dir, exist_ok=True)

    format_str = QUALITY_PRESETS.get(format_id, format_id)

    t0 = time.time()

    def _progress_hook(d):
        if d["status"] == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            if progress_callback:
                progress_callback(downloaded, total, speed, eta)
        elif d["status"] == "finished":
            # Download complete; post-processing (merge) begins — signal to stop spinner
            if progress_callback:
                progress_callback(-1, -1, 0, 0)

    extra = {
        "format": format_str,
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "progress_hooks": [_progress_hook],
        "merge_output_format": "mp4",
        "continuedl": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "writethumbnail": False,
    }
    url = _clean_url(url)
    opts, cookie_file = _build_opts(extra=extra, cookies=cookies)

    logger.info("开始下载视频: %s (quality=%s)", os.path.basename(url[:60]), format_id)
    filepath = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # extract_info with download=True returns the final filepath
            filepath = info.get("requested_downloads", [{}])[0].get("filepath", "")
            if not filepath:
                # Fallback: try to find it from the filename
                filename = info.get("requested_downloads", [{}])[0].get("filename", "")
                if filename and os.path.isfile(filename):
                    filepath = filename
    finally:
        if cookie_file:
            os.unlink(cookie_file)

    # Clean up yt-dlp intermediate files left behind after merging
    _cleanup_intermediate_files(output_dir, filepath)

    elapsed = time.time() - t0
    if filepath and os.path.isfile(filepath):
        size_mb = os.path.getsize(filepath) / 1048576
        logger.info("视频下载完成: %s (%.1f MB, %.1fs)", os.path.basename(filepath), size_mb, elapsed)
    else:
        logger.warning("视频下载可能失败: 未找到输出文件 (filepath=%s)", filepath)
    return filepath


def _cleanup_intermediate_files(output_dir, final_filepath):
    """Remove yt-dlp orphaned intermediate files after merging.

    When yt-dlp downloads bestvideo+bestaudio separately and merges via
    ffmpeg, it sometimes leaves behind .f{id}.mp4 / .f{id}.m4a fragments
    and stuck .temp.mp4 files.  Clean them up so only the final file remains.
    """
    import glob, re
    if not output_dir or not os.path.isdir(output_dir):
        return
    # Recover .temp.mp4 → .mp4 (yt-dlp sometimes gets stuck with temp name)
    for f in glob.glob(os.path.join(output_dir, "*.temp.mp4")):
        final = f.replace(".temp.mp4", ".mp4")
        if not os.path.exists(final):
            try:
                os.rename(f, final)
                if final_filepath and f.endswith(os.path.basename(final_filepath or "") + ".temp.mp4"):
                    pass  # actual filepath was the temp; it's now renamed
            except OSError:
                pass
    # Delete orphaned intermediate stream fragments
    for pattern in ("*.f[0-9]*.mp4", "*.f[0-9]*.m4a", "*.f[0-9]*.webm", "*.f[0-9]*.m4s"):
        for f in glob.glob(os.path.join(output_dir, pattern)):
            try:
                os.remove(f)
                logger.debug("已清理中间文件: %s", os.path.basename(f))
            except OSError:
                pass
