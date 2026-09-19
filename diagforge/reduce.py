"""Multi-level reduction transforms: files, syntax nodes, tokens.

Transforms are plain dicts so they serialise deterministically into the
search tree.  Enumeration order is fully determined by the file contents
(sorted paths, left-to-right spans), never by dict iteration or time.

Pins constrain the search:
  - {"kind": "span", "path": p, "text": t}  -> file p must keep containing t
  - {"kind": "co_keep", "paths": [a, b, ...]} -> files must be kept together
"""

from __future__ import annotations

import re

TOKEN_RE = re.compile(r"\w+|[^\w\s]")


def top_level_spans(text: str):
    """Heuristic syntax nodes: top-level balanced-brace blocks (extended to
    the start of their statement) plus remaining top-level lines."""
    spans = []
    depth = 0
    block_start = None
    line_start = 0
    covered_lines = set()
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            if depth == 0:
                block_start = _statement_start(text, i)
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and block_start is not None:
                    end = i + 1
                    while end < n and text[end] in "; \t":
                        end += 1
                    if end < n and text[end] == "\n":
                        end += 1
                    spans.append((block_start, end))
                    block_start = None
        i += 1
    for (s, e) in spans:
        covered_lines.update(text.count("\n", 0, s) + k for k in range(text[s:e].count("\n") + 1))
    offset = 0
    for lineno, line in enumerate(text.splitlines(keepends=True)):
        stripped = line.strip()
        if stripped and lineno not in covered_lines:
            spans.append((offset, offset + len(line)))
        offset += len(line)
    spans.sort()
    return [(s, e) for (s, e) in spans if text[s:e].strip()]


def _statement_start(text: str, brace_index: int) -> int:
    start = text.rfind("\n\n", 0, brace_index)
    return 0 if start < 0 else start + 2


def token_spans(text: str):
    return [(m.start(), m.end()) for m in TOKEN_RE.finditer(text)]


def enumerate_transforms(files: dict):
    """Yield child transforms in a deterministic order: file level, then
    syntax-node level, then token level."""
    for path in sorted(files):
        yield {"kind": "remove_file", "path": path}
    for path in sorted(files):
        for (start, end) in top_level_spans(files[path]):
            yield {"kind": "remove_span", "path": path, "start": start, "end": end}
    for path in sorted(files):
        toks = token_spans(files[path])
        if len(toks) >= 4:
            half = len(toks) // 2
            yield {"kind": "remove_tokens", "path": path,
                   "start": toks[0][0], "end": toks[half - 1][1]}
            yield {"kind": "remove_tokens", "path": path,
                   "start": toks[half][0], "end": toks[-1][1]}


def apply_transform(files: dict, transform: dict):
    """Return a new file set with the transform applied, or None if the
    transform does not apply to this file set."""
    kind = transform.get("kind")
    files = dict(files)
    if kind == "remove_file":
        if transform["path"] not in files:
            return None
        del files[transform["path"]]
    elif kind in ("remove_span", "remove_tokens"):
        path = transform["path"]
        text = files.get(path)
        if text is None:
            return None
        start, end = transform["start"], transform["end"]
        if not (0 <= start <= end <= len(text)):
            return None
        new_text = text[:start] + text[end:]
        if new_text.strip():
            files[path] = new_text
        else:
            del files[path]
    else:
        return None
    return files if files else None


def violates_pins(files: dict, pins) -> bool:
    for pin in pins:
        if pin["kind"] == "span":
            path, text = pin["path"], pin["text"]
            if path not in files or text not in files[path]:
                return True
        elif pin["kind"] == "co_keep":
            present = [p in files for p in pin["paths"]]
            if any(present) and not all(present):
                return True
    return False
