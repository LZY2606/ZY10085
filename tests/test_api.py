"""End-to-end API test against a real HTTP server."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from diagforge.server import Api, Handler, make_demo_session
from diagforge.store import Store


@pytest.fixture
def server(tmp_path):
    store = Store(str(tmp_path / "api.db"))
    handler = type("H", (Handler,), {"api": Api(store)})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % port, store
    httpd.shutdown()
    httpd.server_close()


def req(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read())


def test_index_page(server):
    base, _ = server
    with urllib.request.urlopen(base + "/") as resp:
        html = resp.read().decode()
    assert "diagforge" in html


def test_demo_session_flow(server):
    base, store = server
    sid = req(base, "/api/demo", {})["id"]
    status = req(base, "/api/sessions/%s/control" % sid,
                 {"action": "step", "steps": 400})
    assert status["status"] == "done"
    assert status["best"]["nfiles"] == 1
    assert status["runs"] > 0

    tree = req(base, "/api/sessions/%s/tree" % sid)
    assert len(tree) == status["candidates"]
    assert tree[0]["parent"] is None

    pins = req(base, "/api/sessions/%s/pins" % sid,
               {"kind": "keep_text",
                "payload": {"file": "main.c", "text": "TRIGGER"}})
    assert pins["pins"][0]["kind"] == "keep_text"

    events = req(base, "/api/sessions/%s/events" % sid)
    kinds = [e["kind"] for e in events]
    assert "session_created" in kinds and "root_evaluated" in kinds

    exported = req(base, "/api/sessions/%s/export" % sid, {})
    assert exported["chain"]


def test_unknown_session_404(server):
    base, _ = server
    with pytest.raises(Exception):
        req(base, "/api/sessions/sess_nope")
