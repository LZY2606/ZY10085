#!/usr/bin/env python3
"""Fake compiler used by the diagforge demo and the test-suite.

- Prints an ICE to stderr and exits 1 when any input contains TRIGGER.
- Includes the pid in the diagnostic when FLAKY is present, simulating a
  nondeterministic crash (used to exercise the unstable branch).
- Otherwise prints ok and exits 0.
"""
import os
import sys


def main(paths):
    text = ""
    for path in paths:
        with open(path, "r", errors="replace") as fh:
            text += fh.read()
    if "FLAKY" in text:
        print("fakecc: internal compiler error (pid=%d)" % os.getpid(),
              file=sys.stderr)
        return 1
    if "TRIGGER" in text:
        print("fakecc: internal compiler error: TRIGGER", file=sys.stderr)
        return 1
    print("fakecc: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
