import pytest

from diagforge.runner import RunResult

from conftest import SAMPLE_FILES


def test_failed_batch_leaves_no_partial_results(store, session):
    sid, bid = session
    before = store.count("candidates")
    with pytest.raises(RuntimeError):
        with store.batch(sid, label="doomed"):
            store.add_candidate(bid, None, {"kind": "remove_file", "path": "b.c"},
                                {"a.c": SAMPLE_FILES["a.c"]}, 3, commit=False)
            store.add_candidate(bid, None, {"kind": "remove_file", "path": "b.h"},
                                {"a.c": SAMPLE_FILES["a.c"],
                                 "b.c": SAMPLE_FILES["b.c"]}, 3, commit=False)
            raise RuntimeError("boom")
    assert store.count("candidates") == before
    assert store.count("batches") == 0


def test_successful_batch_commits_atomically(store, session):
    sid, bid = session
    before = store.count("candidates")
    with store.batch(sid, label="ok"):
        store.add_candidate(bid, None, {"kind": "remove_file", "path": "b.c"},
                            {"a.c": SAMPLE_FILES["a.c"],
                             "b.h": SAMPLE_FILES["b.h"]}, 3, commit=False)
    assert store.count("candidates") == before + 1
    assert store.count("batches") == 1


def test_candidate_dedup_by_content(store, session):
    sid, bid = session
    files = {"a.c": SAMPLE_FILES["a.c"]}
    first = store.add_candidate(bid, None, {"kind": "remove_file", "path": "b.c"},
                                files, 3)
    second = store.add_candidate(bid, None, {"kind": "remove_file", "path": "b.h"},
                                 files, 3)
    assert first == second


def test_claim_order_is_deterministic(store, session):
    sid, bid = session
    store.add_candidate(bid, None, {"kind": "remove_file", "path": "b.c"},
                        {"a.c": "aaaaaaaaaa"}, 3)
    store.add_candidate(bid, None, {"kind": "remove_file", "path": "b.h"},
                        {"a.c": "bb"}, 3)
    first = store.claim_next(bid)
    second = store.claim_next(bid)
    third = store.claim_next(bid)  # the session's root candidate
    assert first["size"] <= second["size"]
    assert second["size"] <= third["size"]
    assert store.claim_next(bid) is None


def test_record_run_fingerprint_stable(store, session):
    sid, bid = session
    cid = store.add_candidate(bid, None, {"kind": "root"}, dict(SAMPLE_FILES), 3)
    res = RunResult(42, "", "internal compiler error", False, "0" * 64)
    store.record_run(cid, res)
    store.record_run(cid, res)
    runs = store.runs_for(cid)
    assert [r["seq"] for r in runs] == [1, 2]
    assert runs[0]["fingerprint"] == runs[1]["fingerprint"]


def test_events_are_append_only_and_undo_restores(store, engine, session):
    sid, bid = session
    cid = store.add_candidate(bid, None, {"kind": "root"}, dict(SAMPLE_FILES), 3)
    eid = engine.decide(sid, "reject", "alice", "not a crash", cid)
    assert store.get_candidate(cid)["state"] == "rejected"
    uid = engine.undo_event(sid, eid, "bob", "wrong call")
    assert store.get_candidate(cid)["state"] == "pending"
    events = store.list_events(sid)
    assert [e["kind"] for e in events] == ["reject", "undo"]
    assert events[0]["id"] == eid and events[1]["id"] == uid
    assert events[1]["actor"] == "bob"
    # original event still present: history was not rewritten
    assert store.get_event(eid)["kind"] == "reject"
