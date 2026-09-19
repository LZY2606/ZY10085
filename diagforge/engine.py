"""The reduction engine: a pausable, deterministic search tree.

Every ``step`` evaluates exactly one queued candidate inside a single
transaction, so the engine can be paused after any step and a failed batch
leaves no partial state.  Candidate ids are content hashes, the queue is
ordered by a fixed priority, and the best candidate is a pure function of
the accepted set — worker completion order cannot change the outcome.
"""
import json

from .predicate import classify
from .runner import run_once
from .snapshot import capture, fingerprint
from .syntax import token_spans
from .transforms import LEVELS, apply_transform, gen_transforms, violates_pins
from .util import files_id


class SnapshotChanged(Exception):
    def __init__(self, old, new):
        super().__init__("environment snapshot changed")
        self.old = old
        self.new = new


class Engine:
    def __init__(self, store, session_id, runner=run_once):
        self.store = store
        self.sid = session_id
        self.runner = runner

    # -- session accessors --------------------------------------------------
    @property
    def session(self):
        return self.store.get_session(self.sid)

    @property
    def pins(self):
        return self.store.list_pins(self.sid)

    # -- root evaluation ----------------------------------------------------
    def start(self, allow_new_branch=False):
        """Evaluate the original sample and open the search tree."""
        self._check_snapshot(allow_new_branch)
        session = self.session
        if session["status"] not in ("created", "paused"):
            return session["status"]
        files = self.store.original_files(self.sid)
        root = files_id(files)
        with self.store.tx() as conn:
            if not self.store.get_candidate(self.sid, root):
                seq = self.store._next_seq(conn, self.sid, "candidates")
                self.store.put_candidate(conn, self.sid, root, None, None, files,
                                         session["epoch"], seq)
                runs = [self.runner(files, session["command"], session["timeout"],
                                    session["env_whitelist"], self.store.scratch)
                        for _ in range(session["k_runs"])]
                for i, run in enumerate(runs):
                    self.store.put_run(conn, root, i, run)
                status, fp, diverged = classify(session["predicate"], runs)
                self.store.finalize_candidate(conn, self.sid, root, status, fp, diverged)
                self.store._log(conn, self.sid, "root_evaluated",
                                {"candidate": root, "status": status})
        return self._after_eval(root)

    # -- one search step ----------------------------------------------------
    def step(self):
        """Evaluate one queued candidate.  Returns False when finished."""
        session = self.session
        if session["status"] != "running":
            return False
        with self.store.tx() as conn:
            transform = self.store.pop_pending(conn, self.sid)
            if transform is None:
                if not self._refill(conn):
                    conn.execute("UPDATE sessions SET status='done' WHERE id=?",
                                 (self.sid,))
                    self.store._log(conn, self.sid, "search_done", {})
                    return False
                transform = self.store.pop_pending(conn, self.sid)
                if transform is None:
                    conn.execute("UPDATE sessions SET status='done' WHERE id=?",
                                 (self.sid,))
                    return False
            base = self.session["best"]
            base_files = self.store.candidate_files(base)
            files = apply_transform(base_files, transform)
            cid = files_id(files)
            if self.store.get_candidate(self.sid, cid) is not None:
                return True  # already evaluated; consumed, move on
            if violates_pins(files, self.pins) or not any(
                v.strip() for v in files.values()):
                return True
            seq = self.store._next_seq(conn, self.sid, "candidates")
            self.store.put_candidate(conn, self.sid, cid, base, transform, files,
                                     session["epoch"], seq)
            runs = [self.runner(files, session["command"], session["timeout"],
                                session["env_whitelist"], self.store.scratch)
                    for _ in range(session["k_runs"])]
            for i, run in enumerate(runs):
                self.store.put_run(conn, cid, i, run)
            status, fp, diverged = classify(session["predicate"], runs)
            self.store.finalize_candidate(conn, self.sid, cid, status, fp, diverged)
            self.store._log(conn, self.sid, "candidate_evaluated",
                            {"candidate": cid, "parent": base,
                             "transform": transform, "status": status})
        self._after_eval(cid)
        return True

    def run_to_completion(self, max_steps=100000):
        steps = 0
        while steps < max_steps and self.step():
            steps += 1
        return steps

    # -- control ------------------------------------------------------------
    def pause(self):
        if self.session["status"] == "running":
            self.store.update_session(self.sid, status="paused")

    def resume(self, allow_new_branch=False):
        if self.session["status"] != "paused":
            return
        self._check_snapshot(allow_new_branch)
        self.store.update_session(self.sid, status="running")

    def _check_snapshot(self, allow_new_branch):
        session = self.session
        current = capture(session["command"], session["env_whitelist"])
        if fingerprint(current) == fingerprint(session["snapshot"]):
            return
        if not allow_new_branch:
            raise SnapshotChanged(session["snapshot"], current)
        self.store.record_branch(self.sid, current, "snapshot_changed")

    # -- internal ------------------------------------------------------------
    def _after_eval(self, cid):
        """Update best; on improvement restart the level schedule."""
        with self.store.tx() as conn:
            cand = self.store.get_candidate(self.sid, cid)
            if cand["status"] != "accepted":
                row = conn.execute("SELECT status FROM sessions WHERE id=?",
                                   (self.sid,)).fetchone()
                if row["status"] == "created":
                    new_status = ("root_unstable"
                                  if cand["status"] == "unstable"
                                  else "root_rejected")
                    conn.execute("UPDATE sessions SET status=? WHERE id=?",
                                 (new_status, self.sid))
                return self.session["status"]
            before = conn.execute("SELECT best FROM sessions WHERE id=?",
                                  (self.sid,)).fetchone()["best"]
            best = self.store.recompute_best(conn, self.sid)
            if best != before and best == cid:
                self.store.clear_unconsumed(conn, self.sid)
                conn.execute("UPDATE sessions SET level_state=? WHERE id=?",
                             (json.dumps({"level": 0, "token_g": 2,
                                          "token_best": None}), self.sid))
                self.store._log(conn, self.sid, "new_best", {"candidate": best})
            status = conn.execute("SELECT status FROM sessions WHERE id=?",
                                  (self.sid,)).fetchone()["status"]
            if status == "created":
                new_status = "running" if best else (
                    "root_unstable" if cand["status"] == "unstable"
                    else "root_rejected")
                conn.execute("UPDATE sessions SET status=? WHERE id=?",
                             (new_status, self.sid))
            return self.session["status"]

    def _refill(self, conn):
        """Expand the best candidate at the current level; advance levels."""
        session = self.session
        best = session["best"]
        if best is None:
            return False
        state = session["level_state"]
        files = self.store.candidate_files(best)
        pins = self.pins
        while True:
            level_name = LEVELS[state["level"]]
            if level_name != "tokens":
                if self.store.was_expanded(conn, self.sid, best, level_name, 0):
                    state["level"] += 1
                    if state["level"] >= len(LEVELS):
                        return False
                    continue
                transforms = gen_transforms(files, level_name, pins)
                self.store.record_expansion(conn, self.sid, best, level_name, 0)
                self.store.add_pending(conn, self.sid, best, transforms)
                self._save_state(conn, state)
                if transforms:
                    return True
                continue  # empty expansion: advance to the next level
            # token level with growing granularity
            if state.get("token_best") != best:
                state["token_g"] = 2
                state["token_best"] = best
            g = state["token_g"]
            max_tokens = max((len(token_spans(t)) for t in files.values()),
                             default=0)
            if g > max_tokens:
                return False
            if self.store.was_expanded(conn, self.sid, best, "tokens", g):
                state["token_g"] = g * 2
                continue
            transforms = gen_transforms(files, "tokens", pins, g)
            self.store.record_expansion(conn, self.sid, best, "tokens", g)
            self.store.add_pending(conn, self.sid, best, transforms)
            state["token_g"] = g * 2
            self._save_state(conn, state)
            if transforms:
                return True
            continue  # nothing to try at this granularity

    def _save_state(self, conn, state):
        conn.execute("UPDATE sessions SET level_state=? WHERE id=?",
                     (json.dumps(state), self.sid))

    # -- status for the UI ---------------------------------------------------
    def status(self):
        session = self.session
        candidates = self.store.list_candidates(self.sid)
        diverged = [c for c in candidates if c["status"] == "unstable"]
        best = self.store.get_candidate(self.sid, session["best"]) \
            if session["best"] else None
        return {
            "id": session["id"],
            "status": session["status"],
            "epoch": session["epoch"],
            "best": best,
            "runs": self.store.run_count(self.sid),
            "candidates": len(candidates),
            "queued": self.store.pending_count(self.sid),
            "earliest_divergence": (
                {"candidate": diverged[0]["id"], "seq": diverged[0]["seq"],
                 "run": diverged[0]["diverged_run"]} if diverged else None),
        }
