#!/usr/bin/env python3
"""Fake compiler used by the demo and the test-suite.

Exits 42 with an "internal compiler error" on stderr whenever any input file
contains the token ``trigger``; otherwise prints a success line and exits 0.
"""
import sys


def main(argv):
    content = ""
    for path in argv:
        with open(path, encoding="utf-8", errors="replace") as fh:
            content += fh.read()
    if "trigger" in content:
        sys.stderr.write("internal compiler error: segmentation fault in pass 'lower'\n")
        return 42
    sys.stdout.write("compilation ok (%d files)\n" % len(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
