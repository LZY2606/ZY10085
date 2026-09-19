"""Lightweight, deterministic source segmentation.

Two granularities are used by the reducer:
- ``top_level_spans``: blank-line separated paragraphs, extended so that
  braces balance inside a chunk (a poor-man's syntax-node layer).
- ``token_spans``: word/punctuation tokens for the token layer.
"""
import re

TOKEN_RE = re.compile(r"\w+|[^\w\s]")


def token_spans(text):
    return [(m.start(), m.end()) for m in TOKEN_RE.finditer(text)]


def top_level_spans(text):
    """Return [(start_line, end_line)) chunks of ``text``."""
    lines = text.splitlines(keepends=True)
    spans = []
    start = None
    depth = 0
    for i, line in enumerate(lines):
        if line.strip() == "":
            if start is not None and depth <= 0:
                spans.append((start, i))
                start = None
                depth = 0
            continue
        if start is None:
            start = i
            depth = 0
        depth += line.count("{") - line.count("}")
    if start is not None:
        spans.append((start, len(lines)))
    return spans


def chunk_ranges(n, g):
    """Partition range(n) into g contiguous chunks, deterministic sizes."""
    if g <= 0 or n <= 0:
        return []
    g = min(g, n)
    base, rem = divmod(n, g)
    spans = []
    i = 0
    for k in range(g):
        size = base + (1 if k < rem else 0)
        spans.append((i, i + size))
        i += size
    return spans
