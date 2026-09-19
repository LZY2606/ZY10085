"""SQLite-backed durable state for sessions, branches, candidates and events.

Guarantees:
  - Candidate ids are content-derived, so re-inserting the same logical
    candidate is a no-op regardless of worker interleaving.
  - Multi-row mutations happen inside ``batch()``; a failing batch rolls back
    and leaves no visible partial results.
  - The event log is append-only: undo is recorded as a new event, history is
    never deleted.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from .model import (
    RULES_VERSION,
    canonical_json,
    candidate_id,
    fileset_hash,
    fileset_size,
    hash_text,
    run_fingerprint,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, name TEXT, command TEXT, predicate TEXT,
  env_whitelist TEXT, timeout_s REAL, rules_version TEXT,
  original_hash TEXT, original_size INTEGER);
CREATE TABLE IF NOT EXISTS branches(
  id TEXT PRIMARY KEY, session_id TEXT, snapshot TEXT, snapshot_fp TEXT,
  seq INTEGER, label TEXT);
CREATE TABLE IF NOT EXISTS candidates(
  id TEXT PRIMARY KEY, branch_id TEXT, parent_id TEXT, transform TEXT,
  files_hash TEXT, size INTEGER, depth INTEGER, state TEXT,
  runs_required INTEGER,
  UNIQUE(branch_id, files_hash));
CREATE TABLE IF NOT EXISTS candidate_files(
  candidate_id TEXT, path TEXT, content TEXT,
  PRIMARY KEY(candidate_id, path));
CREATE TABLE IF NOT EXISTS runs(
  candidate_id TEXT, seq INTEGER, exit_code INTEGER, stdout TEXT,
  stderr TEXT, timeout INTEGER, artifacts_hash TEXT, fingerprint TEXT,
  PRIMARY KEY(candidate_id, seq));
CREATE TABLE IF NOT EXISTS pins(
  id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, payload TEXT,
  active INTEGER, seq INTEGER);
CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, kind TEXT, actor TEXT,
  reason TEXT, before_hash TEXT, after_hash TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS batches(
  id TEXT PRIMARY KEY, session_id TEXT, state TEXT, seq INTEGER);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    def _next_seq(self, table: str, key_col: str, key_val: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS s FROM %s WHERE %s=?" % (table, key_col),
            (key_val,),
        ).fetchone()
        return row["s"]

    @contextmanager
    def batch(self, session_id: str, label: str = ""):
        """Atomic multi-row mutation. On exception everything rolls back and
        no partial results remain visible."""
        with self._lock:
            seq = self._next_seq("batches", "session_id", session_id)
            batch_id = hash_text(canonical_json(
                {"session": session_id, "seq": seq, "label": label}))[:32]
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.execute(
                    "INSERT INTO batches(id, session_id, state, seq) VALUES(?,?,?,?)",
                    (batch_id, session_id, "committed", seq))
                self._conn.commit()

    def _one(self, sql, args=()):
        row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql, args=()):
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def create_session(self, name, files, command, predicate_dict,
                       env_whitelist, timeout_s):
        with self._lock:
            fh = fileset_hash(files)
            sid = hash_text(canonical_json({
                "name": name, "files": fh, "command": command,
                "predicate": predicate_dict, "env": sorted(env_whitelist),
                "timeout": timeout_s, "rules": RULES_VERSION}))[:32]
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, name, command, canonical_json(predicate_dict),
                 canonical_json(sorted(env_whitelist)), float(timeout_s),
                 RULES_VERSION, fh, fileset_size(files)))
            self._conn.commit()
            return sid

    def get_session(self, session_id):
        with self._lock:
            return self._one("SELECT * FROM sessions WHERE id=?", (session_id,))

    def list_sessions(self):
        with self._lock:
            return self._all("SELECT * FROM sessions ORDER BY id")

    def add_branch(self, session_id, snapshot_dict, snapshot_fp, label):
        with self._lock:
            seq = self._next_seq("branches", "session_id", session_id)
            bid = hash_text(canonical_json({
                "session": session_id, "fp": snapshot_fp, "seq": seq}))[:32]
            self._conn.execute(
                "INSERT OR IGNORE INTO branches VALUES(?,?,?,?,?,?)",
                (bid, session_id, canonical_json(snapshot_dict), snapshot_fp,
                 seq, label))
            self._conn.commit()
            return bid

    def get_active_branch(self, session_id):
        with self._lock:
            return self._one(
                "SELECT * FROM branches WHERE session_id=? ORDER BY seq DESC LIMIT 1",
                (session_id,))

    def list_branches(self, session_id):
        with self._lock:
            return self._all(
                "SELECT * FROM branches WHERE session_id=? ORDER BY seq",
                (session_id,))

    def add_candidate(self, branch_id, parent_id, transform, files,
                      runs_required, commit=True):
        """Insert a candidate; dedupes on (branch, files_hash). Returns the
        id of the row that now represents this content."""
        with self._lock:
            fh = fileset_hash(files)
            cid = candidate_id(branch_id, parent_id, transform, fh)
            depth = 0
            if parent_id:
                parent = self._one("SELECT depth FROM candidates WHERE id=?",
                                   (parent_id,))
                depth = (parent["depth"] if parent else 0) + 1
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO candidates"
                "(id,branch_id,parent_id,transform,files_hash,size,depth,state,"
                "runs_required) VALUES(?,?,?,?,?,?,?,?,?)",
                (cid, branch_id, parent_id, canonical_json(transform), fh,
                 fileset_size(files), depth, "pending", int(runs_required)))
            if cur.rowcount:
                self._conn.executemany(
                    "INSERT INTO candidate_files VALUES(?,?,?)",
                    [(cid, p, files[p]) for p in sorted(files)])
                if commit:
                    self._conn.commit()
                return cid
            row = self._one(
                "SELECT id FROM candidates WHERE branch_id=? AND files_hash=?",
                (branch_id, fh))
            if commit:
                self._conn.commit()
            return row["id"] if row else cid

    def get_candidate(self, candidate_id):
        with self._lock:
            return self._one("SELECT * FROM candidates WHERE id=?",
                             (candidate_id,))

    def get_files(self, candidate_id):
        with self._lock:
            rows = self._all(
                "SELECT path, content FROM candidate_files WHERE candidate_id=?"
                " ORDER BY path", (candidate_id,))
            return {r["path"]: r["content"] for r in rows}

    def claim_next(self, branch_id):
        """Atomically claim the highest-priority pending candidate.
        Priority is (size, id), restricted to candidates that can still
        strictly improve on the current best: deterministic for any
        worker interleaving."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT * FROM candidates WHERE branch_id=? AND state='pending'"
                " AND size < COALESCE((SELECT MIN(size) FROM candidates"
                "  WHERE branch_id=? AND state='accepted'), 9223372036854775807)"
                " ORDER BY size, id LIMIT 1",
                (branch_id, branch_id)).fetchone()
            if row is not None:
                self._conn.execute(
                    "UPDATE candidates SET state='running' WHERE id=?",
                    (row["id"],))
            self._conn.commit()
            return dict(row) if row else None

    def set_state(self, candidate_id, state, commit=True):
        with self._lock:
            self._conn.execute(
                "UPDATE candidates SET state=? WHERE id=?",
                (state, candidate_id))
            if commit:
                self._conn.commit()

    def invalidate(self, candidate_ids, commit=True):
        with self._lock:
            self._conn.executemany(
                "UPDATE candidates SET state='invalidated'"
                " WHERE id=? AND state IN ('pending','running')",
                [(cid,) for cid in candidate_ids])
            if commit:
                self._conn.commit()

    def invalidate_superseded(self, branch_id, best_size, commit=True):
        """Candidates that cannot strictly improve on the current best are
        invalidated; deterministic because it depends only on stored sizes."""
        with self._lock:
            self._conn.execute(
                "UPDATE candidates SET state='invalidated'"
                " WHERE branch_id=? AND state IN ('pending','running')"
                " AND size>=?",
                (branch_id, int(best_size)))
            if commit:
                self._conn.commit()

    def candidates_where(self, branch_id, states=None):
        with self._lock:
            if states:
                marks = ",".join("?" for _ in states)
                return self._all(
                    "SELECT * FROM candidates WHERE branch_id=?"
                    " AND state IN (%s) ORDER BY size, id" % marks,
                    (branch_id, *states))
            return self._all(
                "SELECT * FROM candidates WHERE branch_id=? ORDER BY size, id",
                (branch_id,))

    def best_candidate(self, branch_id):
        with self._lock:
            return self._one(
                "SELECT * FROM candidates WHERE branch_id=? AND state='accepted'"
                " ORDER BY size, id LIMIT 1", (branch_id,))

    def root_candidate(self, branch_id):
        with self._lock:
            return self._one(
                "SELECT * FROM candidates WHERE branch_id=? AND parent_id IS NULL",
                (branch_id,))

    def queue(self, branch_id):
        return self.candidates_where(branch_id, ("pending", "running"))

    def earliest_divergence(self, branch_id):
        """Shallowest candidate whose verdict differs from its accepted
        parent; ties break on id so the answer is order-independent."""
        with self._lock:
            return self._one(
                "SELECT c.* FROM candidates c"
                " JOIN candidates p ON c.parent_id = p.id"
                " WHERE c.branch_id=? AND p.state='accepted'"
                " AND c.state IN ('rejected','unstable')"
                " ORDER BY c.depth, c.id LIMIT 1", (branch_id,))

    def stats(self, branch_id):
        with self._lock:
            counts = {r["state"]: r["n"] for r in self._all(
                "SELECT state, COUNT(*) AS n FROM candidates WHERE branch_id=?"
                " GROUP BY state", (branch_id,))}
            runs = self._one(
                "SELECT COUNT(*) AS n FROM runs r JOIN candidates c"
                " ON r.candidate_id=c.id WHERE c.branch_id=?", (branch_id,))
            return {"counts": counts, "runs_total": runs["n"]}

    def record_run(self, candidate_id, result, seq=None, commit=True):
        with self._lock:
            if seq is None:
                seq = self._next_seq("runs", "candidate_id", candidate_id)
            fp = run_fingerprint(result.exit_code, result.stdout, result.stderr,
                                 result.timeout, result.artifacts_hash)
            self._conn.execute(
                "INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?)",
                (candidate_id, seq, result.exit_code, result.stdout,
                 result.stderr, int(result.timeout), result.artifacts_hash, fp))
            if commit:
                self._conn.commit()
            return seq

    def runs_for(self, candidate_id):
        with self._lock:
            return self._all(
                "SELECT * FROM runs WHERE candidate_id=? ORDER BY seq",
                (candidate_id,))

    def add_pin(self, session_id, kind, payload):
        with self._lock:
            seq = self._next_seq("pins", "session_id", session_id)
            pid = hash_text(canonical_json(
                {"session": session_id, "kind": kind, "payload": payload,
                 "seq": seq}))[:32]
            self._conn.execute(
                "INSERT INTO pins VALUES(?,?,?,?,1,?)",
                (pid, session_id, kind, canonical_json(payload), seq))
            self._conn.commit()
            return pid

    def active_pins(self, session_id):
        with self._lock:
            rows = self._all(
                "SELECT * FROM pins WHERE session_id=? AND active=1 ORDER BY seq",
                (session_id,))
            out = []
            for r in rows:
                payload = json.loads(r["payload"])
                payload["kind"] = r["kind"]
                payload["id"] = r["id"]
                out.append(payload)
            return out

    def add_event(self, session_id, kind, actor, reason, before_hash,
                  after_hash, payload):
        with self._lock:
            seq = self._next_seq("events", "session_id", session_id)
            eid = hash_text(canonical_json({
                "session": session_id, "seq": seq, "kind": kind,
                "actor": actor, "reason": reason,
                "payload": payload}))[:32]
            self._conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (eid, session_id, seq, kind, actor, reason, before_hash,
                 after_hash, canonical_json(payload)))
            self._conn.commit()
            return eid

    def get_event(self, event_id):
        with self._lock:
            return self._one("SELECT * FROM events WHERE id=?", (event_id,))

    def list_events(self, session_id):
        with self._lock:
            return self._all(
                "SELECT * FROM events WHERE session_id=? ORDER BY seq",
                (session_id,))

    def count(self, table):
        with self._lock:
            return self._one("SELECT COUNT(*) AS n FROM %s" % table)["n"]
