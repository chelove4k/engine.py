from __future__ import annotations

import contextvars
import copy
import inspect
import json
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
from memory_lib import FieldView, ObjectCache, ValueCache
from scene import (
    RESERVED_COMPONENT_PROPERTIES,
    component_specs_from_data,
    custom_component_names,
    validate_object_data,
)

_DEPENDENCIES = contextvars.ContextVar("pyengine_dependencies", default=None)
_REGISTRATION_LOCK = threading.Lock()
_REGISTRATION_STASH_PREFIX = "__engine_registration_response__"


def push_dependencies(dependencies: dict):
    """Install engine dependencies for the current thread/context."""

    return _DEPENDENCIES.set(dependencies)


def pop_dependencies(token) -> None:
    _DEPENDENCIES.reset(token)


def current_dependencies():
    return _DEPENDENCIES.get()


def _deps_from_stack() -> dict:
    """Compatibility fallback for code that creates GameObjects outside a script."""

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

class VisualCache(ObjectCache):
    pass

class IdCache(ObjectCache):
    pass

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
                raise TypeError("Vector2 operation expects a pair [x, y]")
            return other[0], other[1]
        raise TypeError(f"Unsupported Vector2 operand: {other!r}")

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
        self, script_owner, objects_cache, event_ring=None, event_sequence=None
    ):
        self._script_owner = script_owner
        self._objects_cache = objects_cache
        self._event_ring = event_ring
        self._event_sequence_cache = event_sequence
        if event_ring is not None and event_sequence is not None:
            self._event_sequence = int(event_sequence.get())
        else:
            events = objects_cache.get(self.EVENTS_KEY) or ()
            self._event_sequence = events[-1]["sequence"] if events else 0
        # Queries such as was_pressed() track their own per-key cursor.  They
        # must not use _event_sequence, because _poll_events() advances it
        # before callbacks and update() are executed.
        self._initial_event_sequence = self._event_sequence
        self._query_sequences = {}

    @staticmethod
    def _normalize_key(key):
        return str(key).upper().replace("_", "")

    def is_pressed(self, key):
        return self._script_owner.is_pressed(self._normalize_key(key))

    def get_pressed(self):
        return frozenset(self._script_owner.get_pressed_keys())

    def _events_after(self, cursor):
        """Return events newer than ``cursor`` from shared memory or Manager."""

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
                # The slot was already overwritten by a newer event.
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
            # Only consume events for this key. Advancing to the end of the
            # whole event list would silently swallow presses of other keys.
            self._query_sequences[cursor_key] = max(matching_sequences)
            return True
        return False

    def was_pressed(self, key):
        return self._was_event(key, "key_down")

    def was_released(self, key):
        return self._was_event(key, "key_up")

    def _poll_events(self):
        pending = self._events_after(self._event_sequence)
        if pending:
            self._event_sequence = pending[-1]["sequence"]
        return pending


class Mouse:
    STATE_KEY = ("__engine_mouse__", "state")
    EVENTS_KEY = ("__engine_mouse__", "events")

    def __init__(self, game_object, objects_cache, motion_cache=None):
        self._game_object = game_object
        self._objects_cache = objects_cache
        self._motion_cache = motion_cache
        events = objects_cache.get(self.EVENTS_KEY) or ()
        self._event_sequence = events[-1]["sequence"] if events else 0
        self._click_sequence = self._event_sequence
        # Motion is published in shared memory.  Remember the last sequence
        # seen by this script so the same snapshot isn't dispatched twice.
        self._motion_sequence = (
            int(motion_cache.get(0, "seq"))
            if motion_cache is not None else 0
        )

    @staticmethod
    def _normalize_button(button):
        if isinstance(button, str):
            return button.lower()
        return {1: "left", 2: "middle", 4: "right"}.get(button, str(button))

    def _state(self):
        return self._objects_cache.get(self.STATE_KEY) or {}

    def _pointer(self):
        """Return pointer position/hover without a Manager round-trip."""

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
        position = self._pointer().get("position", (0.0, 0.0))
        return Vector2(position[0], position[1])

    @property
    def screen_position(self):
        position = self._pointer().get("screen_position", (0.0, 0.0))
        return Vector2(position[0], position[1])

    def is_over(self, game_object=None):
        target = game_object if game_object is not None else self._game_object
        return self._pointer().get("hovered_render_id") == getattr(target, "_render_id", None)

    def is_pressed(self, button="left"):
        pressed = self._state().get("buttons", ())
        return self._normalize_button(button) in pressed

    def was_clicked(self, button="left"):
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
        """Return the newest motion snapshot when it changed since last poll."""

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

    def __init__(self, script_owner, camera_target_cache, camera_scale_cache):
        self.owner = script_owner
        self.id_cache = script_owner.__dict__["_script__id_cache"]
        self.objects_cache = script_owner.__dict__["_script__objects_cache"]
        self.visual_cache = script_owner.__dict__["_script__visual_cache"]
        self.tick_cache = script_owner.__dict__.get("_script__rawTick")
        self.camera_target_cache = camera_target_cache
        self.camera_scale_cache = camera_scale_cache
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
        """Rebuild the name -> render_id map so dynamically spawned cameras work."""

        self.targets = {}
        target_ids = self.objects_cache.get((self.INDEX_PREFIX, "type", "camera"), ())
        if not isinstance(target_ids, list):
            # manager.list supports slicing in one server-side call; iterating
            # a proxy directly would turn one lookup into N remote calls.
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
        return self._target_record(render_id) is not None

    def _refresh_target(self):
        """Follow a switch made by another script and validate the target."""

        shared_target = self.camera_target_cache.get()
        if shared_target != self.target_id:
            if self._is_valid_target(shared_target):
                self._select_target(shared_target)
            else:
                self.target_id = -1
                if shared_target not in (-1, None):
                    # The shared target was removed; reset it for everyone.
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
        raise TypeError("move_camera expects (dx, dy) or a pair")

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
            raise ValueError("Camera scale must be finite")
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
                raise RuntimeError("Transform collider is not connected to the shared transform cache")
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
                raise TypeError("Transform.overlaps expects a GameObject with a Transform collider")
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
                     object_id=None,
                     objects_cache=None,
                     object_lock=None,
                    ):
            self.__dict__["_initialized"] = False

            self.id = id
            self.sprite = sprite_path
            self.scale = scale
            self.position = position
            self.rotation = rotation
            self.alpha = alpha
            self.layer = layer

            # Index 5 is kept for backwards compatibility with the old
            # stack-injected cache layout.
            self._ = [None, None, None, None, None, visual_cache]
            self.__dict__["_simulation_time_owner"] = time_owner
            self.__dict__["_simulation_time_cache"] = time_cache
            self.__dict__["_tick_cache"] = tick_cache
            self.__dict__["_layer_index_object_id"] = object_id
            self.__dict__["_layer_index_objects_cache"] = objects_cache
            self.__dict__["_layer_index_lock"] = object_lock

            self.__dict__["_initialized"] = True

        def __setattr__(self, name: str, value: Any):
            self.__dict__[name] = value
            if not self.__dict__["_initialized"]:
                return
            if name == "position" and isinstance(value, (list, tuple)) and not isinstance(value, Vector2):
                self.__dict__[name] = Vector2(value[0], value[1])

            if name[0] != '_':
                self.__visual_changed(name)

        def __getattribute__(self, name: str):
            if name in {"position", "rotation", "scale", "alpha", "sprite", "layer"}:
                values = object.__getattribute__(self, "__dict__")
                if values.get("_initialized"):
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

        def _current_tick(self):
            owner = self.__dict__.get("_simulation_time_owner")
            tick = None
            if owner is not None:
                tick = owner.__dict__.get("_simulation_tick")
            if tick is None:
                tick_cache = self.__dict__.get("_tick_cache")
                if tick_cache is not None:
                    tick = tick_cache.get()
            if tick is None:
                return None
            return int(tick)

        def move(self, dx, dy=None):
            """Translate the object by ``dx``/``dy`` in one cache round-trip.

            Accepts ``move(dx, dy)`` or ``move((dx, dy))``.  When the cache
            supports ``move_position`` the read-modify-write happens under a
            single stripe lock, which is cheaper than reading ``position`` and
            assigning it back.
            """

            if dy is None:
                if isinstance(dx, Vector2):
                    dx, dy = dx.x, dx.y
                elif isinstance(dx, (list, tuple)) and len(dx) == 2:
                    dx, dy = dx[0], dx[1]
                else:
                    raise TypeError("move expects (dx, dy) or a pair")
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

        @staticmethod
        def _layer_index_add(objects_cache, key, object_id):
            bucket = objects_cache.get(key)
            if bucket is None:
                objects_cache[key] = [object_id]
            elif isinstance(bucket, list):
                if object_id in bucket:
                    return
                bucket = list(bucket)
                bucket.append(object_id)
                objects_cache[key] = bucket
            else:
                # Promoted Manager list: append in place.
                bucket.append(object_id)

        @staticmethod
        def _layer_index_remove(objects_cache, key, object_id):
            bucket = objects_cache.get(key)
            if bucket is None:
                return
            if isinstance(bucket, list):
                if object_id not in bucket:
                    return
                bucket = list(bucket)
                bucket.remove(object_id)
                if bucket:
                    objects_cache[key] = bucket
                else:
                    objects_cache.pop(key, None)
                return
            try:
                bucket.remove(object_id)
            except ValueError:
                return
            if not bucket:
                objects_cache.pop(key, None)

        def __update_layer_index(self, old_layer, new_layer):
            if old_layer == new_layer:
                return
            objects_cache = self.__dict__.get("_layer_index_objects_cache")
            object_id = self.__dict__.get("_layer_index_object_id")
            if objects_cache is None or object_id is None:
                return

            lock = self.__dict__.get("_layer_index_lock")
            with (lock if lock is not None else nullcontext()):
                if old_layer is not None:
                    self._layer_index_remove(
                        objects_cache,
                        ("__engine_index__", "layer", float(old_layer)),
                        object_id,
                    )
                self._layer_index_add(
                    objects_cache,
                    ("__engine_index__", "layer", float(new_layer)),
                    object_id,
                )

        def __visual_changed(self, name):
            id = self.__dict__.get("id")
            value = self.__dict__[name]

            if self._[5] is None:
                print('VisualCache не найден')
                return

            visual_cache = self._[5]

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
                new_layer = float(value)
                old_layer = visual_cache.get(id, 'layer')
                visual_cache.set(id, 'layer', new_layer)
                self.__update_layer_index(old_layer, new_layer)
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
                object_id=self._id,
                objects_cache=deps.get("objects_cache"),
                object_lock=deps.get("lock"),
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
            "_is_render": self._is_render,
        }

    @staticmethod
    def _registration_stash_key(request_id):
        return (_REGISTRATION_STASH_PREFIX, request_id)

    def _stash_registration_responses(self, response):
        if self._objects_cache is None or not isinstance(response, dict):
            return
        context = self._lock if self._lock is not None else nullcontext()
        for key, value in response.items():
            with context:
                self._objects_cache[self._registration_stash_key(key)] = value

    def _take_registration_response(self, request_id):
        if self._objects_cache is None:
            return None
        context = self._lock if self._lock is not None else nullcontext()
        with context:
            return self._objects_cache.pop(
                self._registration_stash_key(request_id), None
            )

    def _wait_registration_response(self, request_id):
        """Wait for the response to this exact request.

        Responses are correlated by request_id. If another thread/process wins
        the race for a response, the payload is stashed in the shared object
        store so its owner can pick it up without losing the response.
        """

        deadline = time.monotonic() + REGISTRATION_TIMEOUT
        while True:
            stashed = self._take_registration_response(request_id)
            if stashed is not None:
                return stashed

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"Stream не ответил на регистрацию объекта {self._id!r} "
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
                    "Соединение со Stream закрылось во время регистрации"
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
            # Backwards compatibility with the old response keyed by object id.
            if self._id in response:
                payload = response[self._id]
                extra = {
                    key: value for key, value in response.items()
                    if key != self._id
                }
                if extra:
                    self._stash_registration_responses(extra)
                return payload
            self._stash_registration_responses(response)

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
                return response["object_id"], response["render_id"]
            except KeyError as error:
                raise RuntimeError(
                    f"Некорректный ответ Stream при регистрации: {response!r}"
                ) from error
        if isinstance(response, (list, tuple)) and len(response) == 2:
            return response
        raise RuntimeError(
            f"Некорректный ответ Stream при регистрации: {response!r}"
        )

    def _apply_registration(self, object_id, render_id):
        self.__dict__["_object_id"] = object_id
        self.__dict__["_render_id"] = render_id

        # The object was constructed before Stream assigned a render id.
        # Rebind its component proxies so subsequent Visual/Identity access
        # reads and writes the shared caches instead of local fallback values.
        if self.Visual is not None:
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

        # A Visual component is independent from scripting. Render it even
        # when scripts/components are attached, instead of relying on the
        # script to call render() manually.
        if self.Visual is not None:
            self.render()

    def register(self):
        if self._main_in_stream is None or self._main_out_stream is None:
            raise RuntimeError("Отсутствует подключение GameObject с Stream")

        with _REGISTRATION_LOCK:
            if self._object_id is not None:
                return self

            request_id = uuid.uuid4().hex
            self._main_in_stream.put({
                "command": "register",
                "request_id": request_id,
                **self._registration_record(),
            })
            response = self._wait_registration_response(request_id)

        object_id, render_id = self._parse_registration_response(response, self._id)
        self._apply_registration(object_id, render_id)
        self._publish_registration()
        return self

    @staticmethod
    def register_many(objects):
        """Register many objects with a single Stream round trip.

        ``create_object(list)`` and scene loading use this path.  Stream
        validates the whole batch before mutating shared state, then returns
        one result per object.  Scripts and visuals are still published per
        object after the batch response.
        """

        objects = list(objects)
        pending = [obj for obj in objects if obj._object_id is None]
        if not pending:
            return objects

        first = pending[0]
        if first._main_in_stream is None or first._main_out_stream is None:
            raise RuntimeError("Отсутствует подключение GameObject с Stream")

        with _REGISTRATION_LOCK:
            # Another thread could have registered part of the batch while it
            # was being collected.
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
            object_id, render_id = game_object._parse_registration_response(
                result, game_object._id
            )
            game_object._apply_registration(object_id, render_id)
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
            self._main_in_stream.put({"command": "remove", "id": self._id})

    def render(self):
        if self.Visual is not None and self._main_in_stream is not None:
            self._main_in_stream.put({"command": "render", "id": self._id})


def create_object(json_text, deps: dict | None = None):
    """Create and register an object from JSON text or a Python dict/list.

    Examples:
        create_object('{"object_type": "static_object", ...}')
        create_object({"object_type": "manager", ...})
        create_object([{...}, {...}])

    The registered ``GameObject`` (or a list of them) is returned.
    """

    if isinstance(json_text, (bytes, bytearray)):
        json_text = json_text.decode("utf-8")
    if isinstance(json_text, str):
        data = json.loads(json_text)
    else:
        data = json_text

    if isinstance(data, list):
        # Validate the whole batch before creating anything, so a typo in the
        # last item does not leave a half-created scene.
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


# Historical typo kept as a compatible alias.
creat_object = create_object


class script:        
    def start(self):
        pass

    def update(self):
        pass

    def fixed_update(self):
        pass

    def stop(self):
        """Called once when the script loop stops or its object is removed."""
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
        return normalized in self.__pressed_keys

    def get_pressed_keys(self):
        return self.__pressed_keys

    def get_mouse_clicks(self):
        """Compatibility helper; new components should use Mouse or callbacks."""
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
        """Deliver the shared-memory motion snapshot when it changed."""

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
                # Legacy path used when no shared motion cache is attached.
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
        """Create and register an object from JSON text (or dict/list)."""

        return create_object(json_text, deps=self.__dependencies)

    def creat_object(self, json_text):
        return self.create_object(json_text)

    def find_object(self, by, data):
        selectors = {"c": "class", "class": "class", "n": "name", "name": "name",
                     "l": "layer", "layer": "layer", "t": "type", "type": "type"}
        field = selectors.get(by)
        if field is None:
            raise ValueError("by must be c/class, n/name, l/layer, or t/type")

        if field == "layer" and not self.__objects_cache.get(
                ("__engine_index__", "layer", "__ready__")):
            # Compatibility path for caches built without a live Stream.
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
                # The index bucket may be a manager.list; take one snapshot
                # instead of one Manager round trip per element.
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
        """Read the atomically published pressed-key snapshot."""

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
        # Shared-memory pointer snapshot; mouse motion no longer goes through
        # the Manager event list, so a moving pointer cannot starve keyboard
        # input or force every script to copy event lists.
        self.__motion_cache = motion_cache
        # ``None`` forces one liveness check on the first loop iteration.
        # Without it a script that was queued before its object got removed
        # would accept the already-bumped counter as its starting point and
        # never notice that the object is gone.
        self.__last_object_generation = None
        # Keyboard events live in a shared-memory ring, avoiding a Manager
        # list copy for every script on every key press.
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
        self.__dependencies_token = push_dependencies(self.__dependencies)

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
                    raise ValueError(f"Invalid or reserved component property: {property_name!r}")
                setattr(self, property_name, copy.deepcopy(property_value))
            if self.GameObject.Visual is not None:
                self.GameObject.Visual.__dict__["_simulation_time_owner"] = self

            self.Camera = _Camera(
                self,
                self.__camera_target_cache,
                self.__camera_scale_cache,
            )

            self.start()

            # Polling an event list costs one Manager round-trip per script.
            # Scripts that never define the matching callbacks should not pay
            # for it (and should not make the Manager copy the whole list on
            # every key press or click).
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

            last_fixed_tick = self.__current_tick()

            while True:
                # One lock-free read covers the clock, tick, input and
                # removal counters plus the pressed-key count.  It used to be
                # one shared-memory read under a process-shared lock: with a
                # thousand scripts every worker contended on the same
                # semaphore and the whole pipeline capped at ~25 updates/s.
                # Every field here is a single 4-byte value, so a racing
                # write can only make the snapshot one update stale.
                state = self.__engine_state.get_many_lockless(
                    0, ENGINE_STATE_LOOP_FIELDS
                )
                now = state["time"]
                current_tick = int(state["tick"])

                # Manager calls are the expensive part of the loop.  Without
                # the shared removal counter every script had to ask the
                # Manager about its object every OBJECT_LIVENESS_PERIOD.
                generation = state["object_generation"]
                if generation != self.__last_object_generation:
                    self.__last_object_generation = generation
                    if (self.__objects_cache is not None
                            and self.GameObject._id not in self.__objects_cache):
                        break

                if state["keyboard_sequence"] != self.__last_keyboard_sequence:
                    # Key names are published as a snapshot in shared memory,
                    # so no Manager list proxy and no polling thread is needed.
                    self.__last_keyboard_sequence = state["keyboard_sequence"]
                    self.__pressed_keys = self.__read_pressed_keys(
                        state["key_count"]
                    )

                self.GlobalTime = now
                self.__dict__["_simulation_tick"] = current_tick

                # Pointer movement lives in shared memory, so it is checked
                # locally every iteration.  Keyboard and click events still
                # use the shared counter and the Manager event list.
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

                # Every script advances through the same shared tick sequence.
                # A position written during tick N is therefore stored under
                # exactly the same tick in every process.
                steps = 0
                while (last_fixed_tick < current_tick
                       and steps < MAX_CATCH_UP_STEPS):
                    last_fixed_tick += 1
                    steps += 1
                    self.__dict__["_simulation_tick"] = last_fixed_tick
                    self.GlobalTime = last_fixed_tick * FIXED_DT
                    self.fixed_update()
                if last_fixed_tick < current_tick:
                    # Drop excessive catch-up, but never simulate a tick twice.
                    last_fixed_tick = current_tick

                time.sleep(SCRIPT_UPDATE_PERIOD)
        except Exception:
            traceback.print_exc()
        finally:
            self.__shutdown()

    def __shutdown(self):
        if self.__dict__.get("_script__stopped"):
            return
        self.__dict__["_script__stopped"] = True

        try:
            self.stop()
        except Exception:
            traceback.print_exc()

        token = self.__dict__.pop("_script__dependencies_token", None)
        if token is not None:
            try:
                pop_dependencies(token)
            except (ValueError, RuntimeError):
                pass

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
    """Reusable object behavior configured by a top-level scene component."""
    pass
