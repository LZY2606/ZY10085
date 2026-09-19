import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from diagforge.runner import RunResult  # noqa: E402
from diagforge.store import Store  # noqa: E402
from diagforge.engine import Engine  # noqa: E402
from diagforge.predicate import Predicate  # noqa: E402

FAKECC = ROOT / "examples" / "fakecc.py"

SAMPLE_FILES = {
    "a.c": (
        "int helper(void) {\n"
        "    return 1;\n"
        "}\n"
        "\n"
        "int main(void) {\n"
        "    int trigger = 7;\n"
        "    return trigger;\n"
        "}\n"
    ),
    "b.c": (
        "int unused_alpha(void) {\n"
        "    return 0;\n"
        "}\n"
        "\n"
        "int unused_beta(void) {\n"
        "    return 1;\n"
        "}\n"
    ),
    "b.h": "int unused_alpha(void);\nint unused_beta(void);\n",
}

PRED = Predicate(exit_codes=(42,), stderr_regex=r"internal compiler error",
                 runs=3)


def make_runner(flaky_hashes=()):
    """Deterministic in-process runner: predicate holds iff any file contains
    the token 'trigger'.  ``flaky_hashes`` flips the verdict on the first run
    of the matching content to simulate an unstable candidate."""
    from diagforge.model import fileset_hash

    calls = {}

    def runner(files, command, env_whitelist, timeout_s):
        key = fileset_hash(files)
        seen = calls.get(key, 0)
        calls[key] = seen + 1
        holds = "trigger" in "".join(files[p] for p in sorted(files))
        if key in flaky_hashes and seen == 0:
            holds = not holds
        if holds:
            return RunResult(42, "", "internal compiler error: segv", False,
                             "0" * 64)
        return RunResult(0, "compilation ok", "", False, "0" * 64)

    return runner


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def engine(store):
    return Engine(store, runner=make_runner())


@pytest.fixture
def session(engine):
    return engine.create_session("t", dict(SAMPLE_FILES), "true {src}", PRED)
