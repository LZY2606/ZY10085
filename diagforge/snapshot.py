"""Experiment snapshots: compiler binary fingerprint + whitelisted env.

Resuming a session re-captures the snapshot; if the fingerprint differs from
the active branch's snapshot, a new branch is required so results gathered
under different toolchains are never mixed.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from .model import canonical_json, hash_text, sha256_hex


def capture_snapshot(command_template: str, env_whitelist):
    argv0 = shlex.split(command_template)[0] if command_template.strip() else ""
    resolved = shutil.which(argv0) or argv0
    binary_hash = ""
    if resolved and os.path.isfile(resolved):
        with open(resolved, "rb") as fh:
            binary_hash = sha256_hex(fh.read())
    version = ""
    if resolved and os.path.isfile(resolved):
        try:
            proc = subprocess.run(
                [resolved, "--version"], capture_output=True, text=True, timeout=5
            )
            version = (proc.stdout + proc.stderr)[:4096]
        except Exception:
            version = ""
    env = {k: os.environ.get(k) for k in sorted(env_whitelist)}
    snapshot = {
        "binary": resolved,
        "binary_sha256": binary_hash,
        "version_output": version,
        "env": env,
    }
    return snapshot, hash_text(canonical_json(snapshot))
