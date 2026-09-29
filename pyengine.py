from __future__ import annotations

import copy
import time
from typing import Any
import inspect
from typing import Callable


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


class _Camera:
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
    
    def __init__(self):
        self.__dict__["_render_in_stream"] = self.__find_component_in_stack("RenderInQueue")
        self.__dict__["_main_in_stream"] = self.__find_component_in_stack("MainInQueue")
        self.__dict__["_main_out_stream"] = self.__find_component_in_stack("MainOutQueue")
        self.__dict__["_objects_cache"] = self.__find_component_in_stack('DictProxy')
        self.__dict__["_lock"] = self.__find_component_in_stack('AcquirerProxy')
        self.__dict__["_camera_queue"] = self.__find_component_in_stack('CameraQueue')

        self.position = Vector2(0, 0)
        self.rotation = 0
        self.scale = 1
    

    def move_camera(self, movement: Vector2 | list):
        if isinstance(movement, Vector2):
            dx = movement.x
            dy = movement.y
        elif isinstance(movement, (list, tuple)):

            if len(movement) != 2:
                raise ValueError(
                    "Перемещение камеры должно содержать 2 значения"
                )
            dx = movement[0]
            dy = movement[1]
        else:
            raise TypeError(
                f"Недопустимое перемещение камеры: {movement!r}"
            )

        self.position = self.position + Vector2(dx, dy)
        
        if self._camera_queue is None:
            return

        self._camera_queue.put({
            "command": "camera_move",
            "x": dx,
            "y": dy,
        })

    def scale_camera(self, scale: float):

        self.scale = max(0.01, scale)
        if self._camera_queue is None:
            return

        self._camera_queue.put({
            "command": "camera_scale",
            "scale": self.scale,
        })

    def __send_update(self):
        if self._render_in_stream is None:
            return

        self._render_in_stream.put({
            "command": "camera_update",
            "position": [self.position.x, self.position.y],
            "rotation": self.rotation,
            "scale": self.scale
        })


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

        
    class __Transform: # Временно Visual выполняет функции transform
        def __init__(self, 

                    ):
            pass


    class __Visual(ClassTools):
        
        def __init__(self, 
                     id,
                     sprite_path,
                     scale,
                     position, 
                     rotation, 
                     alpha,
                    ):
            self.__dict__["_initialized"] = False
            
            self.id = id
            self.sprite = sprite_path
            self.scale = scale
            self.position = position
            self.rotation = rotation
            self.alpha = alpha

            self._ = [self._find_component_in_stack("RenderInQueue"),
                       self._find_component_in_stack("MainInQueue"),
                       self._find_component_in_stack("MainOutQueue"),
                       self._find_component_in_stack('DictProxy'),
                       self._find_component_in_stack('AcquirerProxy')]

            self.__dict__["_initialized"] = True

        def __setattr__(self, name: str, value: Any):
            self.__dict__[name] = value
            if not self.__dict__["_initialized"]:
                return
            if name == "position" and isinstance(value, list) and not isinstance(value, Vector2):
                self.__dict__[name] = Vector2(value)

            if name[0] != '_':
                self.__visual_changed(name)

        def __visual_changed(self, name):
            objects_cache =     self.__dict__.get("_")[3]
            lock =              self.__dict__.get("_")[4]
            id =        self.__dict__.get("id")

            if objects_cache is None or lock is None or id is None:
                return

            with lock:
                entry = objects_cache.get(id)
                if entry is None:
                    return

                value = self.__dict__[name]
                entry[name] = value

                if entry.get("_visual") is not None:
                    entry["_visual"][name] = copy.deepcopy(value)
                    entry["_is_changed"] = True

                render_id = entry.get("_render_id")
                is_rendered = entry.get("_is_render", False)

                visual = copy.deepcopy(entry.get("_visual"))

                objects_cache[id] = entry

            if is_rendered and render_id is not None and visual:
                self._[0].put({
                    "command": "update",
                    "render_id": render_id,
                    **visual,
                })


    class __Identity(ClassTools):
        def __init__(self, 
                     id, 
                     class_, 
                     name, 
                     layer, 
                     type
                    ):
            self.__dict__["_initialized"] = False

            self.id = id
            self.class_ = class_
            self.name = name
            self.layer = layer
            self.type = type

            self._ = [self._find_component_in_stack("RenderInQueue"),
                        self._find_component_in_stack("MainInQueue"),
                        self._find_component_in_stack("MainOutQueue"),
                        self._find_component_in_stack('DictProxy'),
                        self._find_component_in_stack('AcquirerProxy')]

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
        self._id = source.get("_id") or str(time.time())
        self._object_id = source.get("object_id")
        self._render_id = source.get("render_id")

        self.Visual = None
        self.Transform = None
        self.Identity = None
        self.Scripting = None

        self._is_render = False
        self._is_changed = False
        self.components = []

        visual = source.get("visual")
        if visual is not None:
            self.Visual = self.__Visual(
                id = self._id,
                sprite_path = visual["sprite"],
                scale = visual["scale"],
                position=Vector2(visual["position"][0], visual["position"][1]),
                rotation=visual["rotation"],
                alpha=visual['alpha']
            )
            self.components.append('visual')

        identity = source.get("id")
        if identity is not None:
            # print(identity)
            self.Identity = self.__Identity(
                id = self._id,
                name = identity["name"],
                class_ = identity["class"],
                layer = identity["layer"],
                type = identity["type"],
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
        _visual = None
        if self.Visual is not None:
            _visual = self.Visual.__dict__.copy()
            _visual.pop('_')
        return {
            "dict_data": copy.deepcopy(self.dict_data),
            "_id": self._id,
            "_object_id": self._object_id,
            "_render_id": self._render_id,
            "_is_render": self._is_render,
            "_is_changed": False,
            "_visual": copy.deepcopy(_visual),
        }

    def register(self):
        if self._main_in_stream is None or self._main_out_stream is None:
            raise "Отсутствует подключение GameObject с Stream"

        self._main_in_stream.put({
            "command": "register",
            "obj": self.__cache_record(),
            "id": self._id,
        })
        object_id, render_id = self._main_out_stream.get()[self._id]

        self.__dict__["_object_id"] = object_id
        self.__dict__["_render_id"] = render_id

        self.dict_data.setdefault("id", {}).update({
            "object_id": object_id,
            "render_id": render_id,
        })
        self.dict_data["_id"] = self._id

        if self.Scripting is not None:
            for path in self.Scripting.scripts:
                self._main_in_stream.put({
                    "command": "add_script",
                    "path": path,
                    "obj_id": object_id,
                    "id": self._id,
                })
        else:
            if self.Visual is not None:
                self.render()
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

    def is_pressed(self, button: str) -> bool:
        return button.upper() in self.__keyboard_input

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
        Time,
        camera_queue
    ):
        self.__keyboard_input = keyboard_input
        self.__rawTime = Time
        self.GlobalTime = Time.value
        self.GameObject = GameObject(game_object)
        self.Camera = _Camera()

        self.start()

        last_time = self.__rawTime.value
        while True:
            self.GlobalTime = self.__rawTime.value

            self.update()
            if last_time != self.GlobalTime:
                self.fixed_update()
                last_time = self.GlobalTime
            time.sleep(1/120)