import io
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from yt_dlp.utils import DownloadError

from bragi_server import storage
from bragi_server.app import create_app
from bragi_server.youtube import (
    ResolveError,
    _fetch_player_response,
    resolve_audio,
    resolve_cast_audio,
    video_title,
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


def test_cast_resolver_requests_only_https_mp4_audio():
    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl:
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "url": "https://r1.googlevideo.com/stream",
            "title": "Test track",
            "ext": "m4a",
        }
        assert resolve_cast_audio("abcdefghijk")["content_type"] == "audio/mp4"
    assert youtube_dl.call_args.args[0]["format"] == "bestaudio[ext=m4a][protocol=https]"


def test_youtube_resolver_returns_loudness_for_selected_format():
    player_response = {
        "streamingData": {
            "adaptiveFormats": [
                {
                    "itag": 140,
                    "mimeType": "audio/mp4",
                    "url": "https://example.test/stream",
                    "loudnessDb": 0.99,
                    "trackAbsoluteLoudnessLkfs": -13.01,
                }
            ]
        }
    }
    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl, patch(
        "bragi_server.youtube._fetch_player_response", return_value=player_response
    ):
        extractor = youtube_dl.return_value.__enter__.return_value
        extractor.extract_info.return_value = {
            "url": "https://example.test/stream",
            "title": "Test track",
            "ext": "m4a",
            "format_id": "140",
        }
        result = resolve_audio("abcdefghijk")

    assert result["loudness"] == {
        "integrated_lufs": -13.01,
        "target_lufs": -14.0,
        "gain_db": -0.99,
        "source": "youtube",
    }


def test_youtube_loudness_matches_format_id_and_skips_drc_variant():
    player_response = {
        "streamingData": {
            "adaptiveFormats": [
                {
                    "itag": 140,
                    "url": "https://example.test/drc",
                    "isDrc": True,
                    "loudnessDb": 0,
                    "trackAbsoluteLoudnessLkfs": -14,
                },
                {
                    "itag": 140,
                    "url": "https://example.test/standard",
                    "loudnessDb": 3.14,
                    "trackAbsoluteLoudnessLkfs": -10.86,
                },
            ]
        }
    }
    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl, patch(
        "bragi_server.youtube._fetch_player_response", return_value=player_response
    ):
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "url": "https://example.test/signed",
            "title": "Test track",
            "ext": "m4a",
            "format_id": "140",
        }
        result = resolve_audio("abcdefghijk")

    assert result["loudness"]["gain_db"] == -3.14
    assert result["loudness"]["integrated_lufs"] == -10.86
    assert result["loudness"]["target_lufs"] == -14.0


def test_youtube_resolver_tolerates_missing_loudness_values():
    player_response = {
        "streamingData": {
            "adaptiveFormats": [
                {"itag": 140, "url": "https://example.test/stream"}
            ]
        }
    }
    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl, patch(
        "bragi_server.youtube._fetch_player_response", return_value=player_response
    ):
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "url": "https://example.test/stream",
            "title": "Test track",
            "ext": "m4a",
            "format_id": "140",
        }
        result = resolve_audio("abcdefghijk")

    assert result["loudness"] is None


def test_fetch_player_response_parses_initial_player_response():
    player_response = {"streamingData": {"adaptiveFormats": []}}
    page = (
        "<html><script>var ytInitialPlayerResponse = "
        + json.dumps(player_response)
        + ";</script></html>"
    )
    with patch(
        "bragi_server.youtube.urllib.request.urlopen",
        return_value=io.BytesIO(page.encode("utf-8")),
    ):
        assert _fetch_player_response("abcdefghijk") == player_response


def test_youtube_resolver_reports_yt_dlp_failure_without_exposing_details():
    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl:
        extractor = youtube_dl.return_value.__enter__.return_value
        extractor.extract_info.side_effect = DownloadError("secret stream URL")

        with pytest.raises(ResolveError, match="YouTube audio resolution failed") as error:
            resolve_audio("abcdefghijk")
    assert "secret stream URL" not in str(error.value)


def test_resolve_audio_reuses_a_recent_result():
    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl:
        extractor = youtube_dl.return_value.__enter__.return_value
        extractor.extract_info.return_value = {
            "url": "https://example.test/stream",
            "title": "Test track",
            "ext": "m4a",
        }

        first = resolve_audio("abcdefghijk")
        first["url"] = "https://example.test/mutated"
        second = resolve_audio("abcdefghijk")

    assert youtube_dl.call_count == 1
    assert second["url"] == "https://example.test/stream"
    assert second["title"] == "Test track"


def test_resolve_cache_separates_audio_formats():
    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl:
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "url": "https://example.test/stream",
            "title": "Test track",
            "ext": "m4a",
        }

        resolve_audio("abcdefghijk")
        resolve_cast_audio("abcdefghijk")

    assert youtube_dl.call_count == 2


def test_resolve_cache_serves_stale_result_and_refreshes_in_background(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("bragi_server.youtube.time.monotonic", lambda: clock[0])

    class SynchronousThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr("bragi_server.youtube.threading.Thread", SynchronousThread)

    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl:
        extractor = youtube_dl.return_value.__enter__.return_value
        extractor.extract_info.return_value = {
            "url": "https://example.test/old",
            "title": "Test track",
            "ext": "m4a",
        }
        first = resolve_audio("abcdefghijk")
        extractor.extract_info.return_value = {
            "url": "https://example.test/new",
            "title": "Test track",
            "ext": "m4a",
        }

        clock[0] += 121
        stale = resolve_audio("abcdefghijk")
        assert first["url"] == "https://example.test/old"
        assert stale["url"] == "https://example.test/old"
        assert youtube_dl.call_count == 2

        clock[0] += 1
        assert resolve_audio("abcdefghijk")["url"] == "https://example.test/new"
        assert youtube_dl.call_count == 2


def test_resolve_cache_schedules_only_one_background_refresh(monkeypatch):
    clock = [1000.0]
    scheduled = []
    monkeypatch.setattr("bragi_server.youtube.time.monotonic", lambda: clock[0])

    class RecordingThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            scheduled.append(args)

        def start(self):
            pass

    monkeypatch.setattr("bragi_server.youtube.threading.Thread", RecordingThread)

    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl:
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "url": "https://example.test/stream",
            "title": "Test track",
            "ext": "m4a",
        }

        resolve_audio("abcdefghijk")
        clock[0] += 121
        resolve_audio("abcdefghijk")
        clock[0] += 2
        resolve_audio("abcdefghijk")

    assert len(scheduled) == 1


def test_resolve_cache_fetches_inline_after_hard_expiry(monkeypatch):
    clock = [1000.0]
    scheduled = []
    monkeypatch.setattr("bragi_server.youtube.time.monotonic", lambda: clock[0])

    class RecordingThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            scheduled.append(args)

        def start(self):
            pass

    monkeypatch.setattr("bragi_server.youtube.threading.Thread", RecordingThread)

    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl:
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "url": "https://example.test/stream",
            "title": "Test track",
            "ext": "m4a",
        }

        resolve_audio("abcdefghijk")
        clock[0] += 601
        resolve_audio("abcdefghijk")

    assert youtube_dl.call_count == 2
    assert scheduled == []


def test_failed_background_refresh_keeps_the_stale_result(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("bragi_server.youtube.time.monotonic", lambda: clock[0])

    class SynchronousThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr("bragi_server.youtube.threading.Thread", SynchronousThread)

    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl:
        extractor = youtube_dl.return_value.__enter__.return_value
        extractor.extract_info.return_value = {
            "url": "https://example.test/old",
            "title": "Test track",
            "ext": "m4a",
        }
        resolve_audio("abcdefghijk")
        extractor.extract_info.side_effect = DownloadError("refresh failed")

        clock[0] += 121
        assert resolve_audio("abcdefghijk")["url"] == "https://example.test/old"
        assert youtube_dl.call_count == 2


def test_resolve_cache_evicts_the_least_recently_used_entry(monkeypatch):
    monkeypatch.setattr("bragi_server.youtube._RESOLVE_CACHE_MAX_ENTRIES", 2)

    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl:
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "url": "https://example.test/stream",
            "title": "Test track",
            "ext": "m4a",
        }

        for video_id in ("abcdefghijk", "12345678901", "abcdefghij2"):
            resolve_audio(video_id)
        assert youtube_dl.call_count == 3

        resolve_audio("abcdefghijk")
        assert youtube_dl.call_count == 4


def test_resolve_cache_does_not_cache_failures():
    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl:
        youtube_dl.return_value.__enter__.return_value.extract_info.side_effect = DownloadError(
            "failed"
        )

        for _ in range(2):
            with pytest.raises(ResolveError, match="YouTube audio resolution failed"):
                resolve_audio("abcdefghijk")

    assert youtube_dl.call_count == 2


def test_video_title_cache_reuses_recent_metadata():
    with patch("bragi_server.youtube.YoutubeDL") as youtube_dl:
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "title": "Video title", "fulltitle": "Video title"
        }

        assert video_title("abcdefghijk") == "Video title"
        assert video_title("abcdefghijk") == "Video title"

    assert youtube_dl.call_count == 1


def test_storage_reads_are_cached_until_files_change(tmp_path):
    users_path = tmp_path / "users.json"
    settings_path = tmp_path / "settings.json"
    storage._write_json(users_path, {"users": [{"username": "alice", "password": "one"}]})
    storage._write_json(settings_path, {"api_allowed_origins": []})

    with patch("bragi_server.storage._read_json", wraps=storage._read_json) as read_json:
        assert storage.read_users(tmp_path)[0]["password"] == "one"
        assert storage.read_users(tmp_path)[0]["password"] == "one"
        assert storage.read_settings(tmp_path)["cast_batch_size"] == storage.DEFAULT_CAST_BATCH_SIZE
        assert storage.read_settings(tmp_path)["cast_batch_size"] == storage.DEFAULT_CAST_BATCH_SIZE
        assert read_json.call_count == 2

    storage._write_json(users_path, {"users": [{"username": "alice", "password": "two"}]})
    assert storage.read_users(tmp_path)[0]["password"] == "two"


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
