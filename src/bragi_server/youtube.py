import re
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


def resolve_audio(video_id: str) -> dict[str, str]:
    options = {
        "format": "bestaudio[ext=m4a][protocol=https]/bestaudio[ext=webm][protocol=https]",
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
    return {"url": stream_url, "title": title, "content_type": content_type}
