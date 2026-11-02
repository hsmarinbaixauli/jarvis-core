from __future__ import annotations

from collections.abc import Sequence

from jarvis.supervisor import BACKOFF_S, supervise


class Fake:
    def __init__(self, codes: list[int], step: float = 1.0) -> None:
        self.codes = codes
        self.calls = 0
        self.sleeps: list[float] = []
        self.now = 0.0
        self.step = step

    def run(self, cmd: Sequence[str]) -> int:
        self.calls += 1
        self.now += self.step
        return self.codes.pop(0)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


def test_clean_exit_stops() -> None:
    fake = Fake([0])
    assert supervise(["x"], run=fake.run, sleep=fake.sleep, clock=fake.clock) == 0
    assert fake.calls == 1 and fake.sleeps == []


def test_restarts_with_backoff_then_recovers() -> None:
    fake = Fake([1, 1, 0])
    assert supervise(["x"], run=fake.run, sleep=fake.sleep, clock=fake.clock) == 0
    assert fake.sleeps == [BACKOFF_S[0], BACKOFF_S[1]]


def test_gives_up_and_reports() -> None:
    fake = Fake([3] * 10)
    reports: list[str] = []
    code = supervise(["x"], max_restarts=2, run=fake.run, sleep=fake.sleep, clock=fake.clock, on_give_up=reports.append)
    assert code == 3 and fake.calls == 3 and "3 veces" in reports[0]


def test_old_crashes_are_forgotten() -> None:
    fake = Fake([1, 1, 1, 0], step=4000)
    assert supervise(["x"], max_restarts=1, window_s=3600, run=fake.run, sleep=fake.sleep, clock=fake.clock) == 0
    assert fake.calls == 4
