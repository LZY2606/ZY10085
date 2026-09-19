"""Isolated local-process execution of a candidate.

Each run copies the candidate files into a fresh scratch directory, expands
the command template, and records exit status, stdout, stderr, timeout and a
summary of any generated artifacts.  The run fingerprint covers exactly
those fields, so wall-clock duration never leaks into results.
"""
import hashlib
import os
import shutil
import subprocess
import tempfile

from .util import hash_obj

ARTIFACT_CAP_BYTES = 1 << 20
ARTIFACT_CAP_COUNT = 64


def expand_command(command, files, workdir):
    args = []
    for part in command:
        if part == "{files}":
            args.extend(sorted(files))
        else:
            args.append(part.replace("{dir}", workdir))
    return args


def run_once(files, command, timeout, env_whitelist, scratch):
    """Run one evaluation; return a plain dict run record."""
    os.makedirs(scratch, exist_ok=True)
    workdir = tempfile.mkdtemp(dir=scratch)
    try:
        for name, content in sorted(files.items()):
            path = os.path.join(workdir, name)
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        env = {k: os.environ[k] for k in env_whitelist if k in os.environ}
        args = expand_command(command, files, workdir)
        timed_out = False
        try:
            proc = subprocess.run(
                args, cwd=workdir, env=env, capture_output=True,
                text=True, timeout=timeout)
            exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = None
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", "replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
        artifacts = {}
        for root, _dirs, names in os.walk(workdir):
            for name in sorted(names):
                rel = os.path.relpath(os.path.join(root, name), workdir)
                if rel in files or len(artifacts) >= ARTIFACT_CAP_COUNT:
                    continue
                with open(os.path.join(root, name), "rb") as fh:
                    data = fh.read(ARTIFACT_CAP_BYTES + 1)
                artifacts[rel] = {
                    "size": len(data),
                    "sha256": hashlib.sha256(data[:ARTIFACT_CAP_BYTES]).hexdigest(),
                    "truncated": len(data) > ARTIFACT_CAP_BYTES,
                }
        run = {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
            "artifacts": artifacts,
        }
        run["fingerprint"] = hash_obj({
            "exit_code": exit_code, "stdout": stdout, "stderr": stderr,
            "timed_out": timed_out, "artifacts": artifacts,
        })
        return run
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
