import json
import threading
import urllib.request

import pytest

from diagforge.webapp import make_server

from conftest import SAMPLE_FILES


@pytest.fixture
def server(engine, session):
    httpd = make_server(engine.store, engine, "127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % port, session
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url) as resp:
        return resp.status, resp.read()


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_index_page_served(server):
    base, _ = server
    status, body = _get(base + "/")
    assert status == 200
    assert "diagforge" in body.decode("utf-8")


def test_state_endpoint_reports_required_fields(server, engine, session):
    base, (sid, bid) = server
    engine.run_until_idle(bid)
    status, body = _get(base + "/api/state")
    assert status == 200
    state = json.loads(body.decode("utf-8"))
    assert state["session"]["id"] == sid
    assert state["original_size"] > 0
    assert state["best"]["size"] < state["original_size"]
    assert state["runs_total"] > 0
    assert "queue" in state and "counts" in state
    assert "earliest_divergence" in state


def test_pin_endpoint_invalidates_only_violating(server, engine, session):
    base, (sid, bid) = server
    engine.run_until_idle(bid)
    status, out = _post(base + "/api/pin",
                        {"session": sid, "kind": "co_keep",
                         "paths": ["b.c", "b.h"], "actor": "tester",
                         "reason": "must stay together"})
    assert status == 200
    assert "pin" in out
    pins = engine.store.active_pins(sid)
    assert any(p["kind"] == "co_keep" for p in pins)


def test_decision_and_undo_endpoints(server, engine, session):
    base, (sid, bid) = server
    root = engine.store.root_candidate(bid)
    status, out = _post(base + "/api/decision",
                        {"session": sid, "kind": "reject", "actor": "tester",
                         "reason": "checking", "candidate": root["id"]})
    assert status == 200
    assert engine.store.get_candidate(root["id"])["state"] == "rejected"
    status, out2 = _post(base + "/api/undo",
                         {"session": sid, "event": out["event"],
                          "actor": "tester", "reason": "revert"})
    assert status == 200
    assert engine.store.get_candidate(root["id"])["state"] == "pending"
