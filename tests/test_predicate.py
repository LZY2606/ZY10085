from diagforge.predicate import Predicate
from diagforge.runner import RunResult


def res(exit_code=42, stdout="", stderr="internal compiler error", timeout=False):
    return RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr,
                     timeout=timeout, artifacts_hash="x")


def test_evaluate_exit_code_and_regex():
    p = Predicate(exit_codes=(42,), stderr_regex=r"internal compiler error")
    assert p.evaluate(res())
    assert not p.evaluate(res(exit_code=0))
    assert not p.evaluate(res(stderr="all good"))


def test_evaluate_timeout_flag():
    p = Predicate(timeout=True)
    assert p.evaluate(res(timeout=True))
    assert not p.evaluate(res(timeout=False))


def test_judge_requires_all_runs():
    p = Predicate(exit_codes=(42,), runs=3)
    good = [res(), res(), res()]
    assert p.judge(good) == "stable"
    assert p.judge(good[:2]) == "pending"
    assert p.judge([res(exit_code=0)] * 3) == "rejected"


def test_flaky_result_is_unstable_not_accepted():
    p = Predicate(exit_codes=(42,), runs=3)
    mixed = [res(), res(exit_code=0), res()]
    assert p.judge(mixed) == "unstable"
    flipped = [res(exit_code=0), res(), res()]
    assert p.judge(flipped) == "unstable"
