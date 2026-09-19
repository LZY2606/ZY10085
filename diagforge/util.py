"""Canonical serialization and content-addressed identifiers.

Everything that feeds an identifier or a fingerprint goes through
``canonical`` so results never depend on dict ordering or platform details.
"""
import hashlib
import json


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_obj(obj):
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def files_id(files):
    """Deterministic candidate id from the file set content."""
    return "cand_" + hash_obj(sorted(files.items()))[:16]


def files_size(files):
    """(file_count, total_bytes) ordering key; smaller is better."""
    return (len(files), sum(len(c.encode("utf-8")) for c in files.values()))
