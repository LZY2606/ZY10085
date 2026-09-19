import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagforge.snapshot import capture  # noqa: E402
from diagforge.store import Store  # noqa: E402

FAKECC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "diagforge", "demo", "fakecc.py")

PREDICATE = {"exit_codes": [1], "stderr_contains": "internal compiler error"}


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


def make_files(trigger=True, extra_files=2, filler_blocks=4):
    blocks = []
    for i in range(filler_blocks):
        blocks.append("static int filler_%d(int x) {\n    return x + %d;\n}\n"
                      % (i, i))
    main = "int main(void) {\n    // TRIGGER crash here\n    return 0;\n}\n" \
        if trigger else "int main(void) {\n    return 0;\n}\n"
    files = {"a.c": "\n".join(blocks) + "\n" + main}
    for i in range(extra_files):
        files["extra%d.c" % i] = (
            "int extra_%d(void) {\n    return %d;\n}\n" % (i, i))
    return files


def make_session(store, files, predicate=PREDICATE, k_runs=2, timeout=10.0,
                 command=None):
    command = command or [sys.executable, FAKECC, "{files}"]
    env_whitelist = ["PATH"]
    sid = store.create_session(
        files=files, command=command, predicate=predicate, k_runs=k_runs,
        timeout=timeout, env_whitelist=env_whitelist,
        snapshot=capture(command, env_whitelist))
    return sid


@pytest.fixture
def session(store):
    return make_session(store, make_files())
