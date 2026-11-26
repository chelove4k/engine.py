"""История предсказаний, сверка заявок и визуальные поправки."""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass

from .config import (
    CORRECTION_DECAY,
    HISTORY_TICKS,
    POSITION_TOLERANCE,
    ROTATION_TOLERANCE,
)
from .protocol import state_crc


@dataclass(slots=True)
class PredictionSample:
    tick: int
    seq: int
    x: float
    y: float
    rotation: float
    crc: int


class PredictionHistory:
    """Кольцо предсказанных состояний владельца (только его объекты)."""

    __slots__ = ("capacity", "_samples", "_order")

    def __init__(self, capacity: int = HISTORY_TICKS):
        self.capacity = max(2, int(capacity))
        self._samples: dict[int, PredictionSample] = {}
        self._order: deque[int] = deque()

    def __len__(self) -> int:
        return len(self._samples)

    def record(
        self,
        tick: int,
        seq: int,
        x: float,
        y: float,
        rotation: float,
    ) -> PredictionSample:
        tick = int(tick)
        sample = PredictionSample(
            tick=tick,
            seq=int(seq),
            x=float(x),
            y=float(y),
            rotation=float(rotation),
            crc=state_crc(x, y, rotation),
        )
        if self._order and tick <= self._order[-1]:
            # Повторная запись тика (пересчёт): заменяем без роста кольца.
            self._samples[tick] = sample
            return sample
        self._samples[tick] = sample
        self._order.append(tick)
        while len(self._order) > self.capacity:
            self._samples.pop(self._order.popleft(), None)
        return sample

    def get(self, tick: int) -> PredictionSample | None:
        return self._samples.get(int(tick))

    def trim(self, before_tick: int) -> None:
        before_tick = int(before_tick)
        while self._order and self._order[0] < before_tick:
            self._samples.pop(self._order.popleft(), None)

    def clear(self) -> None:
        self._samples.clear()
        self._order.clear()


@dataclass(slots=True)
class ClaimCheck:
    ok: bool
    error: float
    rotation_error: float

    def __bool__(self) -> bool:
        return self.ok


def check_claim(
    claim,
    x: float,
    y: float,
    rotation: float,
    *,
    position_tolerance: float = POSITION_TOLERANCE,
    rotation_tolerance: float = ROTATION_TOLERANCE,
) -> ClaimCheck:
    """Сверяет заявку клиента ``(render_id, x, y, rotation, crc)`` с истиной."""

    error = math.hypot(float(x) - claim[1], float(y) - claim[2])
    rotation_error = abs(
        (float(rotation) - claim[3] + 180.0) % 360.0 - 180.0
    )
    return ClaimCheck(
        ok=error <= position_tolerance
        and rotation_error <= rotation_tolerance,
        error=error,
        rotation_error=rotation_error,
    )


@dataclass(slots=True)
class _Offset:
    dx: float
    dy: float
    drot: float
    start: float
    duration: float


class CorrectionBuffer:
    """Визуальные поправки: рендер гасит их, симуляция их не видит."""

    __slots__ = ("decay", "_items")

    def __init__(self, decay: float = CORRECTION_DECAY):
        self.decay = max(1e-3, float(decay))
        self._items: dict[int, _Offset] = {}

    def __len__(self) -> int:
        return len(self._items)

    def add(
        self,
        render_id: int,
        dx: float,
        dy: float,
        drot: float = 0.0,
        *,
        now: float | None = None,
        duration: float | None = None,
    ) -> None:
        moment = time.monotonic() if now is None else now
        self._items[int(render_id)] = _Offset(
            float(dx),
            float(dy),
            float(drot),
            moment,
            max(1e-3, self.decay if duration is None else duration),
        )

    def sample(
        self, render_id: int, *, now: float | None = None
    ) -> tuple[float, float, float]:
        item = self._items.get(int(render_id))
        if item is None:
            return 0.0, 0.0, 0.0
        moment = time.monotonic() if now is None else now
        progress = (moment - item.start) / item.duration
        if progress >= 1.0:
            self._items.pop(int(render_id), None)
            return 0.0, 0.0, 0.0
        if progress < 0.0:
            progress = 0.0
        factor = 1.0 - progress
        return item.dx * factor, item.dy * factor, item.drot * factor

    def sample_all(
        self, *, now: float | None = None
    ) -> dict[int, tuple[float, float, float]]:
        """Снимает все активные поправки и удаляет погасшие."""

        moment = time.monotonic() if now is None else now
        samples = {}
        for render_id in tuple(self._items):
            offset = self.sample(render_id, now=moment)
            if offset != (0.0, 0.0, 0.0):
                samples[render_id] = offset
        return samples

    def clear(self) -> None:
        self._items.clear()
