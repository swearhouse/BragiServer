import gzip
import hashlib
import logging
import os
from pathlib import Path
from threading import RLock

from flask import Flask, jsonify, request

from bragi_server import storage
from bragi_server.audio_proxy import AudioTokenStore, CastQueueTokenStore
from bragi_server.client_api import client_api
from bragi_server.event_log import BragiEventLog
from bragi_server.management import load_session_key, management


LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _read_log_level(variable_name: str) -> int:
    value = os.environ.get(variable_name, "INFO").strip().upper()
    try:
        return LOG_LEVELS[value]
    except KeyError as error:
        valid_levels = ", ".join(LOG_LEVELS)
        raise ValueError(
            f"{variable_name} must be one of: {valid_levels}."
        ) from error


def _read_boolean_setting(variable_name: str, default: bool = False) -> bool:
    value = os.environ.get(variable_name, str(default)).strip().lower()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    raise ValueError(f"{variable_name} must be a boolean value.")


def _add_json_etag(response):
    if (
        request.method != "GET"
        or response.status_code != 200
        or response.mimetype != "application/json"
        or response.direct_passthrough
        or "no-store" in response.headers.get("Cache-Control", "").lower()
    ):
        return response
    data = response.get_data()
    digest = hashlib.sha256(data).hexdigest()
    etag = f'"{digest}"'
    response.headers["ETag"] = etag
    if_none_match = request.headers.get("If-None-Match", "").strip()
    candidates = {
        item.strip().removeprefix("W/").strip('"')
        for item in if_none_match.split(",")
        if item.strip()
    }
    if if_none_match == "*" or digest in candidates:
        response.status_code = 304
        response.set_data(b"")
    return response


def _gzip_json(response):
    if (
        response.status_code != 200
        or response.mimetype != "application/json"
        or response.direct_passthrough
        or "Content-Encoding" in response.headers
        or "gzip" not in request.headers.get("Accept-Encoding", "").lower()
    ):
        return response
    data = response.get_data()
    if len(data) < 128:
        return response
    response.set_data(gzip.compress(data, mtime=0))
    response.headers["Content-Encoding"] = "gzip"
    response.vary.add("Accept-Encoding")
    return response


def create_app(data_directory: Path | str | None = None) -> Flask:
    bragi_log_level = _read_log_level("BRAGI_LOG_LEVEL")
    waitress_log_level = _read_log_level("WAITRESS_LOG_LEVEL")
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("bragi_server").setLevel(bragi_log_level)
    logging.getLogger("waitress").setLevel(waitress_log_level)

    app = Flask(__name__)
    app.logger.setLevel(bragi_log_level)
    if data_directory is None:
        data_directory = os.environ.get(
            "BRAGI_DATA_DIR",
            Path(__file__).resolve().parents[2] / "instance",
        )
    app.config["DATA_DIRECTORY"] = Path(data_directory)
    app.config["BRAGI_LOG_LEVEL"] = bragi_log_level
    app.config["WAITRESS_LOG_LEVEL"] = waitress_log_level
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_read_boolean_setting("BRAGI_COOKIE_SECURE"),
    )
    app.extensions["bragi_event_log"] = BragiEventLog(
        app.config["DATA_DIRECTORY"] / "bragi_log.json"
    )
    try:
        storage.initialize_settings(app.config["DATA_DIRECTORY"])
        storage.initialize_track_ids(app.config["DATA_DIRECTORY"])
    except storage.StorageError as error:
        app.logger.exception("Could not initialize persistent data")
        app.extensions["bragi_event_log"].record(
            "exception", "Could not initialize persistent data.", exception=error
        )
        raise
    app.secret_key = load_session_key(app.config["DATA_DIRECTORY"])
    app.extensions["playback_queues"] = {}
    app.extensions["playback_queues_lock"] = RLock()
    app.extensions["audio_proxy_tokens"] = AudioTokenStore()
    app.extensions["cast_queue_tokens"] = CastQueueTokenStore()
    app.register_blueprint(management)
    app.register_blueprint(client_api)

    @app.after_request
    def optimize_json(response):
        return _gzip_json(_add_json_etag(response))

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    return app
