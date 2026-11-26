"""Очередь ввода игрока на сервере.

Кадры нумеруются клиентом (``seq``). Сервер потребляет по одному кадру на тик
симуляции. Привязка к номеру тика сделала бы сервер заложником расхождения
часов, а порядок по ``seq`` устойчив к задержке и джиттеру:

* опоздавшие и повторные кадры отбрасываются;
* при пропуске повторяется последний кадр (не более ``repeat_ticks``);
* клики не повторяются — это события, а не состояние;
* при переполнении вытесняется самый старый кадр, чтобы не копить задержку.
"""

from __future__ import annotations

from collections import deque

from .config import INPUT_BUFFER_TICKS, INPUT_REPEAT_TICKS
from .protocol import InputFrame


class InputQueue:
    __slots__ = (
        "buffer_ticks",
        "repeat_ticks",
        "_frames",
        "_pending_seqs",
        "_consumed_seq",
        "_last_frame",
        "_repeat_count",
        "received",
        "dropped",
        "repeated",
        "underflow",
    )

    def __init__(
        self,
        *,
        buffer_ticks: int = INPUT_BUFFER_TICKS,
        repeat_ticks: int = INPUT_REPEAT_TICKS,
    ):
        self.buffer_ticks = max(1, int(buffer_ticks))
        self.repeat_ticks = max(0, int(repeat_ticks))
        self._frames: deque[InputFrame] = deque()
        self._pending_seqs: set[int] = set()
        self._consumed_seq = -1
        self._last_frame: InputFrame | None = None
        self._repeat_count = 0
        self.received = 0
        self.dropped = 0
        self.repeated = 0
        self.underflow = 0

    @property
    def pending(self) -> int:
        return len(self._frames)

    @property
    def consumed_seq(self) -> int:
        return self._consumed_seq

    def push(self, frame: InputFrame) -> bool:
        seq = int(frame.seq)
        if seq <= self._consumed_seq or seq in self._pending_seqs:
            self.dropped += 1
            return False
        if len(self._frames) >= self.buffer_ticks:
            stale = self._frames.popleft()
            self._pending_seqs.discard(stale.seq)
            self.dropped += 1
        self._frames.append(frame)
        self._pending_seqs.add(seq)
        self.received += 1
        return True

    def take(self, tick: int) -> InputFrame:
        if self._frames:
            frame = self._frames.popleft()
            self._pending_seqs.discard(frame.seq)
            self._consumed_seq = frame.seq
            self._last_frame = frame
            self._repeat_count = 0
            return frame

        last = self._last_frame
        if last is not None and self._repeat_count < self.repeat_ticks:
            self._repeat_count += 1
            self.repeated += 1
            return InputFrame(
                tick=int(tick),
                seq=last.seq,
                keys=last.keys,
                mouse_x=last.mouse_x,
                mouse_y=last.mouse_y,
                buttons=last.buttons,
                extra_keys=last.extra_keys,
            )

        self.underflow += 1
        return InputFrame(tick=int(tick), seq=last.seq if last is not None else 0)

    def clear(self) -> None:
        self._frames.clear()
        self._pending_seqs.clear()
        self._last_frame = None
        self._consumed_seq = -1
        self._repeat_count = 0
