"""Состояние мира для снапшотов: применение дельт и построение диффа."""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import (
    MASK_FIELDS,
    MASK_SPRITE,
    VISUAL_FIELDS,
    FrameFlags,
    Snapshot,
    SpawnRecord,
    StateRecord,
)


@dataclass(slots=True)
class ObjectState:
    sprite: str = ""
    layer: float = 0.0
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    scale: float = 1.0
    alpha: float = 1.0


class SnapshotState:
    """Плоский снимок мира: ``render_id -> ObjectState``.

    Сервер строит по нему дельту, клиент применяет дельту к своей копии.
    """

    __slots__ = ("tick", "objects")

    def __init__(self):
        self.tick = 0
        self.objects: dict[int, ObjectState] = {}

    def copy(self) -> "SnapshotState":
        clone = SnapshotState()
        clone.tick = self.tick
        clone.objects = {
            render_id: ObjectState(
                state.sprite,
                state.layer,
                state.x,
                state.y,
                state.rotation,
                state.scale,
                state.alpha,
            )
            for render_id, state in self.objects.items()
        }
        return clone

    def put(self, render_id: int, state: ObjectState) -> None:
        self.objects[int(render_id)] = state

    def remove(self, render_id: int) -> None:
        self.objects.pop(int(render_id), None)

    def apply(self, snapshot: Snapshot) -> bool:
        """Применяет снапшот. ``False`` — база дельты неизвестна."""

        full = bool(snapshot.flags & FrameFlags.RESET) or snapshot.base_tick == 0
        if full:
            base_ids: set[int] = set()
        else:
            if snapshot.base_tick != self.tick:
                return False
            base_ids = set(self.objects)

        # Проверяем адресацию записей до любых изменений, чтобы при ошибке
        # состояние осталось прежним, а не применилось наполовину.
        spawn_ids = {spawn.render_id for spawn in snapshot.spawns}
        for record in snapshot.records:
            if record.render_id not in base_ids and record.render_id not in spawn_ids:
                return False

        if full:
            self.objects.clear()

        for render_id in snapshot.despawns:
            self.objects.pop(render_id, None)

        for spawn in snapshot.spawns:
            self.objects[spawn.render_id] = ObjectState(
                spawn.sprite,
                spawn.layer,
                spawn.x,
                spawn.y,
                spawn.rotation,
                spawn.scale,
                spawn.alpha,
            )

        for record in snapshot.records:
            state = self.objects.get(record.render_id)
            if state is None:
                return False
            values = iter(record.values)
            mask = record.mask
            for index, name in enumerate(VISUAL_FIELDS):
                if mask & (1 << index):
                    setattr(state, name, next(values))
            if mask & MASK_SPRITE:
                state.sprite = record.sprite or ""

        self.tick = snapshot.tick
        return True

    def build(
        self,
        tick: int,
        base: "SnapshotState | None" = None,
        *,
        ack_seq: int = 0,
        flags: int = 0,
    ) -> Snapshot:
        """Строит полный снапшот (``base is None``) или дельту от базы."""

        full = base is None
        base_objects = {} if full else base.objects
        despawns: list[int] = []
        spawns: list[SpawnRecord] = []
        records: list[StateRecord] = []

        if not full:
            for render_id in base_objects.keys() - self.objects.keys():
                despawns.append(render_id)

        for render_id in sorted(self.objects):
            current = self.objects[render_id]
            previous = None if full else base_objects.get(render_id)
            if previous is None:
                spawns.append(
                    SpawnRecord(
                        render_id,
                        current.sprite,
                        current.layer,
                        current.x,
                        current.y,
                        current.rotation,
                        current.scale,
                        current.alpha,
                    )
                )
                continue

            mask = 0
            values: list[float] = []
            for index, name in enumerate(VISUAL_FIELDS):
                value = getattr(current, name)
                if value != getattr(previous, name):
                    mask |= 1 << index
                    values.append(value)
            sprite = None
            if current.sprite != previous.sprite:
                sprite = current.sprite
                mask |= MASK_SPRITE
            if mask & MASK_FIELDS or sprite is not None:
                records.append(
                    StateRecord(render_id, mask, tuple(values), sprite)
                )

        if full:
            flags |= FrameFlags.RESET

        return Snapshot(
            tick=tick,
            base_tick=0 if full else base.tick,
            ack_seq=ack_seq,
            flags=flags,
            despawns=tuple(despawns),
            spawns=tuple(spawns),
            records=tuple(records),
        )
