from __future__ import annotations

import contextvars
import copy
import inspect
import json
import os
import queue
import threading
import time
import traceback
import uuid
from contextlib import nullcontext
from typing import Any

from collision import (
    convex_polygons_overlap,
    polygon_bounds,
    transform_polygon,
    validate_convex_polygon,
)
from config import (
    ENGINE_STATE_LOOP_FIELDS,
    FIXED_DT,
    KEYBOARD_EVENT_DOWN,
    KEYBOARD_EVENT_FIELDS,
    MAX_CATCH_UP_STEPS,
    MOUSE_MOTION_FIELDS,
    PRESSED_KEY_FIELDS,
    REGISTRATION_POLL_INTERVAL,
    REGISTRATION_TIMEOUT,
    SCRIPT_UPDATE_PERIOD,
    unpack_pressed_keys,
)
from caches import IdCache, VisualCache
from memory_lib import FieldView, ValueCache
from scene import (
    RESERVED_COMPONENT_PROPERTIES,
    component_specs_from_data,
    custom_component_names,
    validate_object_data,
)

_DEPENDENCIES = contextvars.ContextVar("pyengine_dependencies", default=None)
_REGISTRATION_LOCK = threading.Lock()
REGISTRATION_STASH_PREFIX = "__engine_registration_response__"
# Старое приватное имя оставлено для совместимости с существующим кодом.
_REGISTRATION_STASH_PREFIX = REGISTRATION_STASH_PREFIX


def stash_stream_responses(objects_cache, lock, response):
    """Сохранить чужие ответы Stream, чтобы их не потерял другой читатель.

    ``main_out_stream`` общий: ответ на регистрацию, ``find_object`` или
    ``camera_targets`` может первым прочитать не тот процесс, который ждёт.
    Такой читатель обязан вернуть ответ в общий кэш по ``request_id``, иначе
    ожидающий процесс дождётся таймаута. Функция используется и движком, и
    диагностическими клиентами вроде стресс-теста.
    """

    if objects_cache is None or not isinstance(response, dict):
        return
    context = lock if lock is not None else nullcontext()
    for key, value in response.items():
        with context:
            objects_cache[(REGISTRATION_STASH_PREFIX, key)] = value


def take_stashed_stream_response(objects_cache, lock, request_id):
    """Забрать ранее сохранённый ответ на ``request_id`` из общего кэша."""

    if objects_cache is None:
        return None
    context = lock if lock is not None else nullcontext()
    with context:
        return objects_cache.pop((REGISTRATION_STASH_PREFIX, request_id), None)


def push_dependencies(dependencies: dict):
    """Устанавливает зависимости движка для текущего потока/контекста."""

    return _DEPENDENCIES.set(dependencies)


def pop_dependencies(token) -> None:
    _DEPENDENCIES.reset(token)


def current_dependencies():
    return _DEPENDENCIES.get()


def _deps_from_stack() -> dict:
    """Резервный механизм совместимости для кода, создающего GameObject вне скрипта."""

    names = {
        "RenderInQueue": "render_in_stream",
        "MainInQueue": "main_in_stream",
        "MainOutQueue": "main_out_stream",
        "DictProxy": "objects_cache",
        "AcquirerProxy": "lock",
        "VisualCache": "visual_cache",
        "IdCache": "id_cache",
    }
    found = {}
    for frame_info in inspect.stack():
        for value in frame_info.frame.f_locals.values():
            dependency = names.get(value.__class__.__name__)
            if dependency is not None:
                found[dependency] = value
    return found

class Vector2:
    def __init__(self, x: float = 0.0, y: float = 0.0):
        self.x = x
        self.y = y

    @staticmethod
    def _pair(other):
        if isinstance(other, Vector2):
            return other.x, other.y
        if isinstance(other, (list, tuple)):
            if len(other) != 2:
                raise TypeError("Операция Vector2 ожидает пару [x, y]")
            return other[0], other[1]
        raise TypeError(f"Неподдерживаемый операнд Vector2: {other!r}")

    def __add__(self, other):
        x, y = self._pair(other)
        return Vector2(self.x + x, self.y + y)

    def __sub__(self, other):
        x, y = self._pair(other)
        return Vector2(self.x - x, self.y - y)

    def __mul__(self, value: float):
        return Vector2(
            self.x * value,
            self.y * value
        )

    def __repr__(self):
        return f"Vector2({self.x}, {self.y})"


class MouseEvent:
    def __init__(self, data):
        self.position = Vector2(data.get("x", 0), data.get("y", 0))
        self.screen_position = Vector2(data.get("screen_x", 0), data.get("screen_y", 0))
        self.button = data.get("button")
        self.delta = Vector2(data.get("delta_x", 0), data.get("delta_y", 0))
        self.render_id = data.get("render_id")
        self.scroll = Vector2(data.get("scroll_x", 0), data.get("scroll_y", 0))


class KeyboardEvent:
    def __init__(self, data):
        self.key = data["key"]
        self.modifiers = data.get("modifiers", 0)


class Keyboard:
    EVENTS_KEY = ("__engine_keyboard__", "events")

    def __init__(
        self, script_owner, objects_cache, event_ring=None, event_sequence=None,
        input_source=None,
    ):
        self._script_owner = script_owner
        self._objects_cache = objects_cache
        self._event_ring = event_ring
        self._event_sequence_cache = event_sequence
        self._input_source = input_source
        if event_ring is not None and event_sequence is not None:
            self._event_sequence = int(event_sequence.get())
        else:
            events = objects_cache.get(self.EVENTS_KEY) or ()
            self._event_sequence = events[-1]["sequence"] if events else 0
        # Запросы вроде was_pressed() ведут собственный курсор для каждой
        # клавиши. Они не должны использовать _event_sequence, потому что
        # _poll_events() сдвигает его до выполнения колбэков и update().
        self._initial_event_sequence = self._event_sequence
        self._query_sequences = {}

    def set_input_source(self, input_source):
        self._input_source = input_source

    @staticmethod
    def _normalize_key(key):
        return str(key).upper().replace("_", "")

    def is_pressed(self, key):
        return self._script_owner.is_pressed(self._normalize_key(key))

    def get_pressed(self):
        return frozenset(self._script_owner.get_pressed_keys())

    def _events_after(self, cursor):
        """Возвращает события новее ``cursor`` из shared memory или Manager."""

        if self._event_ring is None or self._event_sequence_cache is None:
            events = self._objects_cache.get(self.EVENTS_KEY) or ()
            return [event for event in events if event["sequence"] > cursor]

        latest = int(self._event_sequence_cache.get())
        if latest <= cursor:
            return []

        capacity = self._event_ring.capacity
        start = max(cursor + 1, latest - capacity + 1)
        pending = []
        for sequence in range(start, latest + 1):
            values = self._event_ring.get_many(
                sequence % capacity, KEYBOARD_EVENT_FIELDS
            )
            if int(values["sequence"]) != sequence:
                # Слот уже перезаписан более новым событием.
                continue
            pending.append({
                "sequence": sequence,
                "type": (
                    "key_down"
                    if int(values["type"]) == KEYBOARD_EVENT_DOWN
                    else "key_up"
                ),
                "key": values["key"],
                "modifiers": int(values["modifiers"]),
            })
        return pending

    def _was_event(self, key, event_type):
        key = self._normalize_key(key)
        cursor_key = (event_type, key)
        cursor = self._query_sequences.get(cursor_key, self._initial_event_sequence)
        matching_sequences = [
            event["sequence"]
            for event in self._events_after(cursor)
            if event["type"] == event_type
            and event["key"] == key
        ]
        if matching_sequences:
            # Забираем события только для этой клавиши. Переход в конец
            # всего списка событий молча поглотил бы нажатия других клавиш.
            self._query_sequences[cursor_key] = max(matching_sequences)
            return True
        return False

    def was_pressed(self, key):
        if self._input_source is not None:
            return self._normalize_key(key) in self._input_source.current.pressed_down
        return self._was_event(key, "key_down")

    def was_released(self, key):
        if self._input_source is not None:
            return self._normalize_key(key) in self._input_source.current.pressed_up
        return self._was_event(key, "key_up")

    def _poll_events(self):
        if self._input_source is not None:
            # Сетевые события клавиш раздаёт script.step: у каждого скрипта
            # собственные колбэки, а источник ввода общий на игрока.
            return []
        pending = self._events_after(self._event_sequence)
        if pending:
            self._event_sequence = pending[-1]["sequence"]
        return pending


class Mouse:
    STATE_KEY = ("__engine_mouse__", "state")
    EVENTS_KEY = ("__engine_mouse__", "events")

    def __init__(self, game_object, objects_cache, motion_cache=None, input_source=None):
        self._game_object = game_object
        self._objects_cache = objects_cache
        self._motion_cache = motion_cache
        self._input_source = input_source
        events = objects_cache.get(self.EVENTS_KEY) or ()
        self._event_sequence = events[-1]["sequence"] if events else 0
        self._click_sequence = self._event_sequence
        # Движение публикуется в shared memory. Запоминаем последнюю
        # последовательность, увиденную скриптом, чтобы не дублировать снимок.
        self._motion_sequence = (
            int(motion_cache.get(0, "seq"))
            if motion_cache is not None else 0
        )

    def set_input_source(self, input_source):
        self._input_source = input_source

    @staticmethod
    def _normalize_button(button):
        if isinstance(button, str):
            return button.lower()
        return {1: "left", 2: "middle", 4: "right"}.get(button, str(button))

    def _state(self):
        return self._objects_cache.get(self.STATE_KEY) or {}

    def _pointer(self):
        """Возвращает позицию указателя и наведение без запроса к Manager."""

        if self._motion_cache is None:
            return self._state()
        values = self._motion_cache.get_many(0, MOUSE_MOTION_FIELDS)
        hovered = int(values["hovered"])
        return {
            "position": (values["x"], values["y"]),
            "screen_position": (values["screen_x"], values["screen_y"]),
            "hovered_render_id": None if hovered < 0 else hovered,
        }

    @property
    def position(self):
        if self._input_source is not None:
            snapshot = self._input_source.current
            return Vector2(snapshot.mouse_x, snapshot.mouse_y)
        position = self._pointer().get("position", (0.0, 0.0))
        return Vector2(position[0], position[1])

    @property
    def screen_position(self):
        if self._input_source is not None:
            snapshot = self._input_source.current
            return Vector2(snapshot.mouse_x, snapshot.mouse_y)
        position = self._pointer().get("screen_position", (0.0, 0.0))
        return Vector2(position[0], position[1])

    def is_over(self, game_object=None):
        if self._input_source is not None:
            # В сетевом кадре нет hovered_render_id: клиент не присылает
            # результат подбора, чтобы не доверять ему выбор объектов.
            return False
        target = game_object if game_object is not None else self._game_object
        return self._pointer().get("hovered_render_id") == getattr(target, "_render_id", None)

    def is_pressed(self, button="left"):
        if self._input_source is not None:
            return self._input_source.current.button_pressed(button)
        pressed = self._state().get("buttons", ())
        return self._normalize_button(button) in pressed

    def was_clicked(self, button="left"):
        if self._input_source is not None:
            own_render_id = getattr(self._game_object, "_render_id", None)
            wanted = self._normalize_button(button)
            for render_id, pressed_button in self._input_source.current.clicks:
                if render_id not in (0, None, own_render_id):
                    continue
                if {1: "left", 2: "middle", 4: "right"}.get(
                    pressed_button, str(pressed_button)
                ) == wanted:
                    return True
            return False
        events = self._objects_cache.get(self.EVENTS_KEY) or ()
        matching_sequences = [
            event["sequence"]
            for event in events
            if event["sequence"] > self._click_sequence
            and event["type"] == "mouse_down"
            and event.get("render_id") == getattr(self._game_object, "_render_id", None)
            and event.get("button") == self._normalize_button(button)
        ]
        if matching_sequences:
            self._click_sequence = max(matching_sequences)
            return True
        return False

    def _poll_motion(self):
        """Возвращает новейший снимок движения, если он изменился с прошлого опроса."""

        if self._motion_cache is None:
            return None
        values = self._motion_cache.get_many(0, MOUSE_MOTION_FIELDS)
        sequence = int(values["seq"])
        if sequence == self._motion_sequence:
            return None
        self._motion_sequence = sequence
        hovered = int(values["hovered"])
        return {
            "sequence": sequence,
            "type": "mouse_move",
            "render_id": None if hovered < 0 else hovered,
            "x": values["x"],
            "y": values["y"],
            "screen_x": values["screen_x"],
            "screen_y": values["screen_y"],
            "button": None,
            "delta_x": values["dx"],
            "delta_y": values["dy"],
            "scroll_x": 0.0,
            "scroll_y": 0.0,
        }

    def _poll_events(self):
        events = self._objects_cache.get(self.EVENTS_KEY) or ()
        pending = [event for event in events if event["sequence"] > self._event_sequence]
        if pending:
            self._event_sequence = pending[-1]["sequence"]
        return pending


class _Camera:
    INDEX_PREFIX = "__engine_index__"

    def __init__(self, script_owner, camera_target_cache, camera_scale_cache,
                 request_stream=None):
        self.owner = script_owner
        self.id_cache = script_owner.__dict__["_script__id_cache"]
        self.objects_cache = script_owner.__dict__["_script__objects_cache"]
        self.visual_cache = script_owner.__dict__["_script__visual_cache"]
        self.tick_cache = script_owner.__dict__.get("_script__rawTick")
        self.camera_target_cache = camera_target_cache
        self.camera_scale_cache = camera_scale_cache
        # В режиме Stream список камер запрашивается командой, а живость
        # цели проверяется по поколению слота в IdCache.
        self._request_stream = request_stream
        self._target_generations = {}
        self.targets = {}
        self.target_id = -1
        self.scale = self.camera_scale_cache.get()
        self.position_x = 0.0
        self.position_y = 0.0

        self._refresh_targets()
        current_target = self.camera_target_cache.get()
        if self._is_valid_target(current_target):
            self._select_target(current_target)
        elif self.targets:
            first_name = min(self.targets, key=lambda value: (value.casefold(), value))
            self.switch(first_name)

    def _refresh_targets(self):
        """Перестраивает карту name -> render_id, чтобы работали динамически созданные камеры."""

        if self._request_stream is not None:
            payload = self._request_stream("camera_targets", {})
            if not isinstance(payload, dict) or "error" in payload:
                return
            self.targets = {}
            self._target_generations = {}
            for entry in payload.get("targets", ()):
                name = entry.get("name")
                render_id = entry.get("render_id")
                if not name or render_id is None:
                    continue
                self.targets.setdefault(name, render_id)
                self._target_generations[render_id] = entry.get(
                    "generation", 0
                )
            return

        self.targets = {}
        target_ids = self.objects_cache.get((self.INDEX_PREFIX, "type", "camera"), ())
        if not isinstance(target_ids, list):
            # manager.list поддерживает срез за один серверный вызов; прямой
            # перебор прокси превратил бы один запрос в N удалённых вызовов.
            try:
                target_ids = target_ids[:]
            except TypeError:
                target_ids = list(target_ids)
        for object_id in target_ids:
            record = self.objects_cache.get(object_id)
            if record is None:
                continue
            render_id = record["_render_id"]
            name = self.id_cache.get(render_id, "name")
            if name:
                self.targets.setdefault(name, render_id)

    def _target_record(self, render_id):
        if render_id is None or render_id < 0:
            return None
        object_id = self.objects_cache.get(
            (self.INDEX_PREFIX, "render_id", render_id)
        )
        if object_id is None:
            return None
        return self.objects_cache.get(object_id)

    def _is_valid_target(self, render_id):
        if render_id is None or render_id < 0:
            return False
        if self._request_stream is not None:
            generation = (
                self.id_cache.get(int(render_id), "generation")
                if self.id_cache is not None else 0
            )
            if not generation:
                return False
            known = self._target_generations.get(render_id)
            return known is None or known == generation
        return self._target_record(render_id) is not None

    def _refresh_target(self):
        """Отслеживает переключение, сделанное другим скриптом, и проверяет цель."""

        shared_target = self.camera_target_cache.get()
        if shared_target != self.target_id:
            if self._is_valid_target(shared_target):
                self._select_target(shared_target)
            else:
                self.target_id = -1
                if shared_target not in (-1, None):
                    # Общая цель удалена; сбрасываем её для всех.
                    self.camera_target_cache.set(-1)

        if self.target_id >= 0 and not self._is_valid_target(self.target_id):
            self.target_id = -1
            self.camera_target_cache.set(-1)

        return self.target_id >= 0

    def _select_target(self, render_id):
        if not self._is_valid_target(render_id):
            return False
        self.target_id = render_id
        self.position_x, self.position_y = self._read_position(render_id)
        self.camera_target_cache.set(render_id)
        return True

    def switch(self, name: str):
        self._refresh_targets()
        render_id = self.targets.get(name)
        if render_id is None:
            return False
        return self._select_target(render_id)

    def swich(self, name: str):
        return self.switch(name)

    def _movement_pair(self, dx, dy):
        if dy is not None:
            return float(dx), float(dy)
        if isinstance(dx, Vector2):
            return float(dx.x), float(dx.y)
        if isinstance(dx, (list, tuple)) and len(dx) == 2:
            return float(dx[0]), float(dx[1])
        raise TypeError("move_camera ожидает (dx, dy) или пару")

    def _read_position(self, render_id):
        get_position = getattr(self.visual_cache, "get_position", None)
        if get_position is not None:
            position = get_position(render_id)
            return position[0], position[1]
        position = self.visual_cache.get_many(render_id, ("x", "y"))
        return position["x"], position["y"]

    def _write_position(self, render_id, x, y, tick):
        set_position = getattr(self.visual_cache, "set_position", None)
        if set_position is not None:
            set_position(render_id, x, y, tick)
            return
        self.visual_cache.set_many(
            render_id, {"x": x, "y": y, "tick": int(tick)}
        )

    def _current_tick(self):
        tick = self.owner.__dict__.get("_simulation_tick")
        if tick is not None:
            return int(tick)
        if self.tick_cache is not None:
            return int(self.tick_cache.get())
        return 0

    def move_camera(self, dx, dy=None):
        if not self._refresh_target():
            return False
        dx, dy = self._movement_pair(dx, dy)
        tick = self._current_tick()
        move_position = getattr(self.visual_cache, "move_position", None)
        if move_position is not None:
            self.position_x, self.position_y = move_position(
                self.target_id, dx, dy, tick
            )
            return True
        x, y = self._read_position(self.target_id)
        self.position_x = x + dx
        self.position_y = y + dy
        self._write_position(
            self.target_id, self.position_x, self.position_y, tick
        )
        return True

    def moveto_camera(self, x: float, y: float):
        if not self._refresh_target():
            return False
        self.position_x = float(x)
        self.position_y = float(y)
        self._write_position(
            self.target_id,
            self.position_x,
            self.position_y,
            self._current_tick(),
        )
        return True

    def scale_camera(self, scale: float):
        scale = float(scale)
        if scale != scale or scale in (float("inf"), float("-inf")):
            raise ValueError("Масштаб камеры должен быть конечным числом")
        self.scale = max(0.01, scale)
        self.camera_scale_cache.set(self.scale)


class GameObject:
    class __Transform:
        def __init__(self, render_id, polygon, visual_cache=None):
            self.render_id = render_id
            self.polygon = validate_convex_polygon(polygon)
            self._visual_cache = visual_cache
            self._cached_pose = None
            self._cached_polygon = ()
            self._cached_bounds = None

        def _world_geometry(self):
            if self._visual_cache is None or self.render_id is None:
                raise RuntimeError("Коллайдер Transform не подключён к общему кэшу трансформаций")
            pose = self._visual_cache.get_many(
                self.render_id, ("x", "y", "rotation", "scale")
            )
            key = (pose["x"], pose["y"], pose["rotation"], pose["scale"])
            if key == self._cached_pose:
                return self._cached_polygon, self._cached_bounds
            polygon = transform_polygon(
                self.polygon, (key[0], key[1]), key[2], key[3]
            )
            self._cached_pose = key
            self._cached_polygon = polygon
            self._cached_bounds = (
                polygon_bounds(polygon) if polygon else None
            )
            return polygon, self._cached_bounds

        def _world_polygon(self):
            return self._world_geometry()[0]

        def overlaps(self, other):
            other_transform = getattr(other, "Transform", other)
            if not isinstance(other_transform, self.__class__):
                raise TypeError("Transform.overlaps ожидает GameObject с коллайдером Transform")
            first, first_bounds = self._world_geometry()
            second, second_bounds = other_transform._world_geometry()
            return convex_polygons_overlap(
                first, second, first_bounds, second_bounds
            )


    class __Visual:
        def __init__(self,
                     id,
                     sprite_path,
                     scale,
                     position,
                     rotation,
                     alpha,
                     layer=0,
                     visual_cache=None,
                     time_owner=None,
                     time_cache=None,
                     tick_cache=None,
                    ):
            self.__dict__["_initialized"] = False

            self.id = id
            self.sprite = sprite_path
            self.scale = scale
            self.position = position
            self.rotation = rotation
            self.alpha = alpha
            self.layer = layer

            # Индекс 5 сохранён для обратной совместимости со старой
            # схемой кэша, внедрявшегося через стек.
            self._ = [None, None, None, None, None, visual_cache]
            self.__dict__["_simulation_time_owner"] = time_owner
            self.__dict__["_simulation_time_cache"] = time_cache
            self.__dict__["_tick_cache"] = tick_cache

            self.__dict__["_initialized"] = True
            # Внутренние вызовы не должны проходить через перегруженный
            # __getattribute__: на каждом визуальном обновлении это заметно.
            self.__dict__["_changed"] = self.__visual_changed

        def __setattr__(self, name: str, value: Any):
            values = object.__getattribute__(self, "__dict__")
            values[name] = value
            if not values["_initialized"]:
                return
            if name == "position" and isinstance(value, (list, tuple)) and not isinstance(value, Vector2):
                values[name] = Vector2(value[0], value[1])

            if name[0] != '_':
                values["_changed"](name)

        def __getattribute__(self, name: str):
            if name in {"position", "rotation", "scale", "alpha", "sprite", "layer"}:
                values = object.__getattribute__(self, "__dict__")
                if values.get("_initialized"):
                    # Готовые представления полей убирают поиск по словарю и
                    # захват блокировки на каждое чтение визуального поля.
                    views = values.get("_field_views")
                    if views is None:
                        views = object.__getattribute__(
                            self, "_build_field_views"
                        )()
                    if views is not None:
                        view = views.get(name)
                        if view is not None:
                            if name == "position":
                                x, y, _tick = view.get_lockless()
                                return Vector2(x, y)
                            if name == "sprite":
                                # Строка читается под блокировкой: 64 байта
                                # нельзя безопасно читать во время записи.
                                return view.get()
                            return view.get_lockless()

                    cache_refs = values.get("_")
                    render_id = values.get("id")
                    if cache_refs and cache_refs[5] is not None and render_id is not None:
                        cache = cache_refs[5]
                        if name == "position":
                            get_position = getattr(cache, "get_position", None)
                            if get_position is not None:
                                position = get_position(render_id)
                                return Vector2(position[0], position[1])
                            position = cache.get_many(render_id, ("x", "y"))
                            return Vector2(position["x"], position["y"])
                        cache_field = "sprite" if name == "sprite" else name
                        return cache.get(render_id, cache_field)
            return object.__getattribute__(self, name)

        def _build_field_views(self):
            """Собрать представления полей после назначения render id."""

            values = object.__getattribute__(self, "__dict__")
            cache = values["_"][5]
            render_id = values.get("id")
            if cache is None or render_id is None:
                return None

            views = {}
            for name in ("rotation", "scale", "alpha", "layer", "sprite"):
                views[name] = cache.field_view(name, int(render_id))
            views["position"] = cache.position_view(int(render_id))
            values["_field_views"] = views
            return views

        def _current_tick(self):
            values = object.__getattribute__(self, "__dict__")
            owner = values.get("_simulation_time_owner")
            tick = None
            if owner is not None:
                tick = owner.__dict__.get("_simulation_tick")
            if tick is None:
                tick_cache = values.get("_tick_cache")
                if tick_cache is not None:
                    tick = tick_cache.get()
            if tick is None:
                return None
            return int(tick)

        def move(self, dx, dy=None):
            """Смещает объект на ``dx``/``dy`` за одно обращение к кэшу.

            Принимает ``move(dx, dy)`` или ``move((dx, dy))``. Если кэш
            поддерживает ``move_position``, чтение-изменение-запись выполняется
            под одной stripe-блокировкой, что дешевле чтения ``position`` и
            обратной записи.
            """

            if dy is None:
                if isinstance(dx, Vector2):
                    dx, dy = dx.x, dx.y
                elif isinstance(dx, (list, tuple)) and len(dx) == 2:
                    dx, dy = dx[0], dx[1]
                else:
                    raise TypeError("move ожидает (dx, dy) или пару")
            dx, dy = float(dx), float(dy)

            visual_cache = self._[5]
            if visual_cache is None:
                print('VisualCache не подключён')
                return None

            render_id = self.__dict__.get("id")
            tick = self._current_tick()
            move_position = getattr(visual_cache, "move_position", None)
            if move_position is None:
                position = self.position
                position.x += dx
                position.y += dy
                self.position = position
                return position

            new_x, new_y = move_position(render_id, dx, dy, tick)
            return Vector2(new_x, new_y)

        def __visual_changed(self, name):
            values = object.__getattribute__(self, "__dict__")
            id = values.get("id")
            value = values[name]
            visual_cache = values["_"][5]
            if visual_cache is None:
                print('VisualCache не найден')
                return

            # Если render id уже назначен, пишем через готовое представление:
            # это убирает повторный поиск поля и вычисление смещения.
            views = values.get("_field_views")
            if views is None and id is not None:
                views = self._build_field_views()
            view = (
                views.get("sprite" if name == "path" else name)
                if views else None
            )
            if view is not None:
                if name == 'position':
                    view.set(value.x, value.y, self._current_tick())
                    return
                if name in {'path', 'sprite'}:
                    view.set(str(value))
                    return
                view.set(value)
                return

            if name == 'position':
                render_id = id
                x = float(value.x)
                y = float(value.y)
                tick = self._current_tick()
                set_position = getattr(visual_cache, "set_position", None)
                if set_position is not None:
                    set_position(render_id, x, y, tick)
                else:
                    position_data = {'x': x, 'y': y}
                    if tick is not None:
                        position_data["tick"] = tick
                    visual_cache.set_many(render_id, position_data)
                return
            if name == 'rotation':
                visual_cache.set(id, 'rotation', float(value))
                return
            if name == 'scale':
                visual_cache.set(id, 'scale', float(value))
                return
            if name == 'alpha':
                visual_cache.set(id, 'alpha', float(value))
                return
            if name == 'layer':
                visual_cache.set(id, 'layer', float(value))
                return
            if name in {'path', 'sprite'}:
                visual_cache.set(id, 'sprite', str(value))
                return


    class __Identity:
        def __init__(self,
                     render_id,
                     id,
                     class_,
                     name,
                     type,
                     id_cache=None,
                    ):
            self.__dict__["_initialized"] = False

            self.render_id = render_id
            self.id = id
            self.class_ = class_
            self.name = name
            self.type = type

            self._ = [None, None, None, None, None, id_cache]

            if id_cache is not None and self.render_id is not None:
                id_cache.set(self.render_id, 'name', self.name)
                id_cache.set(self.render_id, 'type', self.type)
                id_cache.set(self.render_id, 'class', self.class_)
                id_cache.set(self.render_id, 'id', self.id)

            self.__dict__["_initialized"] = True




    class __Scripting:
        def __init__(self,
                     id,
                     scripts,
                     network,
                     ):
            self.__dict__["_initialized"] = False

            self.id = id
            self.scripts = scripts
            self.network = network
            self._ = [None, None, None, None, None]

            self.__dict__["_initialized"] = True

    def __init__(self, data: dict | None = None, deps: dict | None = None):
        source = dict(data or {})

        self.__dict__["_initialized"] = False

        if deps is None:
            deps = current_dependencies()
        if deps is None:
            deps = _deps_from_stack()
        self.__dict__["_dependencies"] = deps

        self.__dict__["_render_in_stream"] = deps.get("render_in_stream")
        self.__dict__["_main_in_stream"] = deps.get("main_in_stream")
        self.__dict__["_main_out_stream"] = deps.get("main_out_stream")
        self.__dict__["_objects_cache"] = deps.get("objects_cache")
        self.__dict__["_lock"] = deps.get("lock")
        self.__dict__["_visual_cache"] = deps.get("visual_cache")
        self.__dict__["_id_cache"] = deps.get("id_cache")
        self.__dict__["_tick_cache"] = deps.get("tick_cache")

        self.dict_data = copy.deepcopy(source)
        self.component_specs = copy.deepcopy(component_specs_from_data(source))
        self._id = source.get("_id") or uuid.uuid4().hex
        self._object_id = source.get("object_id")
        self._render_id = source.get("render_id")
        self._generation = int(source.get("_generation", 0) or 0)

        self.Visual = None
        self.Transform = None
        self.Identity = None
        self.Scripting = None

        self._is_render = False
        self.components = []

        visual = source.get("visual")
        if visual is not None:
            self.Visual = self.__Visual(
                id=self._render_id,
                sprite_path=visual["sprite"],
                scale=visual["scale"],
                position=Vector2(visual["position"][0], visual["position"][1]),
                rotation=visual["rotation"],
                alpha=visual['alpha'],
                layer=visual.get("layer", 0),
                visual_cache=deps.get("visual_cache"),
                time_owner=deps.get("time_owner"),
                time_cache=deps.get("time_cache"),
                tick_cache=deps.get("tick_cache"),
            )
            self.components.append('visual')

        transform = source.get("transform")
        if transform is not None:
            self.Transform = self.__Transform(
                self._render_id,
                transform["polygon"],
                deps.get("visual_cache"),
            )
            self.components.append("transform")

        identity = source.get("id")
        if identity is not None:
            self.Identity = self.__Identity(
                render_id=self._render_id,
                id=self._id,
                name=identity["name"],
                class_=identity["class"],
                type=identity.get("type", source.get("object_type", "")),
                id_cache=deps.get("id_cache"),
            )
            self.components.append("id")

        scripting = source.get("scripting")
        if scripting is not None:
            self.Scripting = self.__Scripting(
                id=self._id,
                scripts=list(scripting.get("scripts", [])),
                network=list(scripting.get("network", [])),
            )
            self.components.append("scripting")

        self.__dict__["_initialized"] = True

    def __cache_record(self):
        return {
            "dict_data": copy.deepcopy(self.dict_data),
            "_id": self._id,
            "_object_id": self._object_id,
            "_render_id": self._render_id,
            "_generation": self._generation,
            "_is_render": self._is_render,
        }

    @staticmethod
    def _registration_stash_key(request_id):
        return (_REGISTRATION_STASH_PREFIX, request_id)

    def _stash_registration_responses(self, response):
        stash_stream_responses(self._objects_cache, self._lock, response)

    def _take_registration_response(self, request_id):
        return take_stashed_stream_response(
            self._objects_cache, self._lock, request_id
        )

    def _wait_stream_response(self, request_id, description, legacy_key=None):
        """Ожидает ответ Stream на запрос с указанным ``request_id``.

        Ответы сопоставляются по request_id. Если другой поток/процесс
        выигрывает гонку за ответ, полезная нагрузка сохраняется в общем
        хранилище, чтобы её владелец мог забрать её без потери ответа.
        """

        deadline = time.monotonic() + REGISTRATION_TIMEOUT
        while True:
            stashed = self._take_registration_response(request_id)
            if stashed is not None:
                return stashed

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"Stream не ответил на {description} "
                    f"за {REGISTRATION_TIMEOUT:.1f} с"
                )

            try:
                response = self._main_out_stream.get(
                    timeout=min(REGISTRATION_POLL_INTERVAL, remaining)
                )
            except queue.Empty:
                continue
            except (EOFError, BrokenPipeError, OSError) as error:
                raise RuntimeError(
                    "Соединение со Stream закрылось во время ожидания ответа"
                ) from error

            if not isinstance(response, dict):
                continue
            if request_id in response:
                payload = response[request_id]
                extra = {
                    key: value for key, value in response.items()
                    if key != request_id
                }
                if extra:
                    self._stash_registration_responses(extra)
                return payload
            # Обратная совместимость со старым ответом с ключом по id объекта.
            if legacy_key is not None and legacy_key in response:
                payload = response[legacy_key]
                extra = {
                    key: value for key, value in response.items()
                    if key != legacy_key
                }
                if extra:
                    self._stash_registration_responses(extra)
                return payload
            self._stash_registration_responses(response)

    def _wait_registration_response(self, request_id):
        return self._wait_stream_response(
            request_id,
            f"регистрацию объекта {self._id!r}",
            legacy_key=self._id,
        )

    def _registration_record(self):
        return {
            "id": self._id,
            "obj": self.__cache_record(),
        }

    @staticmethod
    def _parse_registration_response(response, object_key):
        if isinstance(response, dict):
            if "error" in response:
                raise ValueError(
                    f"Stream отклонил объект {object_key!r}: {response['error']}"
                )
            try:
                return (
                    response["object_id"],
                    response["render_id"],
                    int(response.get("generation", 0) or 0),
                )
            except KeyError as error:
                raise RuntimeError(
                    f"Некорректный ответ Stream при регистрации: {response!r}"
                ) from error
        if isinstance(response, (list, tuple)) and len(response) == 2:
            return response[0], response[1], 0
        raise RuntimeError(
            f"Некорректный ответ Stream при регистрации: {response!r}"
        )

    def _apply_registration(self, object_id, render_id, generation=0):
        self.__dict__["_object_id"] = object_id
        self.__dict__["_render_id"] = render_id
        self.__dict__["_generation"] = int(generation or 0)

        # Объект был создан до того, как Stream назначил render id.
        # Перепривязываем прокси компонентов, чтобы последующие обращения к
        # Visual/Identity читали и писали общие кэши, а не локальные значения.
        if self.Visual is not None:
            # Новый слот или кэш делает старые представления полей
            # недействительными: смещения считаются от render id.
            self.Visual.__dict__.pop("_field_views", None)
            self.Visual.__dict__["id"] = render_id
            self.Visual._[5] = self._visual_cache
        if self.Identity is not None:
            self.Identity.__dict__["render_id"] = render_id
            self.Identity._[5] = self._id_cache
            if self._id_cache is not None:
                self._id_cache.set(render_id, "name", self.Identity.name)
                self._id_cache.set(render_id, "type", self.Identity.type)
                self._id_cache.set(render_id, "class", self.Identity.class_)
                self._id_cache.set(render_id, "id", self._id)

        if self.Transform is not None:
            self.Transform.__dict__["render_id"] = render_id

    def _publish_registration(self):
        object_id = self._object_id
        render_id = self._render_id
        script_data = copy.deepcopy(self.dict_data)
        script_data["_id"] = self._id
        script_data["object_id"] = object_id
        script_data["render_id"] = render_id
        script_data["_generation"] = self._generation

        if self.Scripting is not None:
            for path in self.Scripting.scripts:
                self._main_in_stream.put({
                    "command": "add_script",
                    "path": path,
                    "obj_id": object_id,
                    "id": self._id,
                    "script_data": script_data,
                })

        component_data = copy.deepcopy(self.dict_data)
        component_data["_id"] = self._id
        component_data["object_id"] = object_id
        component_data["render_id"] = render_id
        component_data["_generation"] = self._generation
        for component_spec in self.component_specs:
            component_script_data = copy.deepcopy(component_data)
            component_script_data["_component_config"] = copy.deepcopy(
                component_spec.get("properties", {})
            )
            self._main_in_stream.put({
                "command": "add_script",
                "path": component_spec["path"],
                "obj_id": object_id,
                "id": self._id,
                "script_data": component_script_data,
            })

        # Компонент Visual не зависит от скриптования. Отрисовываем его даже
        # при подключённых скриптах/компонентах, не полагаясь на то, что
        # скрипт вызовет render() вручную.
        if self.Visual is not None:
            self.render()

    def register(self):
        if self._main_in_stream is None or self._main_out_stream is None:
            raise RuntimeError("Отсутствует подключение GameObject с Stream")

        with _REGISTRATION_LOCK:
            if self._object_id is not None:
                # Повторный register() идемпотентен только для живого объекта.
                # Живость проверяется по поколению слота в IdCache: общий реестр
                # объектов принадлежит Stream и клиентам недоступен.
                live = (
                    self._id_cache is not None
                    and self._render_id is not None
                    and self._id_cache.get(
                        int(self._render_id), "generation"
                    ) == self._generation
                )
                if live:
                    return self
                self.__dict__["_object_id"] = None
                self.__dict__["_render_id"] = None
                self.__dict__["_generation"] = 0

            request_id = uuid.uuid4().hex
            self._main_in_stream.put({
                "command": "register",
                "request_id": request_id,
                **self._registration_record(),
            })
            response = self._wait_registration_response(request_id)

        object_id, render_id, generation = self._parse_registration_response(
            response, self._id
        )
        self._apply_registration(object_id, render_id, generation)
        self._publish_registration()
        return self

    @staticmethod
    def register_many(objects):
        """Регистрирует много объектов за одно обращение к Stream.

        ``create_object(list)`` и загрузка сцены используют этот путь. Stream
        проверяет весь пакет до изменения общего состояния, затем возвращает
        по одному результату на объект. Скрипты и визуалы по-прежнему
        публикуются для каждого объекта после ответа на пакет.
        """

        objects = list(objects)
        pending = [obj for obj in objects if obj._object_id is None]
        if not pending:
            return objects

        first = pending[0]
        if first._main_in_stream is None or first._main_out_stream is None:
            raise RuntimeError("Отсутствует подключение GameObject с Stream")

        with _REGISTRATION_LOCK:
            # Другой поток мог зарегистрировать часть пакета, пока он
            # собирался.
            pending = [obj for obj in pending if obj._object_id is None]
            if not pending:
                return objects

            request_id = uuid.uuid4().hex
            first._main_in_stream.put({
                "command": "register_many",
                "request_id": request_id,
                "objects": [obj._registration_record() for obj in pending],
            })
            response = first._wait_registration_response(request_id)

        if not isinstance(response, dict):
            raise RuntimeError(
                f"Некорректный ответ Stream при пакетной регистрации: {response!r}"
            )
        if "error" in response:
            raise ValueError(
                f"Stream отклонил пакет объектов: {response['error']}"
            )
        results = response.get("objects")
        if not isinstance(results, list) or len(results) != len(pending):
            raise RuntimeError(
                f"Некорректный ответ Stream при пакетной регистрации: {response!r}"
            )

        failures = []
        registered = []
        for game_object, result in zip(pending, results):
            if isinstance(result, dict) and "error" in result:
                failures.append(f"{game_object._id}: {result['error']}")
                continue
            object_id, render_id, generation = (
                game_object._parse_registration_response(result, game_object._id)
            )
            game_object._apply_registration(object_id, render_id, generation)
            registered.append(game_object)

        for game_object in registered:
            game_object._publish_registration()

        if failures:
            raise ValueError(
                "Stream отклонил часть объектов: " + "; ".join(failures)
            )
        return objects

    def remove(self):
        if self._main_in_stream is not None and self._id is not None:
            self._main_in_stream.put({
                "command": "remove",
                "id": self._id,
                "generation": self._generation,
            })

    def render(self):
        if self.Visual is not None and self._main_in_stream is not None:
            self._main_in_stream.put({
                "command": "render",
                "id": self._id,
                "generation": self._generation,
            })


def create_object(json_text, deps: dict | None = None):
    """Создаёт и регистрирует объект из JSON-текста или Python dict/list.

    Примеры:
        create_object('{"object_type": "static_object", ...}')
        create_object({"object_type": "manager", ...})
        create_object([{...}, {...}])

    Возвращается зарегистрированный ``GameObject`` (или список таких объектов).
    """

    if isinstance(json_text, (bytes, bytearray)):
        json_text = json_text.decode("utf-8")
    if isinstance(json_text, str):
        data = json.loads(json_text)
    else:
        data = json_text

    if isinstance(data, list):
        # Проверяем весь пакет до создания чего-либо, чтобы опечатка в
        # последнем элементе не оставила наполовину созданную сцену.
        prepared = []
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise TypeError(f"Элемент списка #{index} должен быть JSON-объектом")
            item = copy.deepcopy(item)
            validate_object_data(item)
            prepared.append(item)
        objects = [GameObject(item, deps=deps) for item in prepared]
        GameObject.register_many(objects)
        return objects

    if not isinstance(data, dict):
        raise TypeError("Описание объекта должно быть JSON-объектом или списком объектов")

    data = copy.deepcopy(data)
    validate_object_data(data)
    return GameObject(data, deps=deps).register()


# Историческая опечатка, сохранённая как совместимый псевдоним.
creat_object = create_object


class script:        
    def start(self):
        pass

    def update(self):
        pass

    def fixed_update(self):
        pass

    def stop(self):
        """Вызывается один раз при остановке цикла скрипта или удалении его объекта."""
        pass

    def on_mouse_click(self, event):
        pass

    def on_mouse_press(self, event):
        pass

    def on_mouse_release(self, event):
        pass

    def on_mouse_move(self, event):
        pass

    def on_mouse_enter(self, event):
        pass

    def on_mouse_exit(self, event):
        pass

    def on_mouse_scroll(self, event):
        pass

    def on_key_press(self, event):
        pass

    def on_key_release(self, event):
        pass

    def is_pressed(self, button: str) -> bool:
        normalized = str(button).upper().replace("_", "")
        source = self.__dict__.get("_script__input_source")
        if source is not None:
            return normalized in source.current.pressed
        return normalized in self.__pressed_keys

    def get_pressed_keys(self):
        source = self.__dict__.get("_script__input_source")
        if source is not None:
            return source.current.pressed
        return self.__pressed_keys

    def get_mouse_clicks(self):
        """Вспомогательный метод совместимости; новым компонентам следует использовать Mouse или колбэки."""
        source = self.__dict__.get("_script__input_source")
        if source is not None:
            own_render_id = self.GameObject._render_id
            return [
                {
                    "type": "mouse_down",
                    "render_id": own_render_id,
                    "button": {1: "left", 2: "middle", 4: "right"}.get(
                        button, str(button)
                    ),
                    "x": source.current.mouse_x,
                    "y": source.current.mouse_y,
                }
                for render_id, button in source.current.clicks
                if render_id in (0, own_render_id)
            ]
        events = self.__objects_cache.get(Mouse.EVENTS_KEY) or ()
        new_events = [
            event for event in events
            if event["sequence"] > self.__last_mouse_event_sequence
            and event["type"] == "mouse_down"
            and event.get("render_id") == self.GameObject._render_id
        ]
        if events:
            self.__last_mouse_event_sequence = events[-1]["sequence"]
        return new_events

    def bind_input(self, input_source):
        """Привязывает источник ввода игрока (серверный режим).

        ``None`` возвращает скрипт к локальному вводу из ``engine_state``.
        Вызывается планировщиком один раз при назначении объекта игроку.
        """

        self.__dict__["_script__input_source"] = input_source
        self.Mouse.set_input_source(input_source)
        self.Keyboard.set_input_source(input_source)
        if input_source is not None:
            self.__dict__["_script__pressed_keys"] = input_source.current.pressed

    def set_strict_tick(self, enabled=True):
        """Строгий тик: ровно один ``fixed_update`` за проход (сервер)."""

        self.__dict__["_script__strict_tick"] = bool(enabled)

    def __network_button_event(self, snapshot, button_bit):
        return MouseEvent(
            {
                "x": snapshot.mouse_x,
                "y": snapshot.mouse_y,
                "screen_x": snapshot.mouse_x,
                "screen_y": snapshot.mouse_y,
                "render_id": self.GameObject._render_id,
                "button": {1: "left", 2: "middle", 4: "right"}.get(
                    button_bit, "left"
                ),
            }
        )

    def __dispatch_network_input(self, snapshot):
        """Раздаёт сетевой снимок ввода ровно один раз на тик."""

        if self.__wants_keyboard_events:
            for key in snapshot.pressed_down:
                self.on_key_press(KeyboardEvent({"key": key, "modifiers": 0}))
            for key in snapshot.pressed_up:
                self.on_key_release(KeyboardEvent({"key": key, "modifiers": 0}))

        if not self.__wants_mouse_actions:
            return
        if snapshot.buttons_down:
            for bit in (1, 2, 4):
                if snapshot.buttons_down & bit:
                    self.on_mouse_press(self.__network_button_event(snapshot, bit))
        if snapshot.buttons_up:
            for bit in (1, 2, 4):
                if snapshot.buttons_up & bit:
                    self.on_mouse_release(
                        self.__network_button_event(snapshot, bit)
                    )
        if snapshot.clicks:
            own_render_id = self.GameObject._render_id
            for render_id, button in snapshot.clicks:
                if render_id not in (0, own_render_id):
                    continue
                self.on_mouse_click(
                    self.__network_button_event(snapshot, int(button))
                )

    def __handle_mouse_move(self, data):
        own_render_id = self.GameObject._render_id
        event = MouseEvent(data)
        is_over = data.get("render_id") == own_render_id

        if is_over and not self.__mouse_is_over:
            self.on_mouse_enter(event)
        elif self.__mouse_is_over and not is_over:
            self.on_mouse_exit(event)
        self.__mouse_is_over = is_over
        if is_over:
            self.on_mouse_move(event)

    def __dispatch_mouse_motion(self):
        """Доставляет снимок движения из shared memory, если он изменился."""

        data = self.Mouse._poll_motion()
        if data is not None:
            self.__handle_mouse_move(data)

    def __dispatch_mouse_events(self):
        own_render_id = self.GameObject._render_id
        for data in self.Mouse._poll_events():
            event = MouseEvent(data)
            event_type = data["type"]
            is_over = data.get("render_id") == own_render_id

            if event_type == "mouse_move":
                # Устаревший путь, используемый, когда общий кэш движения не подключён.
                if self.__motion_cache is None:
                    self.__handle_mouse_move(data)
                continue

            if not is_over:
                continue
            if event_type == "mouse_down":
                self.on_mouse_press(event)
                self.on_mouse_click(event)
            elif event_type == "mouse_up":
                self.on_mouse_release(event)
            elif event_type == "mouse_scroll":
                self.on_mouse_scroll(event)

    def __dispatch_keyboard_events(self):
        for data in self.Keyboard._poll_events():
            event = KeyboardEvent(data)
            if data["type"] == "key_down":
                self.on_key_press(event)
            elif data["type"] == "key_up":
                self.on_key_release(event)

    def create_object(self, json_text):
        """Создаёт и регистрирует объект из JSON-текста (или dict/list)."""

        return create_object(json_text, deps=self.__dependencies)

    def creat_object(self, json_text):
        return self.create_object(json_text)

    def __request_stream(self, command, payload):
        """Отправить команду Stream и дождаться ответа по request_id."""

        request_id = f"{os.getpid()}:{self.__stream_request_id}"
        self.__stream_request_id += 1
        message = {"command": command, "request_id": request_id}
        message.update(payload)
        self.__main_in_stream.put(message)
        return self.GameObject._wait_stream_response(
            request_id, f"команду {command}"
        )

    def find_object(self, by, data):
        selectors = {"c": "class", "class": "class", "n": "name", "name": "name",
                     "l": "layer", "layer": "layer", "t": "type", "type": "type"}
        field = selectors.get(by)
        if field is None:
            raise ValueError("by должен быть c/class, n/name, l/layer или t/type")

        if getattr(self, "_script__find_object_via_stream", False):
            payload = self.__request_stream(
                "find_object", {"field": field, "value": data}
            )
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"Некорректный ответ Stream на find_object: {payload!r}"
                )
            if "error" in payload:
                raise ValueError(payload["error"])
            source = payload.get("source")
            if source is None:
                return None
            found = GameObject(source, deps=self.__dependencies)
            if found.Visual is not None:
                found.Visual.__dict__["_simulation_time_owner"] = self
            return found

        if field == "layer":
            object_ids = [
                object_id
                for object_id, record in self.__objects_cache.items()
                if isinstance(record, dict)
                and "_render_id" in record
                and "visual" in record.get("dict_data", {}).get("_components", ())
                and self.__visual_cache.get(record["_render_id"], "layer") == data
            ]
        else:
            object_ids = self.__objects_cache.get(("__engine_index__", field, data), ())
            if not isinstance(object_ids, list):
                # Корзина индекса может быть manager.list; берём один снимок
                # вместо одного запроса к Manager на каждый элемент.
                try:
                    object_ids = object_ids[:]
                except TypeError:
                    object_ids = list(object_ids)
        for object_id in object_ids:
            record = self.__objects_cache.get(object_id)
            if record is None:
                continue
            render_id = record["_render_id"]
            identity = {name: self.__id_cache.get(render_id, name)
                        for name in ("name", "class", "type", "id")}
            field_value = (
                self.__visual_cache.get(render_id, "layer")
                if field == "layer" else identity.get(field)
            )
            if not identity["id"] or field_value != data:
                continue
            source = {
                "object_type": identity["type"],
                "_id": identity["id"],
                "object_id": record["_object_id"],
                "render_id": render_id,
                "_generation": record.get("_generation", 0),
                "id": {key: identity[key] for key in ("name", "class", "type")},
            }
            components = record.get("dict_data", {})
            component_names = components.get("_components", ())
            if "transform" in component_names:
                source["transform"] = components["transform"]
            if "components" in component_names:
                source["components"] = components["components"]
            if "scripting" in component_names:
                source["scripting"] = components["scripting"]
            for custom_name in custom_component_names(components):
                source[custom_name] = copy.deepcopy(components[custom_name])
            if "visual" in component_names:
                visual = self.__visual_cache.get_many(
                    render_id,
                    ("sprite", "x", "y", "scale", "rotation", "alpha", "layer"),
                )
                source["visual"] = {
                    "sprite": visual["sprite"],
                    "position": [visual["x"], visual["y"]],
                    "scale": visual["scale"],
                    "rotation": visual["rotation"],
                    "alpha": visual["alpha"],
                    "layer": visual["layer"],
                }
            found = GameObject(source, deps=self.__dependencies)
            if found.Visual is not None:
                found.Visual.__dict__["_simulation_time_owner"] = self
            return found
        return None

    def __current_tick(self):
        return int(self.__rawTick.get())

    def __read_pressed_keys(self, count=None):
        """Читает атомарно опубликованный снимок нажатых клавиш."""

        if count is None:
            count = int(self.__engine_state.get(0, "key_count"))
        count = max(0, min(int(count), len(PRESSED_KEY_FIELDS)))
        if count == 0:
            return frozenset()
        values = self.__engine_state.get_many(
            0, PRESSED_KEY_FIELDS[:count]
        )
        return unpack_pressed_keys(values.values())

    def __init__(
        self,
        render_in_stream,
        render_out_stream,
        main_in_stream,
        main_out_stream,
        game_object: dict,
        objects_cache,
        lock,
        engine_state,
        motion_cache,
        keyboard_ring,
        raw_visual_cache,
        visual_cache,
        raw_id_cache,
        id_cache,
        object_lock,
        camera_state,
        find_object_via_stream=False,
    ):
        self.__engine_state = engine_state
        self.__objects_cache = objects_cache
        mouse_events = objects_cache.get(Mouse.EVENTS_KEY) or ()
        self.__last_mouse_event_sequence = mouse_events[-1]["sequence"] if mouse_events else 0
        self.__visual_cache = raw_visual_cache
        self.__id_cache = raw_id_cache
        self.__rawTime = FieldView(engine_state, "time")
        self.__rawTick = FieldView(engine_state, "tick")
        self.__dict__["_script__rawTick"] = self.__rawTick
        # Снимок указателя в shared memory; движение мыши больше не идёт через
        # список событий Manager, поэтому движущийся указатель не блокирует
        # ввод с клавиатуры и не заставляет каждый скрипт копировать списки событий.
        self.__motion_cache = motion_cache
        # ``None`` заставляет выполнить одну проверку живости на первой
        # итерации цикла. Без неё скрипт, поставленный в очередь до удаления
        # его объекта, принял бы уже увеличенный счётчик за начальную точку
        # и никогда не заметил бы, что объект исчез.
        self.__last_object_generation = None
        # События клавиатуры живут в кольцевом буфере shared memory, что
        # избавляет от копирования списка Manager каждым скриптом при нажатии.
        self.__keyboard_ring = keyboard_ring
        self.__keyboard_sequence = FieldView(
            engine_state, "keyboard_sequence"
        )
        self.__last_keyboard_sequence = None
        self.__pressed_keys = self.__read_pressed_keys()
        self.__last_input_sequence = None
        self.__camera_target_cache = ValueCache(camera_state[0])
        self.__camera_scale_cache = ValueCache(camera_state[1])
        self.GlobalTime = self.__rawTime.get()
        self.__dict__["_simulation_tick"] = self.__current_tick()

        self.__dependencies = {
            "render_in_stream": render_in_stream,
            "main_in_stream": main_in_stream,
            "main_out_stream": main_out_stream,
            "objects_cache": objects_cache,
            "lock": object_lock,
            "visual_cache": raw_visual_cache,
            "id_cache": raw_id_cache,
            "time_owner": self,
            "tick_cache": self.__rawTick,
        }
        self.__main_in_stream = main_in_stream
        self.__find_object_via_stream = bool(find_object_via_stream)
        self.__stream_request_id = 0
        self.__last_fixed_tick = self.__current_tick()
        self.__script_started = False

        try:
            self.GameObject = GameObject(game_object, deps=self.__dependencies)
            self.Mouse = Mouse(self.GameObject, objects_cache, self.__motion_cache)
            self.Keyboard = Keyboard(
                self,
                objects_cache,
                self.__keyboard_ring,
                self.__keyboard_sequence,
            )
            self.__mouse_is_over = self.Mouse.is_over()
            self.ComponentConfig = copy.deepcopy(game_object.get("_component_config", {}))
            for property_name, property_value in self.ComponentConfig.items():
                if (not property_name.isidentifier()
                        or property_name.startswith("_")
                        or property_name in RESERVED_COMPONENT_PROPERTIES
                        or callable(getattr(self, property_name, None))):
                    raise ValueError(f"Недопустимое или зарезервированное свойство компонента: {property_name!r}")
                setattr(self, property_name, copy.deepcopy(property_value))
            if self.GameObject.Visual is not None:
                self.GameObject.Visual.__dict__["_simulation_time_owner"] = self

            self.Camera = _Camera(
                self,
                self.__camera_target_cache,
                self.__camera_scale_cache,
                self.__request_stream if self.__find_object_via_stream else None,
            )

            self.__activate()

            # Опрос списка событий стоит одного запроса к Manager на скрипт.
            # Скрипты, не определяющие нужные колбэки, не должны за это
            # платить (и не должны заставлять Manager копировать весь список
            # при каждом нажатии клавиши или клике).
            def overrides(name):
                return getattr(type(self), name) is not getattr(script, name)

            self.__wants_keyboard_events = (
                overrides("on_key_press") or overrides("on_key_release")
            )
            self.__wants_mouse_actions = any(
                overrides(name) for name in (
                    "on_mouse_press", "on_mouse_click",
                    "on_mouse_release", "on_mouse_scroll",
                )
            )
            self.__wants_mouse_motion = any(
                overrides(name) for name in (
                    "on_mouse_move", "on_mouse_enter", "on_mouse_exit",
                )
            )

        except Exception:
            traceback.print_exc()
            self.__shutdown()
            raise

    def __activate(self):
        """Выполняет ``start()`` в контексте зависимостей скрипта."""

        if self.__script_started or self.__dict__.get("_script__stopped"):
            return
        self.__script_started = True
        token = push_dependencies(self.__dependencies)
        try:
            self.start()
        finally:
            pop_dependencies(token)

    def __is_alive(self):
        """Проверяет, что слот всё ещё принадлежит этому поколению объекта."""

        render_id = self.GameObject._render_id
        if render_id is None or self.__id_cache is None:
            return False
        return (
            self.__id_cache.get(int(render_id), "generation")
            == self.GameObject._generation
        )

    def step(self, state=None) -> bool:
        """Выполняет одну итерацию скрипта.

        ``state`` — общий снимок часов, tick и счётчиков ввода на проход
        планировщика. Если он не передан, скрипт читает снимок сам: так
        работают автономный ``run_forever()`` и юнит-тесты.

        Возвращает ``False``, когда объект удалён или скрипт остановлен.
        Планировщик вызывает метод из одного потока, поэтому отдельный поток
        на каждый скрипт больше не нужен.
        """

        if self.__dict__.get("_script__stopped"):
            return False

        token = push_dependencies(self.__dependencies)
        try:
            if state is None:
                # Одно чтение без блокировки охватывает часы, tick, счётчики
                # ввода и удаления, а также количество нажатых клавиш.
                state = self.__engine_state.get_many_lockless(
                    0, ENGINE_STATE_LOOP_FIELDS
                )
            now = state["time"]
            current_tick = int(state["tick"])
            input_source = self.__dict__.get("_script__input_source")
            strict_tick = self.__dict__.get("_script__strict_tick", False)

            # Вызовы Manager — самая дорогая часть цикла. Без общего
            # счётчика удалений каждый скрипт должен был спрашивать
            # Manager о своём объекте каждый OBJECT_LIVENESS_PERIOD.
            generation = state["object_generation"]
            if generation != self.__last_object_generation:
                self.__last_object_generation = generation
                if not self.__is_alive():
                    return False

            if input_source is not None:
                # Сетевой ввод игрока уже разложен по тикам планировщиком.
                self.__pressed_keys = input_source.current.pressed
            elif state["keyboard_sequence"] != self.__last_keyboard_sequence:
                # Имена клавиш публикуются снимком в shared memory,
                # поэтому не нужны ни прокси списка Manager, ни поток опроса.
                self.__last_keyboard_sequence = state["keyboard_sequence"]
                self.__pressed_keys = self.__read_pressed_keys(
                    state["key_count"]
                )

            self.GlobalTime = now
            self.__dict__["_simulation_tick"] = current_tick

            if input_source is not None:
                self.__dispatch_network_input(input_source.current)
            else:
                # Движение указателя живёт в shared memory и проверяется
                # локально на каждой итерации. События клавиатуры и кликов
                # по-прежнему используют общий счётчик и список событий Manager.
                if self.__wants_mouse_motion:
                    self.__dispatch_mouse_motion()
                wants_mouse_actions = self.__wants_mouse_actions or (
                    self.__motion_cache is None and self.__wants_mouse_motion
                )
                if wants_mouse_actions or self.__wants_keyboard_events:
                    input_sequence = state["input_sequence"]
                    if input_sequence != self.__last_input_sequence:
                        self.__last_input_sequence = input_sequence
                        if wants_mouse_actions:
                            self.__dispatch_mouse_events()
                        if self.__wants_keyboard_events:
                            self.__dispatch_keyboard_events()

            self.update()

            if strict_tick:
                # Сервер: ровно один шаг на тик. Пропущенные тики не
                # догоняются, иначе один проход моделировал бы мир дважды.
                if self.__last_fixed_tick != current_tick:
                    self.__last_fixed_tick = current_tick
                    self.__dict__["_simulation_tick"] = current_tick
                    self.GlobalTime = current_tick * FIXED_DT
                    self.fixed_update()
                return True

            # Каждый скрипт проходит одну и ту же общую последовательность
            # tick. Поэтому позиция, записанная во время tick N, сохраняется
            # под точно тем же tick в каждом процессе.
            steps = 0
            while (self.__last_fixed_tick < current_tick
                   and steps < MAX_CATCH_UP_STEPS):
                self.__last_fixed_tick += 1
                steps += 1
                self.__dict__["_simulation_tick"] = self.__last_fixed_tick
                self.GlobalTime = self.__last_fixed_tick * FIXED_DT
                self.fixed_update()
            if self.__last_fixed_tick < current_tick:
                # Отбрасываем избыточное догоняющее моделирование,
                # но никогда не моделируем tick дважды.
                self.__last_fixed_tick = current_tick

            return True
        finally:
            pop_dependencies(token)

    def run_forever(self):
        """Автономный цикл для запуска скрипта без планировщика."""

        try:
            while self.step():
                time.sleep(SCRIPT_UPDATE_PERIOD)
        except Exception:
            traceback.print_exc()
        finally:
            self.__shutdown()

    def shutdown(self):
        """Останавливает скрипт и освобождает подключённые кэши."""

        self.__shutdown()

    def __shutdown(self):
        if self.__dict__.get("_script__stopped"):
            return
        self.__dict__["_script__stopped"] = True

        try:
            self.stop()
        except Exception:
            traceback.print_exc()

        for attribute in (
            "_script__rawTime",
            "_script__rawTick",
            "_script__keyboard_sequence",
            "_script__camera_target_cache",
            "_script__camera_scale_cache",
            "_script__visual_cache",
            "_script__id_cache",
        ):
            cache = self.__dict__.pop(attribute, None)
            if cache is None:
                continue
            detach = getattr(cache, "detach", None)
            if detach is None:
                continue
            try:
                detach()
            except Exception:
                pass


class Component(script):
    """Переиспользуемое поведение объекта, настраиваемое компонентом верхнего уровня сцены."""
    pass
