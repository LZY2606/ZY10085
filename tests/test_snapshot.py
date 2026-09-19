import os
import stat

from diagforge.engine import Engine
from diagforge.snapshot import capture_snapshot
from diagforge.store import Store

from conftest import PRED, SAMPLE_FILES, make_runner


def _fake_compiler(tmp_path, body="#!/bin/sh\nexit 0\n"):
    path = tmp_path / "fakecc"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_binary_change_alters_fingerprint(tmp_path):
    cc = _fake_compiler(tmp_path)
    _, fp1 = capture_snapshot("%s {src}" % cc, ["PATH"])
    cc.write_text("#!/bin/sh\nexit 1\n")
    _, fp2 = capture_snapshot("%s {src}" % cc, ["PATH"])
    assert fp1 != fp2


def test_env_whitelist_change_alters_fingerprint(tmp_path, monkeypatch):
    cc = _fake_compiler(tmp_path)
    monkeypatch.setenv("DIAGFORGE_TOOLCHAIN", "clang-15")
    _, fp1 = capture_snapshot("%s {src}" % cc, ["DIAGFORGE_TOOLCHAIN"])
    monkeypatch.setenv("DIAGFORGE_TOOLCHAIN", "clang-16")
    _, fp2 = capture_snapshot("%s {src}" % cc, ["DIAGFORGE_TOOLCHAIN"])
    assert fp1 != fp2


def test_resume_requires_new_branch_on_fingerprint_change(tmp_path, monkeypatch):
    monkeypatch.setenv("DIAGFORGE_TOOLCHAIN", "v1")
    store = Store(":memory:")
    engine = Engine(store, runner=make_runner())
    sid, bid = engine.create_session("t", dict(SAMPLE_FILES), "true {src}",
                                     PRED, env_whitelist=("DIAGFORGE_TOOLCHAIN",))
    same, created = engine.resume_branch(sid)
    assert same == bid and not created
    monkeypatch.setenv("DIAGFORGE_TOOLCHAIN", "v2")
    new_bid, created = engine.resume_branch(sid)
    assert created and new_bid != bid
    branches = store.list_branches(sid)
    assert len(branches) == 2
    # the new branch is seeded with the original sample for re-evaluation
    root = store.root_candidate(new_bid)
    assert root is not None
    store.close()
