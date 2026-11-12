import json
import socket
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from bragi_server import audio_proxy, storage
from bragi_server.app import create_app


URL = "https://r1.googlevideo.com/videoplayback?signature=signed-secret"
AUTH = ("alice", "alice-pass")


@pytest.fixture
def app(tmp_path):
    (tmp_path / "users.json").write_text(json.dumps({"users": [
        {"username": "alice", "password": "alice-pass"},
        {"username": "bob", "password": "bob-pass"},
    ]}), encoding="utf-8")
    return create_app(data_directory=tmp_path)


@pytest.fixture
def dns(monkeypatch):
    def resolve(hostname, port, *, type):
        assert hostname == "r1.googlevideo.com"
        assert port == 443
        return [(socket.AF_INET, type, 6, "", ("8.8.8.8", port))]

    monkeypatch.setattr(audio_proxy.socket, "getaddrinfo", resolve)


class FakeUpstream:
    def __init__(self, status=200, chunks=None, headers=None):
        self.status = status
        self.chunks = iter(chunks if chunks is not None else [b"abc", b"def"])
        self.headers = headers or {}
        self.read_calls = 0
        self.closed = False

    def read(self, size):
        assert size == audio_proxy.CHUNK_SIZE
        self.read_calls += 1
        value = next(self.chunks, b"")
        if isinstance(value, Exception):
            raise value
        return value

    def getheader(self, name):
        return self.headers.get(name)

    def close(self):
        self.closed = True


@pytest.fixture
def upstream(monkeypatch, dns):
    requests = []
    responses = []

    class Connection:
        def __init__(self, hostname, address):
            self.hostname = hostname
            self.address = address
            self.closed = False
            requests.append(self)

        def request(self, method, target, headers):
            self.method, self.target, self.headers = method, target, headers

        def getresponse(self):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        def close(self):
            self.closed = True

    monkeypatch.setattr(audio_proxy, "_PinnedHTTPSConnection", Connection)
    return requests, responses


def issue(client, auth=AUTH):
    response = client.post("/api/v1/audio-proxy", json={"url": URL}, auth=auth)
    assert response.status_code == 200
    assert response.json["expires_in"] == 21600
    assert response.headers["Cache-Control"] == "no-store"
    return response.json["proxy_url"]


def test_authentication_and_reusable_bearer_tokens(app, upstream):
    client = app.test_client()
    assert client.post("/api/v1/audio-proxy", json={"url": URL}).status_code == 401
    assert client.post("/api/v1/audio-proxy", json={"url": URL}, auth=("alice", "wrong")).status_code == 401
    proxy_url = issue(client)
    other_url = issue(client)
    parts = urlsplit(proxy_url)
    token = parse_qs(parts.query)["token"][0]
    assert parts.path == "/api/v1/audio-stream"
    assert not parts.netloc and len(token) == 43 and other_url != proxy_url
    assert URL not in proxy_url
    assert client.get("/api/v1/audio-stream").status_code == 401
    assert client.get(proxy_url + "&token=" + token).status_code == 401
    assert client.head(proxy_url).status_code == 405
    assert client.get("/api/v1/playlists?token=" + token).status_code == 401
    requests, responses = upstream
    responses.extend([FakeUpstream(), FakeUpstream()])
    for _ in range(2):
        response = client.get(proxy_url)
        assert response.status_code == 200
        assert response.data == b"abcdef"
        response.close()
    assert all(connection.closed for connection in requests)


def test_expiry_and_bounded_storage(app, upstream, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(audio_proxy.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(audio_proxy, "MAX_TOKENS", 1)
    client = app.test_client()
    url = issue(client)
    assert client.post("/api/v1/audio-proxy", json={"url": URL}, auth=AUTH).status_code == 503
    upstream[1].append(FakeUpstream())
    now[0] += 21599
    response = client.get(url)
    assert response.status_code == 200
    response.close()
    now[0] += 1
    assert client.get(url).status_code == 401
    assert issue(client) != url


def test_restart_invalidates_tokens(app, upstream):
    client = app.test_client()
    url = issue(client)
    restarted = create_app(data_directory=app.config["DATA_DIRECTORY"]).test_client()
    assert restarted.get(url).status_code == 401
    assert not upstream[0]


@pytest.mark.parametrize("change", ["password", "delete"])
def test_account_changes_revoke_all_tokens_even_if_restored(app, upstream, change):
    client = app.test_client()
    alice = issue(client)
    bob = issue(client, ("bob", "bob-pass"))
    directory = app.config["DATA_DIRECTORY"]
    original = storage.read_users(directory)
    changed = [dict(user) for user in original]
    if change == "password":
        changed[0]["password"] = "new-password"
    else:
        changed.pop(0)
    storage._write_json(directory / "users.json", {"users": changed})
    storage._write_json(directory / "users.json", {"users": original})
    assert client.get(alice).status_code == 401
    assert client.get(bob).status_code == 401
    assert not upstream[0]


@pytest.mark.parametrize("url", [
    None, "", "http://r1.googlevideo.com/a", "https://evil.com/a",
    "https://googlevideo.com.evil.com/a", "https://user@r1.googlevideo.com/a",
    "https://user:pass@r1.googlevideo.com/a", "https://r1.googlevideo.com:444/a",
    "https://r1.googlevideo.com:/a", "https://r1.googlevideo.com/a#fragment",
    "https://r1.googlevideo.com\\@evil.com/a", "https://r1.googlevideo.com/a\n",
    "https://127.0.0.1/a", "https://r1.googlevideo.com/" + "a" * 16384,
])
def test_rejects_unsafe_urls(app, url):
    response = app.test_client().post("/api/v1/audio-proxy", json={"url": url}, auth=AUTH)
    assert response.status_code == 400


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "ff02::1"])
def test_rejects_nonpublic_or_mixed_dns_answers(app, monkeypatch, address):
    def resolve(hostname, port, *, type):
        return [
            (socket.AF_INET, type, 6, "", ("8.8.8.8", port)),
            (socket.AF_INET, type, 6, "", (address, port)),
        ]

    monkeypatch.setattr(audio_proxy.socket, "getaddrinfo", resolve)
    response = app.test_client().post("/api/v1/audio-proxy", json={"url": URL}, auth=AUTH)
    assert response.status_code == 400


def test_rechecks_dns_before_stream_to_prevent_rebinding(app, upstream, monkeypatch):
    client = app.test_client()
    url = issue(client)
    monkeypatch.setattr(audio_proxy.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
    ])
    assert client.get(url).status_code == 502
    assert not upstream[0]


def test_dns_failure_is_explicit_and_sanitized(app, monkeypatch, caplog):
    def fail_lookup(*args, **kwargs):
        raise socket.gaierror(URL)

    monkeypatch.setattr(audio_proxy.socket, "getaddrinfo", fail_lookup)
    response = app.test_client().post("/api/v1/audio-proxy", json={"url": URL}, auth=AUTH)
    assert response.status_code == 502
    log = (app.config["DATA_DIRECTORY"] / "bragi_log.json").read_text(encoding="utf-8")
    assert "signed-secret" not in log
    assert "signed-secret" not in caplog.text


def test_pinned_tls_uses_validated_address_and_original_hostname(monkeypatch):
    sock = Mock()
    wrapped = Mock()
    context = Mock()
    context.wrap_socket.return_value = wrapped
    monkeypatch.setattr(audio_proxy.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(audio_proxy.socket, "socket", lambda *args: sock)
    address = (socket.AF_INET, socket.SOCK_STREAM, 6, ("8.8.8.8", 443))
    connection = audio_proxy._PinnedHTTPSConnection("r1.googlevideo.com", address)
    connection.connect()
    sock.connect.assert_called_once_with(("8.8.8.8", 443))
    context.wrap_socket.assert_called_once_with(sock, server_hostname="r1.googlevideo.com")
    assert connection.sock is wrapped
    connection.close()


def test_failed_tls_handshake_closes_socket(monkeypatch):
    sock = Mock()
    context = Mock()
    context.wrap_socket.side_effect = OSError("TLS failed")
    monkeypatch.setattr(audio_proxy.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(audio_proxy.socket, "socket", lambda *args: sock)
    address = (socket.AF_INET, socket.SOCK_STREAM, 6, ("8.8.8.8", 443))
    connection = audio_proxy._PinnedHTTPSConnection("r1.googlevideo.com", address)
    with pytest.raises(OSError):
        connection.connect()
    sock.close.assert_called_once()


@pytest.mark.parametrize(("status", "range_value", "headers", "chunks"), [
    (200, None, {"Content-Type": "audio/mp4", "Content-Length": "6", "Accept-Ranges": "bytes"}, [b"abc", b"def"]),
    (206, "bytes=3-5", {"Content-Type": "audio/mp4", "Content-Length": "3", "Content-Range": "bytes 3-5/6", "Accept-Ranges": "bytes"}, [b"def"]),
    (416, "bytes=9-", {"Content-Length": "0", "Content-Range": "bytes */6", "Accept-Ranges": "bytes"}, []),
])
def test_streams_status_ranges_and_headers(app, upstream, status, range_value, headers, chunks):
    client = app.test_client()
    url = issue(client)
    source = FakeUpstream(status, chunks, headers)
    upstream[1].append(source)
    request_headers = {"Range": range_value} if range_value else {}
    response = client.get(url, headers=request_headers)
    assert response.status_code == status
    assert response.data == b"".join(chunks)
    assert response.headers["Cache-Control"] == "no-store"
    for name, value in headers.items():
        assert response.headers[name] == value
    connection = upstream[0][0]
    assert connection.hostname == "r1.googlevideo.com"
    assert connection.address[-1] == ("8.8.8.8", 443)
    assert connection.method == "GET"
    assert connection.target == "/videoplayback?signature=signed-secret"
    assert connection.headers.get("Range") == range_value
    assert source.closed and connection.closed


def test_client_disconnect_closes_upstream_without_buffering(app, upstream):
    client = app.test_client()
    url = issue(client)
    source = FakeUpstream(chunks=[b"first", b"second", b"third"])
    upstream[1].append(source)
    response = client.get(url, buffered=False)
    assert source.read_calls == 1
    response.close()
    assert source.closed and upstream[0][0].closed
    assert source.read_calls == 1


def test_cors_preflight_and_stream_respect_allowed_origins(app, upstream):
    origin = "https://player.example.com"
    storage.save_allowed_origins(app.config["DATA_DIRECTORY"], [origin])
    client = app.test_client()
    preflight = client.options("/api/v1/audio-proxy", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert preflight.status_code == 204
    assert preflight.headers["Access-Control-Allow-Origin"] == origin
    stream_preflight = client.options("/api/v1/audio-stream", headers={
        "Origin": origin, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "range",
    })
    assert "Range" in stream_preflight.headers["Access-Control-Allow-Headers"]
    url = issue(client)
    upstream[1].extend([FakeUpstream(), FakeUpstream()])
    response = client.get(url, headers={"Origin": origin})
    assert response.headers["Access-Control-Allow-Origin"] == origin
    assert "Content-Range" in response.headers["Access-Control-Expose-Headers"]
    response.close()
    response = client.get(url, headers={"Origin": "https://evil.example.com"})
    assert "Access-Control-Allow-Origin" not in response.headers
    response.close()


@pytest.mark.parametrize("status", [302, 403, 500])
def test_upstream_redirects_and_errors_are_not_followed(app, upstream, status):
    client = app.test_client()
    url = issue(client)
    source = FakeUpstream(status, headers={"Location": "https://evil.example/"})
    upstream[1].append(source)
    response = client.get(url)
    assert response.status_code == 502
    assert "Location" not in response.headers
    assert source.closed and upstream[0][0].closed
    assert len(upstream[0]) == 1


def test_upstream_failures_are_sanitized_and_streams_closed(app, upstream, caplog):
    client = app.test_client()
    url = issue(client)
    token = parse_qs(urlsplit(url).query)["token"][0]
    upstream[1].append(OSError(URL))
    assert client.get(url).status_code == 502
    assert upstream[0][0].closed
    source = FakeUpstream(chunks=[b"first", OSError(URL)])
    upstream[1].append(source)
    response = client.get(url, buffered=False)
    with pytest.raises(audio_proxy.AudioProxyError, match="stream interrupted"):
        response.get_data()
    response.close()
    assert source.closed and upstream[0][1].closed
    log = (app.config["DATA_DIRECTORY"] / "bragi_log.json").read_text(encoding="utf-8")
    for secret in (token, URL, "signed-secret", AUTH[1]):
        assert secret not in log
        assert secret not in caplog.text
