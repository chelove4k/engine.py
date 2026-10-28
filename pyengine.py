from __future__ import annotations

import copy
import time
from typing import Any
import inspect
from typing import Callable
from collision import convex_polygons_overlap, transform_polygon, validate_convex_polygon
from memory_lib import ObjectCache, ValueCache

class VisualCache(ObjectCache):
    pass

class IdCache(ObjectCache):
    pass

class Vector2:
    def __init__(self, x: float = 0.0, y: float = 0.0):
        self.x = x
        self.y = y

    def __add__(self, other):
        if type(other) == Vector2:
            return Vector2(
                self.x + other.x,
                self.y + other.y
            )
        else:
            if type(other) in [type([]), type((0,))]:
                if len(other) == 2:
                    return Vector2(
                        self.x + other[0],
                        self.y + other[1]
                    )
            else:
                raise f"Недопустимое сложение с  вектором: {self} {other}"

    def __sub__(self, other):
            if type(other) == Vector2:
                return Vector2(
                    self.x - other.x,
                    self.y - other.y
                )
            else:
                if type(other) in [type([]), type((0,))]:
                    if len(other) == 2:
                        return Vector2(
                            self.x - other[0],
                            self.y - other[1]
                        )
                else:
                    raise f"Недопустимое сложение с  вектором: {self} {other}"

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
        events = objects_cache.get(self.EVENTS_KEY, ())
        self._event_sequence = events[-1]["sequence"] if events else 0
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
        events = self._objects_cache.get(self.EVENTS_KEY, ())
        cursor = self._query_sequences.get(cursor_key, self._event_sequence)
        occurred = any(
            event["sequence"] > cursor
            and event["type"] == event_type
            and event["key"] == key
            for event in events
        )
        if events:
            self._query_sequences[cursor_key] = events[-1]["sequence"]
        return occurred

    def was_pressed(self, key):
        return self._was_event(key, "key_down")

    def was_released(self, key):
        return self._was_event(key, "key_up")

    def _poll_events(self):
        events = self._objects_cache.get(self.EVENTS_KEY, ())
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
        events = objects_cache.get(self.EVENTS_KEY, ())
        self._event_sequence = events[-1]["sequence"] if events else 0
        self._click_sequence = self._event_sequence

    @staticmethod
    def _normalize_button(button):
        if isinstance(button, str):
            return button.lower()
        return {1: "left", 2: "middle", 4: "right"}.get(button, str(button))

    def _state(self):
        return self._objects_cache.get(self.STATE_KEY, {})

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
        events = self._objects_cache.get(self.EVENTS_KEY, ())
        clicked = any(
            event["sequence"] > self._click_sequence
            and event["type"] == "mouse_down"
            and event.get("render_id") == getattr(self._game_object, "_render_id", None)
            and event.get("button") == self._normalize_button(button)
            for event in events
        )
        if events:
            self._click_sequence = events[-1]["sequence"]
        return clicked

    def _poll_events(self):
        events = self._objects_cache.get(self.EVENTS_KEY, ())
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
        self.camera_target_cache = camera_target_cache
        self.camera_scale_cache = camera_scale_cache
        self.targets = {}
        self.target_id = -1
        self.scale = self.camera_scale_cache.get()
        self.position_x = 0.0
        self.position_y = 0.0

        target_ids = self.objects_cache.get((self.INDEX_PREFIX, "type", "camera"), ())
        for object_id in target_ids:
            record = self.objects_cache.get(object_id)
            if record is None:
                continue
            render_id = record["_render_id"]
            name = self.id_cache.get(render_id, "name")
            if name:
                self.targets.setdefault(name, render_id)

        current_target = self.camera_target_cache.get()
        if current_target in self.targets.values():
            self._select_target(current_target)
        elif self.targets:
            first_name = min(self.targets, key=lambda value: (value.casefold(), value))
            self.switch(first_name)

    def _select_target(self, render_id):
        self.target_id = render_id
        position = self.visual_cache.get_many(render_id, ("x", "y"))
        self.position_x = position["x"]
        self.position_y = position["y"]
        self.camera_target_cache.set(render_id)

    def switch(self, name: str):
        render_id = self.targets.get(name)
        if render_id is None:
            return False
        self._select_target(render_id)
        return True

    def swich(self, name: str):
        return self.switch(name)

    def move_camera(self, dx, dy=None):
        if self.target_id < 0:
            return False
        if dy is None:
            movement = dx
            if isinstance(movement, Vector2):
                dx, dy = movement.x, movement.y
            elif isinstance(movement, (list, tuple)) and len(movement) == 2:
                dx, dy = movement
            else:
                raise TypeError("move_camera expects (dx, dy) or a pair")
        self.position_x += float(dx)
        self.position_y += float(dy)
        self.visual_cache.set_many(self.target_id, {
            "x": self.position_x,
            "y": self.position_y,
            "sim_time": self.owner.__dict__["_script__rawTime"].get(),
        })
        return True

    def moveto_camera(self, x: float, y: float):
        if self.target_id < 0:
            return False
        self.position_x = float(x)
        self.position_y = float(y)
        self.visual_cache.set_many(self.target_id, {
            "x": self.position_x,
            "y": self.position_y,
            "sim_time": self.owner.__dict__["_script__rawTime"].get(),
        })
        return True

    def scale_camera(self, scale: float):
        self.scale = max(0.01, float(scale))
        self.camera_scale_cache.set(self.scale)


class GameObject:
    class ClassTools:
        def _find_component_in_stack(self, class_):
            seen_ids = set()
            stream = None
            for frame_info in inspect.stack():
                frame = frame_info.frame
                for var_name, var_value in frame.f_locals.items():
                    if var_value.__class__.__name__ == class_:
                        if id(var_value) not in seen_ids:
                            seen_ids.add(id(var_value))
                            stream = var_value
            return stream

        
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


    class __Visual(ClassTools):
        
        def __init__(self, 
                     id,
                     sprite_path,
                     scale,
                     position, 
                     rotation, 
                     alpha,
                     layer=0,
                    ):
            self.__dict__["_initialized"] = False
            
            self.id = id
            self.sprite = sprite_path
            self.scale = scale
            self.position = position
            self.rotation = rotation
            self.alpha = alpha
            self.layer = layer

            self._ = [self._find_component_in_stack("RenderInQueue"),
                       self._find_component_in_stack("MainInQueue"),
                       self._find_component_in_stack("MainOutQueue"),
                       self._find_component_in_stack('DictProxy'),
                       self._find_component_in_stack('AcquirerProxy'),
                       self._find_component_in_stack('VisualCache')]

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
                time_cache = self.__dict__.get("_simulation_time_cache")
                if time_cache is not None:
                    position_data['sim_time'] = time_cache.get()
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


    class __Identity(ClassTools):
        def __init__(self, 
                     render_id,
                     id, 
                     class_, 
                     name, 
                     type
                    ):
            self.__dict__["_initialized"] = False

            self.render_id = render_id
            self.id = id
            self.class_ = class_
            self.name = name
            self.type = type

            self._ = [self._find_component_in_stack("RenderInQueue"),
                        self._find_component_in_stack("MainInQueue"),
                        self._find_component_in_stack("MainOutQueue"),
                        self._find_component_in_stack('DictProxy'),
                        self._find_component_in_stack('AcquirerProxy'),
                        self._find_component_in_stack('IdCache')]
            {
                'name' : str,
                'type' : str,
                'class' : str,
                'id' : float,
            }

            if self._[5] is not None and self.render_id is not None:
                self._[5].set(self.render_id, 'name', self.name)
                self._[5].set(self.render_id, 'type', self.type)
                self._[5].set(self.render_id, 'class', self.class_)
                self._[5].set(self.render_id, 'id', self.id)

            self.__dict__["_initialized"] = True




    class __Scripting(ClassTools):
        def __init__(self,
                     id,
                     scripts,
                     network,
                     ):
            self.__dict__["_initialized"] = False

            self.id = id
            self.scripts = scripts
            self.network = network

            self._ = [self._find_component_in_stack("RenderInQueue"),
                        self._find_component_in_stack("MainInQueue"),
                        self._find_component_in_stack("MainOutQueue"),
                        self._find_component_in_stack('DictProxy'),
                        self._find_component_in_stack('AcquirerProxy')]

            self.__dict__["_initialized"] = True


    def __find_component_in_stack(self, class_):
        seen_ids = set()
        stream = None
        for frame_info in inspect.stack():
            frame = frame_info.frame
            for var_name, var_value in frame.f_locals.items():
                if var_value.__class__.__name__ == class_:
                    if id(var_value) not in seen_ids:
                        seen_ids.add(id(var_value))
                        stream = var_value
        return stream
    
    def __init__(self, data: dict = {}):
        source = data.copy()

        self.__dict__["_initialized"] = False

        self.__dict__["_render_in_stream"] = self.__find_component_in_stack("RenderInQueue")
        self.__dict__["_main_in_stream"] = self.__find_component_in_stack("MainInQueue")
        self.__dict__["_main_out_stream"] = self.__find_component_in_stack("MainOutQueue")
        self.__dict__["_objects_cache"] = self.__find_component_in_stack('DictProxy')
        self.__dict__["_lock"] = self.__find_component_in_stack('AcquirerProxy')

        self.dict_data = copy.deepcopy(source)
        self.component_specs = copy.deepcopy(source.get("components", []))
        self._id = source.get("_id") or str(time.time())
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
                id = self._render_id,
                sprite_path = visual["sprite"],
                scale = visual["scale"],
                position=Vector2(visual["position"][0], visual["position"][1]),
                rotation=visual["rotation"],
                alpha=visual['alpha'],
                layer=visual.get("layer", 0),
            )
            self.components.append('visual')

        transform = source.get("transform")
        if transform is not None:
            self.Transform = self.__Transform(
                self._render_id,
                transform["polygon"],
                self.Visual._[5] if self.Visual is not None else None,
            )
            self.components.append("transform")

        identity = source.get("id")
        if identity is not None:
            self.Identity = self.__Identity(
                render_id= self._render_id,
                id = self._id,
                name = identity["name"],
                class_ = identity["class"],
                type = identity.get("type", source.get("object_type", "")),
            )
            self.components.append("id")

        scripting = source.get("scripting")
        if scripting is not None:
            self.Scripting = self.__Scripting(
                id = self._id,
                scripts = list(scripting.get("scripts", [])),
                network = list(scripting.get("network", [])),
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

    def register(self):
        if self._main_in_stream is None or self._main_out_stream is None:
            raise "Отсутствует подключение GameObject с Stream"

        self._main_in_stream.put({
            "command": "register",
            "obj": self.__cache_record(),
            "script_data": copy.deepcopy(self.dict_data),
            "id": self._id,
        })
        response = self._main_out_stream.get()[self._id]
        if isinstance(response, dict):
            raise ValueError(f"Stream отклонил объект {self._id!r}: {response.get('error', response)}")
        object_id, render_id = response

        self.__dict__["_object_id"] = object_id
        self.__dict__["_render_id"] = render_id

        if self.Scripting is not None:
            script_data = copy.deepcopy(self.dict_data)
            script_data["_id"] = self._id
            script_data["object_id"] = object_id
            script_data["render_id"] = render_id

            for path in self.Scripting.scripts:
                self._main_in_stream.put({
                    "command": "add_script",
                    "path": path,
                    "obj_id": object_id,
                    "id": self._id,
                    "script_data": script_data,
                })
        else:
            if self.Visual is not None:
                self.render()

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
        return self

    def remove(self):
        if self._main_in_stream is not None and self._id is not None:
            self._main_in_stream.put({"command": "remove", "id": self._id})

    def render(self):
        if self.Visual is not None and self._main_in_stream is not None:
            self._main_in_stream.put({"command": "render", "id": self._id})


class script:        
    def start(self):
        pass

    def update(self):
        pass

    def fixed_update(self):
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
        return button.upper() in self.__pressed_keys

    def get_pressed_keys(self):
        return self.__pressed_keys

    def get_mouse_clicks(self):
        """Compatibility helper; new components should use Mouse or callbacks."""
        events = self.__objects_cache.get(Mouse.EVENTS_KEY, ())
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
            found = GameObject(source)
            if found.Visual is not None:
                found.Visual._[5] = self.__visual_cache
                found.Visual.__dict__["_simulation_time_cache"] = self.__rawTime
            if found.Identity is not None:
                found.Identity._[5] = self.__id_cache
            return found
        return None

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
        camera_state
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
        mouse_events = objects_cache.get(Mouse.EVENTS_KEY, ())
        self.__last_mouse_event_sequence = mouse_events[-1]["sequence"] if mouse_events else 0
        self.__visual_cache = raw_visual_cache
        self.__id_cache = raw_id_cache
        self.__rawTime = ValueCache(time_key)
        self.__camera_target_cache = ValueCache(camera_state[0])
        self.__camera_scale_cache = ValueCache(camera_state[1])
        self.GlobalTime = self.__rawTime.get()
        self.GameObject = GameObject(game_object)
        self.Mouse = Mouse(self.GameObject, objects_cache)
        self.Keyboard = Keyboard(self, objects_cache)
        self.__mouse_is_over = self.Mouse.is_over()
        self.ComponentConfig = copy.deepcopy(game_object.get("_component_config", {}))
        reserved_component_properties = {
            "GameObject", "Camera", "Mouse", "Keyboard", "ComponentConfig", "GlobalTime",
            "start", "update", "fixed_update", "is_pressed",
            "get_mouse_clicks", "find_object",
        }
        for property_name, property_value in self.ComponentConfig.items():
            if (not property_name.isidentifier()
                    or property_name.startswith("_")
                    or property_name in reserved_component_properties
                    or callable(getattr(self, property_name, None))):
                raise ValueError(f"Invalid or reserved component property: {property_name!r}")
            setattr(self, property_name, copy.deepcopy(property_value))
        if self.GameObject.Visual is not None:
            self.GameObject.Visual.__dict__["_simulation_time_cache"] = self.__rawTime

        # self.id_cache = IdCache(*id_cache)
        # self.visual_cache = VisualCache(*visual_cache)

        self.Camera = _Camera(
            self,
            self.__camera_target_cache,
            self.__camera_scale_cache,
        )

        self.start()

        last_time = self.__rawTime.get()
        while True:
            if self.__keyboard_snapshot is None:
                self.__pressed_keys = frozenset(self.__keyboard_input[:])
            else:
                self.__pressed_keys = self.__keyboard_snapshot()
            self.GlobalTime = self.__rawTime.get()

            self.__dispatch_mouse_events()
            self.__dispatch_keyboard_events()
            self.update()
            if last_time != self.GlobalTime:
                self.fixed_update()
                last_time = self.GlobalTime
            time.sleep(1/120)


class Component(script):
    """Reusable object behavior configured through a scene component entry."""
    pass
