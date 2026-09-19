"""Export a session as a self-contained, offline-replayable package.

The package contains the original sample, the minimal sample, the full
transform chain with per-step fingerprints, and ``replay.py`` — a stdlib-only
script that re-applies every transform and re-runs the predicate, verifying
each step still holds without importing diagforge.
"""
import json
import os

from . import RULES_VERSION

REPLAY_PY = '''#!/usr/bin/env python3
"""Offline replay for a diagforge export.  No third-party imports.

Usage: python3 replay.py
Reconstructs every chain step from original/ by applying the recorded
transforms, runs the command k_runs times per step, and checks that the
predicate holds stably and the run fingerprint matches the record.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_RE = re.compile(r"\\w+|[^\\w\\s]")


def token_spans(text):
    return [(m.start(), m.end()) for m in TOKEN_RE.finditer(text)]


def apply_transform(files, t):
    files = dict(files)
    kind = t["kind"]
    if kind == "remove_file":
        files.pop(t["file"], None)
    elif kind == "remove_files":
        for name in t["files"]:
            files.pop(name, None)
    elif kind == "remove_lines":
        lines = files[t["file"]].splitlines(keepends=True)
        del lines[t["start"]:t["end"]]
        files[t["file"]] = "".join(lines)
    elif kind == "remove_tokens":
        text = files[t["file"]]
        spans = token_spans(text)
        cut = spans[t["start"]:t["end"]]
        if cut:
            files[t["file"]] = text[:cut[0][0]] + text[cut[-1][1]:]
    else:
        raise ValueError("unknown transform %r" % kind)
    return files


def hash_obj(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def run_once(files, command, timeout):
    workdir = tempfile.mkdtemp()
    try:
        for name, content in sorted(files.items()):
            path = os.path.join(workdir, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(content)
        args = []
        for part in command:
            if part == "{files}":
                args.extend(sorted(files))
            else:
                args.append(part.replace("{dir}", workdir))
        timed_out = False
        try:
            proc = subprocess.run(args, cwd=workdir, capture_output=True,
                                  text=True, timeout=timeout)
            exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = None
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
        return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr,
                "timed_out": timed_out, "artifacts": {}}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_passes(spec, run):
    if run["timed_out"] and not spec.get("allow_timeout", False):
        return False
    if "exit_codes" in spec and run["exit_code"] not in spec["exit_codes"]:
        return False
    if spec.get("stderr_contains") and spec["stderr_contains"] not in run["stderr"]:
        return False
    if spec.get("stdout_contains") and spec["stdout_contains"] not in run["stdout"]:
        return False
    if spec.get("stderr_not_contains") and spec["stderr_not_contains"] in run["stderr"]:
        return False
    return True


def fingerprint(run):
    return hash_obj({k: run[k] for k in
                     ("exit_code", "stdout", "stderr", "timed_out", "artifacts")})


def main():
    with open(os.path.join(HERE, "chain.json")) as fh:
        chain = json.load(fh)
    with open(os.path.join(HERE, "command.json")) as fh:
        config = json.load(fh)
    command = config["command"]
    predicate = config["predicate"]
    k_runs = config["k_runs"]
    timeout = config["timeout"]
    files = {}
    orig = os.path.join(HERE, "original")
    for root, _dirs, names in os.walk(orig):
        for name in sorted(names):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, orig)
            with open(path) as fh:
                files[rel] = fh.read()
    failures = 0
    for i, step in enumerate(chain):
        if step["transform"] is not None:
            files = apply_transform(files, step["transform"])
        digest = hash_obj(sorted(files.items()))
        if digest != step["files_sha256"]:
            print("FAIL step %d: file set hash mismatch" % i)
            failures += 1
            continue
        runs = [run_once(files, command, timeout) for _ in range(k_runs)]
        fps = {fingerprint(r) for r in runs}
        stable = len(fps) == 1
        passed = stable and all(run_passes(predicate, r) for r in runs)
        if not passed:
            print("FAIL step %d (%s): predicate not stable/satisfied" %
                  (i, step["candidate"]))
            failures += 1
        else:
            print("OK   step %d (%s)" % (i, step["candidate"]))
    if failures:
        print("%d step(s) failed" % failures)
        return 1
    print("all %d step(s) verified" % len(chain))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def build_chain(store, sid):
    """Transform chain from root to the current best candidate."""
    session = store.get_session(sid)
    if not session["best"]:
        return []
    chain = []
    cid = session["best"]
    while cid is not None:
        cand = store.get_candidate(sid, cid)
        chain.append(cand)
        cid = cand["parent"]
    chain.reverse()
    from .util import hash_obj
    return [{
        "candidate": c["id"],
        "transform": c["transform"],
        "status": c["status"],
        "fingerprint": c["fingerprint"],
        "files_sha256": hash_obj(sorted(store.candidate_files(c["id"]).items())),
    } for c in chain]


def export_session(store, sid, out_dir):
    session = store.get_session(sid)
    os.makedirs(out_dir, exist_ok=True)
    original = store.original_files(sid)
    minimal = store.candidate_files(session["best"]) if session["best"] else {}
    for sub, files in (("original", original), ("minimal", minimal)):
        for name, content in sorted(files.items()):
            path = os.path.join(out_dir, sub, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
    chain = build_chain(store, sid)
    _write_json(os.path.join(out_dir, "chain.json"), chain)
    _write_json(os.path.join(out_dir, "command.json"), {
        "command": session["command"],
        "predicate": session["predicate"],
        "k_runs": session["k_runs"],
        "timeout": session["timeout"],
    })
    _write_json(os.path.join(out_dir, "manifest.json"), {
        "session": sid,
        "rules_version": RULES_VERSION,
        "epoch": session["epoch"],
        "snapshot": session["snapshot"],
        "original": _tree_id(original),
        "minimal": _tree_id(minimal),
        "chain_length": len(chain),
    })
    with open(os.path.join(out_dir, "replay.py"), "w", encoding="utf-8") as fh:
        fh.write(REPLAY_PY)
    return out_dir


def _tree_id(files):
    from .util import hash_obj
    return hash_obj(sorted(files.items()))


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")
