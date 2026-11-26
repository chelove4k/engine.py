"""Серверная симуляция: тик, ввод игроков и привязка объектов.

Живёт в процессе ``scripter_runner`` рядом со скриптами.  Серверный процесс
присылает на каждый тик пачку «кто что нажал»; здесь она раскладывается по
``FrameInputSource`` игроков и применяется до прохода скриптов.  Скрипты
читают ввод через обычные ``Keyboard``/``Mouse`` — источник подменяется
вызовом ``script.bind_input``.
"""

from __future__ import annotations

from .config import INPUT_BUFFER_TICKS, INPUT_REPEAT_TICKS
from .input_source import FrameInputSource
from .protocol import ProtocolError, decode_input_message


class ServerSimulation:
    """Состояние сетевого тика в процессе симуляции."""

    def __init__(
        self,
        *,
        buffer_ticks: int = INPUT_BUFFER_TICKS,
        repeat_ticks: int = INPUT_REPEAT_TICKS,
    ):
        self.buffer_ticks = max(1, int(buffer_ticks))
        self.repeat_ticks = max(0, int(repeat_ticks))
        self.sources: dict[int, FrameInputSource] = {}
        self.bindings: dict[int, int] = {}
        self.tick = 0
        self.skipped = 0
        self.bad_frames = 0

    # -- игроки -----------------------------------------------------------

    def join(self, client_id: int, render_id: int) -> FrameInputSource:
        client_id = int(client_id)
        source = self.sources.get(client_id)
        if source is None:
            source = FrameInputSource(
                client_id=client_id,
                buffer_ticks=self.buffer_ticks,
                repeat_ticks=self.repeat_ticks,
            )
            self.sources[client_id] = source
        if render_id:
            self.bindings[int(render_id)] = client_id
        return source

    def leave(self, client_id: int) -> None:
        client_id = int(client_id)
        self.sources.pop(client_id, None)
        for render_id, owner in tuple(self.bindings.items()):
            if owner == client_id:
                self.bindings.pop(render_id, None)

    def source(self, client_id: int) -> FrameInputSource | None:
        return self.sources.get(int(client_id))

    def owner_of(self, render_id: int) -> int | None:
        return self.bindings.get(int(render_id))

    def source_for_render(self, render_id: int) -> FrameInputSource | None:
        client_id = self.bindings.get(int(render_id))
        if client_id is None:
            return None
        return self.sources.get(client_id)

    # -- кадры ------------------------------------------------------------

    def push(self, client_id: int, payload) -> bool:
        source = self.sources.get(int(client_id))
        if source is None:
            return False
        try:
            frame = decode_input_message(payload)
        except ProtocolError:
            self.bad_frames += 1
            return False
        return source.push(frame)

    # -- тик --------------------------------------------------------------

    def begin_tick(self, tick: int) -> int:
        """Готовит ввод всех игроков к тику. Возвращает число пропусков."""

        tick = int(tick)
        skipped = max(0, tick - self.tick - 1) if self.tick else 0
        if skipped:
            self.skipped += skipped
        self.tick = tick
        for source in self.sources.values():
            source.advance(tick)
        return skipped

    def stats(self) -> dict:
        received = sum(source.received for source in self.sources.values())
        dropped = sum(source.dropped for source in self.sources.values())
        repeated = sum(source.repeated for source in self.sources.values())
        underflow = sum(source.underflow for source in self.sources.values())
        return {
            "tick": self.tick,
            "players": len(self.sources),
            "bound": len(self.bindings),
            "received": received,
            "dropped": dropped,
            "repeated": repeated,
            "underflow": underflow,
            "skipped": self.skipped,
            "bad_frames": self.bad_frames,
        }
