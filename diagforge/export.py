"""Export packages and offline re-verification.

A package contains the original sample, the minimised sample, the full
transform chain (with per-candidate run fingerprints) and a standalone
``verify.py`` that replays every step and re-checks the predicate without
importing diagforge.
"""

from __future__ import annotations

import json
import os
import shutil

from .model import RULES_VERSION, candidate_fingerprint, fileset_hash
from .predicate import Predicate
from .reduce import apply_transform
from .runner import run_candidate

VERIFY_PY = r'''#!/usr/bin/env python3
"""Standalone offline verifier for a diagforge export package.

Replays the recorded transform chain from original/ and re-runs the compile
command for every accepted step, checking the predicate the recorded number
of times. Exits non-zero if any step fails.
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile


def fileset_hash(files):
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode("utf-8"))
        h.update(b"\x00")
        h.update(files[path].encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def apply_transform(files, transform):
    kind = transform.get("kind")
    files = dict(files)
    if kind == "root":
        return files
    if kind == "remove_file":
        files.pop(transform["path"], None)
    elif kind in ("remove_span", "remove_tokens"):
        path = transform["path"]
        text = files.get(path)
        if text is None:
            return None
        new_text = text[: transform["start"]] + text[transform["end"]:]
        if new_text.strip():
            files[path] = new_text
        else:
            del files[path]
    else:
        return None
    return files if files else None


def run_once(files, command, env_whitelist, timeout_s):
    with tempfile.TemporaryDirectory(prefix="diagforge-verify-") as workdir:
        for rel, content in sorted(files.items()):
            path = os.path.join(workdir, rel)
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        src = " ".join(shlex.quote(p) for p in sorted(files))
        argv = shlex.split(command.replace("{src}", src))
        env = {k: os.environ[k] for k in env_whitelist if k in os.environ}
        try:
            proc = subprocess.run(argv, cwd=workdir, env=env,
                                  capture_output=True, text=True,
                                  timeout=timeout_s)
            return proc.returncode, proc.stdout, proc.stderr, False
        except subprocess.TimeoutExpired:
            return None, "", "", True


def predicate_holds(pred, outcome):
    exit_code, stdout, stderr, timed_out = outcome
    if pred.get("exit_codes") and exit_code not in pred["exit_codes"]:
        return False
    if pred.get("stdout_regex") and not re.search(pred["stdout_regex"], stdout):
        return False
    if pred.get("stderr_regex") and not re.search(pred["stderr_regex"], stderr):
        return False
    if pred.get("timeout") is not None and timed_out != bool(pred["timeout"]):
        return False
    return True


def main(pkg_dir):
    with open(os.path.join(pkg_dir, "session.json"), encoding="utf-8") as fh:
        session = json.load(fh)
    with open(os.path.join(pkg_dir, "chain.json"), encoding="utf-8") as fh:
        chain = json.load(fh)
    files = {}
    orig = os.path.join(pkg_dir, "original")
    for name in sorted(os.listdir(orig)):
        with open(os.path.join(orig, name), encoding="utf-8") as fh:
            files[name] = fh.read()
    pred = session["predicate"]
    runs = int(pred.get("runs", 3))
    ok = True
    for i, step in enumerate(chain):
        if i > 0:
            files = apply_transform(files, step["transform"])
            if files is None:
                print("FAIL step %d: transform does not apply" % i)
                ok = False
                break
        hash_ok = fileset_hash(files) == step["files_hash"]
        outcomes = [run_once(files, session["command"],
                             session["env_whitelist"], session["timeout_s"])
                    for _ in range(runs)]
        stable = all(predicate_holds(pred, o) for o in outcomes)
        status = hash_ok and stable
        if not status:
            ok = False
        print("step %d %s hash_ok=%s predicate_stable=%s size=%d"
              % (i, step["id"], hash_ok, stable, step["size"]))
    print("VERIFY %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
'''


def _write_files(directory, files):
    os.makedirs(directory, exist_ok=True)
    for path in sorted(files):
        with open(os.path.join(directory, path), "w", encoding="utf-8") as fh:
            fh.write(files[path])


def export_package(store, session_id, out_dir):
    """Build the package in a temp sibling and rename it into place, so a
    failed export never leaves a visible partial package."""
    sess = store.get_session(session_id)
    if sess is None:
        raise KeyError("unknown session %s" % session_id)
    branch = store.get_active_branch(session_id)
    best = store.best_candidate(branch["id"])
    root = store.root_candidate(branch["id"])
    if best is None or root is None:
        raise ValueError("session has no accepted candidate to export")

    chain = []
    cur = best
    while cur is not None:
        runs = store.runs_for(cur["id"])
        chain.append({
            "id": cur["id"],
            "transform": json.loads(cur["transform"]),
            "files_hash": cur["files_hash"],
            "size": cur["size"],
            "depth": cur["depth"],
            "state": cur["state"],
            "fingerprint": candidate_fingerprint([r["fingerprint"] for r in runs]),
            "runs": [{"exit_code": r["exit_code"], "timeout": bool(r["timeout"]),
                      "fingerprint": r["fingerprint"]} for r in runs],
        })
        cur = store.get_candidate(cur["parent_id"]) if cur["parent_id"] else None
    chain.reverse()

    session_meta = {
        "name": sess["name"],
        "command": sess["command"],
        "predicate": json.loads(sess["predicate"]),
        "env_whitelist": json.loads(sess["env_whitelist"]),
        "timeout_s": sess["timeout_s"],
        "rules_version": RULES_VERSION,
        "snapshot": json.loads(branch["snapshot"]),
    }

    tmp_dir = out_dir.rstrip("/") + ".tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    try:
        os.makedirs(tmp_dir)
        _write_files(os.path.join(tmp_dir, "original"), store.get_files(root["id"]))
        _write_files(os.path.join(tmp_dir, "minimized"), store.get_files(best["id"]))
        with open(os.path.join(tmp_dir, "chain.json"), "w", encoding="utf-8") as fh:
            json.dump(chain, fh, indent=2, sort_keys=True)
        with open(os.path.join(tmp_dir, "session.json"), "w", encoding="utf-8") as fh:
            json.dump(session_meta, fh, indent=2, sort_keys=True)
        with open(os.path.join(tmp_dir, "verify.py"), "w", encoding="utf-8") as fh:
            fh.write(VERIFY_PY)
        shutil.rmtree(out_dir, ignore_errors=True)
        os.replace(tmp_dir, out_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return {"out_dir": out_dir, "steps": len(chain), "best_size": best["size"]}


def verify_package(pkg_dir, runner=None):
    """In-process re-verification used by tests and the verify CLI. Mirrors
    the standalone verify.py logic."""
    runner = runner or run_candidate
    with open(os.path.join(pkg_dir, "session.json"), encoding="utf-8") as fh:
        session = json.load(fh)
    with open(os.path.join(pkg_dir, "chain.json"), encoding="utf-8") as fh:
        chain = json.load(fh)
    files = {}
    for name in sorted(os.listdir(os.path.join(pkg_dir, "original"))):
        with open(os.path.join(pkg_dir, "original", name), encoding="utf-8") as fh:
            files[name] = fh.read()
    pred = Predicate.from_dict(session["predicate"])
    steps = []
    ok = True
    for i, step in enumerate(chain):
        if i > 0:
            files = apply_transform(files, step["transform"])
            if files is None:
                steps.append({"id": step["id"], "hash_ok": False,
                              "predicate_stable": False})
                ok = False
                break
        hash_ok = fileset_hash(files) == step["files_hash"]
        results = [runner(files, session["command"], session["env_whitelist"],
                          session["timeout_s"]) for _ in range(pred.runs)]
        stable = pred.judge(results) == "stable"
        steps.append({"id": step["id"], "hash_ok": hash_ok,
                      "predicate_stable": stable})
        ok = ok and hash_ok and stable
    return {"ok": ok, "steps": steps}
