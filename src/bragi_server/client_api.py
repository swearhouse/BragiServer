import hmac
import http.client
import random
import uuid

from flask import Blueprint, Response, current_app, g, jsonify, redirect, request, url_for

from bragi_server import storage
from bragi_server.audio_proxy import (
    CHUNK_SIZE,
    CAST_TOKEN_LIFETIME,
    TOKEN_LIFETIME,
    AudioProxyError,
    InvalidAudioURL,
    TokenCapacityError,
    open_upstream,
    public_addresses,
    users_revision,
    validate_url,
)
from bragi_server.management import _fingerprint
from bragi_server.youtube import MetadataError, ResolveError, resolve_audio, resolve_cast_audio, video_title, youtube_video_id


client_api = Blueprint("client_api", __name__, url_prefix="/api/v1")


def _with_thumbnails(item: dict) -> dict:
    videos = []
    for video in item["videos"]:
        video_id = youtube_video_id(video["link"])
        thumbnail_url = (
            f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else None
        )
        videos.append({**video, "thumbnail_url": thumbnail_url})
    return {**item, "videos": videos}


def _playlist_video_title(video_id: str) -> str:
    try:
        return video_title(video_id)
    except MetadataError as error:
        current_app.extensions["bragi_event_log"].record(
            "resolve_error", str(error), context={"video_id": video_id}
        )
        current_app.logger.warning("Could not retrieve title for video %s", video_id)
        raise


def _valid_track_snapshot(index, videos) -> bool:
    return (
        type(index) is int and isinstance(videos, list)
        and 0 <= index < len(videos)
        and all(
            isinstance(video, dict) and set(video) == {"name", "link"}
            and isinstance(video["name"], str) and isinstance(video["link"], str)
            for video in videos
        )
    )


def _authenticated_username() -> str | None:
    credentials = request.authorization
    if credentials is not None and credentials.type.lower() == "basic":
        username = credentials.username
        password = credentials.password
        if isinstance(username, str) and isinstance(password, str):
            user = next(
                (user for user in storage.read_users(current_app.config["DATA_DIRECTORY"])
                 if user["username"] == username),
                None,
            )
            if user is not None and hmac.compare_digest(
                user["password"].encode("utf-8"), password.encode("utf-8")
            ):
                return username
    return None


def _authentication_required():
    response = jsonify(error="Authentication required.")
    response.status_code = 401
    response.headers["WWW-Authenticate"] = 'Basic realm="BragiServer API", charset="UTF-8"'
    return response


@client_api.before_request
def authenticate():
    settings = storage.read_settings(current_app.config["DATA_DIRECTORY"])
    g.api_allowed_origins = settings["api_allowed_origins"]
    g.cast_batch_size = settings["cast_batch_size"]
    if request.method == "OPTIONS":
        return "", 204
    if request.endpoint == "client_api.cast_stream":
        return None
    if request.endpoint == "client_api.audio_stream":
        if request.method == "HEAD":
            return jsonify(error="Method not allowed."), 405
        return None
    username = _authenticated_username()
    if username is None:
        return _authentication_required()
    g.api_username = username
    return None


@client_api.after_request
def client_api_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.vary.add("Origin")
    origin = request.headers.get("Origin")
    if origin in g.get("api_allowed_origins", ()):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Range"
        response.headers["Access-Control-Expose-Headers"] = (
            "Content-Length, Content-Range, Accept-Ranges"
        )
    return response


@client_api.errorhandler(storage.StorageError)
def handle_storage_error(error):
    current_app.logger.exception("Client API storage operation failed")
    current_app.extensions["bragi_event_log"].record(
        "exception", "Client API storage operation failed.", exception=error
    )
    return jsonify(error="Client API storage operation failed."), 500


@client_api.get("/playlists")
def playlists():
    library = storage.read_playlists(current_app.config["DATA_DIRECTORY"])
    return jsonify(
        playlists=[_with_thumbnails(item) for item in library.get(g.api_username, [])]
    )


@client_api.post("/playlists")
def create_playlist():
    payload = request.get_json(silent=True)
    name = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
        return jsonify(error="Enter a playlist name of up to 200 characters."), 400
    playlist = {"id": uuid.uuid4().hex, "name": name.strip(), "videos": []}
    with current_app.extensions["playback_queues_lock"]:
        if _authenticated_username() != g.api_username:
            return _authentication_required()
        if not storage.save_playlist(
            current_app.config["DATA_DIRECTORY"], g.api_username, playlist, create=True
        ):
            return _authentication_required()
    return jsonify(playlist=playlist), 201


@client_api.get("/playlists/<playlist_id>")
def playlist(playlist_id: str):
    library = storage.read_playlists(current_app.config["DATA_DIRECTORY"])
    item = next(
        (item for item in library.get(g.api_username, []) if item["id"] == playlist_id),
        None,
    )
    if item is None:
        return jsonify(error="Playlist not found."), 404
    return jsonify(playlist=_with_thumbnails(item))


@client_api.route("/playlists/<playlist_id>", methods=["PATCH", "DELETE"])
def modify_playlist(playlist_id: str):
    name = ""
    if request.method == "PATCH":
        payload = request.get_json(silent=True)
        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
            return jsonify(error="Enter a playlist name of up to 200 characters."), 400
        name = name.strip()

    with current_app.extensions["playback_queues_lock"]:
        if _authenticated_username() != g.api_username:
            return _authentication_required()
        directory = current_app.config["DATA_DIRECTORY"]
        if request.method == "DELETE":
            if not storage.delete_playlist(directory, g.api_username, playlist_id):
                return jsonify(error="Playlist not found."), 404
            return "", 204
        item = storage.rename_playlist(directory, g.api_username, playlist_id, name)
        if item is None:
            return jsonify(error="Playlist not found."), 404
        return jsonify(playlist=_with_thumbnails(item))


@client_api.post("/playlists/<playlist_id>/tracks")
def add_playlist_track(playlist_id: str):
    payload = request.get_json(silent=True)
    url = payload.get("url") if isinstance(payload, dict) else None
    video_id = youtube_video_id(url) if isinstance(url, str) else None
    if video_id is None:
        return jsonify(error="Enter a valid HTTPS YouTube video URL."), 400
    directory = current_app.config["DATA_DIRECTORY"]
    library = storage.read_playlists(directory)
    if not any(item["id"] == playlist_id for item in library.get(g.api_username, [])):
        return jsonify(error="Playlist not found."), 404
    try:
        title = _playlist_video_title(video_id)
    except MetadataError:
        return jsonify(error="Could not retrieve the YouTube video title."), 422

    with current_app.extensions["playback_queues_lock"]:
        if _authenticated_username() != g.api_username:
            return _authentication_required()
        try:
            item = storage.append_playlist_track(
                directory, g.api_username, playlist_id, {"name": title, "link": url.strip()}
            )
        except ValueError as error:
            return jsonify(error=str(error)), 409
        if item is None:
            return jsonify(error="Playlist not found."), 404
        return jsonify(playlist=_with_thumbnails(item)), 201


@client_api.delete("/playlists/<playlist_id>/tracks")
def delete_playlist_track(playlist_id: str):
    payload = request.get_json(silent=True)
    index = payload.get("index") if isinstance(payload, dict) else None
    videos = payload.get("videos") if isinstance(payload, dict) else None
    if not _valid_track_snapshot(index, videos):
        return jsonify(error="Provide a track index and the current playlist tracks."), 400

    with current_app.extensions["playback_queues_lock"]:
        if _authenticated_username() != g.api_username:
            return _authentication_required()
        try:
            item = storage.remove_playlist_track(
                current_app.config["DATA_DIRECTORY"], g.api_username, playlist_id,
                index, videos,
            )
        except storage.PlaylistChangedError:
            return jsonify(error="The playlist has changed. Refresh and try again."), 409
        if item is None:
            return jsonify(error="Playlist not found."), 404
        return jsonify(playlist=_with_thumbnails(item))


@client_api.patch("/playlists/<playlist_id>/tracks")
def replace_playlist_track(playlist_id: str):
    payload = request.get_json(silent=True)
    index = payload.get("index") if isinstance(payload, dict) else None
    videos = payload.get("videos") if isinstance(payload, dict) else None
    url = payload.get("url") if isinstance(payload, dict) else None
    if not _valid_track_snapshot(index, videos):
        return jsonify(error="Provide a track index and the current playlist tracks."), 400
    video_id = youtube_video_id(url) if isinstance(url, str) else None
    if video_id is None:
        return jsonify(error="Enter a valid HTTPS YouTube video URL."), 400
    directory = current_app.config["DATA_DIRECTORY"]
    library = storage.read_playlists(directory)
    current = next(
        (item for item in library.get(g.api_username, []) if item["id"] == playlist_id),
        None,
    )
    if current is None:
        return jsonify(error="Playlist not found."), 404
    if current["videos"] != videos:
        return jsonify(error="The playlist has changed. Refresh and try again."), 409
    try:
        title = _playlist_video_title(video_id)
    except MetadataError:
        return jsonify(error="Could not retrieve the YouTube video title."), 422

    with current_app.extensions["playback_queues_lock"]:
        if _authenticated_username() != g.api_username:
            return _authentication_required()
        try:
            item = storage.replace_playlist_track(
                directory, g.api_username, playlist_id, index, videos,
                {"name": title, "link": url.strip()},
            )
        except storage.PlaylistChangedError:
            return jsonify(error="The playlist has changed. Refresh and try again."), 409
        if item is None:
            return jsonify(error="Playlist not found."), 404
        return jsonify(playlist=_with_thumbnails(item))


@client_api.route("/queue", methods=["GET", "POST", "DELETE"])
def queue():
    payload = request.get_json(silent=True) if request.method == "POST" else None
    if request.method == "POST" and (
        not isinstance(payload, dict)
        or not isinstance(payload.get("playlist_id"), str)
        or not payload["playlist_id"].strip()
        or not isinstance(payload.get("shuffle", False), bool)
    ):
        return jsonify(error="Provide a playlist_id and an optional boolean shuffle."), 400

    with current_app.extensions["playback_queues_lock"]:
        # A concurrent account deletion must not recreate or expose its queue.
        if _authenticated_username() != g.api_username:
            return _authentication_required()
        queues = current_app.extensions["playback_queues"]
        if request.method == "DELETE":
            queues.pop(g.api_username, None)
            return "", 204
        if request.method == "GET":
            current = queues.get(g.api_username)
            if current is None:
                return jsonify(error="Queue not found."), 404
            return jsonify(queue=_with_thumbnails(current))

        library = storage.read_playlists(current_app.config["DATA_DIRECTORY"])
        item = next(
            (item for item in library.get(g.api_username, [])
             if item["id"] == payload["playlist_id"]),
            None,
        )
        if item is None:
            return jsonify(error="Playlist not found."), 404
        videos = list(item["videos"])
        if not videos:
            return jsonify(error="The playlist has no tracks."), 422
        shuffled = payload.get("shuffle", False)
        if shuffled:
            random.shuffle(videos)
        current = {
            "id": uuid.uuid4().hex,
            "playlist_id": item["id"],
            "name": item["name"],
            "shuffled": shuffled,
            "videos": videos,
        }
        queues[g.api_username] = current
        return jsonify(queue=_with_thumbnails(current)), 201


@client_api.post("/cast-queue")
def create_cast_queue():
    payload = request.get_json(silent=True)
    queue_id = payload.get("queue_id") if isinstance(payload, dict) else None
    if not isinstance(queue_id, str) or not queue_id:
        return jsonify(error="Provide a queue ID."), 400
    with current_app.extensions["playback_queues_lock"]:
        if _authenticated_username() != g.api_username:
            return _authentication_required()
        queue = current_app.extensions["playback_queues"].get(g.api_username)
        if queue is None or queue["id"] != queue_id:
            return jsonify(error="The queue has changed. Create a new queue."), 409
        credentials = request.authorization
        if credentials is None or not isinstance(credentials.password, str):
            return _authentication_required()
        revision = users_revision(current_app.config["DATA_DIRECTORY"])
        try:
            token = current_app.extensions["cast_queue_tokens"].issue(
                g.api_username, _fingerprint(credentials.password),
                tuple(video["link"] for video in queue["videos"]), revision,
            )
        except TokenCapacityError:
            current_app.logger.warning("Cast queue token capacity reached.")
            return jsonify(error="Cast queues are at capacity. Try again later."), 503
    return jsonify(token=token, expires_in=CAST_TOKEN_LIFETIME, batch_size=g.cast_batch_size)


@client_api.get("/cast-stream/<int:index>")
def cast_stream(index: int):
    tokens = request.args.getlist("token")
    if len(tokens) != 1:
        return jsonify(error="Invalid or expired Cast queue."), 401
    store = current_app.extensions["cast_queue_tokens"]
    directory = current_app.config["DATA_DIRECTORY"]
    with current_app.extensions["playback_queues_lock"]:
        entry = store.get(tokens[0], users_revision(directory))
        if entry is None:
            return jsonify(error="Invalid or expired Cast queue."), 401
        user = next((user for user in storage.read_users(directory)
                     if user["username"] == entry.username), None)
        if user is None or not hmac.compare_digest(
            _fingerprint(user["password"]), entry.credential_fingerprint
        ):
            return jsonify(error="Invalid or expired Cast queue."), 401
        if index >= len(entry.videos):
            return jsonify(error="Track not found."), 404
        video_id = youtube_video_id(entry.videos[index])
    if video_id is None:
        return jsonify(error="Invalid YouTube video URL."), 422
    try:
        audio = resolve_cast_audio(video_id)
        if audio["content_type"] != "audio/mp4":
            raise ResolveError("No compatible MP4 audio stream was found.")
        validate_url(audio["url"])
    except ResolveError as error:
        current_app.extensions["bragi_event_log"].record(
            "resolve_error", str(error), context={"video_id": video_id}
        )
        current_app.logger.warning("Could not resolve Cast audio for video %s", video_id)
        return jsonify(error="Could not resolve Cast audio."), 422
    except InvalidAudioURL:
        current_app.logger.warning("Cast audio resolver returned an unsupported destination.")
        return jsonify(error="Could not access the audio source."), 502
    with current_app.extensions["playback_queues_lock"]:
        if store.get(tokens[0], users_revision(directory)) is not entry:
            return jsonify(error="Invalid or expired Cast queue."), 401
    response = redirect(audio["url"], code=302)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@client_api.post("/audio-proxy")
def create_audio_proxy():
    payload = request.get_json(silent=True)
    url = payload.get("url") if isinstance(payload, dict) else None
    try:
        if not isinstance(url, str):
            raise InvalidAudioURL
        hostname, _ = validate_url(url)
        public_addresses(hostname)
    except InvalidAudioURL:
        return jsonify(error="Unsupported audio URL."), 400
    except AudioProxyError as error:
        current_app.logger.warning("Audio proxy host lookup failed.")
        current_app.extensions["bragi_event_log"].record(
            "exception", "Audio proxy host lookup failed.", exception=error
        )
        return jsonify(error="Could not access the audio source."), 502

    with current_app.extensions["playback_queues_lock"]:
        if _authenticated_username() != g.api_username:
            return _authentication_required()
        credentials = request.authorization
        if credentials is None or not isinstance(credentials.password, str):
            return _authentication_required()
        revision = users_revision(current_app.config["DATA_DIRECTORY"])
        try:
            token = current_app.extensions["audio_proxy_tokens"].issue(
                g.api_username, _fingerprint(credentials.password), url, revision
            )
        except TokenCapacityError:
            current_app.logger.warning("Audio proxy token capacity reached.")
            return jsonify(error="Audio proxy is at capacity. Try again later."), 503
    return jsonify(
        proxy_url=url_for("client_api.audio_stream", token=token),
        expires_in=TOKEN_LIFETIME,
    )


@client_api.get("/audio-stream")
def audio_stream():
    tokens = request.args.getlist("token")
    if len(tokens) != 1:
        return jsonify(error="Invalid or expired audio token."), 401
    token = tokens[0]
    store = current_app.extensions["audio_proxy_tokens"]
    with current_app.extensions["playback_queues_lock"]:
        revision = users_revision(current_app.config["DATA_DIRECTORY"])
        entry = store.get(token, revision)
        if entry is None:
            return jsonify(error="Invalid or expired audio token."), 401
        user = next(
            (user for user in storage.read_users(current_app.config["DATA_DIRECTORY"])
             if user["username"] == entry.username),
            None,
        )
        if user is None or not hmac.compare_digest(
            _fingerprint(user["password"]), entry.credential_fingerprint
        ):
            store.discard(token)
            return jsonify(error="Invalid or expired audio token."), 401

    try:
        connection, upstream = open_upstream(entry.url, request.headers.get("Range"))
    except InvalidAudioURL:
        current_app.logger.warning("Audio proxy rejected the upstream destination.")
        return jsonify(error="Could not access the audio source."), 502
    except AudioProxyError as error:
        current_app.logger.warning("Audio proxy upstream request failed.")
        current_app.extensions["bragi_event_log"].record(
            "exception", "Audio proxy upstream request failed.", exception=error
        )
        return jsonify(error="Could not access the audio source."), 502

    if upstream.status not in (200, 206, 416):
        upstream.close()
        connection.close()
        current_app.logger.warning("Audio proxy received upstream HTTP %d.", upstream.status)
        return jsonify(error="Could not access the audio source."), 502

    app = current_app._get_current_object()

    def stream():
        try:
            while True:
                chunk = upstream.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        except (OSError, http.client.HTTPException):
            try:
                raise AudioProxyError("Audio proxy stream interrupted.") from None
            except AudioProxyError as error:
                app.logger.warning("Audio proxy stream interrupted.")
                app.extensions["bragi_event_log"].record(
                    "exception", "Audio proxy stream interrupted.", exception=error
                )
                raise
        finally:
            upstream.close()
            connection.close()

    response = Response(stream(), status=upstream.status)
    response.headers.pop("Content-Type", None)
    for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Content-Encoding"):
        value = upstream.getheader(header)
        if value is not None:
            response.headers[header] = value
    response.headers["Referrer-Policy"] = "no-referrer"
    response.call_on_close(upstream.close)
    response.call_on_close(connection.close)
    return response


@client_api.post("/resolve")
def resolve():
    payload = request.get_json(silent=True)
    url = payload.get("url") if isinstance(payload, dict) else None
    video_id = youtube_video_id(url) if isinstance(url, str) else None
    if video_id is None:
        return jsonify(error="Enter a valid HTTPS YouTube video URL."), 400
    try:
        audio = resolve_audio(video_id)
    except ResolveError as error:
        current_app.extensions["bragi_event_log"].record(
            "resolve_error", str(error), context={"video_id": video_id}
        )
        current_app.logger.warning("Could not resolve audio for video %s", video_id)
        return jsonify(error=str(error)), 422
    return jsonify(audio=audio)
