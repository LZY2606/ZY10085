"""Experiment snapshot: compiler binary fingerprint + env whitelist.

The snapshot is captured when a session is created and re-checked whenever
a paused session is resumed.  Any change forces a new branch (epoch) so old
results are never silently reused under a different compiler.
"""
import hashlib
import os
import shutil

from . import RULES_VERSION
from .util import hash_obj


def capture(command, env_whitelist):
    exe = command[0] if command else ""
    resolved = shutil.which(exe) or (exe if os.path.isfile(exe) else None)
    digest = None
    if resolved and os.path.isfile(resolved):
        h = hashlib.sha256()
        with open(resolved, "rb") as fh:
            for block in iter(lambda: fh.read(65536), b""):
                h.update(block)
        digest = h.hexdigest()
    return {
        "exe": resolved,
        "exe_sha256": digest,
        "env": {k: os.environ.get(k) for k in sorted(env_whitelist)},
        "rules_version": RULES_VERSION,
    }


def fingerprint(snapshot):
    return hash_obj(snapshot)
