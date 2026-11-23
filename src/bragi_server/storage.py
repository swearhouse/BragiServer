import copy
import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path
from threading import RLock
from urllib.parse import parse_qs, urlsplit


_lock = RLock()
_file_cache: dict[str, tuple[tuple[int, int] | None, object]] = {}
DEFAULT_CAST_BATCH_SIZE = 16
MAX_CAST_BATCH_SIZE = 5000
TRACK_ID_PREFIX = "trk_"
_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be",
}


class StorageError(Exception):
    """A data file could not be safely used."""


class PlaylistChangedError(Exception):
    """A playlist's tracks changed since the client read them."""


def parse_allowed_origins(value: str) -> list[str]:
    origins = []
    for item in re.split(r"[,\r\n]+", value):
        origin = item.strip()
        if not origin:
            continue
        try:
            parsed = urlsplit(origin)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname is not None
                and parsed.username is None
                and parsed.password is None
                and not parsed.path
                and "?" not in origin
                and "#" not in origin
                and "*" not in origin
                and not parsed.netloc.endswith(":")
                and parsed.port != 0
                and not any(character.isspace() for character in origin)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Enter exact HTTP(S) origins without paths or trailing slashes.")
        if origin not in origins:
            origins.append(origin)
    return origins


def valid_video_url(url: str) -> bool:
    try:
        parsed = urlsplit(url.strip())
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _YOUTUBE_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            return False
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname == "youtu.be":
            return len(parts) == 1 and bool(parts[0])
        if parsed.path == "/watch":
            ids = parse_qs(parsed.query, keep_blank_values=True).get("v", [])
            return len(ids) == 1 and bool(ids[0].strip())
        return len(parts) == 2 and parts[0] in {"embed", "shorts", "live"} and bool(parts[1])
    except ValueError:
        return False


def new_track_id() -> str:
    return f"{TRACK_ID_PREFIX}{uuid.uuid4().hex}"


def _legacy_track_id(playlist_id: str, index: int, link: str) -> str:
    value = f"bragi:track:{playlist_id}:{index}:{link}"
    return f"{TRACK_ID_PREFIX}{uuid.uuid5(uuid.NAMESPACE_URL, value).hex}"


def _clean_track_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _valid_video(video: object) -> bool:
    if not isinstance(video, dict) or set(video) not in (
        {"name", "link"}, {"id", "name", "link"}
    ):
        return False
    if (
        not isinstance(video.get("name"), str) or not video["name"].strip()
        or not isinstance(video.get("link"), str) or not valid_video_url(video["link"])
    ):
        return False
    return "id" not in video or _clean_track_id(video["id"]) is not None


def _normalize_video(video: dict, playlist_id: str, index: int) -> dict:
    track_id = _clean_track_id(video.get("id"))
    if track_id is None:
        track_id = _legacy_track_id(playlist_id, index, video["link"])
    return {"id": track_id, "name": video["name"], "link": video["link"]}


def _track_identity(video: dict) -> dict:
    return {"name": video["name"], "link": video["link"]}


def tracks_match(stored: list[dict], expected: list[dict]) -> bool:
    if len(stored) != len(expected):
        return False
    for current, candidate in zip(stored, expected):
        if _track_identity(current) != _track_identity(candidate):
            return False
        if "id" in candidate and current.get("id") != _clean_track_id(candidate["id"]):
            return False
    return True


def _read_json(path: Path, *, missing: object = None) -> object:
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError as error:
        if missing is not None:
            return missing
        raise StorageError(f"Missing {path.name}.") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StorageError(f"Could not read {path.name}.") from error


def _cache_key(path: Path) -> str:
    return str(path.resolve())


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _read_cached_json(path: Path, *, missing: object = None) -> object:
    key = _cache_key(path)
    signature = _file_signature(path)
    with _lock:
        cached = _file_cache.get(key)
        if cached is not None and cached[0] == signature:
            return copy.deepcopy(cached[1])
    data = _read_json(path, missing=missing)
    if signature is not None:
        with _lock:
            _file_cache[key] = (signature, data)
            if len(_file_cache) > 64:
                _file_cache.pop(next(iter(_file_cache)))
    return copy.deepcopy(data)


def clear_storage_caches() -> None:
    with _lock:
        _file_cache.clear()


def _write_json(path: Path, data: object) -> None:
    temporary_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as file:
            temporary_path = Path(file.name)
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary_path, path)
        with _lock:
            _file_cache.pop(_cache_key(path), None)
    except (OSError, TypeError, ValueError) as error:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).exception("Could not clean up temporary data file")
        raise StorageError(f"Could not save {path.name}.") from error


def read_settings(directory: Path) -> dict:
    data = _read_cached_json(directory / "settings.json")
    if not isinstance(data, dict):
        raise StorageError("settings.json has an invalid format.")
    origins = data.get("api_allowed_origins")
    if not isinstance(origins, list) or any(not isinstance(item, str) for item in origins):
        raise StorageError("settings.json has an invalid format.")
    try:
        if parse_allowed_origins("\n".join(origins)) != origins:
            raise ValueError("Origins must be distinct exact values.")
    except ValueError as error:
        raise StorageError("settings.json contains invalid API origins.") from error
    batch_size = data.get("cast_batch_size", DEFAULT_CAST_BATCH_SIZE)
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_CAST_BATCH_SIZE:
        raise StorageError("settings.json contains an invalid Cast batch size.")
    data["cast_batch_size"] = batch_size
    return data


def initialize_settings(directory: Path) -> None:
    with _lock:
        path = directory / "settings.json"
        missing = object()
        if _read_json(path, missing=missing) is missing:
            _write_json(path, {"api_allowed_origins": [], "cast_batch_size": DEFAULT_CAST_BATCH_SIZE})
        read_settings(directory)


def initialize_track_ids(directory: Path) -> None:
    with _lock:
        path = directory / "user_playlists.json"
        raw = _read_json(path, missing={})
        library = read_playlists(directory)
        if raw != library:
            _write_json(path, library)


def save_settings(directory: Path, origins: list[str], batch_size: int) -> None:
    if parse_allowed_origins("\n".join(origins)) != origins:
        raise ValueError("Origins must be distinct exact values.")
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_CAST_BATCH_SIZE:
        raise ValueError(f"Cast batch size must be between 1 and {MAX_CAST_BATCH_SIZE}.")
    with _lock:
        settings = read_settings(directory)
        settings["api_allowed_origins"] = origins
        settings["cast_batch_size"] = batch_size
        _write_json(directory / "settings.json", settings)


def save_allowed_origins(directory: Path, origins: list[str]) -> None:
    with _lock:
        save_settings(directory, origins, read_settings(directory)["cast_batch_size"])


def read_users(directory: Path) -> list[dict[str, str]]:
    data = _read_cached_json(directory / "users.json")
    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, list) or any(
        not isinstance(user, dict)
        or not isinstance(user.get("username"), str)
        or not user["username"]
        or not isinstance(user.get("password"), str)
        or not user["password"]
        for user in users
    ):
        raise StorageError("users.json has an invalid format.")
    names = [user["username"] for user in users]
    if len(names) != len(set(names)):
        raise StorageError("users.json contains duplicate usernames.")
    return users


def read_playlists(directory: Path) -> dict[str, list[dict]]:
    data = _read_json(directory / "user_playlists.json", missing={})
    if not isinstance(data, dict):
        raise StorageError("user_playlists.json has an invalid format.")
    for username, playlists in data.items():
        if not isinstance(username, str) or not isinstance(playlists, list):
            raise StorageError("user_playlists.json has an invalid format.")
        ids = set()
        for playlist in playlists:
            if (
                not isinstance(playlist, dict)
                or set(playlist) != {"id", "name", "videos"}
                or not isinstance(playlist.get("id"), str)
                or not playlist["id"].strip()
                or not isinstance(playlist.get("name"), str)
                or not playlist["name"].strip()
                or not isinstance(playlist.get("videos"), list)
                or playlist["id"] in ids
            ):
                raise StorageError("user_playlists.json has an invalid format or duplicate IDs.")
            ids.add(playlist["id"])
            normalized = []
            seen_track_ids = set()
            for index, video in enumerate(playlist["videos"]):
                if not _valid_video(video):
                    raise StorageError(
                        "user_playlists.json has an invalid format or duplicate IDs."
                    )
                item = _normalize_video(video, playlist["id"], index)
                if item["id"] in seen_track_ids:
                    raise StorageError(
                        "user_playlists.json has an invalid format or duplicate IDs."
                    )
                seen_track_ids.add(item["id"])
                normalized.append(item)
            playlist["videos"] = normalized
    return data


def create_user(directory: Path, username: str, password: str) -> None:
    with _lock:
        users = read_users(directory)
        playlists = read_playlists(directory)
        if any(user["username"] == username for user in users):
            raise ValueError("That username already exists.")
        if username in playlists:
            raise ValueError("Playlist data already exists for that username; resolve it first.")
        users.append({"username": username, "password": password})
        _write_json(directory / "users.json", {"users": users})


def delete_user(directory: Path, username: str) -> bool:
    with _lock:
        users = read_users(directory)
        playlists = read_playlists(directory)
        if username == "admin":
            raise ValueError("The admin account cannot be deleted.")
        remaining = [user for user in users if user["username"] != username]
        if len(remaining) == len(users):
            return False
        _write_json(directory / "users.json", {"users": remaining})
        if username in playlists:
            del playlists[username]
            try:
                _write_json(directory / "user_playlists.json", playlists)
            except StorageError as error:
                try:
                    _write_json(directory / "users.json", {"users": users})
                except StorageError as rollback_error:
                    raise StorageError(
                        "Playlist deletion and user rollback failed; restore data from backup."
                    ) from rollback_error
                raise StorageError("Playlist deletion failed; user deletion was rolled back.") from error
        return True


def save_playlist(directory: Path, username: str, playlist: dict, *, create: bool) -> bool:
    with _lock:
        users = read_users(directory)
        if not any(user["username"] == username for user in users):
            return False
        library = read_playlists(directory)
        playlists = library.setdefault(username, [])
        if create:
            playlists.append(playlist)
        else:
            index = next(
                (index for index, item in enumerate(playlists) if item["id"] == playlist["id"]),
                None,
            )
            if index is None:
                return False
            playlists[index] = playlist
        _write_json(directory / "user_playlists.json", library)
        return True


def delete_playlist(directory: Path, username: str, playlist_id: str) -> bool:
    with _lock:
        library = read_playlists(directory)
        playlists = library.get(username, [])
        remaining = [playlist for playlist in playlists if playlist["id"] != playlist_id]
        if len(remaining) == len(playlists):
            return False
        library[username] = remaining
        _write_json(directory / "user_playlists.json", library)
        return True


def rename_playlist(directory: Path, username: str, playlist_id: str, name: str) -> dict | None:
    with _lock:
        library = read_playlists(directory)
        playlist = next(
            (item for item in library.get(username, []) if item["id"] == playlist_id),
            None,
        )
        if playlist is None:
            return None
        playlist["name"] = name
        _write_json(directory / "user_playlists.json", library)
        return playlist


def append_playlist_track(
    directory: Path, username: str, playlist_id: str, video: dict[str, str]
) -> dict | None:
    with _lock:
        library = read_playlists(directory)
        playlist = next(
            (item for item in library.get(username, []) if item["id"] == playlist_id),
            None,
        )
        if playlist is None:
            return None
        if len(playlist["videos"]) >= 5000:
            raise ValueError("The playlist already contains 5000 tracks.")
        playlist["videos"].append({
            "id": new_track_id(), "name": video["name"], "link": video["link"]
        })
        _write_json(directory / "user_playlists.json", library)
        return playlist


def remove_playlist_track(
    directory: Path, username: str, playlist_id: str,
    index: int, expected_videos: list[dict[str, str]],
) -> dict | None:
    return _change_playlist_track(directory, username, playlist_id, index, expected_videos, None)


def replace_playlist_track(
    directory: Path, username: str, playlist_id: str,
    index: int, expected_videos: list[dict[str, str]], video: dict[str, str],
) -> dict | None:
    return _change_playlist_track(directory, username, playlist_id, index, expected_videos, video)


def _change_playlist_track(
    directory: Path, username: str, playlist_id: str,
    index: int, expected_videos: list[dict[str, str]], replacement: dict[str, str] | None,
) -> dict | None:
    with _lock:
        library = read_playlists(directory)
        playlist = next(
            (item for item in library.get(username, []) if item["id"] == playlist_id),
            None,
        )
        if playlist is None:
            return None
        if not tracks_match(playlist["videos"], expected_videos):
            raise PlaylistChangedError
        if replacement is None:
            del playlist["videos"][index]
        else:
            playlist["videos"][index] = {
                "id": new_track_id(), "name": replacement["name"],
                "link": replacement["link"],
            }
        _write_json(directory / "user_playlists.json", library)
        return playlist


def remove_playlist_track_by_id(
    directory: Path, username: str, playlist_id: str, track_id: str
) -> dict | None:
    with _lock:
        library = read_playlists(directory)
        playlist = next(
            (item for item in library.get(username, []) if item["id"] == playlist_id),
            None,
        )
        if playlist is None:
            return None
        index = next(
            (index for index, video in enumerate(playlist["videos"])
             if video["id"] == track_id),
            None,
        )
        if index is None:
            return None
        del playlist["videos"][index]
        _write_json(directory / "user_playlists.json", library)
        return playlist


def replace_playlist_track_by_id(
    directory: Path, username: str, playlist_id: str,
    track_id: str, video: dict[str, str],
) -> dict | None:
    with _lock:
        library = read_playlists(directory)
        playlist = next(
            (item for item in library.get(username, []) if item["id"] == playlist_id),
            None,
        )
        if playlist is None:
            return None
        index = next(
            (index for index, item in enumerate(playlist["videos"])
             if item["id"] == track_id),
            None,
        )
        if index is None:
            return None
        playlist["videos"][index] = {
            "id": new_track_id(), "name": video["name"], "link": video["link"]
        }
        _write_json(directory / "user_playlists.json", library)
        return playlist


def reorder_playlist_tracks(
    directory: Path, username: str, playlist_id: str, track_ids: list[str]
) -> dict | None:
    with _lock:
        library = read_playlists(directory)
        playlist = next(
            (item for item in library.get(username, []) if item["id"] == playlist_id),
            None,
        )
        if playlist is None:
            return None
        current_ids = [video["id"] for video in playlist["videos"]]
        if len(track_ids) != len(current_ids) or set(track_ids) != set(current_ids):
            raise PlaylistChangedError
        by_id = {video["id"]: video for video in playlist["videos"]}
        playlist["videos"] = [by_id[track_id] for track_id in track_ids]
        _write_json(directory / "user_playlists.json", library)
        return playlist
