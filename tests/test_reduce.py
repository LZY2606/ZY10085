from diagforge.reduce import (
    apply_transform,
    enumerate_transforms,
    top_level_spans,
    violates_pins,
)

from conftest import SAMPLE_FILES


def test_top_level_spans_find_functions():
    text = SAMPLE_FILES["b.c"]
    spans = top_level_spans(text)
    bodies = [text[s:e] for s, e in spans]
    assert any("unused_alpha" in b for b in bodies)
    assert any("unused_beta" in b for b in bodies)


def test_enumerate_transforms_is_deterministic():
    first = list(enumerate_transforms(SAMPLE_FILES))
    second = list(enumerate_transforms(SAMPLE_FILES))
    assert first == second
    kinds = [t["kind"] for t in first]
    assert "remove_file" in kinds and "remove_span" in kinds
    assert "remove_tokens" in kinds
    # file level comes before span level which comes before token level
    assert kinds == sorted(kinds, key=["remove_file", "remove_span",
                                       "remove_tokens"].index)


def test_apply_remove_file_and_span():
    files = apply_transform(SAMPLE_FILES, {"kind": "remove_file", "path": "b.c"})
    assert "b.c" not in files and "a.c" in files
    text = SAMPLE_FILES["b.c"]
    start, end = top_level_spans(text)[0]
    shrunk = apply_transform({"b.c": text},
                             {"kind": "remove_span", "path": "b.c",
                              "start": start, "end": end})
    assert len(shrunk["b.c"]) < len(text)


def test_apply_transform_rejects_out_of_range():
    assert apply_transform({"a.c": "x"},
                           {"kind": "remove_span", "path": "a.c",
                            "start": 0, "end": 99}) is None
    assert apply_transform({"a.c": "x"},
                           {"kind": "remove_file", "path": "zz"}) is None


def test_span_pin_blocks_removal():
    pins = [{"kind": "span", "path": "a.c", "text": "trigger"}]
    assert violates_pins({"a.c": "int x;\n"}, pins)
    assert not violates_pins({"a.c": "int trigger;\n"}, pins)
    # pinned file deleted entirely also violates
    assert violates_pins({"b.c": "int y;\n"}, pins)


def test_co_keep_pin_requires_files_together():
    pins = [{"kind": "co_keep", "paths": ["b.c", "b.h"]}]
    assert violates_pins({"b.c": "x"}, pins)
    assert violates_pins({"b.h": "x"}, pins)
    assert not violates_pins({"b.c": "x", "b.h": "y"}, pins)
    assert not violates_pins({"a.c": "x"}, pins)
