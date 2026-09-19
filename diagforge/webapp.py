"""Stdlib HTTP server exposing the JSON API and the single-page UI."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .export import export_package
from .predicate import Predicate
from .reduce import violates_pins

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def build_state(store, engine):
    sessions = store.list_sessions()
    if not sessions:
        return {"session": None}
    sess = sessions[-1]
    sid = sess["id"]
    branch = store.get_active_branch(sid)
    stats = store.stats(branch["id"])
    best = store.best_candidate(branch["id"])
    root = store.root_candidate(branch["id"])
    divergence = store.earliest_divergence(branch["id"])
    queue = store.queue(branch["id"])
    return {
        "session": {
            "id": sid,
            "name": sess["name"],
            "command": sess["command"],
            "predicate": json.loads(sess["predicate"]),
            "rules_version": sess["rules_version"],
        },
        "branch": {"id": branch["id"], "label": branch["label"],
                   "snapshot_fp": branch["snapshot_fp"]},
        "branches": [{"id": b["id"], "label": b["label"]}
                     for b in store.list_branches(sid)],
        "original_size": root["size"] if root else sess["original_size"],
        "best": ({"id": best["id"], "size": best["size"], "depth": best["depth"]}
                 if best else None),
        "runs_total": stats["runs_total"],
        "counts": stats["counts"],
        "earliest_divergence": (
            {"id": divergence["id"], "depth": divergence["depth"],
             "state": divergence["state"],
             "transform": json.loads(divergence["transform"])}
            if divergence else None),
        "queue": [{"id": c["id"], "size": c["size"], "state": c["state"],
                   "depth": c["depth"],
                   "transform": json.loads(c["transform"])} for c in queue],
        "pins": store.active_pins(sid),
        "events": store.list_events(sid),
    }


def make_server(store, engine, host, port):
    class Handler(BaseHTTPRequestHandler):
        server_version = "diagforge/0.1"

        def log_message(self, *args):
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj, indent=2, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as fh:
                    body = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                self._json(build_state(store, engine))
            elif path == "/api/candidate":
                cid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
                cand = store.get_candidate(cid)
                if not cand:
                    self._json({"error": "unknown candidate"}, 404)
                else:
                    self._json({"candidate": cand,
                                "files": store.get_files(cid),
                                "runs": store.runs_for(cid)})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                data = self._body()
                if path == "/api/session":
                    pred = Predicate.from_dict(data.get("predicate", {}))
                    sid, bid = engine.create_session(
                        data.get("name", "adhoc"), data["files"],
                        data["command"], pred,
                        tuple(data.get("env_whitelist", ["PATH"])),
                        float(data.get("timeout_s", 10.0)))
                    self._json({"session": sid, "branch": bid})
                elif path == "/api/pin":
                    sid = data["session"]
                    kind = data["kind"]
                    payload = {k: v for k, v in data.items()
                               if k not in ("session", "kind", "actor", "reason")}
                    pid = store.add_pin(sid, kind, payload)
                    pin = dict(payload, kind=kind)
                    branch = store.get_active_branch(sid)
                    affected = []
                    for cand in store.candidates_where(
                            branch["id"], ("pending", "running")):
                        if violates_pins(store.get_files(cand["id"]), [pin]):
                            affected.append(cand["id"])
                    store.invalidate(affected)
                    store.add_event(sid, "pin", data.get("actor", "web"),
                                    data.get("reason", ""), "", "",
                                    {"pin": pid, "invalidated": affected})
                    self._json({"pin": pid, "invalidated": affected})
                elif path == "/api/decision":
                    eid = engine.decide(data["session"], data["kind"],
                                        data.get("actor", "web"),
                                        data.get("reason", ""),
                                        data["candidate"])
                    self._json({"event": eid})
                elif path == "/api/undo":
                    eid = engine.undo_event(data["session"], data["event"],
                                            data.get("actor", "web"),
                                            data.get("reason", ""))
                    self._json({"event": eid})
                elif path == "/api/resume":
                    bid, created = engine.resume_branch(data["session"])
                    self._json({"branch": bid, "new_branch": created})
                elif path == "/api/export":
                    info = export_package(store, data["session"],
                                          data.get("out_dir", "export"))
                    self._json(info)
                else:
                    self._json({"error": "not found"}, 404)
            except (KeyError, ValueError) as exc:
                self._json({"error": str(exc)}, 400)

    return ThreadingHTTPServer((host, port), Handler)
