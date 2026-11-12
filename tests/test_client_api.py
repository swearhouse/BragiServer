import json
from unittest.mock import patch

import pytest

from bragi_server import storage
from bragi_server.app import create_app
from bragi_server.youtube import MetadataError, ResolveError, video_title
from yt_dlp.utils import DownloadError


@pytest.fixture
def app(tmp_path):
    (tmp_path / "users.json").write_text(json.dumps({
        "users": [
            {"username": "alice", "password": "alice-pass"},
            {"username": "bob", "password": "bob-pass"},
            {"username": "admin", "password": "admin-pass"},
        ]
    }), encoding="utf-8")
    (tmp_path / "user_playlists.json").write_text(json.dumps({
        "alice": [{
            "id": "p1",
            "name": "Alice mix",
            "videos": [{"name": "Track", "link": "https://youtu.be/abcdefghijk"}],
        }],
        "bob": [{"id": "p2", "name": "Bob mix", "videos": []}],
    }), encoding="utf-8")
    return create_app(data_directory=tmp_path)


def test_basic_auth_is_required_and_rechecked_on_every_request(app):
    client = app.test_client()
    for auth in (None, ("alice", "wrong"), ("nobody", "alice-pass")):
        response = client.get("/api/v1/playlists", auth=auth)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].startswith("Basic ")
        assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/api/v1/playlists", auth=("alice", "alice-pass")).status_code == 200
    directory = app.config["DATA_DIRECTORY"]
    (directory / "users.json").write_text(json.dumps({
        "users": [{"username": "alice", "password": "new-password"}]
    }), encoding="utf-8")
    assert client.get("/api/v1/playlists", auth=("alice", "alice-pass")).status_code == 401
    assert client.get("/api/v1/playlists", auth=("alice", "new-password")).status_code == 200
    assert client.get("/api/v1/playlists", auth=("bob", "bob-pass")).status_code == 401


def test_playlists_are_ordered_and_isolated_even_for_admin(app):
    client = app.test_client()
    auth = ("alice", "alice-pass")
    listing = client.get("/api/v1/playlists", auth=auth)
    assert listing.json == {"playlists": [{
        "id": "p1",
        "name": "Alice mix",
        "videos": [{
            "name": "Track",
            "link": "https://youtu.be/abcdefghijk",
            "thumbnail_url": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
        }],
    }]}
    assert client.get("/api/v1/playlists/p1", auth=auth).json == {
        "playlist": listing.json["playlists"][0]
    }
    assert client.get("/api/v1/playlists/p2", auth=auth).status_code == 404
    assert client.get("/api/v1/playlists/p1?owner=alice", auth=("bob", "bob-pass")).status_code == 404
    assert client.get("/api/v1/playlists", auth=("admin", "admin-pass")).json == {"playlists": []}


def test_create_empty_playlist_for_authenticated_user(app):
    client = app.test_client()
    assert client.post("/api/v1/playlists", json={"name": "New"}).status_code == 401
    for name in ("", "   ", "a" * 201, None, 123):
        assert client.post("/api/v1/playlists", json={"name": name}, auth=("alice", "alice-pass")).status_code == 400
    response = client.post("/api/v1/playlists", json={"name": "  New mix  "}, auth=("alice", "alice-pass"))
    assert response.status_code == 201
    playlist = response.json["playlist"]
    assert playlist["id"] and playlist["name"] == "New mix" and playlist["videos"] == []
    assert client.get("/api/v1/playlists", auth=("alice", "alice-pass")).json["playlists"][-1] == playlist
    assert client.get(f"/api/v1/playlists/{playlist['id']}", auth=("bob", "bob-pass")).status_code == 404
    assert client.post("/api/v1/queue", json={"playlist_id": playlist["id"]}, auth=("alice", "alice-pass")).status_code == 422
    assert len(storage.read_playlists(app.config["DATA_DIRECTORY"])["alice"]) == 2


def test_create_playlist_rechecks_credentials_and_reports_storage_failure(app):
    client = app.test_client()
    with patch("bragi_server.client_api._authenticated_username", side_effect=["alice", None]):
        assert client.post("/api/v1/playlists", json={"name": "New"}, auth=("alice", "alice-pass")).status_code == 401
    with patch("bragi_server.storage.save_playlist", side_effect=storage.StorageError("failed")):
        assert client.post("/api/v1/playlists", json={"name": "New"}, auth=("alice", "alice-pass")).status_code == 500
    assert len(storage.read_playlists(app.config["DATA_DIRECTORY"])["alice"]) == 1


def test_rename_and_delete_playlist_preserve_tracks_and_queue_snapshot(app):
    client = app.test_client()
    auth = ("alice", "alice-pass")
    queue = client.post("/api/v1/queue", json={"playlist_id": "p1"}, auth=auth).json
    response = client.patch("/api/v1/playlists/p1", json={"name": "  Renamed  "}, auth=auth)
    assert response.status_code == 200
    assert response.json["playlist"]["name"] == "Renamed"
    assert response.json["playlist"]["videos"][0]["thumbnail_url"]
    stored = storage.read_playlists(app.config["DATA_DIRECTORY"])["alice"][0]
    assert stored["videos"] == [{"name": "Track", "link": "https://youtu.be/abcdefghijk"}]
    assert client.delete("/api/v1/playlists/p1", auth=auth).status_code == 204
    assert client.get("/api/v1/playlists/p1", auth=auth).status_code == 404
    assert client.get("/api/v1/queue", auth=auth).json == queue
    assert storage.read_playlists(app.config["DATA_DIRECTORY"])["bob"][0]["id"] == "p2"


def test_playlist_mutations_validate_auth_ownership_and_input(app):
    client = app.test_client()
    alice = ("alice", "alice-pass")
    assert client.patch("/api/v1/playlists/p1", json={"name": "New"}).status_code == 401
    assert client.delete("/api/v1/playlists/p1").status_code == 401
    for name in ("", "  ", "x" * 201, None, 123):
        assert client.patch("/api/v1/playlists/p1", json={"name": name}, auth=alice).status_code == 400
    assert client.patch("/api/v1/playlists/p2", json={"name": "New"}, auth=alice).status_code == 404
    assert client.delete("/api/v1/playlists/p2", auth=alice).status_code == 404
    assert client.patch("/api/v1/playlists/missing", json={"name": "New"}, auth=alice).status_code == 404
    assert client.delete("/api/v1/playlists/missing", auth=alice).status_code == 404
    with patch("bragi_server.client_api._authenticated_username", side_effect=["alice", None]):
        assert client.patch("/api/v1/playlists/p1", json={"name": "New"}, auth=alice).status_code == 401
    with patch("bragi_server.client_api._authenticated_username", side_effect=["alice", None]):
        assert client.delete("/api/v1/playlists/p1", auth=alice).status_code == 401
    assert storage.read_playlists(app.config["DATA_DIRECTORY"])["alice"][0]["name"] == "Alice mix"


def test_playlist_mutation_cors_preflight(app):
    storage.save_allowed_origins(app.config["DATA_DIRECTORY"], ["https://player.example.com"])
    response = app.test_client().options("/api/v1/playlists/p1", headers={
        "Origin": "https://player.example.com",
        "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert response.status_code == 204
    assert "PATCH" in response.headers["Access-Control-Allow-Methods"]
    assert response.headers["Access-Control-Allow-Origin"] == "https://player.example.com"


def test_add_track_uses_title_and_preserves_playlist_and_queue(app):
    client = app.test_client()
    auth = ("alice", "alice-pass")
    queue = client.post("/api/v1/queue", json={"playlist_id": "p1"}, auth=auth).json
    with patch("bragi_server.client_api.video_title", return_value="New song") as lookup:
        for _ in range(2):
            response = client.post(
                "/api/v1/playlists/p1/tracks",
                json={"url": "  https://youtu.be/12345678901  "}, auth=auth,
            )
            assert response.status_code == 201
    assert lookup.call_count == 2
    lookup.assert_called_with("12345678901")
    tracks = response.json["playlist"]["videos"]
    assert [track["name"] for track in tracks] == ["Track", "New song", "New song"]
    assert tracks[-1] == {
        "name": "New song", "link": "https://youtu.be/12345678901",
        "thumbnail_url": "https://i.ytimg.com/vi/12345678901/hqdefault.jpg",
    }
    saved = storage.read_playlists(app.config["DATA_DIRECTORY"])["alice"][0]
    assert saved["videos"][-1] == {"name": "New song", "link": "https://youtu.be/12345678901"}
    assert client.get("/api/v1/queue", auth=auth).json == queue


def test_add_track_rejects_invalid_auth_ownership_and_failed_lookup(app):
    client = app.test_client()
    auth = ("alice", "alice-pass")
    url = "https://youtu.be/12345678901"
    with patch("bragi_server.client_api.video_title", side_effect=MetadataError("unavailable")) as lookup:
        assert client.post("/api/v1/playlists/p1/tracks", json={"url": url}).status_code == 401
        for invalid in ("https://evil.example/12345678901", "https://youtu.be/short", None, 1):
            assert client.post("/api/v1/playlists/p1/tracks", json={"url": invalid}, auth=auth).status_code == 400
        assert client.post("/api/v1/playlists/p2/tracks", json={"url": url}, auth=auth).status_code == 404
        assert client.post("/api/v1/playlists/missing/tracks", json={"url": url}, auth=auth).status_code == 404
        assert client.post("/api/v1/playlists/p1/tracks", json={"url": url}, auth=auth).status_code == 422
        lookup.assert_called_once_with("12345678901")
    assert len(storage.read_playlists(app.config["DATA_DIRECTORY"])["alice"][0]["videos"]) == 1


def test_add_track_rechecks_auth_and_playlist_after_title_lookup(app):
    client = app.test_client()
    auth = ("alice", "alice-pass")
    url = "https://youtu.be/12345678901"
    directory = app.config["DATA_DIRECTORY"]
    with patch("bragi_server.client_api.video_title", return_value="Song"):
        with patch("bragi_server.client_api._authenticated_username", side_effect=["alice", None]):
            assert client.post("/api/v1/playlists/p1/tracks", json={"url": url}, auth=auth).status_code == 401
    def delete_during_lookup(video_id):
        assert storage.delete_playlist(directory, "alice", "p1")
        return "Song"
    with patch("bragi_server.client_api.video_title", side_effect=delete_during_lookup):
        assert client.post("/api/v1/playlists/p1/tracks", json={"url": url}, auth=auth).status_code == 404


def test_add_track_appends_to_latest_playlist_and_enforces_limit(app):
    client = app.test_client()
    directory = app.config["DATA_DIRECTORY"]
    def update_during_lookup(video_id):
        storage.rename_playlist(directory, "alice", "p1", "New name")
        storage.append_playlist_track(directory, "alice", "p1", {
            "name": "Concurrent", "link": "https://youtu.be/abcdefghijk",
        })
        return "Song"
    with patch("bragi_server.client_api.video_title", side_effect=update_during_lookup):
        response = client.post("/api/v1/playlists/p1/tracks", json={
            "url": "https://youtu.be/12345678901",
        }, auth=("alice", "alice-pass"))
    assert response.json["playlist"]["name"] == "New name"
    assert [item["name"] for item in response.json["playlist"]["videos"]] == ["Track", "Concurrent", "Song"]
    library = storage.read_playlists(directory)
    library["alice"][0]["videos"] = [library["alice"][0]["videos"][0]] * 5000
    storage._write_json(directory / "user_playlists.json", library)
    with patch("bragi_server.client_api.video_title", return_value="Song"):
        assert client.post("/api/v1/playlists/p1/tracks", json={
            "url": "https://youtu.be/12345678901",
        }, auth=("alice", "alice-pass")).status_code == 409
    assert len(storage.read_playlists(directory)["alice"][0]["videos"]) == 5000


def test_delete_specific_duplicate_preserves_order_and_queue(app):
    directory = app.config["DATA_DIRECTORY"]
    library = storage.read_playlists(directory)
    videos = [
        {"name": "Duplicate", "link": "https://youtu.be/abcdefghijk"},
        {"name": "Middle", "link": "https://youtu.be/12345678901"},
        {"name": "Duplicate", "link": "https://youtu.be/abcdefghijk"},
    ]
    library["alice"][0]["videos"] = videos
    storage._write_json(directory / "user_playlists.json", library)
    client = app.test_client()
    auth = ("alice", "alice-pass")
    queue = client.post("/api/v1/queue", json={"playlist_id": "p1"}, auth=auth).json
    response = client.delete(
        "/api/v1/playlists/p1/tracks", json={"index": 2, "videos": videos}, auth=auth,
    )
    assert response.status_code == 200
    assert [video["name"] for video in response.json["playlist"]["videos"]] == ["Duplicate", "Middle"]
    assert response.json["playlist"]["videos"][0]["thumbnail_url"]
    assert storage.read_playlists(directory)["alice"][0]["videos"] == videos[:2]
    assert client.get("/api/v1/queue", auth=auth).json == queue
    assert storage.read_playlists(directory)["bob"][0]["id"] == "p2"


def test_replace_specific_duplicate_preserves_order_and_queue(app):
    directory = app.config["DATA_DIRECTORY"]
    library = storage.read_playlists(directory)
    videos = [
        {"name": "Duplicate", "link": "https://youtu.be/abcdefghijk"},
        {"name": "Middle", "link": "https://youtu.be/12345678901"},
        {"name": "Duplicate", "link": "https://youtu.be/abcdefghijk"},
    ]
    library["alice"][0]["videos"] = videos
    storage._write_json(directory / "user_playlists.json", library)
    client = app.test_client()
    auth = ("alice", "alice-pass")
    queue = client.post("/api/v1/queue", json={"playlist_id": "p1"}, auth=auth).json
    with patch("bragi_server.client_api.video_title", return_value="Replacement") as lookup:
        response = client.patch("/api/v1/playlists/p1/tracks", auth=auth, json={
            "index": 2, "videos": videos, "url": "  https://youtu.be/zyxwvutsrqp  ",
        })
    assert response.status_code == 200
    lookup.assert_called_once_with("zyxwvutsrqp")
    replacement = {"name": "Replacement", "link": "https://youtu.be/zyxwvutsrqp"}
    assert storage.read_playlists(directory)["alice"][0]["videos"] == videos[:2] + [replacement]
    assert response.json["playlist"]["videos"][2] == {
        **replacement, "thumbnail_url": "https://i.ytimg.com/vi/zyxwvutsrqp/hqdefault.jpg",
    }
    assert client.get("/api/v1/queue", auth=auth).json == queue
    assert client.patch("/api/v1/playlists/p1/tracks", auth=auth, json={
        "index": 2, "videos": videos, "url": "https://youtu.be/zyxwvutsrqp",
    }).status_code == 409


def test_replace_track_validates_auth_ownership_input_and_metadata(app):
    client = app.test_client()
    auth = ("alice", "alice-pass")
    videos = [{"name": "Track", "link": "https://youtu.be/abcdefghijk"}]
    body = {"index": 0, "videos": videos, "url": "https://youtu.be/12345678901"}
    path = "/api/v1/playlists/p1/tracks"
    assert client.patch(path, json=body).status_code == 401
    with patch("bragi_server.client_api.video_title", side_effect=MetadataError("unavailable")) as lookup:
        for invalid in (
            {**body, "index": True}, {**body, "index": 1}, {**body, "videos": []},
            {**body, "videos": [{"name": "Track"}]}, {**body, "url": "https://evil.example/12345678901"},
        ):
            assert client.patch(path, json=invalid, auth=auth).status_code == 400
        assert client.patch("/api/v1/playlists/p2/tracks", json=body, auth=auth).status_code == 404
        assert client.patch(path, json={**body, "videos": [{**videos[0], "name": "Stale"}]}, auth=auth).status_code == 409
        assert client.patch(path, json=body, auth=auth).status_code == 422
        lookup.assert_called_once()
    assert storage.read_playlists(app.config["DATA_DIRECTORY"])["alice"][0]["videos"] == videos


def test_replace_track_rechecks_auth_and_snapshot_after_lookup(app):
    client = app.test_client()
    directory = app.config["DATA_DIRECTORY"]
    auth = ("alice", "alice-pass")
    videos = storage.read_playlists(directory)["alice"][0]["videos"]
    body = {"index": 0, "videos": videos, "url": "https://youtu.be/12345678901"}
    path = "/api/v1/playlists/p1/tracks"
    with patch("bragi_server.client_api.video_title", return_value="Replacement"):
        with patch("bragi_server.client_api._authenticated_username", side_effect=["alice", None]):
            assert client.patch(path, json=body, auth=auth).status_code == 401
    def update_during_lookup(video_id):
        storage.append_playlist_track(directory, "alice", "p1", {
            "name": "Concurrent", "link": "https://youtu.be/abcdefghijk",
        })
        return "Replacement"
    with patch("bragi_server.client_api.video_title", side_effect=update_during_lookup):
        assert client.patch(path, json=body, auth=auth).status_code == 409
    assert [item["name"] for item in storage.read_playlists(directory)["alice"][0]["videos"]] == ["Track", "Concurrent"]
    current = storage.read_playlists(directory)["alice"][0]["videos"]
    with patch("bragi_server.client_api.video_title", return_value="Replacement"):
        with patch("bragi_server.storage.replace_playlist_track", side_effect=storage.StorageError("failed")):
            assert client.patch(path, json={**body, "videos": current}, auth=auth).status_code == 500


def test_delete_track_refuses_stale_snapshot_and_repeated_request(app):
    directory = app.config["DATA_DIRECTORY"]
    client = app.test_client()
    auth = ("alice", "alice-pass")
    videos = storage.read_playlists(directory)["alice"][0]["videos"]
    storage.append_playlist_track(directory, "alice", "p1", {
        "name": "Concurrent", "link": "https://youtu.be/12345678901",
    })
    stale = client.delete("/api/v1/playlists/p1/tracks", json={"index": 0, "videos": videos}, auth=auth)
    assert stale.status_code == 409
    current = storage.read_playlists(directory)["alice"][0]["videos"]
    assert [video["name"] for video in current] == ["Track", "Concurrent"]
    body = {"index": 0, "videos": current}
    assert client.delete("/api/v1/playlists/p1/tracks", json=body, auth=auth).status_code == 200
    assert client.delete("/api/v1/playlists/p1/tracks", json=body, auth=auth).status_code == 409
    assert [video["name"] for video in storage.read_playlists(directory)["alice"][0]["videos"]] == ["Concurrent"]


def test_delete_track_validates_input_authentication_and_ownership(app):
    client = app.test_client()
    auth = ("alice", "alice-pass")
    videos = [{"name": "Track", "link": "https://youtu.be/abcdefghijk"}]
    path = "/api/v1/playlists/p1/tracks"
    assert client.delete(path, json={"index": 0, "videos": videos}).status_code == 401
    for body in (
        {}, {"index": True, "videos": videos}, {"index": -1, "videos": videos},
        {"index": 1, "videos": videos}, {"index": 0, "videos": []},
        {"index": 0, "videos": [{"name": "Track"}]},
        {"index": 0, "videos": [{"name": "Track", "link": None}]},
    ):
        assert client.delete(path, json=body, auth=auth).status_code == 400
    assert client.delete("/api/v1/playlists/p2/tracks", json={"index": 0, "videos": videos}, auth=auth).status_code == 404
    assert client.delete("/api/v1/playlists/missing/tracks", json={"index": 0, "videos": videos}, auth=auth).status_code == 404
    with patch("bragi_server.client_api._authenticated_username", side_effect=["alice", None]):
        assert client.delete(path, json={"index": 0, "videos": videos}, auth=auth).status_code == 401
    assert storage.read_playlists(app.config["DATA_DIRECTORY"])["alice"][0]["videos"] == videos


def test_video_title_extracts_metadata_without_requiring_audio_formats():
    with patch("bragi_server.youtube.YoutubeDL") as extractor:
        instance = extractor.return_value.__enter__.return_value
        instance.extract_info.return_value = {"title": "Video title", "fulltitle": "  Video title  "}
        assert video_title("12345678901") == "Video title"
        instance.extract_info.assert_called_once_with(
            "https://www.youtube.com/watch?v=12345678901", download=False
        )
        options = extractor.call_args.args[0]
        assert options["skip_download"] and options["ignore_no_formats_error"]
        for info in (
            None, {}, {"title": "youtube video #12345678901", "fulltitle": ""},
            {"fulltitle": " "}, {"fulltitle": "x" * 201},
        ):
            instance.extract_info.return_value = info
            with pytest.raises(MetadataError):
                video_title("12345678901")
        instance.extract_info.side_effect = DownloadError("sensitive source details")
        with pytest.raises(MetadataError, match="Could not retrieve"):
            video_title("12345678901")


def test_queue_shuffle_is_a_snapshot_and_does_not_change_playlist(app):
    directory = app.config["DATA_DIRECTORY"]
    path = directory / "user_playlists.json"
    library = json.loads(path.read_text(encoding="utf-8"))
    videos = [
        {"name": "First", "link": "https://youtu.be/abcdefghijk"},
        {"name": "Second", "link": "https://youtu.be/12345678901"},
        {"name": "First", "link": "https://youtu.be/abcdefghijk"},
        {"name": "Third", "link": "https://youtu.be/abcdefghijl"},
    ]
    library["alice"][0]["videos"] = videos
    path.write_text(json.dumps(library), encoding="utf-8")
    client = app.test_client()
    auth = ("alice", "alice-pass")
    with patch("bragi_server.client_api.random.shuffle", side_effect=lambda items: items.reverse()) as shuffle:
        response = client.post("/api/v1/queue", json={"playlist_id": "p1", "shuffle": True}, auth=auth)
    assert response.status_code == 201
    shuffle.assert_called_once()
    queue = response.json["queue"]
    assert queue["id"]
    assert queue["playlist_id"] == "p1"
    assert queue["shuffled"] is True
    assert [(video["name"], video["link"]) for video in queue["videos"]] == [
        (video["name"], video["link"]) for video in reversed(videos)
    ]
    assert queue["videos"][0]["thumbnail_url"] == "https://i.ytimg.com/vi/abcdefghijl/hqdefault.jpg"
    assert queue["videos"][1]["thumbnail_url"] == queue["videos"][3]["thumbnail_url"]
    assert storage.read_playlists(directory)["alice"][0]["videos"] == videos
    library["alice"][0]["videos"] = []
    path.write_text(json.dumps(library), encoding="utf-8")
    assert client.get("/api/v1/queue", auth=auth).json == {"queue": queue}


def test_thumbnails_are_derived_for_playlists_and_queues_without_changing_storage(app):
    directory = app.config["DATA_DIRECTORY"]
    path = directory / "user_playlists.json"
    library = json.loads(path.read_text(encoding="utf-8"))
    videos = [
        {"name": "Short link", "link": "https://youtu.be/abcdefghijk?t=42"},
        {"name": "Watch link", "link": "https://www.youtube.com/watch?v=12345678901"},
        {"name": "Invalid ID", "link": "https://youtu.be/short"},
    ]
    library["alice"][0]["videos"] = videos
    path.write_text(json.dumps(library), encoding="utf-8")
    original = path.read_bytes()
    expected = [
        {**videos[0], "thumbnail_url": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg"},
        {**videos[1], "thumbnail_url": "https://i.ytimg.com/vi/12345678901/hqdefault.jpg"},
        {**videos[2], "thumbnail_url": None},
    ]
    client = app.test_client()
    auth = ("alice", "alice-pass")
    listing = client.get("/api/v1/playlists", auth=auth)
    detail = client.get("/api/v1/playlists/p1", auth=auth)
    created = client.post("/api/v1/queue", json={"playlist_id": "p1"}, auth=auth)
    current = client.get("/api/v1/queue", auth=auth)
    assert listing.json["playlists"][0]["videos"] == expected
    assert detail.json["playlist"]["videos"] == expected
    assert created.status_code == 201
    assert created.json["queue"]["videos"] == expected
    assert current.json == created.json
    assert path.read_bytes() == original
    assert storage.read_playlists(directory) == library


def test_queue_lifecycle_is_per_user_and_temporary(app):
    directory = app.config["DATA_DIRECTORY"]
    path = directory / "user_playlists.json"
    library = json.loads(path.read_text(encoding="utf-8"))
    library["bob"][0]["videos"] = [{"name": "Bob track", "link": "https://youtu.be/12345678901"}]
    path.write_text(json.dumps(library), encoding="utf-8")
    client = app.test_client()
    alice = ("alice", "alice-pass")
    bob = ("bob", "bob-pass")
    payload = {"playlist_id": "p1"}
    assert client.get("/api/v1/queue").status_code == 401
    assert client.post("/api/v1/queue", json=payload).status_code == 401
    assert client.delete("/api/v1/queue").status_code == 401
    assert client.get("/api/v1/queue", auth=alice).status_code == 404
    first = client.post("/api/v1/queue", json=payload, auth=alice)
    assert first.status_code == 201
    assert first.json["queue"]["shuffled"] is False
    assert client.get("/api/v1/queue", auth=bob).status_code == 404
    assert client.post("/api/v1/queue", json=payload, auth=bob).status_code == 404
    bob_queue = client.post("/api/v1/queue", json={"playlist_id": "p2"}, auth=bob).json
    second = client.post("/api/v1/queue", json=payload, auth=alice)
    assert second.json["queue"]["id"] != first.json["queue"]["id"]
    assert client.get("/api/v1/queue", auth=alice).json == second.json
    assert client.get("/api/v1/queue", auth=bob).json == bob_queue
    restarted_client = create_app(data_directory=app.config["DATA_DIRECTORY"]).test_client()
    assert restarted_client.get("/api/v1/queue", auth=alice).status_code == 404
    assert client.delete("/api/v1/queue", auth=alice).status_code == 204
    assert client.delete("/api/v1/queue", auth=alice).status_code == 204
    assert client.get("/api/v1/queue", auth=alice).status_code == 404


def test_queue_rechecks_authentication_before_creating(app):
    directory = app.config["DATA_DIRECTORY"]
    path = directory / "users.json"
    read_users = storage.read_users
    calls = 0

    def delete_after_auth(data_directory):
        nonlocal calls
        users = read_users(data_directory)
        calls += 1
        if calls == 1:
            path.write_text(json.dumps({
                "users": [user for user in users if user["username"] != "alice"]
            }), encoding="utf-8")
        return users

    client = app.test_client()
    with patch("bragi_server.storage.read_users", side_effect=delete_after_auth):
        response = client.post(
            "/api/v1/queue", json={"playlist_id": "p1"}, auth=("alice", "alice-pass")
        )
    assert response.status_code == 401
    assert "alice" not in app.extensions["playback_queues"]


@pytest.mark.parametrize("payload", [
    None, {}, {"playlist_id": ""}, {"playlist_id": "p1", "shuffle": 1},
    {"playlist_id": "p1", "shuffle": "true"},
])
def test_invalid_queue_request_preserves_current_queue(app, payload):
    client = app.test_client()
    auth = ("alice", "alice-pass")
    original = client.post("/api/v1/queue", json={"playlist_id": "p1"}, auth=auth).json
    assert client.post("/api/v1/queue", json=payload, auth=auth).status_code == 400
    assert client.get("/api/v1/queue", auth=auth).json == original


def test_missing_or_empty_playlist_does_not_replace_queue(app):
    client = app.test_client()
    auth = ("alice", "alice-pass")
    original = client.post("/api/v1/queue", json={"playlist_id": "p1"}, auth=auth).json
    assert client.post("/api/v1/queue", json={"playlist_id": "missing"}, auth=auth).status_code == 404
    directory = app.config["DATA_DIRECTORY"]
    path = directory / "user_playlists.json"
    library = json.loads(path.read_text(encoding="utf-8"))
    library["alice"][0]["videos"] = []
    path.write_text(json.dumps(library), encoding="utf-8")
    assert client.post("/api/v1/queue", json={"playlist_id": "p1"}, auth=auth).status_code == 422
    assert client.get("/api/v1/queue", auth=auth).json == original


def test_resolve_validates_url_and_returns_temporary_audio(app):
    client = app.test_client()
    auth = ("alice", "alice-pass")
    audio = {
        "url": "https://example.test/stream?signature=secret",
        "title": "Track",
        "content_type": "audio/mp4",
    }
    with patch("bragi_server.client_api.resolve_audio", return_value=audio) as resolver:
        assert client.post("/api/v1/resolve", json={"url": "https://youtu.be/abcdefghijk"}).status_code == 401
        assert client.post("/api/v1/resolve", json={"url": "https://evil.test/audio"}, auth=auth).status_code == 400
        response = client.post(
            "/api/v1/resolve", json={"url": "https://youtu.be/abcdefghijk"}, auth=auth
        )
    resolver.assert_called_once_with("abcdefghijk")
    assert response.json == {"audio": audio}
    assert response.headers["Cache-Control"] == "no-store"

    with patch("bragi_server.client_api.resolve_audio", side_effect=ResolveError("Failed.")):
        response = client.post(
            "/api/v1/resolve", json={"url": "https://youtu.be/abcdefghijk"}, auth=auth
        )
    assert response.status_code == 422
    log = (app.config["DATA_DIRECTORY"] / "bragi_log.json").read_text(encoding="utf-8")
    assert json.loads(log)["events"][-1]["category"] == "resolve_error"
    assert "secret" not in log


def test_storage_failure_is_json_and_logged(app):
    (app.config["DATA_DIRECTORY"] / "user_playlists.json").write_text("{bad", encoding="utf-8")
    response = app.test_client().get("/api/v1/playlists", auth=("alice", "alice-pass"))
    assert response.status_code == 500
    assert response.is_json
    log = json.loads((app.config["DATA_DIRECTORY"] / "bragi_log.json").read_text(encoding="utf-8"))
    assert log["events"][-1]["category"] == "exception"


def test_cross_origin_access_is_opt_in_and_supports_authorization(app):
    client = app.test_client()
    origin = "https://player.example.com"
    headers = {
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    assert "Access-Control-Allow-Origin" not in client.options(
        "/api/v1/resolve", headers=headers
    ).headers
    storage.save_allowed_origins(
        app.config["DATA_DIRECTORY"], [origin, "https://other.example.com:8443"]
    )
    preflight = client.options("/api/v1/resolve", headers=headers)
    assert preflight.status_code == 204
    assert preflight.headers["Access-Control-Allow-Origin"] == origin
    assert preflight.headers["Access-Control-Allow-Credentials"] == "true"
    assert "Authorization" in preflight.headers["Access-Control-Allow-Headers"]
    assert "DELETE" in preflight.headers["Access-Control-Allow-Methods"]
    failed_auth = client.get("/api/v1/playlists", headers={"Origin": origin})
    assert failed_auth.status_code == 401
    assert failed_auth.headers["Access-Control-Allow-Origin"] == origin
    disallowed = client.get(
        "/api/v1/playlists", headers={"Origin": "https://evil.example.com"},
        auth=("alice", "alice-pass"),
    )
    assert "Access-Control-Allow-Origin" not in disallowed.headers


@pytest.mark.parametrize("origin", ["*", "https://*.example.com", "https://example.com/path", "https://user@example.com", "https://example.com:bad"])
def test_invalid_allowed_origins_fail_startup(tmp_path, origin):
    (tmp_path / "settings.json").write_text(json.dumps({
        "api_allowed_origins": [origin]
    }), encoding="utf-8")
    with pytest.raises(storage.StorageError, match="settings.json"):
        create_app(data_directory=tmp_path)
