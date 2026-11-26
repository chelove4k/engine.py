"""Мост из ``GameServer`` в процесс симуляции движка.

Серверный цикл не считает мир сам: он публикует тик и входные кадры в
``worker_queue``, а авторитетное состояние читает из ``VisualCache`` после
того, как процесс скриптов подтвердил выполнение тика.  Заявки клиентов
проверяются по тому же состоянию, поэтому CORRECTION всегда содержит
настоящую серверную позицию.
"""

from __future__ import annotations

import time

from .protocol import encode_input_message
from .state import ObjectState, SnapshotState

VISUAL_FIELDS = (
    "sprite",
    "layer",
    "x",
    "y",
    "rotation",
    "scale",
    "alpha",
)


class EngineWorld:
    """``World`` для ``GameServer`` поверх общих кэшей движка."""

    def __init__(
        self,
        *,
        worker_queue,
        worker_id: int = 0,
        visual_cache,
        owner_slots=(),
        replicated_ids=(),
        executed_tick=None,
        tick_timeout: float = 0.25,
    ):
        self.worker_queue = worker_queue
        self.worker_id = int(worker_id)
        self.visual_cache = visual_cache
        self.owner_slots = [int(item) for item in owner_slots]
        self.replicated_ids = {int(item) for item in replicated_ids}
        self.executed_tick = executed_tick
        self.tick_timeout = max(0.01, float(tick_timeout))
        self.owner_by_client: dict[int, int] = {}
        self.client_by_owner: dict[int, int] = {}
        self._free_slots = list(self.owner_slots)
        self._last_seq: dict[int, int] = {}
        self._pending: list[tuple[int, bytes]] = []
        self.missed_ticks = 0
        self.last_tick = 0

    # -- игроки -----------------------------------------------------------

    def on_join(self, client_id: int) -> int | None:
        render_id = self._free_slots.pop(0) if self._free_slots else None
        if render_id is not None:
            self.owner_by_client[int(client_id)] = render_id
            self.client_by_owner[render_id] = int(client_id)
        self._send({
            "command": "net_join",
            "client_id": int(client_id),
            "render_id": render_id or 0,
        })
        return render_id

    def on_leave(self, client_id: int) -> None:
        client_id = int(client_id)
        render_id = self.owner_by_client.pop(client_id, None)
        if render_id is not None:
            self.client_by_owner.pop(render_id, None)
            if render_id not in self._free_slots:
                self._free_slots.append(render_id)
        self._last_seq.pop(client_id, None)
        self._send({"command": "net_leave", "client_id": client_id})

    def owner_of(self, render_id: int):
        return self.client_by_owner.get(int(render_id))

    def render_id_for(self, client_id: int):
        return self.owner_by_client.get(int(client_id))

    # -- тик --------------------------------------------------------------

    def apply_input(self, client_id: int, frame, tick: int) -> None:
        client_id = int(client_id)
        # Повтор последнего кадра нужен только симуляции: она сама повторит
        # состояние, если новый кадр не пришёл. В сеть его не гоняем.
        if frame.seq <= self._last_seq.get(client_id, -1):
            return
        self._last_seq[client_id] = frame.seq
        self._pending.append((client_id, encode_input_message(frame)))

    def step(self, tick: int) -> None:
        tick = int(tick)
        self._send({
            "command": "net_tick",
            "tick": tick,
            "inputs": self._pending,
        })
        self._pending = []
        self.last_tick = tick
        self._wait_tick(tick)

    def _send(self, payload) -> None:
        message = dict(payload)
        message["proc_id"] = self.worker_id
        self.worker_queue.put(message)

    def _wait_tick(self, tick: int) -> None:
        if self.executed_tick is None:
            return
        deadline = time.perf_counter() + self.tick_timeout
        while True:
            if int(self.executed_tick.value) >= tick:
                return
            if time.perf_counter() >= deadline:
                self.missed_ticks += 1
                return
            time.sleep(0.0005)

    # -- состояние --------------------------------------------------------

    def snapshot_state(self) -> SnapshotState:
        snapshot = SnapshotState()
        for render_id in self.replicated_ids:
            state = self.object_state(render_id)
            if state is not None:
                snapshot.put(render_id, state)
        return snapshot

    def object_state(self, render_id: int) -> ObjectState | None:
        try:
            values = self.visual_cache.get_many(
                int(render_id), VISUAL_FIELDS
            )
        except (KeyError, IndexError, ValueError):
            return None
        if not values.get("sprite"):
            return None
        return ObjectState(
            values["sprite"],
            float(values["layer"]),
            float(values["x"]),
            float(values["y"]),
            float(values["rotation"]),
            float(values["scale"]),
            float(values["alpha"]),
        )

    def set_replicated(self, render_ids) -> None:
        self.replicated_ids = {int(item) for item in render_ids}
