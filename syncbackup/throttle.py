"""效能控制：傳輸限速、CPU 使用率上限與即時速度量測。"""
from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Token bucket 限速器，最多累積 1 秒的額度。"""

    def __init__(self, bytes_per_sec: float = 0):
        self.rate = bytes_per_sec
        self._allowance = 0.0
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def set_rate(self, bytes_per_sec: float):
        with self._lock:
            self.rate = max(0.0, bytes_per_sec)
            self._allowance = 0.0
            self._last = time.monotonic()

    def consume(self, n: int):
        with self._lock:
            if self.rate <= 0:
                return
            now = time.monotonic()
            self._allowance = min(self.rate, self._allowance + (now - self._last) * self.rate)
            self._last = now
            self._allowance -= n
            wait = -self._allowance / self.rate if self._allowance < 0 else 0
        if wait > 0:
            time.sleep(wait)


class CpuGovernor:
    """限制「目前執行緒」的 CPU 使用率：用的 CPU 時間超過比例就讓出一段時間。"""

    WINDOW = 2.0

    def __init__(self):
        self.limit = 100
        self._reset()

    def _reset(self):
        self._t0 = time.monotonic()
        self._c0 = time.thread_time()

    def tick(self):
        if self.limit >= 100:
            return
        wall = time.monotonic() - self._t0
        if wall < 0.05:
            return
        cpu = time.thread_time() - self._c0
        target = cpu / (max(self.limit, 1) / 100)
        if target > wall:
            time.sleep(min(target - wall, 1.0))
        if wall > self.WINDOW:
            self._reset()


class SpeedMeter:
    def __init__(self, window: float = 3.0):
        self.window = window
        self._samples: deque = deque()
        self._lock = threading.Lock()

    def add(self, n: int):
        now = time.monotonic()
        with self._lock:
            self._samples.append((now, n))
            self._trim(now)

    def _trim(self, now):
        while self._samples and now - self._samples[0][0] > self.window:
            self._samples.popleft()

    def rate(self) -> float:
        now = time.monotonic()
        with self._lock:
            self._trim(now)
            return sum(n for _, n in self._samples) / self.window
