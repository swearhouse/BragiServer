import hashlib
import hmac
import os
import re
import secrets
import uuid
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from bragi_server import storage


management = Blueprint("management", __name__)
_USERNAME = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")


def load_session_key(directory: Path) -> bytes:
    path = directory / "session.key"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as file:
                file.write(secrets.token_bytes(32))
        key = path.read_bytes()
    except OSError as error:
        raise storage.StorageError("Could not initialize the session signing key.") from error
    if len(key) != 32:
        raise storage.StorageError("The session signing key is invalid.")
    return key


def _directory() -> Path:
    return current_app.config["DATA_DIRECTORY"]


def _fingerprint(password: str) -> str:
    return hmac.new(
        current_app.secret_key, password.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@management.app_context_processor
def template_helpers():
    return {"csrf_token": _csrf_token}


@management.before_request
def check_session_and_csrf():
    if request.method == "POST":
        expected = session.get("csrf_token")
        supplied = request.form.get("csrf_token", "")
        if not isinstance(expected, str) or not secrets.compare_digest(
            expected.encode("utf-8"), supplied.encode("utf-8")
        ):
            abort(400, description="Invalid or missing form token. Reload the page and retry.")

    if request.endpoint == "management.login":
        return None

    username = session.get("username")
    fingerprint = session.get("credential_fingerprint")
    if not isinstance(username, str) or not isinstance(fingerprint, str):
        return redirect(url_for("management.login"))
    user = next((user for user in storage.read_users(_directory()) if user["username"] == username), None)
    if user is None or not secrets.compare_digest(_fingerprint(user["password"]), fingerprint):
        session.clear()
        return redirect(url_for("management.login"))
    return None


@management.app_errorhandler(storage.StorageError)
def handle_storage_error(error):
    current_app.logger.exception("Management storage operation failed")
    current_app.extensions["bragi_event_log"].record(
        "exception", "Management storage operation failed.", exception=error
    )
    return render_template("error.html", message=str(error)), 500


@management.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        users = storage.read_users(_directory())
        user = next((user for user in users if user["username"] == username), None)
        if user is None or not hmac.compare_digest(
            _fingerprint(user["password"]), _fingerprint(password)
        ):
            return render_template("login.html", error="Incorrect username or password."), 401
        session.clear()
        session["username"] = username
        session["credential_fingerprint"] = _fingerprint(user["password"])
        return redirect(url_for("management.playlists"))
    return render_template("login.html")


@management.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("management.login"))


def _admin_only() -> None:
    if session["username"] != "admin":
        abort(403)


def _owner() -> str:
    username = session["username"]
    owner = request.values.get("owner", username)
    if owner != username and username != "admin":
        abort(403)
    if not any(user["username"] == owner for user in storage.read_users(_directory())):
        abort(404)
    return owner


@management.get("/")
def playlists():
    owner = _owner()
    users = storage.read_users(_directory()) if session["username"] == "admin" else []
    library = storage.read_playlists(_directory())
    return render_template(
        "playlists.html", owner=owner, users=users, playlists=library.get(owner, [])
    )


@management.route("/playlists/new", methods=["GET", "POST"])
@management.route("/playlists/<playlist_id>/edit", methods=["GET", "POST"])
def edit_playlist(playlist_id: str | None = None):
    owner = _owner()
    library = storage.read_playlists(_directory())
    existing = next(
        (item for item in library.get(owner, []) if item["id"] == playlist_id), None
    ) if playlist_id is not None else None
    if playlist_id is not None and existing is None:
        abort(404)

    playlist = existing or {"name": "", "videos": []}
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        names = request.form.getlist("video_name")
        links = request.form.getlist("video_link")
        videos = [
            {"name": video_name.strip(), "link": link.strip()}
            for video_name, link in zip(names, links)
            if video_name.strip() or link.strip()
        ]
        playlist = {"name": name, "videos": videos}
        if (
            not name or len(name) > 200 or len(names) != len(links)
            or len(videos) > 5000
            or any(
                not video["name"] or len(video["name"]) > 200
                or not storage.valid_video_url(video["link"])
                for video in videos
            )
        ):
            return render_template(
                "playlist_edit.html", owner=owner, playlist=playlist,
                playlist_id=playlist_id,
                error="Enter a playlist name and a name and HTTPS YouTube video URL for each track.",
            ), 400
        playlist["id"] = playlist_id or uuid.uuid4().hex
        if not storage.save_playlist(_directory(), owner, playlist, create=playlist_id is None):
            abort(404)
        flash("Playlist saved.")
        return redirect(url_for("management.playlists", owner=owner))
    return render_template(
        "playlist_edit.html", owner=owner, playlist=playlist, playlist_id=playlist_id
    )


@management.post("/playlists/<playlist_id>/delete")
def remove_playlist(playlist_id: str):
    owner = _owner()
    if not storage.delete_playlist(_directory(), owner, playlist_id):
        abort(404)
    flash("Playlist deleted.")
    return redirect(url_for("management.playlists", owner=owner))


@management.get("/admin/users")
def users():
    _admin_only()
    return render_template("users.html", users=storage.read_users(_directory()))


@management.route("/admin/settings", methods=["GET", "POST"])
def settings():
    _admin_only()
    settings_data = storage.read_settings(_directory())
    origins_text = "\n".join(settings_data["api_allowed_origins"])
    if request.method == "POST":
        if "api_allowed_origins" not in request.form:
            return render_template(
                "settings.html", origins=origins_text, error="Origins field is required."
            ), 400
        origins_text = request.form["api_allowed_origins"]
        try:
            origins = storage.parse_allowed_origins(origins_text)
        except ValueError as error:
            return render_template("settings.html", origins=origins_text, error=str(error)), 400
        storage.save_allowed_origins(_directory(), origins)
        flash("Settings saved.")
        return redirect(url_for("management.settings"))
    return render_template("settings.html", origins=origins_text)


@management.post("/admin/users")
def add_user():
    _admin_only()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    if not _USERNAME.fullmatch(username) or not password or len(password) > 256:
        flash("Username must contain 1–64 letters, digits, dots, underscores or hyphens; password is required (max 256 characters).")
    else:
        try:
            storage.create_user(_directory(), username, password)
        except ValueError as error:
            flash(str(error))
        else:
            flash("User created.")
    return redirect(url_for("management.users"))


@management.post("/admin/users/delete")
def remove_user():
    _admin_only()
    username = request.form.get("username", "")
    try:
        with current_app.extensions["playback_queues_lock"]:
            deleted = storage.delete_user(_directory(), username)
            if deleted:
                current_app.extensions["playback_queues"].pop(username, None)
    except ValueError as error:
        flash(str(error))
    else:
        if not deleted:
            abort(404)
        flash("User and their playlists deleted.")
    return redirect(url_for("management.users"))
