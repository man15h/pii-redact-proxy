"""Machinery for recording and replaying golden fixtures of the proxy's wire behaviour.

A redacting proxy has exactly two observable surfaces, and both matter:

  * what it sent **upstream** — proves the secret was replaced on the way out
  * what it returned to the **client** — proves the placeholder was restored

A fixture captures both for one request. A refactor is faithful when every
fixture replays byte for byte.

`redactor` and `session` are stubbed with a one-line substitution. That is
deliberate and it bounds what these fixtures prove: they characterize the
*apps* — message traversal, header allowlist, fail-closed catch-all, SSE
carry and tool-call buffering — not the recognizers. Presidio and spaCy stay
out, so the harness runs on a venv of fastapi + httpx, and the recognizers keep
their own coverage.

The sentinel is deliberately `<ZZTESTREF_1>` and not a realistic placeholder
like an internal hostname: agents editing these files run *behind* pii-proxy,
so a literal matching a live mapping gets de-redacted on the way in and
re-redacted on the way out, and the fixture stops saying what it appears to.
"""
import http.server
import json
import os
import pathlib
import socket
import sys
import threading
import types

SECRET = "zz-test-host.invalid"
PLACEHOLDER = "<ZZTESTREF_1>"

# Response headers that change every run and would churn every golden.
VOLATILE_HEADERS = {"date", "server", "content-length", "transfer-encoding"}


# Where the redactor and session modules live, in each tree the harness drives.
# A flat tree has them next to the apps; this repo has them under the core
# package. Registering the same fake under both names is what lets one fixture
# set verify both trees, so a golden never has to be re-recorded just because a
# file moved.
_REDACTOR_NAMES = ("redactor", "pii_redact_proxy.core.redactor")
_SESSION_NAMES = ("session", "pii_redact_proxy.core.session")


def stub_modules():
    """Install fake `redactor` and `session` before any app module imports them."""
    fake_redactor = types.ModuleType("redactor")
    fake_redactor.redact = lambda t: t.replace(SECRET, PLACEHOLDER)
    fake_redactor.de_redact = lambda t: t.replace(PLACEHOLDER, SECRET)
    for name in _REDACTOR_NAMES:
        sys.modules[name] = fake_redactor

    fake_session = types.ModuleType("session")

    class _Mapper:
        def get_mappings_snapshot(self):
            return {SECRET: PLACEHOLDER}

        def clear(self):
            pass

    fake_session.mapper = _Mapper()
    for name in _SESSION_NAMES:
        sys.modules[name] = fake_session


class FakeUpstream:
    """Records the request the proxy forwarded, and replies with a scripted body.

    `script` is set per case before the proxy is driven: either a dict (sent as
    JSON) or a list of dicts (sent as SSE `data:` lines followed by `[DONE]`).
    """

    def __init__(self):
        self.seen = {}
        self.script = {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _record(self):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = raw.decode("utf-8", "replace")
                outer.seen = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()
                                if k.lower() not in ("host", "content-length",
                                                     "connection", "accept-encoding")},
                    "body": body,
                }

            def _reply(self):
                script = outer.script
                if isinstance(script, list):
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    for chunk in script:
                        # A chunk is either a bare payload (`data:` only, the
                        # OpenAI shape) or a (event_name, payload) pair —
                        # Anthropic's stream carries an `event:` header and the
                        # code under test dispatches on it.
                        if isinstance(chunk, (tuple, list)):
                            name, payload = chunk
                            line = f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()
                        else:
                            line = f"data: {json.dumps(chunk)}\n\n".encode()
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                    tail = b"data: [DONE]\n\n"
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(tail), tail))
                    self.wfile.write(b"0\r\n\r\n")
                    return
                out = json.dumps(script).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                self._record()
                self._reply()

            def do_GET(self):
                self._record()
                self._reply()

            def log_message(self, *a):
                pass

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self._srv.shutdown()


def load_app(app_path, case, upstream_url):
    """Return the FastAPI app for one case, from whichever tree shape we are given.

    Two shapes:

    * **flat** — the apps sit side by side in one directory, each hardcoding
      its upstream in a module constant and building a module-level
      `http_client` from it. There is no environment variable to set, so the
      client is rebound after import.
    * **package** — `build_app(provider)` assembles the real thing, and
      `UPSTREAM_BASE_URL` in the environment redirects it. No monkeypatching:
      the fixtures drive the same assembly the container does.

    Same fixtures either way. A golden should never move because a file did.
    """
    import httpx

    root = pathlib.Path(app_path).resolve()
    stub_modules()
    sys.path.insert(0, str(root))
    os.environ.setdefault("LOG_LEVEL", "WARNING")

    flat = root / f"{case['module']}.py"
    if flat.exists():
        mod = __import__(case["module"])
        mod.http_client = httpx.AsyncClient(
            base_url=upstream_url,
            timeout=httpx.Timeout(connect=5, read=30, write=10, pool=5),
        )
        return mod.app

    os.environ["UPSTREAM_BASE_URL"] = upstream_url
    from pii_redact_proxy import build_app
    return build_app(case["provider"])


def _clean_response_headers(headers):
    return {k.lower(): v for k, v in headers.items()
            if k.lower() not in VOLATILE_HEADERS}


def run_case(app_path, case):
    """Drive one case through its app and return the recording."""
    from fastapi.testclient import TestClient

    upstream = FakeUpstream()
    try:
        app = load_app(app_path, case, upstream.url)
        upstream.script = case.get("upstream_response", {})
        client = TestClient(app)
        req = case["request"]

        if case.get("stream"):
            with client.stream(req.get("method", "POST"), req["path"],
                               json=req.get("body"),
                               headers=req.get("headers", {})) as r:
                text = "".join(r.iter_text())
                response = {"status": r.status_code,
                            "headers": _clean_response_headers(r.headers),
                            "sse": text}
        else:
            r = client.request(req.get("method", "POST"), req["path"],
                               json=req.get("body"),
                               headers=req.get("headers", {}))
            try:
                body = r.json()
            except ValueError:
                body = r.text
            response = {"status": r.status_code,
                        "headers": _clean_response_headers(r.headers),
                        "body": body}

        return {
            "case": case["name"],
            "provider": case["provider"],
            "module": case["module"],
            "request": req,
            "upstream": upstream.seen,
            "response": response,
        }
    finally:
        upstream.stop()


def fixture_path(root, case_name):
    return pathlib.Path(root) / (case_name.replace("/", "__") + ".json")


def write_fixture(root, recording):
    p = fixture_path(root, recording["case"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(recording, indent=2, sort_keys=True) + "\n")
    return p


def read_fixture(root, case_name):
    return json.loads(fixture_path(root, case_name).read_text())


def diff(expected, actual, path=""):
    """Return a list of human-readable differences between two recordings."""
    out = []
    if type(expected) is not type(actual):
        return [f"{path or '<root>'}: {type(expected).__name__} -> {type(actual).__name__}"]
    if isinstance(expected, dict):
        for k in sorted(set(expected) | set(actual)):
            if k not in expected:
                out.append(f"{path}.{k}: added ({actual[k]!r})")
            elif k not in actual:
                out.append(f"{path}.{k}: removed (was {expected[k]!r})")
            else:
                out += diff(expected[k], actual[k], f"{path}.{k}")
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            out.append(f"{path}: length {len(expected)} -> {len(actual)}")
        for i, (e, a) in enumerate(zip(expected, actual)):
            out += diff(e, a, f"{path}[{i}]")
    elif expected != actual:
        out.append(f"{path}: {expected!r} -> {actual!r}")
    return out
