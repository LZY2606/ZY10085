"""Engine behaviour: reduction, determinism, stability, pins, atomicity."""
import os
import stat
import sys

import pytest

from diagforge.engine import Engine, SnapshotChanged
from diagforge.snapshot import capture
from diagforge.util import files_id

from conftest import FAKECC, PREDICATE, make_files, make_session


def run_all(engine):
    engine.start()
    return engine.run_to_completion()


def test_reduction_finds_minimal_trigger(store, session):
    engine = Engine(store, session)
    run_all(engine)
    status = engine.status()
    assert status["status"] == "done"
    best = store.candidate_files(status["best"]["id"])
    assert len(best) == 1
    content = next(iter(best.values()))
    assert "TRIGGER" in content
    # node/token levels must shrink past the file level (regression: an
    # empty expansion at one level must not terminate the search early)
    assert "filler" not in content
    assert len(content) < 80
    # every surviving candidate along the way satisfied the predicate stably
    for cand in store.list_candidates(session):
        if cand["status"] == "accepted":
            runs = store.get_runs(cand["id"])
            assert len({r["fingerprint"] for r in runs}) == 1


def test_determinism_across_runs(tmp_path):
    results = []
    from diagforge.store import Store
    for i in range(2):
        store = Store(str(tmp_path / ("det%d.db" % i)))
        sid = make_session(store, make_files())
        engine = Engine(store, sid)
        run_all(engine)
        st = engine.status()
        events = [(e["kind"], e["payload"]) for e in store.list_events(sid)]
        results.append((st["best"]["id"], st["runs"], events))
    assert results[0] == results[1]


def test_flaky_candidate_goes_unstable(store):
    files = {"a.c": "int x; // FLAKY TRIGGER\n",
             "b.c": "int y;\n"}
    sid = make_session(store, files)
    engine = Engine(store, sid)
    engine.start()
    assert engine.session["status"] == "root_unstable"
    accepted = [c for c in store.list_candidates(sid)
                if c["status"] == "accepted"]
    assert accepted == []
    status = engine.status()
    assert status["earliest_divergence"] is not None


def test_root_rejected_when_predicate_fails(store):
    sid = make_session(store, make_files(trigger=False))
    engine = Engine(store, sid)
    engine.start()
    assert engine.session["status"] == "root_rejected"


def test_keep_text_pin(store):
    files = make_files()
    files["a.c"] = "int keep_me;\n" + files["a.c"]
    sid = make_session(store, files)
    store.add_pin(sid, "keep_text", {"file": "a.c", "text": "keep_me"})
    engine = Engine(store, sid)
    run_all(engine)
    best = store.candidate_files(engine.status()["best"]["id"])
    assert "keep_me" in best.get("a.c", "")


def test_couple_files_pin(store):
    files = make_files()
    sid = make_session(store, files)
    store.add_pin(sid, "couple_files", {"files": ["extra0.c", "extra1.c"]})
    engine = Engine(store, sid)
    run_all(engine)
    for cand in store.list_candidates(sid):
        if cand["status"] == "invalidated":
            continue
        cfiles = store.candidate_files(cand["id"])
        present = [f in cfiles for f in ("extra0.c", "extra1.c")]
        assert all(present) or not any(present)


def test_pin_invalidates_only_affected(store, session):
    engine = Engine(store, session)
    engine.start()
    engine.run_to_completion(max_steps=6)
    before = {c["id"]: c["status"] for c in store.list_candidates(session)}
    store.add_pin(session, "keep_text",
                  {"file": "a.c", "text": "TRIGGER"})
    after = {c["id"]: c["status"] for c in store.list_candidates(session)}
    invalidated = [cid for cid, st in after.items()
                   if st == "invalidated" and before[cid] != "invalidated"]
    for cid in invalidated:
        assert "TRIGGER" not in store.candidate_files(cid).get("a.c", "")


def test_failed_batch_leaves_no_partial_results(store, session, monkeypatch):
    calls = {"n": 0}

    def flaky_runner(files, command, timeout, env, scratch):
        calls["n"] += 1
        if calls["n"] == 3:  # second run of the first queued candidate
            raise RuntimeError("boom")
        from diagforge.runner import run_once
        return run_once(files, command, timeout, env, scratch)

    engine = Engine(store, session, runner=flaky_runner)
    engine.start()
    with pytest.raises(RuntimeError):
        engine.step()
    cands = [c for c in store.list_candidates(session)
             if c["parent"] is not None]
    assert cands == []  # no partial candidate/run rows survived the rollback
    engine.run_to_completion()  # engine recovers and finishes cleanly
    assert engine.session["status"] == "done"


def test_snapshot_change_requires_new_branch(store, tmp_path):
    exe = tmp_path / "fakecc.py"
    with open(FAKECC) as fh:
        exe.write_text(fh.read())
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    command = [str(exe), "{files}"]
    sid = make_session(store, make_files(), command=command)
    engine = Engine(store, sid)
    engine.start()
    engine.pause()
    with open(exe, "a") as fh:
        fh.write("\n# touched\n")
    with pytest.raises(SnapshotChanged):
        engine.resume()
    engine.resume(allow_new_branch=True)
    assert engine.session["epoch"] == 2
    kinds = [e["kind"] for e in store.list_events(sid)]
    assert "new_branch" in kinds


def test_worker_order_independence(tmp_path):
    """Same runs submitted in different orders give the same best."""
    from diagforge.predicate import classify
    from diagforge.runner import run_once
    from diagforge.store import Store
    from diagforge.util import files_id

    outcomes = []
    for order in ([0, 1, 2], [2, 0, 1]):
        store = Store(str(tmp_path / ("w%d.db" % order[0])))
        sid = make_session(store, make_files())
        files = store.original_files(sid)
        session = store.get_session(sid)
        # evaluate three candidates: root, minus extra0, minus both extras
        variants = [
            files,
            {k: v for k, v in files.items() if k != "extra0.c"},
            {k: v for k, v in files.items() if "extra" not in k},
        ]
        with store.tx() as conn:
            for i, variant in enumerate(variants):
                cid = files_id(variant)
                store.put_candidate(conn, sid, cid, None, None, variant, 1, i + 1)
        for idx in order:
            variant = variants[idx]
            cid = files_id(variant)
            runs = [run_once(variant, session["command"], session["timeout"],
                             session["env_whitelist"], store.scratch)
                    for _ in range(session["k_runs"])]
            with store.tx() as conn:
                for i, run in enumerate(runs):
                    store.put_run(conn, cid, i, run)
                status, fp, div = classify(session["predicate"], runs)
                store.finalize_candidate(conn, sid, cid, status, fp, div)
                best = store.recompute_best(conn, sid)
        outcomes.append(best)
    assert outcomes[0] == outcomes[1]
    best_files = store.candidate_files(outcomes[0])
    assert "extra0.c" not in best_files and "extra1.c" not in best_files
