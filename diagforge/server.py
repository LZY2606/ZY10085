"""HTTP API + web UI, stdlib only.

A single background thread advances every running session one step at a
time; each step is one transaction, so pausing or killing the server never
leaves half-recorded batches.  All endpoints are deterministic: no clocks,
no random ids.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import Engine, SnapshotChanged
from .export import build_chain, export_session
from .snapshot import capture
from .store import Store

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
DEMO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo")

DEFAULT_ENV_WHITELIST = ["PATH", "LANG", "LC_ALL", "SYSTEMROOT"]


def make_demo_session(store):
    files = {}
    src = os.path.join(DEMO_DIR, "src")
    for name in sorted(os.listdir(src)):
        with open(os.path.join(src, name), encoding="utf-8") as fh:
            files[name] = fh.read()
    command = ["python3", os.path.join(DEMO_DIR, "fakecc.py"), "{files}"]
    predicate = {"exit_codes": [1], "stderr_contains": "internal compiler error"}
    sid = store.create_session(
        files=files, command=command, predicate=predicate,
        k_runs=2, timeout=10.0, env_whitelist=DEFAULT_ENV_WHITELIST,
        snapshot=capture(command, DEFAULT_ENV_WHITELIST))
    return sid


class Api:
    """Framework-free request handlers; easy to drive from tests."""

    def __init__(self, store):
        self.store = store

    # -- helpers -------------------------------------------------------------
    def engine(self, sid):
        return Engine(self.store, sid)

    # -- endpoints -----------------------------------------------------------
    def list_sessions(self):
        out = []
        for s in self.store.list_sessions():
            out.append(self.engine(s["id"]).status())
        return out

    def create_session(self, body):
        files = body["files"]
        command = body["command"]
        predicate = body["predicate"]
        k_runs = int(body.get("k_runs", 3))
        timeout = float(body.get("timeout", 10.0))
        env_whitelist = body.get("env_whitelist", DEFAULT_ENV_WHITELIST)
        snapshot = capture(command, env_whitelist)
        sid = self.store.create_session(
            files=files, command=command, predicate=predicate, k_runs=k_runs,
            timeout=timeout, env_whitelist=env_whitelist, snapshot=snapshot)
        return {"id": sid}

    def demo(self):
        return {"id": make_demo_session(self.store)}

    def session_detail(self, sid):
        status = self.engine(sid).status()
        status["session"] = self.store.get_session(sid)
        return status

    def control(self, sid, body):
        action = body.get("action")
        allow = bool(body.get("allow_new_branch", False))
        engine = self.engine(sid)
        try:
            if action == "start":
                engine.start(allow_new_branch=allow)
            elif action == "pause":
                engine.pause()
            elif action == "resume":
                engine.resume(allow_new_branch=allow)
            elif action == "step":
                steps = int(body.get("steps", 1))
                if self.store.get_session(sid)["status"] in ("created",):
                    engine.start(allow_new_branch=allow)
                else:
                    self.store.update_session(sid, status="running")
                for _ in range(steps):
                    if not engine.step():
                        break
                engine.pause()
            else:
                return {"error": "unknown action"}, 400
        except SnapshotChanged as exc:
            return {"error": "snapshot_changed",
                    "message": "compiler binary or whitelisted environment "
                               "changed; resume with allow_new_branch=true "
                               "to open a new epoch",
                    "old": exc.old, "new": exc.new}, 409
        return self.engine(sid).status()

    def candidates(self, sid):
        return self.store.list_candidates(sid)

    def tree(self, sid):
        return [
            {"id": c["id"], "parent": c["parent"], "transform": c["transform"],
             "status": c["status"], "nfiles": c["nfiles"],
             "nbytes": c["nbytes"], "seq": c["seq"],
             "fingerprint": c["fingerprint"]}
            for c in self.store.list_candidates(sid)
        ]

    def candidate_runs(self, sid, cid):
        return self.store.get_runs(cid)

    def add_pin(self, sid, body):
        pin_id = self.store.add_pin(sid, body["kind"], body["payload"])
        return {"pin_id": pin_id, "pins": self.store.list_pins(sid)}

    def deactivate_pin(self, sid, pin_id):
        self.store.deactivate_pin(sid, int(pin_id))
        return {"pins": self.store.list_pins(sid)}

    def pins(self, sid):
        return self.store.list_pins(sid)

    def add_decision(self, sid, body):
        action = body["action"]
        payload = body.get("payload", {})
        before = after = None
        if action in ("accept_candidate", "reject_candidate"):
            cid = payload["candidate"]
            cand = self.store.get_candidate(sid, cid)
            if cand is None:
                return {"error": "unknown candidate"}, 404
            before = {"status": cand["status"]}
            new_status = "accepted" if action == "accept_candidate" else "rejected"
            with self.store.tx() as conn:
                conn.execute(
                    "UPDATE candidates SET status=? WHERE session=? AND id=?",
                    (new_status, sid, cid))
                self.store.recompute_best(conn, sid)
            after = {"status": new_status}
            payload = dict(payload, before=before, after=after)
        seq = self.store.add_decision(
            sid, body.get("operator", "anonymous"), body.get("reason", ""),
            action, payload)
        return {"seq": seq, "decisions": self.store.list_decisions(sid)}

    def undo_decision(self, sid, seq, body):
        decisions = {d["seq"]: d for d in self.store.list_decisions(sid)}
        target = decisions.get(int(seq))
        if target is None:
            return {"error": "unknown decision"}, 404
        payload = target["payload"]
        if target["action"] in ("accept_candidate", "reject_candidate"):
            cid = payload["candidate"]
            before_status = payload["before"]["status"]
            with self.store.tx() as conn:
                conn.execute(
                    "UPDATE candidates SET status=? WHERE session=? AND id=?",
                    (before_status, sid, cid))
                self.store.recompute_best(conn, sid)
        new_seq = self.store.add_decision(
            sid, body.get("operator", "anonymous"), body.get("reason", ""),
            "undo", {"target_seq": int(seq), "restored": payload})
        return {"seq": new_seq, "decisions": self.store.list_decisions(sid)}

    def events(self, sid):
        return self.store.list_events(sid)

    def export(self, sid, body):
        out_dir = body.get("out_dir") or os.path.join("exports", sid)
        export_session(self.store, sid, out_dir)
        return {"out_dir": out_dir, "chain": build_chain(self.store, sid)}


class Handler(BaseHTTPRequestHandler):
    api = None  # set by serve()

    def log_message(self, *args):  # keep the demo quiet
        pass

    # -- plumbing -------------------------------------------------------------
    def _send(self, obj, status=200, content_type="application/json"):
        if isinstance(obj, (dict, list)):
            body = json.dumps(obj, indent=1, sort_keys=True).encode()
        elif isinstance(obj, str):
            body = obj.encode()
        else:
            body = obj
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def _parts(self):
        return [p for p in self.path.split("?")[0].split("/") if p]

    # -- routing ----------------------------------------------------------------
    def do_GET(self):
        parts = self._parts()
        try:
            if not parts:
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as fh:
                    return self._send(fh.read(), content_type="text/html")
            if parts[0] != "api":
                return self._send({"error": "not found"}, 404)
            if parts == ["api", "sessions"]:
                return self._send(self.api.list_sessions())
            if len(parts) >= 3 and parts[1] == "sessions":
                sid = parts[2]
                tail = parts[3:]
                if not tail:
                    return self._send(self.api.session_detail(sid))
                if tail == ["candidates"]:
                    return self._send(self.api.candidates(sid))
                if tail == ["tree"]:
                    return self._send(self.api.tree(sid))
                if tail == ["pins"]:
                    return self._send(self.api.pins(sid))
                if tail == ["events"]:
                    return self._send(self.api.events(sid))
                if tail == ["decisions"]:
                    return self._send(self.api.store.list_decisions(sid))
                if tail == ["export"]:
                    return self._send({"chain": build_chain(self.api.store, sid)})
                if len(tail) == 2 and tail[0] == "runs":
                    return self._send(self.api.candidate_runs(sid, tail[1]))
            return self._send({"error": "not found"}, 404)
        except KeyError:
            return self._send({"error": "unknown session"}, 404)
        except Exception as exc:  # pragma: no cover - defensive
            return self._send({"error": str(exc)}, 500)

    def do_POST(self):
        parts = self._parts()
        try:
            body = self._body()
            if parts == ["api", "sessions"]:
                return self._send(self.api.create_session(body))
            if parts == ["api", "demo"]:
                return self._send(self.api.demo())
            if len(parts) >= 3 and parts[1] == "sessions":
                sid = parts[2]
                tail = parts[3:]
                if tail == ["control"]:
                    result = self.api.control(sid, body)
                    if isinstance(result, tuple):
                        return self._send(result[0], result[1])
                    return self._send(result)
                if tail == ["pins"]:
                    return self._send(self.api.add_pin(sid, body))
                if len(tail) == 2 and tail[0] == "pins" and tail[1].isdigit():
                    return self._send(self.api.deactivate_pin(sid, tail[1]))
                if tail == ["decisions"]:
                    result = self.api.add_decision(sid, body)
                    if isinstance(result, tuple):
                        return self._send(result[0], result[1])
                    return self._send(result)
                if len(tail) == 3 and tail[0] == "decisions" and tail[2] == "undo":
                    result = self.api.undo_decision(sid, parts[4], body)
                    if isinstance(result, tuple):
                        return self._send(result[0], result[1])
                    return self._send(result)
                if tail == ["export"]:
                    return self._send(self.api.export(sid, body))
            return self._send({"error": "not found"}, 404)
        except KeyError:
            return self._send({"error": "unknown session"}, 404)
        except Exception as exc:  # pragma: no cover - defensive
            return self._send({"error": str(exc)}, 500)


def _runner_loop(store, stop):
    while not stop.is_set():
        for session in store.list_sessions():
            if session["status"] == "running":
                try:
                    Engine(store, session["id"]).step()
                except Exception as exc:  # keep the loop alive, record the failure
                    with store.tx() as conn:
                        store._log(conn, session["id"], "step_error",
                                   {"error": str(exc)})
        stop.wait(0.05)


def serve(host, port, db_path):
    store = Store(db_path)
    handler = type("BoundHandler", (Handler,), {"api": Api(store)})
    httpd = ThreadingHTTPServer((host, port), handler)
    stop = threading.Event()
    worker = threading.Thread(target=_runner_loop, args=(store, stop),
                              daemon=True)
    worker.start()
    print("diagforge listening on http://%s:%d (db: %s)" % (host, port, db_path))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join()
        httpd.server_close()
