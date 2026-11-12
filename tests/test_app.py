import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from yt_dlp.utils import DownloadError

from bragi_server.app import create_app
from bragi_server.youtube import (
    ResolveError,
    resolve_audio,
    youtube_video_id,
)
from bragi_server.event_log import BragiEventLog, EventLogError


def test_health_endpoint_reports_service_status(tmp_path):
    response = create_app(data_directory=tmp_path).test_client().get("/health")

    assert response.status_code == 200
    assert response.json == {"status": "ok"}


def test_data_directory_comes_from_environment(monkeypatch, tmp_path):
    data_directory = tmp_path / "persistent-data"
    monkeypatch.setenv("BRAGI_DATA_DIR", str(data_directory))

    app = create_app()

    assert app.config["DATA_DIRECTORY"] == Path(data_directory)
    assert (data_directory / "bragi_log.json").is_file()


def test_app_initializes_event_log_in_configured_data_directory(tmp_path):
    create_app(data_directory=tmp_path)

    assert json.loads((tmp_path / "bragi_log.json").read_text(encoding="utf-8")) == {
        "events": []
    }


def test_removed_test_endpoints_remain_unavailable_with_old_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAGI_ENABLE_CAST_TEST_ENDPOINTS", "true")
    client = create_app(data_directory=tmp_path).test_client()
    assert client.get("/_test/cast/audio/1.wav").status_code == 404
    assert client.post("/_test/cast/resolve").status_code == 404


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=abcdefghijk", "abcdefghijk"),
        ("https://youtu.be/abcdefghijk?t=12", "abcdefghijk"),
        ("https://youtube.com/shorts/abcdefghijk", "abcdefghijk"),
        ("https://youtube.com.evil.example/watch?v=abcdefghijk", None),
        ("https://youtube.com/watch?v=abcdefghijk&v=12345678901", None),
        ("https://youtube.com/watch?v=short", None),
        ("http://youtube.com/watch?v=abcdefghijk", None),
    ],
)
def test_youtube_video_id_only_accepts_direct_youtube_videos(url, expected):
    assert youtube_video_id(url) == expected


def test_youtube_resolver_requests_direct_audio_and_returns_its_mime_type():
    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl:
        extractor = youtube_dl.return_value.__enter__.return_value
        extractor.extract_info.return_value = {
            "url": "https://example.test/stream",
            "title": "Test track",
            "ext": "m4a",
        }

        result = resolve_audio("abcdefghijk")

    extractor.extract_info.assert_called_once_with(
        "https://www.youtube.com/watch?v=abcdefghijk", download=False
    )
    assert "[protocol=https]" in youtube_dl.call_args.args[0]["format"]
    assert result["content_type"] == "audio/mp4"


def test_youtube_resolver_reports_yt_dlp_failure_without_exposing_details():
    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl:
        extractor = youtube_dl.return_value.__enter__.return_value
        extractor.extract_info.side_effect = DownloadError("secret stream URL")

        with pytest.raises(ResolveError, match="YouTube audio resolution failed") as error:
            resolve_audio("abcdefghijk")
    assert "secret stream URL" not in str(error.value)


def test_log_level_environment_variables_configure_separate_loggers(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("BRAGI_LOG_LEVEL", "debug")
    monkeypatch.setenv("WAITRESS_LOG_LEVEL", "ERROR")

    app = create_app(data_directory=tmp_path)

    assert app.config["BRAGI_LOG_LEVEL"] == logging.DEBUG
    assert app.config["WAITRESS_LOG_LEVEL"] == logging.ERROR
    assert logging.getLogger("bragi_server").level == logging.DEBUG
    assert logging.getLogger("waitress").level == logging.ERROR


@pytest.mark.parametrize("variable_name", ["BRAGI_LOG_LEVEL", "WAITRESS_LOG_LEVEL"])
def test_invalid_log_level_fails_app_startup(monkeypatch, variable_name):
    monkeypatch.setenv(variable_name, "VERBOSE")

    with pytest.raises(ValueError, match=variable_name):
        create_app()


def test_event_log_migrates_empty_object_and_records_categorized_event(tmp_path):
    log_path = tmp_path / "bragi_log.json"
    log_path.write_text("{}\n", encoding="utf-8")
    log = BragiEventLog(log_path)

    log.record(
        "resolve_error",
        "Could not resolve audio",
        context={"video_id": "video-123"},
    )

    data = json.loads(log_path.read_text(encoding="utf-8"))
    assert len(data["events"]) == 1
    assert data["events"][0]["category"] == "resolve_error"
    assert data["events"][0]["severity"] == "ERROR"
    assert data["events"][0]["context"] == {"video_id": "video-123"}
    assert data["events"][0]["timestamp"]


def test_event_log_records_exception_type_and_traceback(tmp_path):
    log = BragiEventLog(tmp_path / "bragi_log.json")
    try:
        raise RuntimeError("resolution failed")
    except RuntimeError as error:
        log.record("exception", "Unexpected resolver failure", exception=error)

    event = json.loads(log.path.read_text(encoding="utf-8"))["events"][0]
    assert event["category"] == "exception"
    assert event["exception"]["type"] == "RuntimeError"
    assert "resolution failed" in event["exception"]["traceback"]


def test_event_log_keeps_only_the_latest_events(tmp_path, monkeypatch):
    monkeypatch.setattr("bragi_server.event_log.MAX_EVENTS", 2)
    log = BragiEventLog(tmp_path / "bragi_log.json")

    for message in ("first", "second", "third"):
        log.record("critical_warning", message)

    events = json.loads(log.path.read_text(encoding="utf-8"))["events"]
    assert [event["message"] for event in events] == ["second", "third"]


def test_event_log_does_not_replace_malformed_json(tmp_path):
    log_path = tmp_path / "bragi_log.json"
    log_path.write_text("{invalid json", encoding="utf-8")

    with pytest.raises(EventLogError, match="Could not read"):
        BragiEventLog(log_path)

    assert log_path.read_text(encoding="utf-8") == "{invalid json"
