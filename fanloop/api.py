"""The loop's HTTP API (spec 5.6): reads for everyone, token-protected writes over
TCP for Hermes, and a local Unix socket for fanctl.

This is the only way in from the lab network, so it stays small and checks every
input strictly: exact keys, real numbers, known levels, bounded bodies."""
import hmac
import http.client
import http.server
import json
import os
import shutil
import socket
import socketserver
import stat
import threading
import time
import traceback
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from fanloop.levels import level_from_name
from fanloop.policy import WriteRequest, WriteResult
from fanloop.thresholds import PARAMS, AmbientRule, Change

MAX_BODY = 16384
_DRAIN_LIMIT = 1 << 20
API_ERROR_EVERY_S = 60.0
_ERROR_LOG_LOCK = threading.Lock()
_SCALAR_PARAMS = {"step_up.low", "step_up.medium", "step_up.high", "margin", "hold_s"}


class ApiError(Exception):
    def __init__(self, status: int, error: str, detail: str = ""):
        super().__init__(detail)
        self.status, self.error, self.detail = status, error, detail


def _bad(detail: str) -> ApiError:
    return ApiError(400, "bad-request", detail)


# -- input checks ----------------------------------------------------------------------

def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _number(x: Any, name: str) -> float:
    if not _is_number(x):
        raise _bad(f"{name} must be a number")
    return x


def _int(x: Any, name: str) -> int:
    if not isinstance(x, int) or isinstance(x, bool):
        raise _bad(f"{name} must be a whole number")
    return x


def _object(body: Any, required: Sequence[str], optional: Sequence[str] = ()) -> Dict:
    if not isinstance(body, dict):
        raise _bad("the body must be a JSON object")
    keys = set(body)
    missing = set(required) - keys
    unknown = keys - set(required) - set(optional)
    if missing or unknown:
        raise _bad(f"expected keys {sorted(required)}" + (f" (optional {sorted(optional)})" if optional else "")
                   + f", got {sorted(keys)}")
    return body


def _rule(x: Any) -> AmbientRule:
    d = _object(x, ("inlet_min", "level"))
    if not isinstance(d["level"], str):
        raise _bad("an ambient rule's level must be a level name")
    try:
        level = level_from_name(d["level"])
    except ValueError as e:
        raise _bad(str(e)) from None
    return AmbientRule(_number(d["inlet_min"], "inlet_min"), level)


def _change(x: Any) -> Change:
    d = _object(x, ("param", "from", "to"))
    param = d["param"]
    if not isinstance(param, str) or len(param) > 64:
        raise _bad("param must be a parameter name")
    if param in _SCALAR_PARAMS:
        return Change(param, _number(d["from"], "from"), _number(d["to"], "to"))
    if param == "ambient.add":
        if d["from"] is not None:
            raise _bad("ambient.add: from must be null")
        return Change(param, None, _rule(d["to"]))
    if param == "ambient.remove":
        if d["to"] is not None:
            raise _bad("ambient.remove: to must be null")
        return Change(param, _rule(d["from"]), None)
    if param == "ambient.replace":
        return Change(param, _rule(d["from"]), _rule(d["to"]))
    # Not a writable parameter: the policy rejects it with `unknown-param`.
    if not all(v is None or _is_number(v) for v in (d["from"], d["to"])):
        raise _bad("from and to must be numbers")
    return Change(param, d["from"], d["to"])


def _changes(x: Any) -> List[Change]:
    if not isinstance(x, list) or len(x) > len(PARAMS) * 4:
        raise _bad("changes must be a short list")
    return [_change(c) for c in x]


def _reason(x: Any) -> str:
    if not isinstance(x, str) or len(x) > 1000:
        raise _bad("reason must be text of at most 1000 characters")
    return x


def change_to_json(c: Change) -> Dict:
    def value(v):
        return v.to_dict() if isinstance(v, AmbientRule) else v
    return {"param": c.param, "from": value(c.from_), "to": value(c.to)}


def _result(r: WriteResult) -> Tuple[int, Dict]:
    body = {"ok": r.ok, "error": r.error, "detail": r.detail, "old_version": r.old_version,
            "new_version": r.new_version, "thresholds": r.new.to_dict() if r.new else r.old.to_dict()}
    return (200 if r.ok else 409), body


# -- query strings ------------------------------------------------------------------------

_INT_LIMIT = 2 ** 62  # SQLite integers are 64-bit; Python's aren't


def _q(query: Dict[str, List[str]], name: str, default, convert: Callable):
    if name not in query:
        return default
    try:
        value = convert(query[name][-1])
    except (ValueError, OverflowError):
        raise _bad(f"{name} must be a number") from None
    if isinstance(value, int) and not -_INT_LIMIT < value < _INT_LIMIT:
        raise _bad(f"{name} is out of range")
    return value


# -- routes -----------------------------------------------------------------------------------
# Each returns (status, payload). `local` routes exist only on the Unix socket,
# `tcp` routes only on the network side.

def r_status(h, q, body, now):
    return 200, h.service.status(now)


def r_history(h, q, body, now):
    end = _q(q, "end", now, float)
    start = _q(q, "start", end - 86400, float)
    return 200, {"samples": h.store.samples(start, end, max_points=300)}


def r_events(h, q, body, now):
    since = _q(q, "since", now - 86400, float)
    kinds = [k for k in q.get("kinds", [""])[-1].split(",") if k] or None
    return 200, {"events": h.store.events(since, kinds=kinds, limit=500)}


def r_episodes(h, q, body, now):
    since = _q(q, "since", 0.0, float)
    version = _q(q, "version", None, int)
    return 200, {"episodes": [e.to_dict() for e in h.store.episodes(since_ts=since, version=version, limit=200)]}


def r_changes(h, q, body, now):
    return 200, {"versions": h.store.versions(since_version=_q(q, "since_version", 0, int),
                                             since_ts=_q(q, "since", 0.0, float))}


def r_summary(h, q, body, now):
    until = _q(q, "until", now, float)
    return 200, h.service.summary(_q(q, "since", until - 86400, float), until)


def r_diff(h, q, body, now):
    version = _q(q, "version", None, int)
    if version is None:
        raise _bad("version is required")
    try:
        return 200, {"changes": [change_to_json(c) for c in h.service.diff(version)]}
    except KeyError:
        raise ApiError(404, "unknown-version", f"there is no v{version}") from None


def r_thresholds(h, q, body, now):
    if h.server.is_local:
        d = _object(body, ("reason", "changes"))
        source = "user-cli"
    else:
        d = _object(body, ("source", "reason", "changes"))
        source = d["source"]
        if source not in ("review", "user-chat"):
            raise _bad("source must be review or user-chat")
    req = WriteRequest(source, _reason(d["reason"]), _changes(d["changes"]))
    return _result(h.service.write(req, now))


def r_mark_start(h, q, body, now):
    if body not in (None, {}):
        _object(body, ())
    return 200, {"id": h.service.mark_start(now)}


def r_mark_end(h, q, body, now):
    d = _object(body, ("id",))
    return 200, {"ended": h.service.mark_end(_int(d["id"], "id"), now)}


def r_revert(h, q, body, now):
    d = _object(body, ("version",))
    return _result(h.service.revert(_int(d["version"], "version"), now))


def r_hold(h, q, body, now):
    d = _object(body, ("level",), ("until",))
    if not isinstance(d["level"], str):
        raise _bad("level must be a level name")
    try:
        level = level_from_name(d["level"])
    except ValueError as e:
        raise _bad(str(e)) from None
    until = d.get("until")
    if until is not None:
        until = _number(until, "until")
    try:
        hold = h.service.hold_set(level, until, now)
    except ValueError as e:
        raise _bad(str(e)) from None
    return 200, {"hold": {"level": hold.level.name.lower(), "since": hold.since, "until": hold.until}}


def r_hold_off(h, q, body, now):
    hold = h.service.hold_clear(now)
    return 200, {"ended": hold is not None}


def r_mute(h, q, body, now):
    d = _object(body, ("until",))
    until = _number(d["until"], "until")
    if until <= now:
        raise _bad("until must be in the future")
    h.service.mute(until, now)
    return 200, {"muted_until": until}


def r_mute_off(h, q, body, now):
    h.service.unmute(now)
    return 200, {"muted_until": None}


def r_test_alert(h, q, body, now):
    h.service.test_alert(now)
    return 200, {"sent": True}


BOTH, LOCAL, TCP = "both", "local", "tcp"
ROUTES: Dict[Tuple[str, str], Tuple[Callable, str]] = {
    ("GET", "/status"): (r_status, BOTH),
    ("GET", "/history"): (r_history, BOTH),
    ("GET", "/events"): (r_events, BOTH),
    ("GET", "/episodes"): (r_episodes, BOTH),
    ("GET", "/changes"): (r_changes, BOTH),
    ("GET", "/summary"): (r_summary, BOTH),
    ("GET", "/diff"): (r_diff, LOCAL),
    ("POST", "/thresholds"): (r_thresholds, BOTH),
    ("POST", "/marks/start"): (r_mark_start, TCP),
    ("POST", "/marks/end"): (r_mark_end, TCP),
    ("POST", "/revert"): (r_revert, LOCAL),
    ("POST", "/hold"): (r_hold, LOCAL),
    ("POST", "/hold/off"): (r_hold_off, LOCAL),
    ("POST", "/mute"): (r_mute, LOCAL),
    ("POST", "/mute/off"): (r_mute_off, LOCAL),
    ("POST", "/test-alert"): (r_test_alert, LOCAL),
}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "fanloop"
    sys_version = ""

    @property
    def service(self):
        return self.server.service

    @property
    def store(self):
        return self.server.store

    def setup(self):
        self.timeout = getattr(self.server, "request_timeout_s", REQUEST_TIMEOUT_S)  # idle connections end
        super().setup()

    def log_message(self, fmt, *args):  # the loop's events are its log
        pass

    def address_string(self):
        return "local" if getattr(self.server, "is_local", False) else super().address_string()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise _bad("bad Content-Length") from None
        if length < 0 or length > _DRAIN_LIMIT:
            self.close_connection = True
            raise _bad("body too large")
        raw = self.rfile.read(length) if length else b""
        if length > MAX_BODY:
            raise _bad(f"body larger than {MAX_BODY} bytes")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):  # RecursionError: nesting too deep
            raise _bad("the body is not valid JSON") from None

    def _log_internal_error(self, now: float, path: str, e: Exception) -> None:
        """At most one api-error event per API_ERROR_EVERY_S per server, with a count
        of the ones skipped, so a client repeating a failing request can't fill the disk."""
        srv = self.server
        with _ERROR_LOG_LOCK:
            last = getattr(srv, "_api_error_at", None)
            if last is not None and time.monotonic() - last < API_ERROR_EVERY_S:
                srv._api_errors_skipped = getattr(srv, "_api_errors_skipped", 0) + 1
                return
            srv._api_error_at = time.monotonic()
            skipped, srv._api_errors_skipped = getattr(srv, "_api_errors_skipped", 0), 0
        try:
            self.store.add_event(now, "api-error", {"path": path[:200], "error": repr(e)[:300],
                                                    "trace": traceback.format_exc()[-1500:],
                                                    "skipped_since_last": skipped})
        except Exception:
            pass

    def _authorized(self) -> bool:
        token = self.server.token or ""
        header = self.headers.get("Authorization", "")
        if not token or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[len("Bearer "):].encode(), token.encode())

    def _dispatch(self, method: str) -> None:
        now = time.time()
        url = urllib.parse.urlsplit(self.path)
        try:
            route = ROUTES.get((method, url.path))
            local = self.server.is_local
            # Route and token come first: an unauthenticated body is never read or parsed.
            # The connection then closes, so its unread body can't pose as the next request.
            if route is None or (route[1] == LOCAL and not local) or (route[1] == TCP and local):
                self.close_connection = True
                raise ApiError(404, "not-found", f"no route {method} {url.path}")
            if method == "POST" and not local and not self._authorized():
                self.close_connection = True
                raise ApiError(401, "unauthorized", "a valid write token is required")
            body = self._read_body() if method == "POST" else None
            status, payload = route[0](self, urllib.parse.parse_qs(url.query), body, now)
        except ApiError as e:
            status, payload = e.status, {"error": e.error, "detail": e.detail}
        except Exception as e:
            self._log_internal_error(now, url.path, e)
            status, payload = 500, {"error": "internal", "detail": "the loop logged this error"}
        try:
            self._send(status, payload)
        except OSError:
            pass  # the client went away


REQUEST_TIMEOUT_S = 10.0
MAX_CONNECTIONS = 16


class _Bounded:
    """At most max_connections handled at once; more are closed at once. Each one's
    socket times out after request_timeout_s idle. Together they keep a client that
    opens connections and sends nothing from using up the loop's file descriptors."""
    request_timeout_s = REQUEST_TIMEOUT_S
    max_connections = MAX_CONNECTIONS

    def process_request(self, request, client_address):
        if not hasattr(self, "_slots"):
            self._slots = threading.BoundedSemaphore(self.max_connections)
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class TcpServer(_Bounded, http.server.ThreadingHTTPServer):
    daemon_threads = True
    is_local = False


class UnixHTTPServer(_Bounded, socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    is_local = True

    def server_close(self):
        super().server_close()
        try:
            os.unlink(self.server_address)
        except OSError:
            pass


class UnixHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection over a Unix socket (used by fanctl and the tests)."""

    def __init__(self, socket_path: str, timeout: float = 10.0):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


def make_servers(service, store, host: str, port: int, socket_path: str, write_token: str,
                 socket_group: Optional[str], request_timeout_s: float = REQUEST_TIMEOUT_S,
                 max_connections: int = MAX_CONNECTIONS) -> Tuple[TcpServer, UnixHTTPServer]:
    if os.path.exists(socket_path) and stat.S_ISSOCK(os.stat(socket_path).st_mode):
        os.unlink(socket_path)  # left over from a previous run
    tcp = TcpServer((host, port), Handler)
    try:
        unix = UnixHTTPServer(socket_path, Handler)
        os.chmod(socket_path, 0o660)
        if socket_group:
            shutil.chown(socket_path, group=socket_group)
    except BaseException:
        tcp.server_close()
        raise
    for server in (tcp, unix):
        server.service, server.store, server.token = service, store, write_token
        server.request_timeout_s, server.max_connections = request_timeout_s, max_connections
    return tcp, unix


def serve(servers, poll_interval: float = 0.5) -> List[threading.Thread]:
    threads = []
    for server in servers:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": poll_interval},
                                  name=f"api-{'local' if server.is_local else 'tcp'}", daemon=True)
        thread.start()
        threads.append(thread)
    return threads
