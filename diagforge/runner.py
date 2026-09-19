"""Isolated local-process execution of candidate file sets.

Each candidate is materialised into a fresh temporary directory, the compile
command template is rendered with ``{src}`` replaced by the sorted, quoted
file list, and the process runs with a timeout and a whitelisted environment.
The result records exit status, stdout, stderr, timeout flag and a hash
summary of any generated artifacts.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass


@dataclass
class RunResult:
    exit_code: object  # int, or None when the run timed out
    stdout: str
    stderr: str
    timeout: bool
    artifacts_hash: str


def render_command(command_template: str, files: dict) -> list:
    src = " ".join(shlex.quote(path) for path in sorted(files))
    return shlex.split(command_template.replace("{src}", src))


def _artifacts_hash(workdir: str, inputs: set) -> str:
    h = hashlib.sha256()
    for root, dirs, names in os.walk(workdir):
        dirs.sort()
        for name in sorted(names):
            rel = os.path.relpath(os.path.join(root, name), workdir)
            if rel in inputs:
                continue
            h.update(rel.encode("utf-8"))
            h.update(b"\x00")
            with open(os.path.join(root, name), "rb") as fh:
                h.update(fh.read())
            h.update(b"\x00")
    return h.hexdigest()


def run_candidate(files: dict, command_template: str, env_whitelist, timeout_s: float) -> RunResult:
    inputs = set(files)
    with tempfile.TemporaryDirectory(prefix="diagforge-") as workdir:
        for rel, content in sorted(files.items()):
            path = os.path.join(workdir, rel)
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        env = {k: os.environ[k] for k in env_whitelist if k in os.environ}
        argv = render_command(command_template, files)
        try:
            proc = subprocess.run(
                argv,
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            result = RunResult(
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                timeout=False,
                artifacts_hash=_artifacts_hash(workdir, inputs),
            )
        except subprocess.TimeoutExpired as exc:
            result = RunResult(
                exit_code=None,
                stdout=(exc.stdout or b"").decode("utf-8", "replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or ""),
                stderr=(exc.stderr or b"").decode("utf-8", "replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or ""),
                timeout=True,
                artifacts_hash=_artifacts_hash(workdir, inputs),
            )
        return result
