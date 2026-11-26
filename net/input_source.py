"""Источники ввода для скриптов.

Единственная точка подмены локального ввода сетевым.  Скрипты продолжают
работать с ``Keyboard``/``Mouse`` и ``get_pressed_keys()``; меняется только
источник данных:

* одиночная игра и клиент — прежний путь через ``engine_state``;
* сервер — ``FrameInputSource`` на каждого игрока: один кадр ввода на тик.

Кадры нумеруются клиентом (``seq``), поэтому источник устойчив к джиттеру:
очередь отбрасывает опоздавшие кадры, повторяет последний при пропуске и
никогда не повторяет клики.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import INPUT_BUFFER_TICKS, INPUT_REPEAT_TICKS
from .input_queue import InputQueue
from .keys import unpack_keys
from .protocol import InputFrame

BUTTON_BITS = {"left": 1, "middle": 2, "right": 4}
BIT_BUTTONS = {value: name for name, value in BUTTON_BITS.items()}


@dataclass(slots=True, frozen=True)
class InputSnapshot:
    """Состояние ввода игрока на конкретном тике симуляции."""

    tick: int = 0
    seq: int = 0
    pressed: frozenset[str] = frozenset()
    pressed_down: frozenset[str] = frozenset()
    pressed_up: frozenset[str] = frozenset()
    mouse_x: float = 0.0
    mouse_y: float = 0.0
    buttons: int = 0
    buttons_down: int = 0
    buttons_up: int = 0
    clicks: tuple[tuple[int, int], ...] = ()
    claims: tuple[tuple[int, float, float, float, int], ...] = ()

    def button_pressed(self, button: str) -> bool:
        bit = BUTTON_BITS.get(str(button).lower())
        return bool(bit and self.buttons & bit)


@dataclass(slots=True)
class FrameInputSource:
    """Ввод одного игрока на сервере.

    ``advance(tick)`` вызывается ровно один раз за тик симуляции до прохода
    скриптов.  Все скрипты игрока затем читают один и тот же ``current``.
    """

    client_id: int = 0
    buffer_ticks: int = INPUT_BUFFER_TICKS
    repeat_ticks: int = INPUT_REPEAT_TICKS
    queue: InputQueue = field(init=False)
    current: InputSnapshot = field(default_factory=InputSnapshot, init=False)
    _last_tick: int = field(default=-1, init=False)
    _pending_tick: int = field(default=-1, init=False)
    received: int = field(default=0, init=False)
    dropped: int = field(default=0, init=False)
    repeated: int = field(default=0, init=False)
    underflow: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.queue = InputQueue(
            buffer_ticks=self.buffer_ticks,
            repeat_ticks=self.repeat_ticks,
        )

    @property
    def last_tick(self) -> int:
        return self._last_tick

    def push(self, frame: InputFrame) -> bool:
        accepted = self.queue.push(frame)
        if accepted:
            self.received += 1
        else:
            self.dropped += 1
        return accepted

    def advance(self, tick: int) -> bool:
        """Готовит ввод на тик. ``False`` — тик уже был обработан."""

        tick = int(tick)
        if tick <= self._last_tick:
            return False
        self._last_tick = tick
        self._pending_tick = tick
        frame = self.queue.take(tick)
        pressed = unpack_keys(frame.keys, frame.extra_keys)
        previous = self.current.pressed
        previous_buttons = self.current.buttons
        buttons = int(frame.buttons) & 0xFF
        self.current = InputSnapshot(
            tick=tick,
            seq=frame.seq,
            pressed=pressed,
            pressed_down=pressed - previous,
            pressed_up=previous - pressed,
            mouse_x=frame.mouse_x,
            mouse_y=frame.mouse_y,
            buttons=buttons,
            buttons_down=buttons & ~previous_buttons,
            buttons_up=previous_buttons & ~buttons,
            clicks=frame.clicks,
            claims=frame.claims,
        )
        self.received = self.queue.received
        self.dropped = self.queue.dropped
        self.repeated = self.queue.repeated
        self.underflow = self.queue.underflow
        return True

    def reset(self) -> None:
        self.queue.clear()
        self.current = InputSnapshot(tick=self._last_tick)
        self._pending_tick = -1


def snapshot_from_frame(
    frame: InputFrame,
    *,
    previous_pressed=frozenset(),
    previous_buttons: int = 0,
    tick: int | None = None,
) -> InputSnapshot:
    """Строит снимок из кадра (удобно для тестов и клиентского предсказания)."""

    pressed = unpack_keys(frame.keys, frame.extra_keys)
    previous = frozenset(previous_pressed)
    buttons = int(frame.buttons) & 0xFF
    return InputSnapshot(
        tick=int(frame.tick if tick is None else tick),
        seq=int(frame.seq),
        pressed=pressed,
        pressed_down=pressed - previous,
        pressed_up=previous - pressed,
        mouse_x=frame.mouse_x,
        mouse_y=frame.mouse_y,
        buttons=buttons,
        buttons_down=buttons & ~int(previous_buttons),
        buttons_up=int(previous_buttons) & ~buttons,
        clicks=frame.clicks,
        claims=frame.claims,
    )
