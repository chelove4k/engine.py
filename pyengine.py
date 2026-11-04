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

from collision import convex_polygons_overlap, transform_polygon, validate_convex_polygon
from config import (
    FIXED_DT,
    MAX_CATCH_UP_STEPS,
    REGISTRATION_POLL_INTERVAL,
    REGISTRATION_TIMEOUT,
    SCRIPT_UPDATE_PERIOD,
)
from memory_lib import ObjectCache, ValueCache
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

    def __init__(self, script_owner, objects_cache):
        self._script_owner = script_owner
        self._objects_cache = objects_cache
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

    def _was_event(self, key, event_type):
        key = self._normalize_key(key)
        cursor_key = (event_type, key)
        events = self._objects_cache.get(self.EVENTS_KEY) or ()
        cursor = self._query_sequences.get(cursor_key, self._initial_event_sequence)
        matching_sequences = [
            event["sequence"]
            for event in events
            if event["sequence"] > cursor
            and event["type"] == event_type
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
        events = self._objects_cache.get(self.EVENTS_KEY) or ()
        pending = [event for event in events if event["sequence"] > self._event_sequence]
        if pending:
            self._event_sequence = pending[-1]["sequence"]
        return pending


class Mouse:
    STATE_KEY = ("__engine_mouse__", "state")
    EVENTS_KEY = ("__engine_mouse__", "events")

    def __init__(self, game_object, objects_cache):
        self._game_object = game_object
        self._objects_cache = objects_cache
        events = objects_cache.get(self.EVENTS_KEY) or ()
        self._event_sequence = events[-1]["sequence"] if events else 0
        self._click_sequence = self._event_sequence

    @staticmethod
    def _normalize_button(button):
        if isinstance(button, str):
            return button.lower()
        return {1: "left", 2: "middle", 4: "right"}.get(button, str(button))

    def _state(self):
        return self._objects_cache.get(self.STATE_KEY) or {}

    @property
    def position(self):
        position = self._state().get("position", (0.0, 0.0))
        return Vector2(position[0], position[1])

    @property
    def screen_position(self):
        position = self._state().get("screen_position", (0.0, 0.0))
        return Vector2(position[0], position[1])

    def is_over(self, game_object=None):
        target = game_object if game_object is not None else self._game_object
        return self._state().get("hovered_render_id") == getattr(target, "_render_id", None)

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
        position = self.visual_cache.get_many(render_id, ("x", "y"))
        self.position_x = position["x"]
        self.position_y = position["y"]
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
        position = self.visual_cache.get_many(self.target_id, ("x", "y"))
        self.position_x = position["x"] + dx
        self.position_y = position["y"] + dy
        self.visual_cache.set_many(self.target_id, {
            "x": self.position_x,
            "y": self.position_y,
            "sim_time": float(self.owner.GlobalTime),
            "tick": self._current_tick(),
        })
        return True

    def moveto_camera(self, x: float, y: float):
        if not self._refresh_target():
            return False
        self.position_x = float(x)
        self.position_y = float(y)
        self.visual_cache.set_many(self.target_id, {
            "x": self.position_x,
            "y": self.position_y,
            "sim_time": float(self.owner.GlobalTime),
            "tick": self._current_tick(),
        })
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

        def _world_polygon(self):
            if self._visual_cache is None or self.render_id is None:
                raise RuntimeError("Transform collider is not connected to the shared transform cache")
            pose = self._visual_cache.get_many(self.render_id, ("x", "y", "rotation", "scale"))
            return transform_polygon(self.polygon, (pose["x"], pose["y"]), pose["rotation"], pose["scale"])

        def overlaps(self, other):
            other_transform = getattr(other, "Transform", other)
            if not isinstance(other_transform, self.__class__):
                raise TypeError("Transform.overlaps expects a GameObject with a Transform collider")
            return convex_polygons_overlap(self._world_polygon(), other_transform._world_polygon())


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

            # Index 5 is kept for backwards compatibility with the old
            # stack-injected cache layout.
            self._ = [None, None, None, None, None, visual_cache]
            self.__dict__["_simulation_time_owner"] = time_owner
            self.__dict__["_simulation_time_cache"] = time_cache
            self.__dict__["_tick_cache"] = tick_cache

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
                            position = cache.get_many(render_id, ("x", "y"))
                            return Vector2(position["x"], position["y"])
                        cache_field = "sprite" if name == "sprite" else name
                        return cache.get(render_id, cache_field)
            return object.__getattribute__(self, name)

        def __visual_changed(self, name):
            id = self.__dict__.get("id")
            value = self.__dict__[name]

            if self._[5] is None:
                print('VisualCache не найден')
                return

            visual_cache = self._[5]

            if name == 'position':
                position_data = {
                    'x': float(value.x),
                    'y': float(value.y),
                }

                owner = self.__dict__.get("_simulation_time_owner")
                tick = None

                if owner is not None:
                    position_data["sim_time"] = float(owner.GlobalTime)
                    tick = owner.__dict__.get("_simulation_tick")
                else:
                    time_cache = self.__dict__.get("_simulation_time_cache")
                    if time_cache is not None:
                        position_data["sim_time"] = time_cache.get()

                if tick is None:
                    tick_cache = self.__dict__.get("_tick_cache")
                    if tick_cache is not None:
                        tick = tick_cache.get()
                if tick is not None:
                    tick = int(tick)
                    position_data["tick"] = tick
                    if owner is not None:
                        written = owner.__dict__.setdefault(
                            "_visual_tick_written_by_render_id", {}
                        )
                        written[id] = tick

                visual_cache.set_many(id, position_data)
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
                "obj": self.__cache_record(),
                "script_data": copy.deepcopy(self.dict_data),
                "id": self._id,
            })
            response = self._wait_registration_response(request_id)

        if isinstance(response, dict):
            if "error" in response:
                raise ValueError(
                    f"Stream отклонил объект {self._id!r}: {response['error']}"
                )
            try:
                object_id = response["object_id"]
                render_id = response["render_id"]
            except KeyError as error:
                raise RuntimeError(
                    f"Некорректный ответ Stream при регистрации: {response!r}"
                ) from error
        elif isinstance(response, (list, tuple)) and len(response) == 2:
            object_id, render_id = response
        else:
            raise RuntimeError(
                f"Некорректный ответ Stream при регистрации: {response!r}"
            )

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
        return self

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
        return [GameObject(item, deps=deps).register() for item in prepared]

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

    def __dispatch_mouse_events(self):
        own_render_id = self.GameObject._render_id
        for data in self.Mouse._poll_events():
            event = MouseEvent(data)
            event_type = data["type"]
            is_over = data.get("render_id") == own_render_id

            if event_type == "mouse_move":
                if is_over and not self.__mouse_is_over:
                    self.on_mouse_enter(event)
                elif self.__mouse_is_over and not is_over:
                    self.on_mouse_exit(event)
                self.__mouse_is_over = is_over
                if is_over:
                    self.on_mouse_move(event)
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

        if field == "layer":
            # Layer can change through Visual.layer, so look up its current value.
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
        if self.__dict__.get("_script__rawTick") is not None:
            return int(self.__dict__["_script__rawTick"].get())
        return int(self.__rawTime.get() / FIXED_DT)

    def __write_tick_heartbeat(self, tick):
        visual = self.GameObject.Visual
        render_id = self.GameObject._render_id
        if visual is None or render_id is None or self.__visual_cache is None:
            return
        written = self.__dict__.get("_visual_tick_written_by_render_id") or {}
        if written.get(render_id) == tick:
            return
        self.__visual_cache.set(render_id, "tick", int(tick))

    def __init__(
        self,
        render_in_stream,
        render_out_stream,
        main_in_stream,
        main_out_stream,
        game_object: dict,
        objects_cache,
        lock,
        keyboard_input,
        time_key,
        raw_visual_cache,
        visual_cache,
        raw_id_cache,
        id_cache,
        object_lock,
        camera_state,
        tick_key=None
    ):
        self.__keyboard_snapshot = getattr(keyboard_input, "snapshot", None)
        self.__keyboard_input = (
            None if self.__keyboard_snapshot is not None else keyboard_input
        )
        if self.__keyboard_snapshot is None:
            self.__pressed_keys = frozenset(keyboard_input[:])
        else:
            self.__pressed_keys = self.__keyboard_snapshot()
        self.__objects_cache = objects_cache
        mouse_events = objects_cache.get(Mouse.EVENTS_KEY) or ()
        self.__last_mouse_event_sequence = mouse_events[-1]["sequence"] if mouse_events else 0
        self.__visual_cache = raw_visual_cache
        self.__id_cache = raw_id_cache
        self.__rawTime = ValueCache(time_key)
        self.__rawTick = ValueCache(tick_key) if tick_key is not None else None
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
            self.Mouse = Mouse(self.GameObject, objects_cache)
            self.Keyboard = Keyboard(self, objects_cache)
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

            last_fixed_tick = self.__current_tick()

            while True:
                if (self.__objects_cache is not None
                        and self.GameObject._id not in self.__objects_cache):
                    break

                if self.__keyboard_snapshot is None:
                    self.__pressed_keys = frozenset(self.__keyboard_input[:])
                else:
                    self.__pressed_keys = self.__keyboard_snapshot()

                now = self.__rawTime.get()
                current_tick = self.__current_tick()
                self.GlobalTime = now
                self.__dict__["_simulation_tick"] = current_tick

                self.__dispatch_mouse_events()
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
                    self.__write_tick_heartbeat(last_fixed_tick)
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
