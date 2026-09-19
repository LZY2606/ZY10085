import json
import os
import subprocess
import sys

import pytest

from diagforge.engine import Engine
from diagforge.export import export_package, verify_package
from diagforge.predicate import Predicate
from diagforge.store import Store

from conftest import FAKECC

SMALL_FILES = {
    "main.c": (
        "int main(void) {\n"
        "    int trigger = 1;\n"
        "    return trigger;\n"
        "}\n"
    ),
    "note.h": "/* nothing needed here */\n",
}


@pytest.fixture(scope="module")
def finished():
    store = Store(":memory:")
    engine = Engine(store)  # real subprocess runner against fakecc.py
    command = "%s %s {src}" % (sys.executable, FAKECC)
    pred = Predicate(exit_codes=(42,), stderr_regex=r"internal compiler error",
                     runs=2)
    sid, bid = engine.create_session("export-test", dict(SMALL_FILES),
                                     command, pred, timeout_s=15.0)
    engine.run_until_idle(bid)
    yield store, sid
    store.close()


def test_export_package_replays_offline(tmp_path, finished):
    store, sid = finished
    out = str(tmp_path / "pkg")
    info = export_package(store, sid, out)
    assert info["steps"] >= 2
    assert os.path.isdir(os.path.join(out, "original"))
    assert os.path.isdir(os.path.join(out, "minimized"))
    with open(os.path.join(out, "chain.json"), encoding="utf-8") as fh:
        chain = json.load(fh)
    assert chain[0]["transform"]["kind"] == "root"
    assert all(step["fingerprint"] for step in chain)
    report = verify_package(out)
    assert report["ok"], report


def test_standalone_verify_script(tmp_path, finished):
    store, sid = finished
    out = str(tmp_path / "pkg")
    export_package(store, sid, out)
    proc = subprocess.run([sys.executable, os.path.join(out, "verify.py"), out],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "VERIFY OK" in proc.stdout


def test_tampered_chain_fails_verification(tmp_path, finished):
    store, sid = finished
    out = str(tmp_path / "pkg")
    export_package(store, sid, out)
    chain_path = os.path.join(out, "chain.json")
    with open(chain_path, encoding="utf-8") as fh:
        chain = json.load(fh)
    chain[-1]["files_hash"] = "0" * 64
    with open(chain_path, "w", encoding="utf-8") as fh:
        json.dump(chain, fh)
    report = verify_package(out)
    assert not report["ok"]
