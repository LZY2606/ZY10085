"""Store invariants: decisions, undo, append-only history, atomicity."""
import pytest

from diagforge.engine import Engine

from conftest import make_files, make_session


def test_decisions_and_undo_are_append_only(store, session):
    engine = Engine(store, session)
    engine.start()
    root = engine.status()
    root_id = store.list_candidates(session)[0]["id"]

    seq = store.add_decision(session, "alice", "looks wrong", "note", {})
    assert seq == 1

    # force-reject the root, then undo: history grows, never shrinks
    from diagforge.server import Api
    api = Api(store)
    api.add_decision(session, {"operator": "alice", "reason": "testing",
                               "action": "reject_candidate",
                               "payload": {"candidate": root_id}})
    assert store.get_candidate(session, root_id)["status"] == "rejected"
    n_before = len(store.list_decisions(session))
    api.undo_decision(session, 2, {"operator": "bob", "reason": "mistake"})
    assert store.get_candidate(session, root_id)["status"] == "accepted"
    decisions = store.list_decisions(session)
    assert len(decisions) == n_before + 1
    assert decisions[-1]["action"] == "undo"
    assert decisions[-1]["payload"]["target_seq"] == 2
    # original decision rows untouched
    assert decisions[1]["action"] == "reject_candidate"


def test_decision_records_operator_reason_and_versions(store, session):
    from diagforge.server import Api
    engine = Engine(store, session)
    engine.start()
    root_id = store.list_candidates(session)[0]["id"]
    api = Api(store)
    api.add_decision(session, {"operator": "carol", "reason": "verify",
                               "action": "reject_candidate",
                               "payload": {"candidate": root_id}})
    d = store.list_decisions(session)[-1]
    assert d["operator"] == "carol" and d["reason"] == "verify"
    assert d["payload"]["before"] == {"status": "accepted"}
    assert d["payload"]["after"] == {"status": "rejected"}


def test_session_ids_are_content_addressed(store):
    files = make_files()
    sid1 = make_session(store, files)
    sid2 = make_session(store, files)
    assert sid1 == sid2
    assert len(store.list_sessions()) == 1


def test_pause_resume_cycle(store, session):
    engine = Engine(store, session)
    engine.start()
    assert engine.session["status"] == "running"
    engine.pause()
    assert engine.session["status"] == "paused"
    assert engine.step() is False  # paused engine does no work
    engine.resume()
    assert engine.session["status"] == "running"
    engine.run_to_completion()
    assert engine.session["status"] == "done"
