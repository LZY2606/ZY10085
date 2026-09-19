from diagforge.engine import Engine
from diagforge.model import fileset_hash
from diagforge.store import Store

from conftest import PRED, SAMPLE_FILES, make_runner


def run_fresh(n_workers):
    store = Store(":memory:")
    engine = Engine(store, runner=make_runner())
    sid, bid = engine.create_session("t", dict(SAMPLE_FILES), "true {src}", PRED)
    engine.run_workers(bid, n_workers=n_workers)
    return store, engine, sid, bid


def test_minimizes_to_smaller_trigger_sample(engine, session):
    sid, bid = session
    engine.run_until_idle(bid)
    best = engine.store.best_candidate(bid)
    assert best is not None
    original = engine.store.root_candidate(bid)
    assert best["size"] < original["size"]
    files = engine.store.get_files(best["id"])
    assert "trigger" in "".join(files.values())


def test_worker_count_does_not_change_final_best():
    store1, _, _, bid1 = run_fresh(1)
    store3, _, _, bid3 = run_fresh(3)
    best1 = store1.best_candidate(bid1)
    best3 = store3.best_candidate(bid3)
    assert best1["files_hash"] == best3["files_hash"]
    accepted1 = {c["files_hash"] for c in store1.candidates_where(bid1, ("accepted",))}
    accepted3 = {c["files_hash"] for c in store3.candidates_where(bid3, ("accepted",))}
    assert accepted1 == accepted3
    store1.close()
    store3.close()


def test_unstable_candidate_is_not_accepted():
    files = dict(SAMPLE_FILES)
    target = {"a.c": files["a.c"]}
    flaky = fileset_hash(target)
    store = Store(":memory:")
    engine = Engine(store, runner=make_runner(flaky_hashes=(flaky,)))
    sid, bid = engine.create_session("t", files, "true {src}", PRED)
    engine.run_until_idle(bid)
    unstable = store.candidates_where(bid, ("unstable",))
    assert unstable, "expected the flaky candidate to be judged unstable"
    accepted = store.candidates_where(bid, ("accepted",))
    assert all(c["files_hash"] != flaky for c in accepted)
    store.close()


def test_pins_are_enforced_and_invalidate_only_affected(engine, session):
    sid, bid = session
    engine.run_until_idle(bid)
    store = engine.store
    pending_before = {c["id"] for c in store.queue(bid)}
    invalidated_before = {c["id"] for c in store.candidates_where(bid, ("invalidated",))}
    store.add_pin(sid, "co_keep", {"paths": ["b.c", "b.h"]})
    pins = store.active_pins(sid)
    from diagforge.reduce import violates_pins

    affected = [c["id"] for c in store.candidates_where(bid, ("pending", "running"))
                if violates_pins(store.get_files(c["id"]), pins)]
    store.invalidate(affected)
    invalidated = {c["id"] for c in store.candidates_where(bid, ("invalidated",))}
    assert invalidated - invalidated_before == set(affected)
    assert set(affected) <= pending_before
    remaining = {c["id"] for c in store.queue(bid)}
    assert remaining == pending_before - set(affected)


def test_search_tree_records_parent_transform_and_fingerprint(engine, session):
    sid, bid = session
    engine.run_until_idle(bid)
    store = engine.store
    best = store.best_candidate(bid)
    chain = []
    cur = best
    while cur:
        chain.append(cur)
        cur = store.get_candidate(cur["parent_id"]) if cur["parent_id"] else None
    chain.reverse()
    assert chain[0]["parent_id"] is None
    for parent, child in zip(chain, chain[1:]):
        assert child["parent_id"] == parent["id"]
        assert child["depth"] == parent["depth"] + 1
        runs = store.runs_for(child["id"])
        assert len(runs) == PRED.runs
        assert all(r["fingerprint"] for r in runs)


def test_earliest_divergence_is_deterministic(engine, session):
    sid, bid = session
    engine.run_until_idle(bid)
    store = engine.store
    div = store.earliest_divergence(bid)
    assert div is not None
    parent = store.get_candidate(div["parent_id"])
    assert parent["state"] == "accepted"
    assert div["state"] in ("rejected", "unstable")
