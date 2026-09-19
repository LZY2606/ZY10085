"""Expected-predicate evaluation and stability judgement.

A candidate is *accepted* only when the predicate holds for every one of the
required runs.  Mixed outcomes are classified ``unstable`` and can never be
accepted on the strength of a single lucky pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Predicate:
    exit_codes: tuple = ()
    stdout_regex: str = ""
    stderr_regex: str = ""
    timeout: object = None
    runs: int = 3

    @staticmethod
    def from_dict(spec: dict) -> "Predicate":
        return Predicate(
            exit_codes=tuple(spec.get("exit_codes", ())),
            stdout_regex=spec.get("stdout_regex", ""),
            stderr_regex=spec.get("stderr_regex", ""),
            timeout=spec.get("timeout"),
            runs=int(spec.get("runs", 3)),
        )

    def to_dict(self) -> dict:
        return {
            "exit_codes": list(self.exit_codes),
            "stdout_regex": self.stdout_regex,
            "stderr_regex": self.stderr_regex,
            "timeout": self.timeout,
            "runs": self.runs,
        }

    def evaluate(self, result) -> bool:
        if self.exit_codes and result.exit_code not in self.exit_codes:
            return False
        if self.stdout_regex and not re.search(self.stdout_regex, result.stdout):
            return False
        if self.stderr_regex and not re.search(self.stderr_regex, result.stderr):
            return False
        if self.timeout is not None and bool(result.timeout) != bool(self.timeout):
            return False
        return True

    def judge(self, results) -> str:
        """Return 'pending', 'stable', 'rejected' or 'unstable'."""
        if len(results) < self.runs:
            return "pending"
        outcomes = [self.evaluate(r) for r in results[: self.runs]]
        if all(outcomes):
            return "stable"
        if not any(outcomes):
            return "rejected"
        return "unstable"
