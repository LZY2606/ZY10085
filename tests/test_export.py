"""Export package: original + minimal + chain, offline replay."""
import json
import os
import subprocess
import sys

from diagforge.engine import Engine
from diagforge.export import build_chain, export_session


def test_export_and_offline_replay(store, session, tmp_path):
    engine = Engine(store, session)
    engine.start()
    engine.run_to_completion()
    out = str(tmp_path / "export")
    export_session(store, session, out)

    for name in ("original", "minimal", "chain.json", "command.json",
                 "manifest.json", "replay.py"):
        assert os.path.exists(os.path.join(out, name)), name

    with open(os.path.join(out, "manifest.json")) as fh:
        manifest = json.load(fh)
    assert manifest["chain_length"] >= 2

    chain = build_chain(store, session)
    assert chain[0]["transform"] is None
    assert all(step["status"] == "accepted" for step in chain)

    proc = subprocess.run([sys.executable, os.path.join(out, "replay.py")],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "verified" in proc.stdout


def test_minimal_dir_matches_best(store, session, tmp_path):
    engine = Engine(store, session)
    engine.start()
    engine.run_to_completion()
    out = str(tmp_path / "export2")
    export_session(store, session, out)
    best = engine.status()["best"]["id"]
    best_files = store.candidate_files(best)
    for name, content in best_files.items():
        with open(os.path.join(out, "minimal", name)) as fh:
            assert fh.read() == content
