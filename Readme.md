# BragiServer

BragiServer is a server-side Python service intended to support reliable
YouTube audio playback and, eventually, Chromecast playlist playback without
the phone issuing a command for every track.

## Status

The initial foundation is a Flask application served by Waitress, with a
Docker image and persistent data-directory mount for CasaOS. `GET /health`
is a health endpoint. A browser management UI supports accounts, playlists,
and API browser-origin settings.

The management UI has its own browser login. The playback-client API supports
playlist reading, creation, renaming, deletion and song management, temporary queue preparation, direct YouTube audio
resolution, and browser audio proxying. Chromecast control is not implemented.

The management UI uses plaintext passwords in `users.json`; do not expose
the app directly to the internet. Use HTTPS through a trusted reverse proxy
for remote access and set `BRAGI_COOKIE_SECURE` to `true`.

For building a separate playback client, see the [client guide](CLIENT_GUIDE.md).

## Development

Requires Python 3.10 or newer:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m bragi_server
```

The server listens on `0.0.0.0:8000`. Locally, its data directory defaults to
`instance`; set `BRAGI_DATA_DIR` to use another directory. On startup, the
server initializes `bragi_log.json` and `settings.json` in the data directory.

## Environment variables

Compose sets these variables for the container. Change them in the Compose
configuration (or CasaOS's corresponding environment settings):

| Variable | Default | Purpose |
| --- | --- | --- |
| `BRAGI_DATA_DIR` | `/var/lib/bragi-server` in Docker; `instance` locally | Data directory |
| `BRAGI_COOKIE_SECURE` | `false` | Require HTTPS for the management session cookie; set to `true` behind HTTPS |
| `BRAGI_LOG_LEVEL` | `INFO` | BragiServer and Flask application logs |
| `WAITRESS_LOG_LEVEL` | `INFO` | Waitress server logs, independently of the app log level |

Both log-level variables accept `DEBUG`, `INFO`, `WARNING`, `ERROR`, or
`CRITICAL`. An unsupported value stops application startup with an error.
`BRAGI_COOKIE_SECURE` accepts `true` or `false`.

Run the tests with:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## Docker and CasaOS

Build and start with Docker Compose:

```bash
docker compose up -d --build
```

The service listens on port `8000`. Compose bind-mounts the CasaOS host
directory `/DATA/AppData/bragi-server` at `/var/lib/bragi-server` inside the
container. Create the host directory before starting Compose. The host files
remain available if the container is rebuilt or removed.

The project `instance` folder is not automatically copied to the CasaOS host
directory. If you want to use those files, copy them to
`/DATA/AppData/bragi-server` on the CasaOS host and change any default
account passwords before making the service accessible. Ensure the directory
is writable by the container's `bragi` user.

## Management UI and data files

Open the server address in a browser and sign in using `users.json` from its
data directory. Create that file manually before first login; the server does
not create default accounts. For example, use the existing project `instance`
files for local development or copy them into the CasaOS host data directory.
Change any default passwords before sharing access.

The `admin` username is the administrator and can create and delete users and
manage any user's playlists and API settings. Other users manage only their own playlists.
Deleting a user also removes their playlists; the `admin` account cannot be
deleted in the UI. New usernames can contain letters, digits, `.`, `_`, and
`-` (up to 64 characters). Password changes are not implemented in the UI;
edit `users.json` directly when needed.

The server uses its own JSON files in Audify-compatible shapes:

- `users.json`: an object with a `users` array; each entry has `username` and
  `password` strings.
- `user_playlists.json`: an object keyed by username. Each value is an array
  of playlists with `id`, `name`, and ordered `videos`; each video has `name`
  and `link` strings.
- `settings.json`: an object containing `api_allowed_origins`, a list of exact
  allowed browser origins. The server creates it with an empty list if absent.

`user_playlists.json` is created when a playlist is first saved. A missing
or malformed `users.json`, or malformed playlist or settings file, is reported instead of
silently replaced. The server also creates `session.key` in the data
directory to sign management sessions; keep it private and persistent. A
password change or user deletion invalidates that user's existing session.
The requested plaintext-password approach means anyone who can read the
host directory can read the passwords. HTTPS protects credentials in transit
but not the files on disk. Back up the data directory. JSON writes are
serialized within one server process; do not run multiple server instances
against the same data files. User deletion updates two files: a failure is
reported and the account update is rolled back where possible, but a crash
between writes can require restoring from backup.

## Playback-client API

The API uses HTTP Basic authentication with the username and password in
`users.json`, except for token-authenticated audio-stream GETs. It does not
use the management session cookie. HTTP is available for local testing.
Use HTTPS in production, including on a LAN: Basic authentication does not
encrypt credentials.
Do not expose the service directly to the internet; the API does not provide
rate limiting. A deleted user or changed password takes effect on the next
request. Each account, including `admin`, can manage only its own playlists.

All responses use `Cache-Control: no-store`. Endpoints return JSON except the
audio stream:

- `GET /api/v1/playlists` returns `{"playlists": [...]}`, including ordered
  `videos` with their saved `name`, YouTube `link`, and `thumbnail_url`.
- `POST /api/v1/playlists` accepts `{"name": "New playlist"}` and returns 201
  with `{"playlist": {"id": "...", "name": "New playlist", "videos": []}}`.
  Names are trimmed and must contain 1–200 characters; invalid input returns 400.
- `GET /api/v1/playlists/<id>` returns `{"playlist": {...}}`, or 404 if the
  playlist is not owned by the authenticated user or does not exist.
- `PATCH /api/v1/playlists/<id>` accepts `{"name": "New name"}` and returns
  the renamed playlist. Names are trimmed and must contain 1–200 characters.
  Tracks are preserved. Invalid names return 400; unavailable playlists return 404.
- `DELETE /api/v1/playlists/<id>` deletes an owned playlist and returns 204,
  or 404 if unavailable. Existing queue snapshots and playback are unchanged.
- `POST /api/v1/playlists/<id>/tracks` accepts a YouTube video
  `{"url": "https://youtu.be/<video-id>"}`, retrieves its title from YouTube,
  and returns 201 with the updated `{"playlist": {...}}`. It appends the
  track, including duplicates; existing queues remain unchanged. Invalid URLs
  return 400, unavailable playlists 404, a playlist already containing
  5,000 tracks 409, and a failed title lookup 422.
- `DELETE /api/v1/playlists/<id>/tracks` accepts a zero-based `index` and
  the last-seen ordered `videos` list containing only each track's `name`
  and `link`. It returns 200 with the updated playlist. If the tracks changed,
  it returns 409 instead of deleting a potentially different song. Existing
  queue snapshots and playback are unchanged.
- `PATCH /api/v1/playlists/<id>/tracks` accepts the same `index` and `videos`
  snapshot plus a replacement YouTube `url`. It retrieves the new title and
  returns the updated playlist. Invalid input returns 400, unavailable
  playlists 404, changed tracks 409, and failed title lookup 422. Existing
  queue snapshots and playback are unchanged.
- `POST /api/v1/queue` accepts `{"playlist_id": "id", "shuffle": true}` and
  replaces the user's active queue with a snapshot of that playlist. `shuffle`
  is optional and defaults to `false`. Returns 201 with `{"queue": {...}}`;
  the queue contains `id`, `playlist_id`, `name`, `shuffled`, and `videos`.
  Invalid input returns 400, an unavailable playlist returns 404, and an
  empty playlist returns 422. Failed requests do not replace the active queue.
- `GET /api/v1/queue` returns the active queue or 404 if none exists.
- `DELETE /api/v1/queue` clears the active queue and returns 204, including
  when there is no queue. This does not stop or change Chromecast playback.
- `POST /api/v1/resolve` accepts `{"url": "https://youtu.be/<video-id>"}` and
  returns `{"audio": {"url": "...", "title": "...", "content_type": "..."}}`.
  Invalid input returns 400; a resolution failure returns 422.
- `POST /api/v1/audio-proxy` accepts `{"url": "<resolved audio URL>"}` with
  Basic authentication and returns `{"proxy_url": "/api/v1/audio-stream?token=...",
  "expires_in": 21600}`. The relative URL carries a bearer token; its GET
  requires no Basic header and supports repeated requests and byte ranges.
  Only public HTTPS Googlevideo destinations are accepted; redirects are not
  followed. Invalid URLs return 400, upstream failures 502, and a full token
  store 503.

Playlist and queue responses derive each `thumbnail_url` from its YouTube
video ID without changing the saved playlist or contacting YouTube. The value
is `null` when the link has no valid video ID. The client fetches the image
from YouTube and should show a placeholder if it is unavailable.

Invalid or missing Basic credentials return 401 with a Basic authentication
challenge; invalid stream tokens return 401 without that challenge. Storage
failures return 500. For example, over HTTPS:

```bash
curl --user 'USERNAME' https://bragi.example/api/v1/playlists
```

Curl prompts for the password. Do not put credentials in command history or
logs. The resolved audio URL is temporary and may expire or fail on a
Chromecast; Chromecast continues to use that direct URL without proxying or
refresh. Do not log or publicly share it.
This API does not yet prepare Cast queues or manage playback.

Audio proxy tokens are held in memory for up to six hours, are reusable for
seeks, and are lost on restart. Any change to `users.json` invalidates all
tokens, not just those of the changed account. The source URL may expire
sooner; clients must resolve and request a new proxy URL when necessary.
Treat proxy URLs as credentials: do not log or share them. Configure any
reverse proxy, access logger, monitoring, and analytics to omit the query
string (or disable access logging for `/api/v1/audio-stream`); a full request
URL contains the bearer token. Use HTTPS in production to protect it in transit.

Queue order is a snapshot: shuffling never changes the saved playlist, and
later playlist edits do not change an existing queue. Each user has one queue;
starting another replaces it, so multiple clients for that user share it.
Queues are held only in server memory and disappear on restart or user
deletion. They contain YouTube links, not resolved audio URLs. The client must
resolve tracks and load and control the Chromecast queue. The client also
tracks playback status; the server does not know which track is playing.
Replacing or clearing the server queue does not change a queue already loaded
on a receiver. Long queues of temporary resolved URLs may expire before
playback reaches later tracks.

Cross-origin browser access is disabled by default. Sign in as `admin` and
open **Settings** to add exact origins, such as `https://player.example.com`
or `https://other.example.com:8443`. Do not include paths or a trailing slash.
Changes apply immediately and are saved in the persistent `settings.json`.
Allow only trusted applications: allowed origins can make authenticated
requests. Same-origin browsers and native clients do not require this setting.
The former `BRAGI_API_ALLOWED_ORIGINS` environment variable is no longer used;
re-enter any previously configured origins in Settings after upgrading.

## Event log

`bragi_log.json` is a JSON object with an `events` array. Events are grouped
by their `category`: `critical_warning`, `resolve_error`, or `exception`.
Each event includes a UTC timestamp, severity, and message. Optional context
can identify the affected operation; exception events also include the
exception type and traceback. The log retains the latest 1,000 events.

Application code can record an event through the Flask app extension:

```python
current_app.extensions["bragi_event_log"].record(
    "resolve_error",
    "Could not resolve audio",
    context={"video_id": video_id},
)
```

Use the `exception` category with the caught exception instance for unexpected
failures. Do not include passwords, authentication tokens, or signed source
URLs in event messages or context. Invalid or unreadable log data is reported
as an application startup error rather than silently overwritten.
