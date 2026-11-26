"""Константы и политики сетевого режима.

Значения собраны здесь, а не в ``config.py`` движка, чтобы одиночная игра не
зависела от сети и не тянула лишние модули.
"""

from __future__ import annotations

import os
import threading

try:  # при запуске из каталога движка
    from config import FIXED_DT, SIMULATION_HZ
except ImportError:  # автономные тесты/утилиты
    SIMULATION_HZ = 100
    FIXED_DT = 1.0 / SIMULATION_HZ

PROTOCOL_VERSION = 1

DEFAULT_HOST = os.environ.get("PYENGINE_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("PYENGINE_PORT", "7777"))
DEFAULT_MAX_PLAYERS = int(os.environ.get("PYENGINE_MAX_PLAYERS", "32"))

TICK_RATE = SIMULATION_HZ
TICK_DT = FIXED_DT

SNAPSHOT_HZ = 20
SNAPSHOT_INTERVAL_TICKS = max(1, TICK_RATE // SNAPSHOT_HZ)
SNAPSHOT_HISTORY = 32

INPUT_DELAY_TICKS = 2
INPUT_BUFFER_TICKS = 16
INPUT_REPEAT_TICKS = 5
MAX_INPUT_PER_SEC = TICK_RATE + 10
MAX_CATCH_UP_TICKS = 10

INTERP_DELAY_TICKS = 10
HISTORY_TICKS = TICK_RATE

MAX_FRAME_BYTES = 64 * 1024
MAX_PENDING_BYTES = 256 * 1024
MAX_NAME_BYTES = 32

MAX_CLICKS_PER_FRAME = 8
MAX_CLAIMS_PER_FRAME = 16

POSITION_TOLERANCE = 0.05
ROTATION_TOLERANCE = 0.5
# Допуск на приращение между соседними заявками. Джиттер доставки сдвигает
# момент применения кадра на 1-2 тика, поэтому приращения клиента и сервера
# законно расходятся на столько же. Значение — в единицах мира; для быстрых
# сцен его стоит поднять. Накопленное расхождение ловит MAX_CLAIM_DRIFT.
CLAIM_DELTA_TOLERANCE = 4.0
CLAIM_DELTA_ROTATION_TOLERANCE = 10.0
# Максимальный абсолютный сдвиг заявки от серверной истины, который ещё
# считается сетевой задержкой. Больше — телепорт/подмена позиции.
MAX_CLAIM_DRIFT = 32.0
CORRECTION_DECAY = 0.15
CORRECTION_SNAP_DISTANCE = 64.0

DESYNC_WARN = 5
DESYNC_KICK = 25
MAX_SINGLE_ERROR = 500.0

HANDSHAKE_TIMEOUT = 5.0
IDLE_TIMEOUT = 15.0
SEND_TIMEOUT = 10.0


class AnticheatPolicy:
    """Режим античита, переключаемый на лету из терминала сервера.

    ``log`` — только считать и писать расхождения в лог (по умолчанию).
    ``kick`` — отключать игрока после порога нарушений.
    """

    MODES = ("log", "kick")

    def __init__(self, mode: str = "log"):
        self._lock = threading.Lock()
        self._mode = "log"
        self.set_mode(mode)

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, mode: str) -> str:
        normalized = str(mode).strip().lower()
        if normalized not in self.MODES:
            raise ValueError(
                f"Режим античита должен быть одним из {self.MODES}: {mode!r}"
            )
        with self._lock:
            self._mode = normalized
        return normalized

    def should_kick(self, desyncs: int) -> bool:
        with self._lock:
            return self._mode == "kick" and desyncs >= DESYNC_KICK


DEFAULT_ANTICHEAT = AnticheatPolicy("log")
