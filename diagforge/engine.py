"""The minimisation engine: a pausable, deterministic search tree.

Every candidate records its parent, the transform that produced it and the
fingerprint of its run results.  Candidates are evaluated one at a time in
strict (size, id) priority order; workers only parallelise the repeated runs
*within* one candidate, and each repetition is stored under a pre-assigned
sequence number.  The final best candidate is therefore a pure function of
the inputs and the runner, independent of worker count or return order.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from .model import canonical_json, hash_text
from .predicate import Predicate
from .reduce import apply_transform, enumerate_transforms, violates_pins
from .runner import run_candidate
from .snapshot import capture_snapshot


class Engine:
    def __init__(self, store, runner=None):
        self.store = store
        self.runner = runner or run_candidate

    def create_session(self, name, files, command, predicate,
                       env_whitelist=("PATH",), timeout_s=10.0):
        snapshot, snap_fp = capture_snapshot(command, env_whitelist)
        sid = self.store.create_session(
            name, files, command, predicate.to_dict(), env_whitelist, timeout_s
        )
        bid = self.store.add_branch(sid, snapshot, snap_fp, label="base")
        self.store.add_candidate(bid, None, {"kind": "root"}, files, predicate.runs)
        return sid, bid

    def resume_branch(self, session_id):
        """Return (branch_id, created_new). A changed compiler/env fingerprint
        forces a new branch instead of mixing results."""
        sess = self.store.get_session(session_id)
        env_whitelist = json.loads(sess["env_whitelist"])
        snapshot, snap_fp = capture_snapshot(sess["command"], env_whitelist)
        branch = self.store.get_active_branch(session_id)
        if branch and branch["snapshot_fp"] == snap_fp:
            return branch["id"], False
        label = "resumed-%d" % (len(self.store.list_branches(session_id)) + 1)
        bid = self.store.add_branch(session_id, snapshot, snap_fp, label=label)
        root = self.store.root_candidate(branch["id"]) if branch else None
        if root:
            files = self.store.get_files(root["id"])
            pred = Predicate.from_dict(json.loads(sess["predicate"]))
            self.store.add_candidate(bid, None, {"kind": "root"}, files, pred.runs)
        return bid, True

    def step(self, branch_id, n_workers=1):
        cand = self.store.claim_next(branch_id)
        if cand is None:
            return False
        self._process(branch_id, cand, n_workers=n_workers)
        return True

    def _process(self, branch_id, cand, n_workers=1):
        row = self.store._one("SELECT session_id FROM branches WHERE id=?",
                              (branch_id,))
        sess = self.store.get_session(row["session_id"])
        pred = Predicate.from_dict(json.loads(sess["predicate"]))
        env_whitelist = json.loads(sess["env_whitelist"])
        files = self.store.get_files(cand["id"])
        done = len(self.store.runs_for(cand["id"]))
        remaining = max(0, pred.runs - done)
        if remaining:
            command, timeout_s = sess["command"], sess["timeout_s"]
            if n_workers > 1 and remaining > 1:
                # Repetitions get pre-assigned sequence numbers, so parallel
                # workers returning in any order store identical results.
                with ThreadPoolExecutor(max_workers=n_workers) as pool:
                    results = list(pool.map(
                        lambda _: self.runner(files, command, env_whitelist,
                                              timeout_s),
                        range(remaining)))
            else:
                results = [self.runner(files, command, env_whitelist,
                                       timeout_s) for _ in range(remaining)]
            for i, result in enumerate(results):
                self.store.record_run(cand["id"], result, seq=done + i + 1)
        verdict = pred.judge(self._run_results(cand["id"]))
        state = {"stable": "accepted", "rejected": "rejected",
                 "unstable": "unstable"}[verdict]
        self.store.set_state(cand["id"], state)
        if state == "accepted":
            self.store.invalidate_superseded(branch_id, cand["size"])
            self._spawn_children(branch_id, sess["id"], cand, files, pred.runs)

    def _run_results(self, candidate_id):
        from .runner import RunResult

        return [
            RunResult(exit_code=r["exit_code"], stdout=r["stdout"],
                      stderr=r["stderr"], timeout=bool(r["timeout"]),
                      artifacts_hash=r["artifacts_hash"])
            for r in self.store.runs_for(candidate_id)
        ]

    def _spawn_children(self, branch_id, session_id, cand, files, runs_required):
        pins = self.store.active_pins(session_id)
        with self.store.batch(session_id, label="spawn:%s" % cand["id"]):
            for transform in enumerate_transforms(files):
                child = apply_transform(files, transform)
                if child is None or violates_pins(child, pins):
                    continue
                self.store.add_candidate(branch_id, cand["id"], transform,
                                         child, runs_required, commit=False)

    def run_until_idle(self, branch_id, max_steps=None, n_workers=1):
        steps = 0
        while max_steps is None or steps < max_steps:
            if not self.step(branch_id, n_workers=n_workers):
                break
            steps += 1
        return steps

    def run_workers(self, branch_id, n_workers=1, stop=None):
        while not (stop and stop.is_set()):
            if not self.step(branch_id, n_workers=n_workers):
                return

    def decide(self, session_id, kind, actor, reason, candidate_id):
        """Human decision ('accept'/'reject') recorded with actor, reason and
        before/after fingerprints. Never rewrites history."""
        cand = self.store.get_candidate(candidate_id)
        if cand is None:
            raise KeyError("unknown candidate %s" % candidate_id)
        before = self._entity_fp(cand)
        new_state = "accepted" if kind == "accept" else "rejected"
        self.store.set_state(candidate_id, new_state)
        after = self._entity_fp(self.store.get_candidate(candidate_id))
        eid = self.store.add_event(
            session_id, kind, actor, reason, before, after,
            {"candidate": candidate_id, "before_state": cand["state"],
             "after_state": new_state})
        if kind == "accept":
            files = self.store.get_files(candidate_id)
            sess = self.store.get_session(session_id)
            pred = Predicate.from_dict(json.loads(sess["predicate"]))
            self._spawn_children(cand["branch_id"], session_id,
                                 self.store.get_candidate(candidate_id),
                                 files, pred.runs)
        return eid

    def undo_event(self, session_id, event_id, actor, reason):
        """Undo appends a compensating event; the original event stays."""
        ev = self.store.get_event(event_id)
        if ev is None:
            raise KeyError("unknown event %s" % event_id)
        payload = json.loads(ev["payload"])
        cand_id = payload.get("candidate")
        before_state = payload.get("before_state")
        if cand_id and before_state:
            self.store.set_state(cand_id, before_state)
        return self.store.add_event(
            session_id, "undo", actor, reason, ev["after_hash"],
            ev["before_hash"],
            {"undoes": event_id, "candidate": cand_id,
             "restored_state": before_state})

    def _entity_fp(self, cand):
        return hash_text(canonical_json(
            {"id": cand["id"], "state": cand["state"],
             "files": cand["files_hash"]}))
