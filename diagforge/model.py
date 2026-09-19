"""Content-addressed identifiers and deterministic hashing helpers.

Every id in the system is derived from the *content* of the inputs, never
from wall-clock time, randomness, or insertion order.  Replaying the same
inputs with the same RULES_VERSION therefore reproduces the same ids.
"""

from __future__ import annotations

import hashlib
import json

RULES_VERSION = "1"


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    return sha256_hex(text.encode("utf-8"))


def fileset_hash(files: dict) -> str:
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode("utf-8"))
        h.update(b"\x00")
        h.update(files[path].encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def fileset_size(files: dict) -> int:
    return sum(len(content.encode("utf-8")) for content in files.values())


def candidate_id(branch_id, parent_id, transform, files_hash) -> str:
    return hash_text(
        canonical_json(
            {
                "branch": branch_id,
                "parent": parent_id,
                "transform": transform,
                "files": files_hash,
                "rules": RULES_VERSION,
            }
        )
    )[:32]


def run_fingerprint(exit_code, stdout, stderr, timeout, artifacts_hash) -> str:
    return hash_text(
        canonical_json(
            {
                "exit": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "timeout": bool(timeout),
                "artifacts": artifacts_hash,
            }
        )
    )


def candidate_fingerprint(run_fingerprints) -> str:
    """Order-sensitive fingerprint of the full run sequence of a candidate."""
    return hash_text(canonical_json(list(run_fingerprints)))
