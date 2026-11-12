import hashlib
import http.client
import ipaddress
import re
import secrets
import socket
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit

from bragi_server import storage


TOKEN_LIFETIME = 21600
MAX_TOKENS = 1024
MAX_URL_LENGTH = 16384
UPSTREAM_TIMEOUT = 15
CHUNK_SIZE = 64 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_HOST = re.compile(
    r"(?:(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.)*googlevideo\.com\Z"
)


class InvalidAudioURL(ValueError):
    """The upstream audio URL or its resolved addresses are not permitted."""


class AudioProxyError(Exception):
    """An upstream audio request failed without exposing its URL."""


class TokenCapacityError(Exception):
    """The proxy token store has reached its limit."""


@dataclass(frozen=True)
class AudioToken:
    username: str
    credential_fingerprint: str = field(repr=False)
    url: str = field(repr=False)
    expires_at: float


def users_revision(directory: Path) -> tuple[int, int, int, int, int]:
    try:
        stat = (directory / "users.json").stat()
    except OSError as error:
        raise storage.StorageError("Could not inspect users.json.") from error
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


class AudioTokenStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[bytes, AudioToken] = {}
        self._users_revision: tuple[int, int, int, int, int] | None = None

    def _sync(self, revision: tuple[int, int, int, int, int]) -> None:
        if self._users_revision != revision:
            self._entries.clear()
            self._users_revision = revision
        now = time.monotonic()
        for key in [key for key, entry in self._entries.items() if entry.expires_at <= now]:
            del self._entries[key]

    def issue(
        self,
        username: str,
        fingerprint: str,
        url: str,
        revision: tuple[int, int, int, int, int],
    ) -> str:
        with self._lock:
            self._sync(revision)
            if len(self._entries) >= MAX_TOKENS:
                raise TokenCapacityError
            token = secrets.token_urlsafe(32)
            self._entries[hashlib.sha256(token.encode("ascii")).digest()] = AudioToken(
                username, fingerprint, url, time.monotonic() + TOKEN_LIFETIME
            )
            return token

    def get(
        self, token: str, revision: tuple[int, int, int, int, int]
    ) -> AudioToken | None:
        with self._lock:
            self._sync(revision)
            if not _TOKEN.fullmatch(token):
                return None
            return self._entries.get(hashlib.sha256(token.encode("ascii")).digest())

    def discard(self, token: str) -> None:
        if not _TOKEN.fullmatch(token):
            return
        with self._lock:
            self._entries.pop(hashlib.sha256(token.encode("ascii")).digest(), None)


def validate_url(url: str) -> tuple[str, str]:
    if not url or len(url) > MAX_URL_LENGTH or any(
        ord(char) < 33 or ord(char) > 126 or char == "\\" for char in url
    ):
        raise InvalidAudioURL
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if (
            parsed.scheme != "https"
            or hostname is None
            or not _HOST.fullmatch(hostname)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.netloc.lower() not in {hostname, f"{hostname}:443"}
            or "#" in url
        ):
            raise InvalidAudioURL
    except ValueError:
        raise InvalidAudioURL from None
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return hostname, target


def public_addresses(hostname: str) -> list[tuple]:
    try:
        results = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise AudioProxyError("Could not resolve the audio host.") from None
    addresses = []
    for family, socktype, proto, _, sockaddr in results:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            raise InvalidAudioURL from None
        if (
            not address.is_global
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise InvalidAudioURL
        addresses.append((family, socktype, proto, sockaddr))
    if not addresses:
        raise AudioProxyError("Could not resolve the audio host.")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: tuple):
        self._tls_context = ssl.create_default_context()
        super().__init__(hostname, 443, timeout=UPSTREAM_TIMEOUT, context=self._tls_context)
        self._address = address

    def connect(self) -> None:
        family, socktype, proto, sockaddr = self._address
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(self.timeout)
            sock.connect(sockaddr)
            self.sock = self._tls_context.wrap_socket(sock, server_hostname=self.host)
            sock = None
        finally:
            if sock is not None:
                sock.close()


def open_upstream(url: str, range_header: str | None):
    hostname, target = validate_url(url)
    addresses = public_addresses(hostname)
    headers = {"Accept-Encoding": "identity"}
    if range_header:
        headers["Range"] = range_header
    for address in addresses:
        connection = None
        try:
            connection = _PinnedHTTPSConnection(hostname, address)
            connection.request("GET", target, headers=headers)
            upstream = connection.getresponse()
        except (OSError, http.client.HTTPException, ValueError):
            if connection is not None:
                connection.close()
            continue
        return connection, upstream
    raise AudioProxyError("Could not connect to the audio host.") from None
