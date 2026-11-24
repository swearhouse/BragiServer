import json
import math
import re
import threading
import time
import urllib.request
from typing import TypedDict
from urllib.parse import parse_qs, urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_AUDIO_TYPES = {"m4a": "audio/mp4", "webm": "audio/webm"}
_PLAYER_RESPONSE_RE = re.compile(r"ytInitialPlayerResponse\s*=\s*")
_PLAYER_PAGE_TIMEOUT = 15
_PLAYER_PAGE_MAX_BYTES = 5_000_000
_LOUDNESS_SOURCE = "youtube"
_RESOLVE_FRESH_SECONDS = 120
_RESOLVE_STALE_SECONDS = 600
_RESOLVE_CACHE_MAX_ENTRIES = 64
_TITLE_TTL_SECONDS = 86400
_TITLE_CACHE_MAX_ENTRIES = 512
_resolve_cache: dict[tuple[str, str], tuple[float, "AudioResolution"]] = {}
_resolve_cache_lock = threading.RLock()
_resolve_refreshing: set[tuple[str, str]] = set()
_resolve_generation = 0
_title_cache: dict[str, tuple[float, str]] = {}
_title_cache_lock = threading.RLock()


class LoudnessInfo(TypedDict):
    integrated_lufs: float
    target_lufs: float
    gain_db: float
    source: str


class AudioResolution(TypedDict):
    url: str
    title: str
    content_type: str
    loudness: LoudnessInfo | None


class ResolveError(Exception):
    """A video could not be resolved to a supported audio stream."""


class MetadataError(Exception):
    """The YouTube video title could not be retrieved."""


def youtube_video_id(value: str) -> str | None:
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _YOUTUBE_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            return None
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname == "youtu.be" and len(parts) == 1:
            video_id = parts[0]
        elif parsed.path == "/watch":
            values = parse_qs(parsed.query, keep_blank_values=True).get("v", [])
            video_id = values[0] if len(values) == 1 else ""
        elif len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}:
            video_id = parts[1]
        else:
            return None
    except ValueError:
        return None
    return video_id if _VIDEO_ID.fullmatch(video_id) else None


def video_title(video_id: str) -> str:
    now = time.monotonic()
    with _title_cache_lock:
        cached = _title_cache.pop(video_id, None)
        if cached is not None:
            stored_at, title = cached
            if now - stored_at < _TITLE_TTL_SECONDS:
                _title_cache[video_id] = cached
                return title

    title = _fetch_title(video_id)

    with _title_cache_lock:
        _title_cache.pop(video_id, None)
        _title_cache[video_id] = (time.monotonic(), title)
        while len(_title_cache) > _TITLE_CACHE_MAX_ENTRIES:
            _title_cache.pop(next(iter(_title_cache)))
    return title


def clear_title_cache() -> None:
    with _title_cache_lock:
        _title_cache.clear()


def _fetch_title(video_id: str) -> str:
    options = {
        "noplaylist": True,
        "skip_download": True,
        "ignore_no_formats_error": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
    except DownloadError:
        raise MetadataError("Could not retrieve the YouTube video title.") from None
    # yt-dlp may synthesize "title" when the extractor did not provide one.
    title = info.get("fulltitle") if isinstance(info, dict) else None
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
        raise MetadataError("Could not retrieve a valid YouTube video title.")
    return title.strip()


def resolve_audio(video_id: str) -> AudioResolution:
    return _resolve_audio(video_id, "bestaudio[ext=m4a][protocol=https]/bestaudio[ext=webm][protocol=https]")


def resolve_cast_audio(video_id: str) -> AudioResolution:
    return _resolve_audio(video_id, "bestaudio[ext=m4a][protocol=https]")


def clear_resolve_cache() -> None:
    global _resolve_generation
    with _resolve_cache_lock:
        _resolve_generation += 1
        _resolve_cache.clear()
        _resolve_refreshing.clear()


def _store_resolve_result(
    key: tuple[str, str], result: AudioResolution, generation: int | None = None
) -> None:
    with _resolve_cache_lock:
        if generation is not None and generation != _resolve_generation:
            return
        _resolve_cache.pop(key, None)
        _resolve_cache[key] = (time.monotonic(), result)
        while len(_resolve_cache) > _RESOLVE_CACHE_MAX_ENTRIES:
            _resolve_cache.pop(next(iter(_resolve_cache)))


def _copy_resolution(result: AudioResolution) -> AudioResolution:
    copied = dict(result)
    loudness = copied.get("loudness")
    if loudness is not None:
        copied["loudness"] = dict(loudness)
    return copied


def _resolve_audio(video_id: str, audio_format: str) -> AudioResolution:
    key = (video_id, audio_format)
    now = time.monotonic()
    with _resolve_cache_lock:
        cached = _resolve_cache.pop(key, None)
        if cached is not None:
            stored_at, result = cached
            age = now - stored_at
            if age < _RESOLVE_FRESH_SECONDS:
                _resolve_cache[key] = cached
                return _copy_resolution(result)
            if age < _RESOLVE_STALE_SECONDS:
                _resolve_cache[key] = cached
                _schedule_resolve_refresh(key, video_id, audio_format)
                return _copy_resolution(result)

    result = _fetch_audio(video_id, audio_format)
    _store_resolve_result(key, result)
    return _copy_resolution(result)


def _schedule_resolve_refresh(
    key: tuple[str, str], video_id: str, audio_format: str
) -> None:
    with _resolve_cache_lock:
        if key in _resolve_refreshing:
            return
        _resolve_refreshing.add(key)
        generation = _resolve_generation
    thread = threading.Thread(
        target=_refresh_resolve_cache,
        args=(key, video_id, audio_format, generation),
        daemon=True,
    )
    try:
        thread.start()
    except Exception:
        with _resolve_cache_lock:
            _resolve_refreshing.discard(key)


def _refresh_resolve_cache(
    key: tuple[str, str], video_id: str, audio_format: str, generation: int
) -> None:
    result: AudioResolution | None = None
    try:
        result = _fetch_audio(video_id, audio_format)
    except Exception:
        pass
    finally:
        with _resolve_cache_lock:
            if result is not None and generation == _resolve_generation:
                _resolve_cache.pop(key, None)
                _resolve_cache[key] = (time.monotonic(), result)
                while len(_resolve_cache) > _RESOLVE_CACHE_MAX_ENTRIES:
                    _resolve_cache.pop(next(iter(_resolve_cache)))
            _resolve_refreshing.discard(key)


def _fetch_audio(video_id: str, audio_format: str) -> AudioResolution:
    options = {
        "format": audio_format,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
    except DownloadError as error:
        raise ResolveError("YouTube audio resolution failed.") from error

    if not isinstance(info, dict):
        raise ResolveError("No compatible direct audio stream was found.")
    stream_url = info.get("url")
    title = info.get("title")
    content_type = _AUDIO_TYPES.get(info.get("ext"))
    if (
        not isinstance(stream_url, str)
        or not stream_url.startswith("https://")
        or not isinstance(title, str)
        or not title.strip()
        or content_type is None
    ):
        raise ResolveError("No compatible direct audio stream was found.")

    loudness: LoudnessInfo | None = None
    try:
        loudness = _extract_loudness(video_id, info)
    except Exception:
        pass
    return {
        "url": stream_url,
        "title": title,
        "content_type": content_type,
        "loudness": loudness,
    }


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _fetch_player_response(video_id: str) -> dict[str, object] | None:
    request = urllib.request.Request(
        f"https://www.youtube.com/watch?v={video_id}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_PLAYER_PAGE_TIMEOUT) as response:
            page = response.read(_PLAYER_PAGE_MAX_BYTES).decode("utf-8", "ignore")
    except Exception:
        return None
    match = _PLAYER_RESPONSE_RE.search(page)
    if match is None:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(page[match.end():])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _extract_loudness(video_id: str, info: dict[str, object]) -> LoudnessInfo | None:
    player_response = _fetch_player_response(video_id)
    if player_response is None:
        return None
    streaming_data = player_response.get("streamingData")
    if not isinstance(streaming_data, dict):
        return None
    formats = streaming_data.get("adaptiveFormats")
    if not isinstance(formats, list):
        return None
    candidates = [item for item in formats if isinstance(item, dict)]

    format_info: dict[str, object] | None = None
    stream_url = info.get("url")
    if isinstance(stream_url, str):
        format_info = next(
            (item for item in candidates if item.get("url") == stream_url), None
        )
    if format_info is None:
        format_id = info.get("format_id")
        if format_id is not None:
            matches = [
                item for item in candidates
                if str(item.get("itag")) == str(format_id)
            ]
            format_info = next(
                (
                    item for item in matches
                    if not item.get("isDrc") and not item.get("xtags")
                ),
                matches[0] if matches else None,
            )
    if format_info is None:
        return None

    attenuation = _finite_number(format_info.get("loudnessDb"))
    integrated = _finite_number(format_info.get("trackAbsoluteLoudnessLkfs"))
    if attenuation is None or integrated is None:
        return None
    gain_db = round(-attenuation, 2)
    target_lufs = round(integrated - attenuation, 2)
    if not -30 <= gain_db <= 30 or not -70 <= target_lufs <= 0:
        return None
    return {
        "integrated_lufs": round(integrated, 2),
        "target_lufs": target_lufs,
        "gain_db": gain_db,
        "source": _LOUDNESS_SOURCE,
    }
