from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import queue
import threading
import time
import traceback

from pyengine import GameObject
from collision import validate_convex_polygon
import scripter_runner
from memory_lib import ObjectCache, ValueCache

from radon.visitors import ComplexityVisitor


# Choose the scene loaded when the engine starts.
SCENE_FILE = "camera_stability.json"

OBJECT_TYPE_RULES = {
    "static_object": {
        "allowed": frozenset({"id", "visual", "transform"}),
        "required": frozenset({"id"}),
    },
    "dynamic_object": {
        "allowed": frozenset({"id", "visual", "scripting", "transform"}),
        "required": frozenset(),
    },
    "background_object": {
        "allowed": frozenset({"visual", "transform"}),
        "required": frozenset({"visual"}),
    },
    "camera": {
        "allowed": frozenset({"id", "visual", "transform"}),
        "required": frozenset({"id", "visual"}),
    },
    "manager": {
        "allowed": frozenset({"id", "scripting"}),
        "required": frozenset({"id", "scripting"}),
    },
}


def validate_object_data(data: dict) -> None:
    if not isinstance(data, dict):
        raise TypeError("Описание объекта должно быть JSON-объектом")

    identity = data.get("id")
    object_type = data.get("object_type")
    if not isinstance(object_type, str):
        raise ValueError("У объекта должно быть текстовое поле 'object_type'")
    if identity is not None and not isinstance(identity, dict):
        raise ValueError("Компонент 'id' должен быть объектом")
    if identity is not None and identity.get("type", object_type) != object_type:
        raise ValueError("Поле 'id.type' должно совпадать с 'object_type'")
    rules = OBJECT_TYPE_RULES.get(object_type)
    if rules is None:
        raise ValueError(
            f"Неизвестный тип объекта {object_type!r}. "
            f"Допустимые типы: {', '.join(OBJECT_TYPE_RULES)}"
        )

    components = set(data) & {"id", "visual", "scripting", "transform"}
    unknown = set(data) - {"object_type", "id", "visual", "scripting", "transform", "_id", "object_id", "render_id"}
    if unknown:
        raise ValueError(f"Неизвестные компоненты объекта: {', '.join(sorted(unknown))}")

    forbidden = components - rules["allowed"]
    missing = rules["required"] - components
    if forbidden or missing:
        details = []
        if forbidden:
            details.append(f"запрещены компоненты: {', '.join(sorted(forbidden))}")
        if missing:
            details.append(f"обязательны компоненты: {', '.join(sorted(missing))}")
        raise ValueError(f"Тип {object_type!r}: {'; '.join(details)}")

    if identity is not None:
        required_identity = {"name", "class", "layer"}
        missing_identity = required_identity - set(identity)
        if missing_identity:
            raise ValueError(
                f"В компоненте 'id' отсутствуют поля: {', '.join(sorted(missing_identity))}"
            )
        if any(not isinstance(identity[field], str) for field in required_identity):
            raise ValueError("Поля id.name, id.class и id.layer должны быть строками")

    if "visual" in data:
        visual = data["visual"]
        if not isinstance(visual, dict):
            raise ValueError("Компонент 'visual' должен быть объектом")
        required = {"sprite", "position", "scale", "rotation", "alpha"}
        missing_visual = required - set(visual)
        if missing_visual:
            raise ValueError(
                f"В компоненте 'visual' отсутствуют поля: {', '.join(sorted(missing_visual))}"
            )
        if not isinstance(visual["sprite"], str):
            raise ValueError("Поле 'visual.sprite' должно быть строкой")
        position = visual["position"]
        if not isinstance(position, (list, tuple)) or len(position) != 2:
            raise ValueError("Поле 'visual.position' должно содержать два числа")
        try:
            float(position[0])
            float(position[1])
            float(visual["scale"])
            float(visual["rotation"])
            float(visual["alpha"])
        except (TypeError, ValueError) as error:
            raise ValueError("Поля visual.position/scale/rotation/alpha должны быть числами") from error

    if "transform" in data:
        transform = data["transform"]
        if not isinstance(transform, dict) or set(transform) != {"polygon"}:
            raise ValueError("transform must contain only a polygon field")
        if "visual" not in data:
            raise ValueError("transform collider requires visual for position, rotation, and scale")
        validate_convex_polygon(transform["polygon"])

    if "scripting" in data:
        scripting = data["scripting"]
        if (not isinstance(scripting, dict)
                or not isinstance(scripting.get("scripts", []), list)
                or any(not isinstance(path, str) for path in scripting.get("scripts", []))):
            raise ValueError("Компонент 'scripting.scripts' должен быть списком путей")


class Stream:
    INDEX_PREFIX = "__engine_index__"

    def __add_identity_indexes(self, object_id, identity, object_type):
        if identity is None:
            return
        indexed_values = dict(identity)
        indexed_values.setdefault("type", object_type)
        for field_name in ("name", "class", "layer", "type"):
            value = indexed_values.get(field_name)
            if value is None:
                continue
            key = (self.INDEX_PREFIX, field_name, value)
            object_ids = list(self.objects_cache.get(key, ()))
            object_ids.append(object_id)
            self.objects_cache[key] = object_ids

    def __add_render_index(self, object_id, render_id):
        self.objects_cache[(self.INDEX_PREFIX, "render_id", render_id)] = object_id

    def __remove_identity_indexes(self, object_id, render_id):
        render_key = (self.INDEX_PREFIX, "render_id", render_id)
        if self.objects_cache.get(render_key) == object_id:
            self.objects_cache.pop(render_key, None)
        for field_name in ("name", "class", "layer", "type"):
            value = self.id_cache.get(render_id, field_name)
            key = (self.INDEX_PREFIX, field_name, value)
            object_ids = list(self.objects_cache.get(key, ()))
            if object_id not in object_ids:
                continue
            object_ids.remove(object_id)
            if object_ids:
                self.objects_cache[key] = object_ids
            else:
                self.objects_cache.pop(key, None)

    def print(self, *args):
        print(f"[STREAM  : {self.stream_time.get():.4f}]", *args, flush=True)

    def __check_queue(self):
        while True:
            try:
                data = self.main_in_stream.get()
            except queue.Empty:
                continue
            except (EOFError, BrokenPipeError, OSError):
                return
            if not isinstance(data, dict):
                continue

            try:
                match data.get("command"):
                    case "register":
                        self.__register(data)

                    case "add_script":
                        self.__add_script(data)

                    case "render":
                        self.__render(data)

                    case "remove":
                        self.__remove(data)

                    case "input":
                        self.__register_input_button(data)

            except Exception:
                traceback.print_exc()
                self.print('Ошибка очереди')

    def __register_input_button(self, data):
        key = data["key"]
        if data["type"] == "key_down" and key not in self.keyboard_input:
            self.keyboard_input.append(key)
        elif data["type"] == "key_up" and key in self.keyboard_input:
            self.keyboard_input.remove(key)

    def __register(self, data):
        id = data["id"]
        object = data["obj"]
        script_data = data.get("script_data", object.get("dict_data", {}))
        try:
            validate_object_data(script_data)
        except (TypeError, ValueError) as error:
            self.main_out_stream.put({id: {"error": str(error)}})
            raise

        with self.lock:
            object_id = self._next_object_id
            render_id = self._next_render_id
            self._next_object_id += 1
            self._next_render_id += 1

            object["_id"] = id
            object["_object_id"] = object_id
            object["_render_id"] = render_id
            object["_is_render"] = False
            object["_is_changed"] = False
            object["changes"] = []
            object["dict_data"].update({
                "object_id": object_id,
                "render_id": render_id,
            })
            object["dict_data"]["_id"] = id
            object["dict_data"]["_components"] = [
                component for component in ("id", "visual", "scripting", "transform")
                if component in object["dict_data"]
            ]
            object["dict_data"]["render_id"] = render_id
            object_type = object["dict_data"].get("object_type")
            identity = object["dict_data"].pop("id", None)
            visual = object["dict_data"].pop("visual", None)

            if identity is not None:
                for field_name in ("name", "layer", "type", "class"):
                    value = identity.get(field_name, object_type if field_name == "type" else None)
                    if value is not None:
                        self.id_cache.set(render_id, field_name, value)
                self.id_cache.set(render_id, "id", id)
                self.__add_identity_indexes(id, identity, object_type)

            if visual is not None:
                position = visual.get("position", [0, 0])
                self.visual_cache.set_many(render_id, {
                    "x": float(position[0]),
                    "y": float(position[1]),
                    "sim_time": self.stream_time.get(),
                    "rotation": float(visual.get("rotation", 0)),
                    "scale": float(visual.get("scale", 1)),
                    "alpha": float(visual.get("alpha", 1)),
                    "sprite": str(visual.get("sprite", "")),
                })

            self.objects_cache[id] = object
            self.__add_render_index(id, render_id)

        self.main_out_stream.put({id: (object_id, render_id)})

    def __add_script(self, data):
        diff = self.get_script_diff(data['path'])
        status_info = [*self.proccess_status_info]

        minimum = min(status_info)
        id = 0
        for i in range(len(status_info)):
            if status_info[i] == minimum:
                id = i
                break

        self.proccess_status_info[id] += diff
        self.procces_in_stream.put({
            'proc_id' : id,
            "path": data["path"],
            "id": data["id"],
            "obj_id" : data["obj_id"],
            "script_data": data["script_data"],
        })

    def __render(self, data):
        id = data["id"]

        with self.lock:
            object = self.objects_cache.get(id)
            if object is None:
                return
            
            object["_is_render"] = True
            self.objects_cache[id] = object
            render_id = object["_render_id"]

        visual = self.visual_cache.get_many(
            render_id,
            ("x", "y", "sim_time", "rotation", "scale", "alpha", "sprite"),
        )

        self.render_in_stream.put({
            "command": "add",
            "render_id": render_id,
            "path": visual["sprite"],
            "x": visual["x"],
            "y": visual["y"],
            "sim_time": visual["sim_time"],
            "scale": visual["scale"],
            "rotation": visual["rotation"],
            "alpha": visual["alpha"],
        })

    def __remove(self, data):
        id = data["id"]

        with self.lock:
            object = self.objects_cache.pop(id, None)
            if object is not None:
                self.__remove_identity_indexes(id, object["_render_id"])

        if object is not None:
            self.render_in_stream.put({
                "command": "remove",
                "render_id": object["_render_id"],
            })

    def __start_scripter_runners(self, process_count: int):
        for i in range(process_count):
            self.proccess_status_info.append(0)
        workers = []
        for worker_id in range(process_count):
            worker = multiprocessing.Process(
                target=scripter_runner.scripter_runner,
                args=(
                    self.time_key,
                    worker_id,
                    self.render_in_stream,
                    self.change_in_stream,
                    self.main_in_stream,
                    self.main_out_stream,
                    self.objects_cache,
                    self.procces_in_stream,
                    self.lock,
                    self.keyboard_input,
                    self.proccess_status_info,
                    self.visual_cache_data,
                    self.id_cache_data,
                    self.object_lock,
                    self.camera_state
                ),
                daemon=True,
            )
            worker.start()
            workers.append(worker)
        self.print(f"Процесы в колличестве {process_count} запущены")

    def __init_scene_objects(self, path: Path):
        self.print('Инициализация обьектов...')
        for i in range(1):
            for data in json.load(path.open(encoding="utf-8")):
                validate_object_data(data)
                GameObject(data).register()
        self.print('Все обьекты инициализированы')

    def get_script_diff(self, path):
        
            if path in self._script_complexity_cache:
                return self._script_complexity_cache[path]

            source_code = open(path).read()

            visitor = ComplexityVisitor.from_code(
                source_code
            )

            sum_index = 0

            for block in visitor.blocks:
                sum_index += block.complexity

            self._script_complexity_cache[path] = sum_index

            return sum_index
    
    def __init__(
        self,
        time_key,
        render_in_stream,
        change_in_stream,
        main_in_stream,
        main_out_stream,
        objects_cache,
        proccess_status_info,
        procces_in_stream,
        lock,
        keyboard_input,
        visual_cache,
        id_cache,
        object_lock,
        camera_state
    ):
        self.render_in_stream = render_in_stream
        self.change_in_stream = change_in_stream
        self.main_in_stream = main_in_stream
        self.main_out_stream = main_out_stream
        self.objects_cache = objects_cache
        self.proccess_status_info = proccess_status_info
        self.procces_in_stream = procces_in_stream
        self.lock = lock
        self.keyboard_input = keyboard_input
        self.time_key = time_key
        self.stream_time = ValueCache(time_key)
        self.visual_cache_data = visual_cache
        self.id_cache_data = id_cache
        self.visual_cache = ObjectCache(*visual_cache)
        self.id_cache = ObjectCache(*id_cache)
        self.object_lock = object_lock
        self.camera_state = camera_state

        self.camera_rotation = 0.0
        self.camera_scale = 1.0

        self._next_object_id = 0
        self._next_render_id = 0

        self._script_complexity_cache = {}

        cpu_count = os.cpu_count() or 1
        proccess_count = 1
        worker_count = max(1, min(proccess_count, cpu_count - 1))
        self.__start_scripter_runners(worker_count)

        threading.Thread(target=self.__check_queue, daemon=True).start()

        scene_path = Path(__file__).parent / "access" / "scenes" / SCENE_FILE
        self.__init_scene_objects(scene_path)

        GameObject({
            "object_type": "manager",
            "id": {
                "name": "camera_manager",
                "class": "CameraManager",
                "layer": "system",
                "type": "manager",
            },
            "scripting": {
                "scripts": ["access\\scripts\\camera.py"],
                "network": [],
            },
        }).register()

        while True:
            # self.print(Time)
            time.sleep(1)
