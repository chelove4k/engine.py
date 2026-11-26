"""Транспорт в памяти: тесты и отладка без сокетов.

Поддерживает задержку, джиттер и потери — этого достаточно, чтобы проверять
предсказание и коррекции в детерминированных тестах и на одной машине.
"""

from __future__ import annotations

import heapq
import random
import time

from .config import MAX_FRAME_BYTES, MAX_PENDING_BYTES
from .transport import ConnectionClosed, FrameTooLarge


class LoopbackNetwork:
    """Фабрика пар соединений с общей настройкой канала."""

    def __init__(
        self,
        *,
        latency: float = 0.0,
        jitter: float = 0.0,
        loss: float = 0.0,
        seed: int | None = None,
        max_frame: int = MAX_FRAME_BYTES,
        pending_limit: int = MAX_PENDING_BYTES,
    ):
        if latency < 0 or jitter < 0:
            raise ValueError("Задержка и джиттер не могут быть отрицательными")
        if not 0.0 <= loss <= 1.0:
            raise ValueError("Потери должны быть в диапазоне 0..1")
        self.latency = float(latency)
        self.jitter = float(jitter)
        self.loss = float(loss)
        self.rng = random.Random(seed)
        self.max_frame = max_frame
        self.pending_limit = pending_limit

    def pair(self) -> tuple["LoopbackConnection", "LoopbackConnection"]:
        left = LoopbackConnection(self)
        right = LoopbackConnection(self)
        left._peer = right
        right._peer = left
        return left, right


class LoopbackConnection:
    __slots__ = (
        "_network",
        "_peer",
        "_inbox",
        "_seq",
        "closed",
        "bytes_in",
        "bytes_out",
        "dropped",
        "sent",
        "max_frame",
        "pending_limit",
    )

    def __init__(self, network: LoopbackNetwork):
        self._network = network
        self._peer: LoopbackConnection | None = None
        self._inbox: list[tuple[float, int, bytes]] = []
        self._seq = 0
        self.closed = False
        self.bytes_in = 0
        self.bytes_out = 0
        self.dropped = 0
        self.sent = 0
        self.max_frame = network.max_frame
        self.pending_limit = network.pending_limit

    @property
    def pending_bytes(self) -> int:
        return 0

    @property
    def pending_input(self) -> bool:
        return bool(self._inbox)

    def send_payload(self, payload, *, droppable: bool = False) -> bool:
        if self.closed:
            raise ConnectionClosed("Соединение закрыто")
        if self._peer is None or self._peer.closed:
            raise ConnectionClosed("Пир закрыт")
        if len(payload) > self.max_frame:
            raise FrameTooLarge(len(payload))

        network = self._network
        if network.loss and network.rng.random() < network.loss:
            self.dropped += 1
            return True

        delay = network.latency
        if network.jitter:
            delay += network.rng.uniform(-network.jitter, network.jitter)
        deliver_at = time.perf_counter() + max(0.0, delay)
        self._seq += 1
        heapq.heappush(
            self._peer._inbox, (deliver_at, self._seq, bytes(payload))
        )
        self.sent += 1
        self.bytes_out += len(payload)
        return True

    def flush(self) -> bool:
        return True

    def receive(self, limit: int = 256) -> list[bytes]:
        now = time.perf_counter()
        payloads: list[bytes] = []
        while self._inbox and len(payloads) < limit and self._inbox[0][0] <= now:
            _, _, payload = heapq.heappop(self._inbox)
            payloads.append(payload)
            self.bytes_in += len(payload)
        if self.closed and not self._inbox and not payloads:
            raise ConnectionClosed("Соединение закрыто")
        return payloads

    def close(self) -> None:
        self.closed = True
