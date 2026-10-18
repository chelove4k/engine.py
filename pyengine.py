from __future__ import annotations

import copy
import time
from typing import Any
import inspect
from typing import Callable
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
                raise ValueError("Перемещение камеры должно содержать 2 значения")
            dx = movement[0]
            dy = movement[1]
        else:
            raise TypeError(f"Недопустимое перемещение камеры: {movement!r}")

        self.position = self.position + Vector2(dx, dy)
        
        if self._camera_queue is None:
            return
        
        self._camera_queue.put({
            'position': [dx, dy]
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
                       self._find_component_in_stack('AcquirerProxy'),
                       self._find_component_in_stack('VisualCache')]

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
            id = self.__dict__.get("id")
            value = self.__dict__[name]

            if self._[5] is None:
                print('VisualCache не найден')
                return

            visual_cache = self._[5]

            if name == 'position':
                visual_cache.set(id, 'x', float(value.x))
                visual_cache.set(id, 'y', float(value.y))
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
            if name == 'path':
                visual_cache.set(id, 'path', str(value))
                return


    class __Identity(ClassTools):
        def __init__(self, 
                     render_id,
                     id, 
                     class_, 
                     name, 
                     layer, 
                     type
                    ):
            self.__dict__["_initialized"] = False

            self.render_id = render_id
            self.id = id
            self.class_ = class_
            self.name = name
            self.layer = layer
            self.type = type

            self._ = [self._find_component_in_stack("RenderInQueue"),
                        self._find_component_in_stack("MainInQueue"),
                        self._find_component_in_stack("MainOutQueue"),
                        self._find_component_in_stack('DictProxy'),
                        self._find_component_in_stack('AcquirerProxy'),
                        self._find_component_in_stack('IdCache')]
            {
                'name' : str,
                'layer' : str,
                'type' : str,
                'class' : str,
                'id' : float,
            }

            if self._[5] is not None and self.render_id is not None:
                self._[5].set(self.render_id, 'layer', self.layer)
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
                id = self._render_id,
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
                render_id= self._render_id,
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
        # Component state lives in its dedicated cache; objects_cache keeps
        # only the source data needed to construct a script-side GameObject.
        return {
            "dict_data": copy.deepcopy(self.dict_data),
            "_id": self._id,
            "_object_id": self._object_id,
            "_render_id": self._render_id,
            "_is_render": self._is_render,
            "_is_changed": False,
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
        object_id, render_id = self._main_out_stream.get()[self._id]

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

    def find_object(self, by, data):
        '''
        "c" - find by class
        "n" - find by name
        "l" - find by layer
        "t" - find by type
        '''
        find = None
        if by == 'c':
            pass
        if by == 'n':
            pass
        if by == 'l':
            pass
        if by == 't':
            pass

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
        camera_queue,
        raw_visual_cache,
        visual_cache,
        raw_id_cache,
        id_cache,
        object_lock
    ):
        self.__keyboard_input = keyboard_input
        self.__rawTime = ValueCache(time_key)
        self.GlobalTime = self.__rawTime.get()
        self.GameObject = GameObject(game_object)

        # self.id_cache = IdCache(*id_cache)
        # self.visual_cache = VisualCache(*visual_cache)

        self.Camera = _Camera()

        self.start()

        last_time = self.__rawTime.get()
        while True:
            self.GlobalTime = self.__rawTime.get()

            self.update()
            if last_time != self.GlobalTime:
                self.fixed_update()
                last_time = self.GlobalTime
            time.sleep(1/120)
