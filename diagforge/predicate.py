"""Predicate evaluation and stability classification.

A candidate is only *accepted* when all K runs produce the identical
fingerprint and every run satisfies the predicate.  Identical fingerprints
with a failing predicate mean *rejected*; differing fingerprints mean
*unstable* — a flaky candidate can never sneak in through one lucky run.
"""


def run_passes(spec, run):
    if run["timed_out"] and not spec.get("allow_timeout", False):
        return False
    if "exit_codes" in spec and run["exit_code"] not in spec["exit_codes"]:
        return False
    if spec.get("stderr_contains") and spec["stderr_contains"] not in run["stderr"]:
        return False
    if spec.get("stdout_contains") and spec["stdout_contains"] not in run["stdout"]:
        return False
    if spec.get("stderr_not_contains") and spec["stderr_not_contains"] in run["stderr"]:
        return False
    return True


def classify(spec, runs):
    """Return (status, fingerprint_or_None, diverged_run_or_None)."""
    fingerprints = [r["fingerprint"] for r in runs]
    if len(set(fingerprints)) != 1:
        first = fingerprints[0]
        diverged = next(i for i, fp in enumerate(fingerprints) if fp != first)
        return "unstable", None, diverged
    ok = all(run_passes(spec, r) for r in runs)
    return ("accepted" if ok else "rejected"), fingerprints[0], None
