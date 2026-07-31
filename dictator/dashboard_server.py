"""Local HTTP server backing the embedded dashboard window.

A tiny ``ThreadingHTTPServer`` bound to ``127.0.0.1`` on an *ephemeral* port. It
serves the packaged ``web/dashboard.html`` (with a per-launch random token
substituted in) and a small JSON/media API the page calls. The WKWebView window
(``dictator.dashboard``) loads ``server.url``.

Security model: the server is loopback-only, and a random ``secrets``-derived
token gates every ``/api/*`` request. The served HTML embeds that token and
sends it as the ``X-Dictator-Token`` header on each call; the ``<audio>`` element
and the download anchor cannot set headers, so those two GET endpoints also
accept the token as a ``?t=`` query parameter. Any request without the right
token gets a 403 — so another local process or page can't read the user's
transcripts or voice clips.

The server is decoupled from the app internals via a ``providers`` dict of
callables (see ``DashboardServer.__init__``). It owns only the HTTP plumbing,
token gating, Range serving of ``store.audio_path(id)``, and shaping
``/api/data`` from ``providers["stats"]() + providers["get_settings"]()`` plus
``store.recent(200)`` grouped by calendar day. Nothing here imports AppKit, so
it can run (and be tested) headless.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

_TOKEN_PLACEHOLDER = "__DICTATOR_TOKEN__"


def _lang_label(language) -> str:
    code = (language or "").strip()
    return code.upper() if code else "—"


def _mode_label(mode) -> str:
    return "Live" if str(mode or "").lower() == "live" else "Batch"


def _group_label(d: date, today: date) -> str:
    delta = (today - d).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    return f"{d:%b} {d.day}"


def _date_label(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


class DashboardServer:
    """Serve the dashboard HTML + JSON/media API on 127.0.0.1:<ephemeral>.

    ``providers`` supplies the app-backed callables:
      ``stats()`` -> {"words","wpm","streak","dictations"}
      ``regenerate(entry_id, lang)`` -> str
      ``copy(entry_id)`` -> None
      ``get_settings()`` -> {"mode","language","swallow_ptt"}
      ``set_setting(key, value)`` -> None
    """

    def __init__(self, store, providers: dict, html_path):
        self.store = store
        self.providers = providers or {}
        self.html_path = Path(html_path)
        self.token = secrets.token_urlsafe(32)
        self.host = "127.0.0.1"
        self.port: int | None = None
        self.url: str | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._html_cache: str | None = None
        self._lock = threading.Lock()

    # --- lifecycle ---
    def start(self) -> str:
        """Bind + serve on a daemon thread (idempotent). Returns the URL."""
        with self._lock:
            if self._httpd is not None:
                return self.url
            handler = _make_handler(self)
            httpd = ThreadingHTTPServer((self.host, 0), handler)
            httpd.daemon_threads = True
            self._httpd = httpd
            self.port = httpd.server_address[1]
            self.url = f"http://{self.host}:{self.port}/"
            self._thread = threading.Thread(
                target=httpd.serve_forever, name="dictator-dashboard",
                daemon=True)
            self._thread.start()
            return self.url

    def stop(self) -> None:
        with self._lock:
            if self._httpd is not None:
                try:
                    self._httpd.shutdown()
                    self._httpd.server_close()
                except Exception:
                    pass
                self._httpd = None

    # --- HTML ---
    def html(self) -> bytes:
        """The dashboard HTML with the live token substituted (cached)."""
        if self._html_cache is None:
            raw = self.html_path.read_text(encoding="utf-8")
            self._html_cache = raw.replace(_TOKEN_PLACEHOLDER, self.token)
        return self._html_cache.encode("utf-8")

    # --- /api/data assembly ---
    def data(self) -> dict:
        stats = {}
        settings = {}
        try:
            stats = dict(self.providers["stats"]() or {})
        except Exception:
            stats = {"words": 0, "wpm": 0, "streak": 0, "dictations": 0}
        try:
            settings = dict(self.providers["get_settings"]() or {})
        except Exception:
            settings = {"mode": "batch", "language": None, "swallow_ptt": True}
        try:
            entries = self.store.recent(200) if self.store is not None else []
        except Exception:
            entries = []
        return {
            # Change token: the page records this on load and polls /api/version
            # to detect new dictations/regenerates and refresh live.
            "version": getattr(self.store, "version", 0),
            "stats": {
                "words": int(stats.get("words", 0) or 0),
                "wpm": float(stats.get("wpm", 0) or 0),
                "streak": int(stats.get("streak", 0) or 0),
                "dictations": int(stats.get("dictations", 0) or 0),
            },
            "settings": {
                "mode": "live" if str(settings.get("mode", "batch")).lower()
                == "live" else "batch",
                "language": settings.get("language"),
                "swallow_ptt": bool(settings.get("swallow_ptt", True)),
            },
            "groups": self._group(entries),
        }

    def _item(self, entry: dict) -> dict:
        ts = entry.get("ts", "") or ""
        try:
            dt = datetime.fromisoformat(ts)
            time_s = f"{dt:%H:%M}"
        except Exception:
            time_s = ts[11:16] if len(ts) >= 16 else ""
        text = entry.get("text", "") or ""
        return {
            "id": entry.get("id", ""),
            "time": time_s,
            "dur": float(entry.get("duration_s", 0) or 0),
            "mode": _mode_label(entry.get("mode")),
            "lang": _lang_label(entry.get("language")),
            "app": entry.get("app") or "—",
            "status": "ok" if text.strip() else "empty",
            "text": text,
            "has_audio": bool(entry.get("audio_file")),
        }

    def _group(self, entries: list) -> list:
        """Group newest-first entries by calendar day, preserving order."""
        today = date.today()
        groups: list[dict] = []
        index: dict[date, dict] = {}
        for e in entries:
            ts = e.get("ts", "") or ""
            try:
                d = datetime.fromisoformat(ts).date()
            except Exception:
                d = today
            grp = index.get(d)
            if grp is None:
                grp = {
                    "group": _group_label(d, today),
                    "date": _date_label(d),
                    "items": [],
                }
                index[d] = grp
                groups.append(grp)
            grp["items"].append(self._item(e))
        return groups

    # --- provider passthroughs (each guarded by the handler) ---
    def regenerate(self, entry_id: str, lang: str) -> str:
        return self.providers["regenerate"](entry_id, lang) or ""

    def copy(self, entry_id: str) -> None:
        self.providers["copy"](entry_id)

    def set_setting(self, key, value) -> None:
        self.providers["set_setting"](key, value)


def _make_handler(server: DashboardServer):
    class Handler(BaseHTTPRequestHandler):
        # Silence per-request stderr logging (rumps/menu-bar has no console).
        def log_message(self, *args):  # noqa: D401
            pass

        # --- token gate ---
        def _authed(self, query: dict) -> bool:
            # compare_digest raises on non-ASCII; our token is url-safe ASCII, so
            # a non-ASCII candidate is simply a failed auth (clean 403, not a 500).
            hdr = self.headers.get("X-Dictator-Token", "")
            if hdr and hdr.isascii() and secrets.compare_digest(hdr, server.token):
                return True
            qt = (query.get("t") or [""])[0]
            return bool(qt) and qt.isascii() and secrets.compare_digest(qt, server.token)

        # --- response helpers ---
        def _send_json(self, obj, status: int = 200) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_status(self, status: int, message: str = "") -> None:
            self._send_json({"error": message or ""}, status=status)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception:
                raise ValueError("bad json")
            if not isinstance(obj, dict):
                raise ValueError("bad json")
            return obj

        # --- GET ---
        def do_GET(self):  # noqa: N802
            parts = urlsplit(self.path)
            path = parts.path
            query = parse_qs(parts.query)
            if path == "/" or path == "/index.html":
                self._serve_html()
                return
            if path == "/api/data":
                if not self._authed(query):
                    self._send_status(403, "forbidden")
                    return
                try:
                    self._send_json(server.data())
                except Exception as e:
                    self._send_status(500, str(e))
                return
            if path == "/api/version":
                # Cheap change token the page polls: the store bumps .version on
                # every new dictation and regenerate, so the UI can refresh live.
                if not self._authed(query):
                    self._send_status(403, "forbidden")
                    return
                self._send_json({"version": getattr(server.store, "version", 0)})
                return
            if path.startswith("/api/audio/"):
                if not self._authed(query):
                    self._send_status(403, "forbidden")
                    return
                self._serve_audio(unquote(path[len("/api/audio/"):]),
                                  download=False)
                return
            if path.startswith("/api/download/"):
                if not self._authed(query):
                    self._send_status(403, "forbidden")
                    return
                self._serve_audio(unquote(path[len("/api/download/"):]),
                                  download=True)
                return
            self._send_status(404, "not found")

        # --- POST ---
        def do_POST(self):  # noqa: N802
            parts = urlsplit(self.path)
            path = parts.path
            query = parse_qs(parts.query)
            if not self._authed(query):
                self._send_status(403, "forbidden")
                return
            try:
                body = self._read_body()
            except ValueError:
                self._send_status(400, "bad request")
                return

            if path == "/api/regenerate":
                entry_id = body.get("id")
                lang = body.get("lang", "")
                if not entry_id or not isinstance(entry_id, str):
                    self._send_status(400, "missing id")
                    return
                if server.store is not None and \
                        server.store.audio_path(entry_id) is None:
                    self._send_status(404, "no clip")
                    return
                try:
                    text = server.regenerate(entry_id, lang or "")
                    self._send_json({"text": text})
                except Exception as e:
                    self._send_status(500, str(e))
                return

            if path == "/api/copy":
                entry_id = body.get("id")
                if not entry_id or not isinstance(entry_id, str):
                    self._send_status(400, "missing id")
                    return
                try:
                    server.copy(entry_id)
                    self._send_json({"ok": True})
                except KeyError:
                    self._send_status(404, "not found")
                except Exception as e:
                    self._send_status(500, str(e))
                return

            if path == "/api/setting":
                key = body.get("key")
                if not key or not isinstance(key, str):
                    self._send_status(400, "missing key")
                    return
                try:
                    server.set_setting(key, body.get("value"))
                    self._send_json({"ok": True})
                except Exception as e:
                    self._send_status(500, str(e))
                return

            self._send_status(404, "not found")

        # --- html ---
        def _serve_html(self):
            try:
                body = server.html()
            except Exception as e:
                self._send_status(500, str(e))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        # --- audio (Range-capable) ---
        def _serve_audio(self, entry_id: str, download: bool):
            path = None
            if server.store is not None and entry_id:
                try:
                    path = server.store.audio_path(entry_id)
                except Exception:
                    path = None
            if path is None or not path.exists():
                self._send_status(404, "no clip")
                return
            try:
                data = path.read_bytes()
            except Exception as e:
                self._send_status(500, str(e))
                return
            total = len(data)
            headers = [("Content-Type", "audio/wav"),
                       ("Accept-Ranges", "bytes"),
                       ("Cache-Control", "no-store")]
            if download:
                headers.append(
                    ("Content-Disposition",
                     f'attachment; filename="dictator-{entry_id}.wav"'))

            rng = self.headers.get("Range")
            start, end = self._parse_range(rng, total)
            if rng is not None and start is None:
                # Unsatisfiable range.
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return

            if start is None:
                self.send_response(200)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(total))
                self.end_headers()
                self.wfile.write(data)
                return

            chunk = data[start:end + 1]
            self.send_response(206)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)

        @staticmethod
        def _parse_range(rng, total):
            """Return (start, end) inclusive, or (None, None) if no/invalid Range.

            (None, None) with a present-but-unsatisfiable Range is signalled by
            the caller checking ``rng is not None and start is None``.
            """
            if not rng or not rng.startswith("bytes=") or total <= 0:
                return None, None
            spec = rng[len("bytes="):].split(",")[0].strip()
            if "-" not in spec:
                return None, None
            lo, hi = spec.split("-", 1)
            lo, hi = lo.strip(), hi.strip()
            try:
                if lo == "":
                    # suffix: last N bytes
                    n = int(hi)
                    if n <= 0:
                        return None, None
                    start = max(0, total - n)
                    return start, total - 1
                start = int(lo)
                if start >= total:
                    return None, None
                end = int(hi) if hi != "" else total - 1
                end = min(end, total - 1)
                if end < start:
                    return None, None
                return start, end
            except ValueError:
                return None, None

    return Handler
