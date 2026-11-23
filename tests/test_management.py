import json
from unittest.mock import patch

import pytest

from bragi_server.app import create_app
from bragi_server import storage


@pytest.fixture
def app(tmp_path):
    (tmp_path / "users.json").write_text(json.dumps({
        "users": [
            {"username": "admin", "password": "admin-pass"},
            {"username": "alice", "password": "alice-pass"},
            {"username": "bob", "password": "bob-pass"},
        ]
    }), encoding="utf-8")
    (tmp_path / "user_playlists.json").write_text(json.dumps({
        "alice": [{"id": "p1", "name": "Alice mix", "videos": []}],
        "bob": [{"id": "p2", "name": "Bob mix", "videos": []}],
    }), encoding="utf-8")
    return create_app(data_directory=tmp_path)


def _token(client):
    with client.session_transaction() as browser_session:
        return browser_session["csrf_token"]


def _login(client, username, password):
    client.get("/login")
    return client.post("/login", data={
        "csrf_token": _token(client), "username": username, "password": password
    })


def _post(client, path, **data):
    client.get("/")
    return client.post(path, data={"csrf_token": _token(client), **data})


def test_login_requires_csrf_and_authentication(app):
    client = app.test_client()
    assert client.get("/").status_code == 302
    assert client.post("/login", data={"username": "admin", "password": "admin-pass"}).status_code == 400
    assert _login(client, "admin", "incorrect").status_code == 401
    assert _login(client, "admin", "admin-pass").status_code == 302
    assert client.get("/admin/users").status_code == 200
    assert client.post("/admin/users", data={"username": "new", "password": "x"}).status_code == 400


def test_regular_user_cannot_access_admin_or_other_users_playlists(app):
    client = app.test_client()
    _login(client, "alice", "alice-pass")
    response = client.get("/")
    assert b"Alice mix" in response.data
    assert b"Bob mix" not in response.data
    assert b"Show playlists" not in response.data
    assert client.get("/admin/users").status_code == 403
    assert client.get("/?owner=bob").status_code == 403
    assert _post(client, "/playlists/p2/delete", owner="bob").status_code == 403
    assert storage.read_playlists(app.config["DATA_DIRECTORY"])["bob"][0]["id"] == "p2"


def test_admin_sees_all_users_playlists_and_can_create_for_each(app):
    client = app.test_client()
    _login(client, "admin", "admin-pass")
    response = client.get("/")
    assert b"Alice mix" in response.data
    assert b"Bob mix" in response.data
    assert b"Show playlists" not in response.data
    assert b'<select' not in response.data
    for owner in ("admin", "alice", "bob"):
        assert f'/playlists/new?owner={owner}'.encode() in response.data
    assert b'/playlists/p1/edit?owner=alice' in response.data
    assert b'/playlists/p2/edit?owner=bob' in response.data
    assert b'name="owner" value="alice"' in response.data
    assert b'name="owner" value="bob"' in response.data


def test_admin_create_delete_user_and_their_playlists(app):
    client = app.test_client()
    _login(client, "admin", "admin-pass")
    assert _post(client, "/admin/users", username="charlie", password="secret").status_code == 302
    assert any(user["username"] == "charlie" for user in storage.read_users(app.config["DATA_DIRECTORY"]))
    assert _post(client, "/admin/users/delete", username="alice").status_code == 302
    assert "alice" not in storage.read_playlists(app.config["DATA_DIRECTORY"])
    assert all(user["username"] != "alice" for user in storage.read_users(app.config["DATA_DIRECTORY"]))
    assert _post(client, "/admin/users/delete", username="admin").status_code == 302
    assert any(user["username"] == "admin" for user in storage.read_users(app.config["DATA_DIRECTORY"]))


def test_playlist_create_edit_delete_persist_and_owner_isolation(app):
    client = app.test_client()
    _login(client, "admin", "admin-pass")
    response = _post(
        client, "/playlists/new", owner="alice", name="New mix",
        video_name=["First", "Second"],
        video_link=["https://youtu.be/abcdefghijk", "https://youtube.com/watch?v=12345678901"],
    )
    assert response.status_code == 302
    library = storage.read_playlists(app.config["DATA_DIRECTORY"])
    playlist = library["alice"][-1]
    assert playlist["videos"] == []
    playlist_id = playlist["id"]
    response = _post(client, f"/playlists/{playlist_id}/edit", owner="alice", name="Updated")
    assert response.status_code == 302
    assert storage.read_playlists(app.config["DATA_DIRECTORY"])["alice"][-1]["name"] == "Updated"
    assert _post(client, f"/playlists/{playlist_id}/delete", owner="alice").status_code == 302
    assert len(storage.read_playlists(app.config["DATA_DIRECTORY"])["alice"]) == 1
    assert storage.read_playlists(app.config["DATA_DIRECTORY"])["bob"][0]["name"] == "Bob mix"


def test_playlist_edit_preserves_tracks_and_has_no_track_controls(app):
    client = app.test_client()
    _login(client, "alice", "alice-pass")
    directory = app.config["DATA_DIRECTORY"]
    first = {"name": "First", "link": "https://youtu.be/abcdefghijk"}
    second = {"name": "Second", "link": "https://youtu.be/12345678901"}
    storage.append_playlist_track(directory, "alice", "p1", first)
    response = client.get("/playlists/p1/edit")
    assert b"video_name" not in response.data
    assert b"video_link" not in response.data
    assert b"Add track" not in response.data
    storage.append_playlist_track(directory, "alice", "p1", second)
    response = _post(
        client, "/playlists/p1/edit", owner="alice", name="Changed",
        video_name="Injected", video_link="https://youtu.be/zzzzzzzzzzz",
    )
    assert response.status_code == 302
    playlist = storage.read_playlists(directory)["alice"][0]
    assert playlist["name"] == "Changed"
    assert [
        {"name": video["name"], "link": video["link"]} for video in playlist["videos"]
    ] == [first, second]
    assert [video["id"] for video in playlist["videos"]]


def test_invalid_playlist_name_does_not_change_playlist(app):
    client = app.test_client()
    _login(client, "alice", "alice-pass")
    response = _post(client, "/playlists/p1/edit", owner="alice", name="  ")
    assert response.status_code == 400
    assert storage.read_playlists(app.config["DATA_DIRECTORY"])["alice"][0]["name"] == "Alice mix"


def test_deleted_or_password_changed_user_loses_session(app):
    client = app.test_client()
    _login(client, "alice", "alice-pass")
    directory = app.config["DATA_DIRECTORY"]
    users = storage.read_users(directory)
    users[1]["password"] = "new-password"
    (directory / "users.json").write_text(json.dumps({"users": users}), encoding="utf-8")
    assert client.get("/").status_code == 302
    assert _login(client, "alice", "new-password").status_code == 302
    storage.delete_user(directory, "alice")
    assert client.get("/").status_code == 302


def test_user_delete_rolls_back_if_playlist_save_fails(app):
    directory = app.config["DATA_DIRECTORY"]
    original_write = storage._write_json

    def failing_write(path, data):
        if path.name == "user_playlists.json":
            raise storage.StorageError("Simulated write failure.")
        original_write(path, data)

    with patch("bragi_server.storage._write_json", side_effect=failing_write):
        with pytest.raises(storage.StorageError, match="rolled back"):
            storage.delete_user(directory, "alice")
    assert any(user["username"] == "alice" for user in storage.read_users(directory))
    assert "alice" in storage.read_playlists(directory)


def test_session_key_persists_between_app_instances(app):
    directory = app.config["DATA_DIRECTORY"]
    assert create_app(data_directory=directory).secret_key == app.secret_key


def test_malformed_playlist_file_is_not_overwritten(app):
    directory = app.config["DATA_DIRECTORY"]
    path = directory / "user_playlists.json"
    path.write_text("{bad", encoding="utf-8")
    client = app.test_client()
    _login(client, "admin", "admin-pass")
    response = client.get("/")
    assert response.status_code == 500
    assert path.read_text(encoding="utf-8") == "{bad"


def test_admin_settings_persist_and_change_cors_immediately(app):
    directory = app.config["DATA_DIRECTORY"]
    settings_path = directory / "settings.json"
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "api_allowed_origins": [], "cast_batch_size": 16,
    }
    client = app.test_client()
    _login(client, "admin", "admin-pass")
    assert b"Settings" in client.get("/").data
    assert client.get("/admin/settings").status_code == 200
    origin = "https://player.example.com"
    headers = {"Origin": origin, "Access-Control-Request-Method": "POST"}
    assert "Access-Control-Allow-Origin" not in client.options("/api/v1/resolve", headers=headers).headers
    response = _post(
        client, "/admin/settings",
        api_allowed_origins=f"{origin}\nhttps://other.example.com:8443\n{origin}", cast_batch_size="27",
    )
    assert response.status_code == 302
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "api_allowed_origins": [origin, "https://other.example.com:8443"], "cast_batch_size": 27,
    }
    assert client.options("/api/v1/resolve", headers=headers).headers["Access-Control-Allow-Origin"] == origin
    restarted_client = create_app(data_directory=directory).test_client()
    assert restarted_client.options("/api/v1/resolve", headers=headers).headers["Access-Control-Allow-Origin"] == origin
    assert storage.read_settings(directory)["cast_batch_size"] == 27
    assert _post(client, "/admin/settings", api_allowed_origins="", cast_batch_size="27").status_code == 302
    assert "Access-Control-Allow-Origin" not in client.options("/api/v1/resolve", headers=headers).headers


def test_settings_are_admin_only_csrf_protected_and_reject_invalid_values(app):
    client = app.test_client()
    assert client.get("/admin/settings").status_code == 302
    _login(client, "alice", "alice-pass")
    assert b"Settings</a>" not in client.get("/").data
    assert client.get("/admin/settings").status_code == 403
    assert _post(client, "/admin/settings", api_allowed_origins="https://evil.example", cast_batch_size="16").status_code == 403
    _login(client, "admin", "admin-pass")
    assert _post(client, "/admin/settings", api_allowed_origins="https://trusted.example", cast_batch_size="16").status_code == 302
    assert client.post("/admin/settings", data={"api_allowed_origins": "https://evil.example"}).status_code == 400
    assert _post(client, "/admin/settings").status_code == 400
    assert _post(client, "/admin/settings", api_allowed_origins="https://example.com/path", cast_batch_size="16").status_code == 400
    assert storage.read_settings(app.config["DATA_DIRECTORY"])["api_allowed_origins"] == ["https://trusted.example"]


def test_settings_write_failure_does_not_change_live_cors(app):
    client = app.test_client()
    _login(client, "admin", "admin-pass")
    with patch("bragi_server.storage._write_json", side_effect=storage.StorageError("Write failed.")):
        response = _post(client, "/admin/settings", api_allowed_origins="https://player.example.com", cast_batch_size="16")
    assert response.status_code == 500
    assert storage.read_settings(app.config["DATA_DIRECTORY"])["api_allowed_origins"] == []
    preflight = client.options("/api/v1/resolve", headers={"Origin": "https://player.example.com"})
    assert "Access-Control-Allow-Origin" not in preflight.headers


def test_cast_batch_setting_validates_and_preserves_legacy_settings(app):
    directory = app.config["DATA_DIRECTORY"]
    path = directory / "settings.json"
    legacy = {"api_allowed_origins": ["https://trusted.example"]}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    client = app.test_client()
    _login(client, "admin", "admin-pass")
    assert b'value="16"' in client.get("/admin/settings").data
    for batch in ("", "0", "5001", "-1", "1.5", "true", "999999999999999999999"):
        response = _post(client, "/admin/settings", api_allowed_origins="", cast_batch_size=batch)
        assert response.status_code == 400
        assert b"Cast batch size must be between" in response.data
        assert json.loads(path.read_text(encoding="utf-8")) == legacy
    assert _post(client, "/admin/settings", api_allowed_origins="https://trusted.example", cast_batch_size="5000").status_code == 302
    assert storage.read_settings(directory) == {**legacy, "cast_batch_size": 5000}
    assert _post(client, "/admin/settings", api_allowed_origins="https://trusted.example", cast_batch_size="1").status_code == 302
    restarted = create_app(data_directory=directory)
    assert storage.read_settings(restarted.config["DATA_DIRECTORY"])["cast_batch_size"] == 1
    library = storage.read_playlists(directory)
    library["alice"][0]["videos"] = [{"name": "Track", "link": "https://youtu.be/abcdefghijk"}]
    storage._write_json(directory / "user_playlists.json", library)
    auth = ("alice", "alice-pass")
    queue = client.post("/api/v1/queue", json={"playlist_id": "p1"}, auth=auth).json["queue"]
    response = client.post("/api/v1/cast-queue", json={"queue_id": queue["id"]}, auth=auth)
    assert response.json["batch_size"] == 1


@pytest.mark.parametrize("value", [0, 5001, True, "16", 1.5, None])
def test_invalid_cast_batch_setting_fails_startup(app, value):
    path = app.config["DATA_DIRECTORY"] / "settings.json"
    path.write_text(json.dumps({"api_allowed_origins": [], "cast_batch_size": value}), encoding="utf-8")
    with pytest.raises(storage.StorageError, match="invalid Cast batch size"):
        create_app(data_directory=app.config["DATA_DIRECTORY"])


def test_deleting_user_clears_their_temporary_queue(app):
    directory = app.config["DATA_DIRECTORY"]
    path = directory / "user_playlists.json"
    library = json.loads(path.read_text(encoding="utf-8"))
    library["alice"][0]["videos"] = [{"name": "Track", "link": "https://youtu.be/abcdefghijk"}]
    path.write_text(json.dumps(library), encoding="utf-8")
    client = app.test_client()
    assert client.post(
        "/api/v1/queue", json={"playlist_id": "p1"}, auth=("alice", "alice-pass")
    ).status_code == 201
    _login(client, "admin", "admin-pass")
    assert _post(client, "/admin/users/delete", username="alice").status_code == 302
    assert _post(client, "/admin/users", username="alice", password="alice-pass").status_code == 302
    assert client.get("/api/v1/queue", auth=("alice", "alice-pass")).status_code == 404


def test_malformed_settings_are_reported_without_overwrite(app):
    path = app.config["DATA_DIRECTORY"] / "settings.json"
    path.write_text("{bad", encoding="utf-8")
    client = app.test_client()
    response = client.options("/api/v1/resolve", headers={"Origin": "https://player.example.com"})
    assert response.status_code == 500
    assert response.is_json
    assert "Access-Control-Allow-Origin" not in response.headers
    assert path.read_text(encoding="utf-8") == "{bad"
    with pytest.raises(storage.StorageError, match="settings.json"):
        create_app(data_directory=app.config["DATA_DIRECTORY"])
    assert path.read_text(encoding="utf-8") == "{bad"
