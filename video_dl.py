"""Video download module — wraps yt-dlp for info extraction and download."""

import logging
import os
import tempfile
import time

import requests
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

# Bilibili request headers — shared across modules
BILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}

# Shared session for B站 API calls (avoids repeated TCP+TLS handshakes)
_bili_session = requests.Session()
_bili_session.headers.update(BILI_HEADERS)


def _make_cookiefile(cookies):
    """Write cookie string to a temp file for yt-dlp. Returns path or None."""
    if not cookies or not cookies.strip():
        return None
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    tmp.write(cookies.strip())
    tmp.close()
    return tmp.name


def _clean_url(url):
    """Strip tracking parameters that can confuse yt-dlp.

    Removes Bilibili tracking/analytics query params while preserving
    functional params like ``p`` (page), ``t`` (timestamp), and ``q``.
    """
    import re
    TRACK_PARAMS = (
        "spm_id_from|vd_source|share_source|share_medium|share_plat|"
        "share_session_id|share_tag|timestamp|unique_k|up_id|from_source|"
        "from_spmid|plat_id|session_id|trackid|"
        "broadcast_type|share_from|ugc_share|search_group|source_type|"
        "csr|refer_source|refer_plat|refer_session_id|from"
    )
    # Remove each tracking param: [?&]key=value  (value is everything up to the next & or end)
    url = re.sub(rf'[?&]({TRACK_PARAMS})=[^&]*', '', url)
    # Fix edge case: if the only remaining params were all tracking, we're left
    # with a dangling '?' — strip it.  If the first remaining param now has a
    # leading '&', convert it to '?' so the URL stays well-formed.
    # e.g. /video/BV123/&legit_param=1 → /video/BV123/?legit_param=1
    if '&' in url and '?' not in url:
        url = url.replace('&', '?', 1)
    return url.rstrip('?')


def _extract_bvid(url_or_id):
    """Extract a BV/AV id from a Bilibili URL or yt-dlp entry id.

    Handles:
        https://www.bilibili.com/video/BV1xx411c7mD/
        https://www.bilibili.com/video/av123456/
        https://m.bilibili.com/video/BV1xx411c7mD/
        BV1xx411c7mD  (bare id)
    Returns the id string (BV... or av...) or None.
    """
    import re
    # Already a bare BV/AV id?
    if re.match(r'^(BV[A-Za-z0-9]+|av\d+)$', url_or_id):
        return url_or_id
    # Extract from full URL
    m = re.search(r'bilibili\.com/video/([A-Za-z0-9]+)', url_or_id)
    return m.group(1) if m else None


def _extract_season_id(url_or_info):
    """Extract a Bilibili season/ss id from a URL or yt-dlp info dict.

    Accepts a string URL or a dict (yt-dlp info).
    Returns the season id as int, or None.

    Only matches ``ss`` (season) patterns — NOT ``ep`` (episode) patterns,
    since those are episode ids, not season ids.  For episode URLs, use
    yt-dlp's ``season_id`` field (passed via info dict) instead.
    """
    import re
    if isinstance(url_or_info, dict):
        sid = url_or_info.get("season_id")
        if sid:
            return int(sid)
        # Try to get it from the webpage_url
        url_or_info = url_or_info.get("webpage_url", "")
    # String: only match ss(\d+) — ep(\d+) is an episode id, not a season id
    for pat in (r'bilibili\.com/bangumi/play/ss(\d+)',
                r'bilibili\.com/cheese/play/ss(\d+)'):
        m = re.search(pat, url_or_info)
        if m:
            return int(m.group(1))
    return None


def _is_bilibili(info_or_url):
    """Check if a yt-dlp info dict or URL is from Bilibili.

    Uses extractor_key for accuracy when available; falls back to domain check.
    """
    if isinstance(info_or_url, dict):
        extractor = (info_or_url.get("extractor_key") or "").lower()
        if extractor:
            return extractor.startswith("bili")
        info_or_url = info_or_url.get("webpage_url", "")
    return "bilibili.com" in info_or_url or "b23.tv" in info_or_url


def _build_opts(extra=None, cookies=None):
    """Build yt-dlp options dict with common headers + optional cookiefile."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "playlist_items": "1:200",  # 合集最多取前200个
        "http_headers": dict(BILI_HEADERS),
    }
    if extra:
        opts.update(extra)
    cookie_file = _make_cookiefile(cookies)
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts, cookie_file


# ── Bilibili API helpers ──────────────────────────────────────────────


def extract_audio_url(track_id, cookies=None):
    """从 B站视频提取音频流 URL，用于音乐下载。

    Args:
        track_id: B站视频 ID (BV号)
        cookies: Optional Netscape-format cookie string

    Returns:
        (audio_url, title) tuple. Raises Exception if no audio stream found.
    """
    bili_url = f"https://www.bilibili.com/video/{track_id}/"
    opts, cookie_file = _build_opts(
        extra={"format": "bestaudio/best"},
        cookies=cookies,
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(bili_url, download=False)
    finally:
        if cookie_file:
            os.unlink(cookie_file)

    audio_url = None
    # Prefer pure audio formats (acodec != none, vcodec == none)
    for fmt in info.get("formats", []):
        if fmt.get("acodec") != "none" and fmt.get("vcodec") == "none":
            audio_url = fmt.get("url")
            break
    # Fallback: any format with audio
    if not audio_url:
        for fmt in info.get("formats", []):
            if fmt.get("acodec") != "none":
                audio_url = fmt.get("url")
                break
    if not audio_url:
        raise Exception("未找到 B站音频流")

    title = info.get("title", "")
    logger.info("yt-dlp 音频提取成功: %s", title[:50])
    return audio_url, title


def _bilibili_view_api(bvid):
    """Call B站 /x/web-interface/view for a given BV id. Returns data dict or None."""
    try:
        resp = _bili_session.get(
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
            timeout=10)
        return (resp.json().get("data") or {}) if resp.ok else None
    except requests.RequestException:
        logger.debug("B站 view API 请求失败: bvid=%s", bvid, exc_info=True)
        return None


def _get_bilibili_collection(bvid):
    """Query B站 view API for ugc_season (合集). Returns collection-format dict or None."""
    data = _bilibili_view_api(bvid)
    if not data:
        return None

    ugc = data.get("ugc_season")
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

    return {
        "title": ugc.get("title", ""),
        "thumbnail": ugc.get("cover", ""),
        "is_playlist": True,
        "playlist_count": len(items),
        "playlist_items": items,
    }


def _get_bilibili_bangumi_season(season_id):
    """Query B站 PGC API for full season episode list.

    Filters to main-section episodes only (section_type == 0).
    Returns collection-format dict or None.
    """
    try:
        resp = _bili_session.get(
            f"https://api.bilibili.com/pgc/view/web/season?season_id={season_id}",
            timeout=10)
        data = resp.json()
    except requests.RequestException:
        logger.debug("B站 PGC API 请求失败: season_id=%s", season_id, exc_info=True)
        return None

    result = data.get("result") or {}
    episodes = result.get("episodes", [])
    if not episodes:
        return None

    items = []
    for ep in episodes:
        if ep.get("section_type") != 0:
            continue
        items.append({
            "id": str(ep.get("id") or ep.get("aid", "")),
            "url": ep.get("link") or ep.get("share_url") or "",
            "title": ep.get("long_title") or ep.get("title") or "?",
            "display_title": ep.get("long_title") or ep.get("title") or "?",
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


def _enrich_multipart_titles(bvid, items):
    """Fill real page titles for B站 multi-part videos via view API."""
    import urllib.parse

    data = _bilibili_view_api(bvid)
    if not data:
        return None

    pages = data.get("pages", [])
    if not pages:
        return None

    title_map = {p["page"]: p["part"] for p in pages if p.get("page") and p.get("part")}

    changed = False
    for item in items:
        parsed = urllib.parse.urlparse(item.get("url", ""))
        qs = urllib.parse.parse_qs(parsed.query)
        p_vals = qs.get("p", [])
        if p_vals:
            try:
                pn = int(p_vals[0])
            except ValueError:
                continue
            if pn in title_map:
                item["title"] = title_map[pn]
                item["display_title"] = title_map[pn]
                changed = True

    return items if changed else None


# ── Bilibili quality name → yt-dlp format selector ────────────────────
# Used to build quality options that show ALL available tiers (杜比/4K/1080P…)
# even when the user is not logged in.  The format selectors auto-adapt:
# with cookies they pick the real tier; without, they fall back to the best
# accessible stream.
#
# Format: {quality_number: (display_label, format_selector)}
# Quality numbers lower than 32 are omitted (no one wants <360P).
_BILI_QN_OPTIONS = {
    127: ("8K",        "bestvideo[height<=4320]+bestaudio/best[height<=4320]"),
    126: ("杜比视界",  "bestvideo+bestaudio/best"),
    125: ("HDR",       "bestvideo+bestaudio/best"),
    120: ("4K",        "bestvideo[height<=2160]+bestaudio/best[height<=2160]"),
    116: ("1080P60",   "bestvideo[height<=1080]+bestaudio/best[height<=1080]"),
    112: ("1080P+",    "bestvideo[height<=1080]+bestaudio/best[height<=1080]"),
    80:  ("1080P",     "bestvideo[height<=1080]+bestaudio/best[height<=1080]"),
    74:  ("720P60",    "bestvideo[height<=720]+bestaudio/best[height<=720]"),
    64:  ("720P",      "bestvideo[height<=720]+bestaudio/best[height<=720]"),
    32:  ("480P",      "bestvideo[height<=480]+bestaudio/best[height<=480]"),
    16:  ("360P",      "bestvideo[height<=360]+bestaudio/best[height<=360]"),
}


def _bilibili_get_qualities(bvid):
    """Query B站 PlayURL API for the list of available quality tiers.

    Returns a list of (label, format_selector) tuples sorted highest-first,
    or an empty list on failure.
    """
    # First get cid from view API
    data = _bilibili_view_api(bvid)
    if not data:
        return []
    cid = data.get("cid")
    if not cid:
        # Try first page's cid
        pages = data.get("pages") or []
        cid = pages[0].get("cid") if pages else None
    if not cid:
        return []

    try:
        resp = _bili_session.get(
            "https://api.bilibili.com/x/player/playurl",
            params={"bvid": bvid, "cid": cid, "qn": "127", "fnval": "4048", "fourk": "1"},
            timeout=10,
        )
        pdata = (resp.json().get("data") or {}) if resp.ok else {}
    except requests.RequestException:
        logger.debug("B站 PlayURL API 请求失败", exc_info=True)
        return []

    accept_quality = pdata.get("accept_quality") or []
    if not accept_quality:
        return []

    # accept_quality is sorted highest-first by B站; deduplicate labels
    options = []
    seen_labels = set()
    for qn in accept_quality:
        entry = _BILI_QN_OPTIONS.get(qn)
        if not entry:
            continue
        label, selector = entry
        if label in seen_labels:
            continue
        seen_labels.add(label)
        options.append((label, selector))

    return options


def get_video_info(url, cookies=None):
    """Extract video metadata using yt-dlp.

    Args:
        url: Video URL (any format yt-dlp supports, including b23.tv short links).
        cookies: Optional Netscape-format cookie string.

    Returns dict with: title, thumbnail, duration, uploader, formats
    """
    import re

    url = _clean_url(url)
    opts, cookie_file = _build_opts(cookies=cookies)
    # Use extract_flat for fast playlist handling
    opts["extract_flat"] = True
    opts["playlist_items"] = "1:200"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    finally:
        if cookie_file:
            os.unlink(cookie_file)

    # Use yt-dlp's resolved URL for subsequent matching.
    # This handles b23.tv short links, mobile redirects, etc. — the final
    # URL will be the canonical bilibili.com form.
    resolved_url = info.get("webpage_url", url)
    is_bili = _is_bilibili(info)

    # ── Playlist / collection ──────────────────────────────────────────
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
            cover_cf = None
            try:
                cover_opts, cover_cf = _build_opts(cookies=cookies)
                cover_opts["playlist_items"] = "1:1"
                with yt_dlp.YoutubeDL(cover_opts) as ydl:
                    cover_info = ydl.extract_info(url, download=False)
                first_entry = (cover_info.get("entries") or [{}])[0]
                thumbnail = first_entry.get("thumbnail", "")
            except Exception:
                logger.debug("封面提取失败，跳过", exc_info=True)
            finally:
                if cover_cf:
                    os.unlink(cover_cf)

        # If titles are all placeholder "Part N", try to enrich from Bilibili APIs
        # (yt-dlp extract_flat doesn't return real episode titles)
        if is_bili and items and all(
            t["title"] == f"Part {i + 1}" for i, t in enumerate(items)
        ):
            # 1) Bangumi / PGC season — prefer season_id from yt-dlp, fall back to URL regex
            season_id = _extract_season_id(info)
            if not season_id:
                season_id = _extract_season_id(resolved_url)
            if season_id:
                season = _get_bilibili_bangumi_season(season_id)
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

            # 2) B站 multi-part video — try BVID from both info dict and resolved URL
            bvid = _extract_bvid(info.get("id", "")) or _extract_bvid(resolved_url)
            if bvid:
                enriched = _enrich_multipart_titles(bvid, items)
                if enriched:
                    items = enriched

        return {
            "title": info.get("title", ""),
            "thumbnail": thumbnail,
            "uploader": info.get("uploader", ""),
            "webpage_url": resolved_url,
            "is_playlist": True,
            "playlist_count": playlist_count,
            "playlist_items": items,
        }

    # ── Single video ───────────────────────────────────────────────────
    # Check if this Bilibili video belongs to a wider collection / season

    if is_bili:
        # 1) UGC 合集 — need BVID from resolved URL or yt-dlp id
        bvid = _extract_bvid(resolved_url) or _extract_bvid(info.get("id", ""))
        if bvid:
            collection = _get_bilibili_collection(bvid)
            if collection:
                return collection

    # 2) Bangumi / PGC season — use season_id from yt-dlp (works for any URL format)
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

    # ── Build quality options ──────────────────────────────────────────
    # Strategy: group by resolution, showing the best format per height.
    # For DASH sources (Bilibili — video + audio are separate streams) we
    # build yt-dlp format strings that auto-merge.  For combined sources
    # (YouTube) we use the actual format ids directly.
    raw = info.get("formats", [])

    # Classify formats
    combined_fmts = []        # has both video and audio
    video_by_height = {}      # height -> best video-only format
    audio_fmts = []           # audio-only formats

    for f in raw:
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        ext = f.get("ext", "")
        has_v = vcodec and vcodec != "none"
        has_a = acodec and acodec != "none"

        # Skip non-playable
        if "storyboard" in (f.get("format_note") or "").lower():
            continue
        if ext == "mhtml":
            continue

        if has_v and has_a:
            combined_fmts.append(f)
        elif has_v:
            height = f.get("height") or 0
            if not height:
                continue
            cur_best = video_by_height.get(height)
            if cur_best is None or (f.get("tbr") or 0) > (cur_best.get("tbr") or 0):
                video_by_height[height] = f
        elif has_a:
            audio_fmts.append(f)

    formats = []

    if combined_fmts:
        # Non-DASH source (YouTube etc.): use actual format ids
        seen_heights = set()
        for f in sorted(combined_fmts, key=lambda x: x.get("height") or 0, reverse=True):
            height = f.get("height") or 0
            if height in seen_heights:
                continue
            seen_heights.add(height)
            res = f.get("resolution") or f"{height}p"
            formats.append({
                "id": f.get("format_id", ""),
                "ext": f.get("ext", "mp4"),
                "resolution": str(res),
                "filesize": f.get("filesize") or f.get("filesize_approx") or 0,
                "has_video": True,
                "has_audio": True,
                "tbr": f.get("tbr") or 0,
            })
        # Also add any DASH video resolutions we missed
        for height in sorted(video_by_height, reverse=True):
            if height in seen_heights:
                continue
            f = video_by_height[height]
            formats.append({
                "id": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
                "ext": f.get("ext", "mp4"),
                "resolution": f"{height}p",
                "filesize": 0,
                "has_video": True,
                "has_audio": True,
                "tbr": f.get("tbr") or 0,
            })
    else:
        # DASH-only source
        if is_bili:
            # Bilibili: query PlayURL API for the real quality tier list.
            # Even without cookies the API reports ALL tiers (杜比/4K/1080P+…);
            # the format selectors auto-adapt to what's actually accessible.
            bvid = _extract_bvid(resolved_url) or _extract_bvid(info.get("id", ""))
            if bvid:
                quality_list = _bilibili_get_qualities(bvid)
                if quality_list:
                    for label, selector in quality_list:
                        formats.append({
                            "id": selector,
                            "ext": "mp4",
                            "resolution": label,
                            "filesize": 0,
                            "has_video": True,
                            "has_audio": True,
                            "tbr": 0,
                        })
        if not formats:
            # Fallback: build from actually-available DASH heights
            for height in sorted(video_by_height, reverse=True):
                f = video_by_height[height]
                formats.append({
                    "id": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
                    "ext": f.get("ext", "mp4"),
                    "resolution": f"{height}p",
                    "filesize": f.get("filesize") or f.get("filesize_approx") or 0,
                    "has_video": True,
                    "has_audio": True,
                    "tbr": f.get("tbr") or 0,
                })

    # Audio-only option
    if audio_fmts:
        best_audio = max(audio_fmts, key=lambda x: x.get("tbr") or 0)
        formats.append({
            "id": "bestaudio/best",
            "ext": best_audio.get("ext", "m4a"),
            "resolution": "audio only",
            "filesize": 0,
            "has_video": False,
            "has_audio": True,
            "tbr": best_audio.get("tbr") or 0,
        })

    return {
        "title": info.get("title", ""),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration") or 0,
        "uploader": info.get("uploader", ""),
        "webpage_url": info.get("webpage_url", url),
        "formats": formats,
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
    filepath = _cleanup_intermediate_files(output_dir, filepath)

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

    Returns:
        Corrected filepath if a .temp.mp4 was renamed, else the original.
    """
    import glob, re
    corrected = final_filepath
    if not output_dir or not os.path.isdir(output_dir):
        return corrected
    # Recover .temp.mp4 → .mp4 (yt-dlp sometimes gets stuck with temp name)
    for f in glob.glob(os.path.join(output_dir, "*.temp.mp4")):
        final = f.replace(".temp.mp4", ".mp4")
        if not os.path.exists(final):
            try:
                os.rename(f, final)
                if final_filepath and os.path.basename(f) == os.path.basename(final_filepath or ""):
                    corrected = final  # update returned path to the renamed file
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
    return corrected
