"""Часы сервера и оценка смещения часов клиента."""

from __future__ import annotations

import time

from .config import TICK_RATE


class ServerClock:
    """Монотонный тик сервера, выводимый из ``perf_counter``.

    В полноценном сервере источником тика остаётся ``engine_state``; этот класс
    нужен автономному сетевому ядру и тестам, а также для сверки.
    """

    __slots__ = ("tick_rate", "start_tick", "_origin", "_clock", "_tick")

    def __init__(
        self,
        tick_rate: int = TICK_RATE,
        *,
        start_tick: int = 1,
        clock=time.perf_counter,
    ):
        self.tick_rate = max(1, int(tick_rate))
        self.start_tick = int(start_tick)
        self._clock = clock
        self._origin = clock()
        self._tick = self.start_tick

    def now(self) -> float:
        return self._clock()

    def tick(self) -> int:
        candidate = self.start_tick + int(
            (self._clock() - self._origin) * self.tick_rate
        )
        if candidate > self._tick:
            self._tick = candidate
        return self._tick

    def seconds_for_tick(self, tick: int) -> float:
        return (int(tick) - self.start_tick) / self.tick_rate

    def tick_at(self, when: float) -> int:
        return self.start_tick + int((when - self._origin) * self.tick_rate)


class ClientClockSync:
    """Смещение часов и RTT по выборке ``PING/PONG``.

    Все метки — микросекунды.  ``offset_us`` — добавка к локальным часам,
    чтобы получить серверные.  Медиана защищает от единичного выброса.
    """

    __slots__ = ("tick_rate", "sample_limit", "offset_us", "rtt_us", "_offsets")

    def __init__(self, tick_rate: int = TICK_RATE, *, sample_limit: int = 8):
        self.tick_rate = max(1, int(tick_rate))
        self.sample_limit = max(1, int(sample_limit))
        self.offset_us = 0
        self.rtt_us = 0
        self._offsets: list[int] = []

    def add_sample(self, t0: int, t1: int, t2: int, t3: int) -> int:
        """``t0``/``t3`` — локальные, ``t1``/``t2`` — серверные метки."""

        rtt = (t3 - t0) - (t2 - t1)
        if rtt < 0:
            return self.offset_us
        offset = int(((t1 - t0) + (t2 - t3)) / 2)
        self._offsets.append(offset)
        if len(self._offsets) > self.sample_limit:
            del self._offsets[0]
        ordered = sorted(self._offsets)
        self.offset_us = ordered[len(ordered) // 2]
        self.rtt_us = rtt
        return self.offset_us

    def server_time_us(self, local_us: int) -> int:
        return int(local_us) + self.offset_us

    def estimate_tick(self, local_us: int, anchor_tick: int, anchor_local_us: int) -> int:
        """Оценка серверного тика от последнего известного якоря."""

        elapsed_us = int(local_us) - int(anchor_local_us)
        return int(anchor_tick) + int(elapsed_us * self.tick_rate / 1_000_000)
