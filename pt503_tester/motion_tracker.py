"""Software-side movement completion estimation from position feedback."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TrackerResult:
    status: str
    message: str = ""


@dataclass
class MotionTracker:
    """Estimate arrival because Pelco-D has no dedicated arrival response.

    In target mode, the requested axes must remain within ``tolerance`` for a
    number of complete samples.  In settle mode, used for presets whose target
    is unknown to the application, both axes merely have to stop changing.
    """

    label: str
    target_pan: float | None = None
    target_tilt: float | None = None
    tolerance: float = 0.2
    stable_samples: int = 3
    timeout_seconds: float = 30.0
    settle_mode: bool = False
    settle_grace_seconds: float = 1.0
    started_at: float = field(default_factory=time.perf_counter)
    current_pan: float | None = None
    current_tilt: float | None = None
    _updated_axes: set[str] = field(default_factory=set)
    _previous_pair: tuple[float, float] | None = None
    _stable_count: int = 0

    def update(self, axis: str, value: float, now: float | None = None) -> TrackerResult:
        now = time.perf_counter() if now is None else now
        if self.is_timed_out(now):
            return TrackerResult("timeout", self._timeout_message(now))

        if axis == "pan":
            self.current_pan = value % 360.0
        elif axis == "tilt":
            self.current_tilt = value
        else:
            raise ValueError(f"지원하지 않는 축입니다: {axis}")
        self._updated_axes.add(axis)

        required = {"pan", "tilt"} if self.settle_mode else self._required_axes()
        if not required or not required.issubset(self._updated_axes):
            return TrackerResult("tracking")
        self._updated_axes.difference_update(required)

        if self.settle_mode:
            return self._evaluate_settle(now)
        return self._evaluate_target(now)

    def check_timeout(self, now: float | None = None) -> TrackerResult:
        now = time.perf_counter() if now is None else now
        if self.is_timed_out(now):
            return TrackerResult("timeout", self._timeout_message(now))
        return TrackerResult("tracking")

    def is_timed_out(self, now: float) -> bool:
        return now - self.started_at >= self.timeout_seconds

    def elapsed(self, now: float | None = None) -> float:
        now = time.perf_counter() if now is None else now
        return max(0.0, now - self.started_at)

    def _required_axes(self) -> set[str]:
        axes: set[str] = set()
        if self.target_pan is not None:
            axes.add("pan")
        if self.target_tilt is not None:
            axes.add("tilt")
        return axes

    @staticmethod
    def pan_error(current: float, target: float) -> float:
        direct = abs((current % 360.0) - (target % 360.0))
        return min(direct, 360.0 - direct)

    def _evaluate_target(self, now: float) -> TrackerResult:
        within = True
        details: list[str] = []
        if self.target_pan is not None:
            if self.current_pan is None:
                return TrackerResult("tracking")
            error = self.pan_error(self.current_pan, self.target_pan)
            details.append(f"Pan 오차 {error:.2f}°")
            within = within and error <= self.tolerance
        if self.target_tilt is not None:
            if self.current_tilt is None:
                return TrackerResult("tracking")
            error = abs(self.current_tilt - self.target_tilt)
            details.append(f"Tilt 오차 {error:.2f}°")
            within = within and error <= self.tolerance

        self._stable_count = self._stable_count + 1 if within else 0
        if self._stable_count >= self.stable_samples:
            return TrackerResult(
                "completed",
                f"{self.label}: 목표 도달 추정 · {', '.join(details)} · "
                f"{self.elapsed(now):.2f}초",
            )
        return TrackerResult("tracking")

    def _evaluate_settle(self, now: float) -> TrackerResult:
        if self.current_pan is None or self.current_tilt is None:
            return TrackerResult("tracking")
        pair = (self.current_pan, self.current_tilt)
        if self.elapsed(now) < self.settle_grace_seconds or self._previous_pair is None:
            self._previous_pair = pair
            return TrackerResult("tracking")

        pan_delta = self.pan_error(pair[0], self._previous_pair[0])
        tilt_delta = abs(pair[1] - self._previous_pair[1])
        self._previous_pair = pair
        stable = pan_delta <= self.tolerance and tilt_delta <= self.tolerance
        self._stable_count = self._stable_count + 1 if stable else 0
        if self._stable_count >= self.stable_samples:
            return TrackerResult(
                "settled",
                f"{self.label}: 정지 추정 · 목표좌표를 몰라 도달 여부는 검증하지 못함 · "
                f"{self.elapsed(now):.2f}초",
            )
        return TrackerResult("tracking")

    def _timeout_message(self, now: float) -> str:
        return f"{self.label}: {self.elapsed(now):.1f}초 안에 완료를 확인하지 못했습니다."
