"""Transforms, pin constraints, and candidate generation.

A transform is a plain JSON dict so it can be stored, replayed offline and
hashed.  ``apply_transform`` is the single source of truth; the exported
replay script embeds an identical copy.
"""
from .syntax import chunk_ranges, token_spans, top_level_spans

LEVELS = ("files", "nodes", "tokens")


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
        raise ValueError("unknown transform kind: %r" % kind)
    return files


def violates_pins(files, pins):
    """True if the file set breaks any active pin constraint."""
    for pin in pins:
        if not pin.get("active", 1):
            continue
        kind = pin["kind"]
        payload = pin["payload"]
        if kind == "keep_text":
            if payload["text"] not in files.get(payload["file"], ""):
                return True
        elif kind == "keep_file":
            if payload["file"] not in files:
                return True
        elif kind == "couple_files":
            present = [f in files for f in payload["files"]]
            if any(present) and not all(present):
                return True
    return False


def _coupling_groups(pins):
    groups = []
    for pin in pins:
        if pin.get("active", 1) and pin["kind"] == "couple_files":
            groups.append(list(pin["payload"]["files"]))
    return groups


def gen_transforms(files, level, pins, token_granularity=2):
    """Yield candidate transforms for ``files`` at ``level``.

    Transforms whose result would violate a pin or empty the whole sample
    are skipped here so the queue never contains useless work.
    """
    out = []
    if level == "files":
        grouped = set()
        for group in _coupling_groups(pins):
            present = [f for f in group if f in files]
            if len(present) >= 2 and len(files) > len(present):
                out.append({"kind": "remove_files", "files": sorted(present)})
                grouped.update(present)
        for name in sorted(files):
            if name in grouped or len(files) <= 1:
                continue
            out.append({"kind": "remove_file", "file": name})
    elif level == "nodes":
        for name in sorted(files):
            for start, end in top_level_spans(files[name]):
                out.append({"kind": "remove_lines", "file": name,
                            "start": start, "end": end})
    elif level == "tokens":
        for name in sorted(files):
            n = len(token_spans(files[name]))
            if n == 0 or token_granularity > n:
                continue
            for start, end in chunk_ranges(n, token_granularity):
                out.append({"kind": "remove_tokens", "file": name,
                            "start": start, "end": end})
    else:
        raise ValueError("unknown level %r" % level)

    kept = []
    for t in out:
        result = apply_transform(files, t)
        if not any(v.strip() for v in result.values()):
            continue
        if violates_pins(result, pins):
            continue
        kept.append(t)
    return kept


def level_of(transform):
    return {"remove_file": "files", "remove_files": "files",
            "remove_lines": "nodes", "remove_tokens": "tokens"}[transform["kind"]]
