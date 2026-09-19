"""SQLite-backed persistent state.

Design rules:
- Every mutation happens inside ``tx()``; a failed batch rolls back and
  leaves no visible partial results.
- History is append-only: decisions and events are never deleted, undo is
  recorded as a new decision row.
- No wall-clock timestamps anywhere; ordering uses per-session logical
  sequence numbers so identical inputs replay to identical outputs.
"""
import json
import os
import sqlite3
import threading

from . import RULES_VERSION
from .util import files_id, files_size, hash_obj

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  command TEXT NOT NULL,
  predicate TEXT NOT NULL,
  k_runs INTEGER NOT NULL,
  timeout REAL NOT NULL,
  env_whitelist TEXT NOT NULL,
  snapshot TEXT NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 1,
  best TEXT,
  level_state TEXT NOT NULL,
  rules_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files(
  session TEXT NOT NULL, name TEXT NOT NULL, content TEXT NOT NULL,
  PRIMARY KEY(session, name));
CREATE TABLE IF NOT EXISTS candidates(
  session TEXT NOT NULL, id TEXT NOT NULL, parent TEXT, transform TEXT,
  nfiles INTEGER NOT NULL, nbytes INTEGER NOT NULL,
  status TEXT NOT NULL, fingerprint TEXT, diverged_run INTEGER,
  epoch INTEGER NOT NULL, seq INTEGER NOT NULL,
  PRIMARY KEY(session, id));
CREATE TABLE IF NOT EXISTS candidate_files(
  candidate TEXT NOT NULL, name TEXT NOT NULL, content TEXT NOT NULL,
  PRIMARY KEY(candidate, name));
CREATE TABLE IF NOT EXISTS runs(
  candidate TEXT NOT NULL, run_index INTEGER NOT NULL,
  exit_code INTEGER, stdout TEXT, stderr TEXT, timed_out INTEGER,
  artifacts TEXT, fingerprint TEXT,
  PRIMARY KEY(candidate, run_index));
CREATE TABLE IF NOT EXISTS pending(
  session TEXT NOT NULL, base TEXT NOT NULL, transform TEXT NOT NULL,
  level TEXT NOT NULL, rank INTEGER NOT NULL,
  file TEXT NOT NULL, span_start INTEGER NOT NULL, span_end INTEGER NOT NULL,
  consumed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(session, base, transform));
CREATE TABLE IF NOT EXISTS expansions(
  session TEXT NOT NULL, base TEXT NOT NULL, level TEXT NOT NULL, g INTEGER NOT NULL,
  PRIMARY KEY(session, base, level, g));
CREATE TABLE IF NOT EXISTS pins(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS decisions(
  session TEXT NOT NULL, seq INTEGER NOT NULL,
  operator TEXT NOT NULL, reason TEXT NOT NULL,
  action TEXT NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(session, seq));
CREATE TABLE IF NOT EXISTS events(
  session TEXT NOT NULL, seq INTEGER NOT NULL,
  kind TEXT NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(session, seq));
"""

LEVEL_RANK = {"files": 0, "nodes": 1, "tokens": 2}


class Store:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        self.scratch = os.path.join(os.path.dirname(os.path.abspath(path)), "scratch")

    # -- transaction helper -------------------------------------------------
    def tx(self):
        return _Tx(self)

    # -- sessions -----------------------------------------------------------
    def create_session(self, files, command, predicate, k_runs, timeout,
                       env_whitelist, snapshot):
        sid = "sess_" + hash_obj({
            "files": sorted(files.items()), "command": command,
            "predicate": predicate, "k_runs": k_runs, "timeout": timeout,
            "env_whitelist": sorted(env_whitelist),
            "rules_version": RULES_VERSION,
        })[:16]
        with self.tx() as conn:
            row = conn.execute("SELECT id FROM sessions WHERE id=?", (sid,)).fetchone()
            if row:
                return sid
            conn.execute(
                "INSERT INTO sessions(id,status,command,predicate,k_runs,timeout,"
                "env_whitelist,snapshot,epoch,best,level_state,rules_version)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, "created", json.dumps(command), json.dumps(predicate),
                 k_runs, timeout, json.dumps(list(env_whitelist)),
                 json.dumps(snapshot), 1, None,
                 json.dumps({"level": 0, "token_g": 2, "token_best": None}),
                 RULES_VERSION))
            for name, content in sorted(files.items()):
                conn.execute("INSERT INTO files(session,name,content) VALUES(?,?,?)",
                             (sid, name, content))
            self._log(conn, sid, "session_created",
                      {"snapshot_fingerprint": hash_obj(snapshot)})
        return sid

    def get_session(self, sid):
        with self.lock:
            row = self.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            raise KeyError(sid)
        return self._session_dict(row)

    def list_sessions(self):
        with self.lock:
            rows = self.conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
        return [self._session_dict(r) for r in rows]

    def update_session(self, sid, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v) if isinstance(v, (dict, list)) else v
                for v in fields.values()]
        with self.tx() as conn:
            conn.execute(f"UPDATE sessions SET {cols} WHERE id=?", (*vals, sid))

    def _session_dict(self, row):
        d = dict(row)
        for key in ("command", "predicate", "env_whitelist", "snapshot", "level_state"):
            d[key] = json.loads(d[key])
        return d

    # -- original files -----------------------------------------------------
    def original_files(self, sid):
        with self.lock:
            rows = self.conn.execute(
                "SELECT name, content FROM files WHERE session=? ORDER BY name",
                (sid,)).fetchall()
        return {r["name"]: r["content"] for r in rows}

    # -- candidates ---------------------------------------------------------
    def put_candidate(self, conn, sid, cid, parent, transform, files, epoch, seq):
        nfiles, nbytes = files_size(files)
        conn.execute(
            "INSERT OR IGNORE INTO candidates"
            "(session,id,parent,transform,nfiles,nbytes,status,fingerprint,"
            " diverged_run,epoch,seq) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sid, cid, parent,
             json.dumps(transform, sort_keys=True) if transform else None,
             nfiles, nbytes, "evaluating", None, None, epoch, seq))
        for name, content in sorted(files.items()):
            conn.execute(
                "INSERT OR IGNORE INTO candidate_files(candidate,name,content)"
                " VALUES(?,?,?)", (cid, name, content))

    def get_candidate(self, sid, cid):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM candidates WHERE session=? AND id=?",
                (sid, cid)).fetchone()
        return self._candidate_dict(row) if row else None

    def candidate_files(self, cid):
        with self.lock:
            rows = self.conn.execute(
                "SELECT name, content FROM candidate_files WHERE candidate=?"
                " ORDER BY name", (cid,)).fetchall()
        return {r["name"]: r["content"] for r in rows}

    def list_candidates(self, sid):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM candidates WHERE session=? ORDER BY seq", (sid,)).fetchall()
        return [self._candidate_dict(r) for r in rows]

    def _candidate_dict(self, row):
        d = dict(row)
        d["transform"] = json.loads(d["transform"]) if d["transform"] else None
        return d

    def finalize_candidate(self, conn, sid, cid, status, fingerprint, diverged_run):
        conn.execute(
            "UPDATE candidates SET status=?, fingerprint=?, diverged_run=?"
            " WHERE session=? AND id=?", (status, fingerprint, diverged_run, sid, cid))

    def recompute_best(self, conn, sid):
        row = conn.execute(
            "SELECT id FROM candidates WHERE session=? AND status='accepted'"
            " ORDER BY nfiles, nbytes, id LIMIT 1", (sid,)).fetchone()
        best = row["id"] if row else None
        conn.execute("UPDATE sessions SET best=? WHERE id=?", (best, sid))
        return best

    # -- runs ---------------------------------------------------------------
    def put_run(self, conn, cid, run_index, run):
        conn.execute(
            "INSERT OR REPLACE INTO runs"
            "(candidate,run_index,exit_code,stdout,stderr,timed_out,artifacts,"
            " fingerprint) VALUES(?,?,?,?,?,?,?,?)",
            (cid, run_index, run["exit_code"], run["stdout"], run["stderr"],
             1 if run["timed_out"] else 0, json.dumps(run["artifacts"],
             sort_keys=True), run["fingerprint"]))

    def get_runs(self, cid):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE candidate=? ORDER BY run_index",
                (cid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["artifacts"] = json.loads(d["artifacts"])
            d["timed_out"] = bool(d["timed_out"])
            out.append(d)
        return out

    def run_count(self, sid):
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM runs r JOIN candidates c ON r.candidate=c.id"
                " WHERE c.session=?", (sid,)).fetchone()
        return row["n"]

    # -- pending queue ------------------------------------------------------
    def add_pending(self, conn, sid, base, transforms):
        for t in transforms:
            level = self._level_of(t)
            conn.execute(
                "INSERT OR IGNORE INTO pending"
                "(session,transform,base,level,rank,file,span_start,span_end,consumed)"
                " VALUES(?,?,?,?,?,?,?,?,0)",
                (sid, json.dumps(t, sort_keys=True), base, level, LEVEL_RANK[level],
                 t.get("file", ""), t.get("start", -1), t.get("end", -1)))

    def pop_pending(self, conn, sid):
        row = conn.execute(
            "SELECT transform FROM pending WHERE session=? AND consumed=0"
            " ORDER BY rank, file, span_start, span_end, transform, base"
            " LIMIT 1",
            (sid,)).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE pending SET consumed=1 WHERE session=? AND transform=?",
            (sid, row["transform"]))
        return json.loads(row["transform"])

    def pending_count(self, sid):
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM pending WHERE session=? AND consumed=0",
                (sid,)).fetchone()
        return row["n"]

    def clear_unconsumed(self, conn, sid):
        conn.execute("DELETE FROM pending WHERE session=? AND consumed=0", (sid,))

    @staticmethod
    def _level_of(t):
        return {"remove_file": "files", "remove_files": "files",
                "remove_lines": "nodes", "remove_tokens": "tokens"}[t["kind"]]

    # -- expansions ---------------------------------------------------------
    def record_expansion(self, conn, sid, base, level, g):
        conn.execute(
            "INSERT OR IGNORE INTO expansions(session,base,level,g) VALUES(?,?,?,?)",
            (sid, base, level, g))

    def was_expanded(self, conn, sid, base, level, g):
        row = conn.execute(
            "SELECT 1 FROM expansions WHERE session=? AND base=? AND level=? AND g=?",
            (sid, base, level, g)).fetchone()
        return row is not None

    # -- pins ---------------------------------------------------------------
    def add_pin(self, sid, kind, payload):
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO pins(session,kind,payload,active) VALUES(?,?,?,1)",
                (sid, kind, json.dumps(payload, sort_keys=True)))
            pin_id = cur.lastrowid
            affected = self._invalidate_locked(conn, sid)
            self._log(conn, sid, "pin_added",
                      {"pin_id": pin_id, "kind": kind, "payload": payload,
                       "invalidated": affected})
        return pin_id

    def list_pins(self, sid):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM pins WHERE session=? ORDER BY id", (sid,)).fetchall()
        return [{"id": r["id"], "kind": r["kind"],
                 "payload": json.loads(r["payload"]), "active": r["active"]}
                for r in rows]

    def deactivate_pin(self, sid, pin_id):
        with self.tx() as conn:
            conn.execute("UPDATE pins SET active=0 WHERE session=? AND id=?",
                         (sid, pin_id))
            self._log(conn, sid, "pin_deactivated", {"pin_id": pin_id})

    def _invalidate_locked(self, conn, sid):
        """Mark only the candidates that violate the now-active pins."""
        from .transforms import violates_pins
        pins = [{"kind": r["kind"], "payload": json.loads(r["payload"]),
                 "active": r["active"]}
                for r in conn.execute(
                    "SELECT * FROM pins WHERE session=? AND active=1", (sid,))]
        affected = []
        rows = conn.execute(
            "SELECT id, status FROM candidates WHERE session=?"
            " AND status IN ('accepted','evaluating','unstable')", (sid,)).fetchall()
        for row in rows:
            files = {r["name"]: r["content"] for r in conn.execute(
                "SELECT name, content FROM candidate_files WHERE candidate=?",
                (row["id"],))}
            if violates_pins(files, pins):
                conn.execute(
                    "UPDATE candidates SET status='invalidated'"
                    " WHERE session=? AND id=?", (sid, row["id"]))
                affected.append(row["id"])
        if affected:
            self.recompute_best(conn, sid)
        return affected

    # -- decisions & events (append-only) -----------------------------------
    def add_decision(self, sid, operator, reason, action, payload):
        with self.tx() as conn:
            seq = self._next_seq(conn, sid, "decisions")
            conn.execute(
                "INSERT INTO decisions(session,seq,operator,reason,action,payload)"
                " VALUES(?,?,?,?,?,?)",
                (sid, seq, operator, reason, action,
                 json.dumps(payload, sort_keys=True)))
            self._log(conn, sid, "decision",
                      {"seq": seq, "operator": operator, "action": action})
        return seq

    def list_decisions(self, sid):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM decisions WHERE session=? ORDER BY seq", (sid,)).fetchall()
        return [{"seq": r["seq"], "operator": r["operator"], "reason": r["reason"],
                 "action": r["action"], "payload": json.loads(r["payload"])}
                for r in rows]

    def list_events(self, sid):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE session=? ORDER BY seq", (sid,)).fetchall()
        return [{"seq": r["seq"], "kind": r["kind"], "payload": json.loads(r["payload"])}
                for r in rows]

    def _log(self, conn, sid, kind, payload):
        seq = self._next_seq(conn, sid, "events")
        conn.execute(
            "INSERT INTO events(session,seq,kind,payload) VALUES(?,?,?,?)",
            (sid, seq, kind, json.dumps(payload, sort_keys=True)))
        return seq

    @staticmethod
    def _next_seq(conn, sid, table):
        row = conn.execute(
            f"SELECT COALESCE(MAX(seq),0)+1 AS n FROM {table} WHERE session=?",
            (sid,)).fetchone()
        return row["n"]

    # -- branches (snapshot changes) ----------------------------------------
    def record_branch(self, sid, snapshot, reason):
        with self.tx() as conn:
            row = conn.execute("SELECT epoch FROM sessions WHERE id=?", (sid,)).fetchone()
            epoch = row["epoch"] + 1
            conn.execute("UPDATE sessions SET epoch=?, snapshot=? WHERE id=?",
                         (epoch, json.dumps(snapshot), sid))
            self._log(conn, sid, "new_branch",
                      {"epoch": epoch, "reason": reason,
                       "snapshot_fingerprint": hash_obj(snapshot)})
        return epoch


class _Tx:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.store.lock.acquire()
        return self.store.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.store.conn.commit()
            else:
                self.store.conn.rollback()
        finally:
            self.store.lock.release()
        return False
