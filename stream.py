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
import scripter_runner
from memory_lib import ObjectCache, ValueCache

from radon.visitors import ComplexityVisitor


class Stream:
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
            identity = object["dict_data"].pop("id", None)
            visual = object["dict_data"].pop("visual", None)

            if identity is not None:
                for field_name in ("name", "layer", "type", "class"):
                    value = identity.get(field_name)
                    if value is not None:
                        self.id_cache.set(render_id, field_name, value)
                self.id_cache.set(render_id, "id", id)

            if visual is not None:
                position = visual.get("position", [0, 0])
                self.visual_cache.set(render_id, "x", float(position[0]))
                self.visual_cache.set(render_id, "y", float(position[1]))
                self.visual_cache.set(render_id, "rotation", float(visual.get("rotation", 0)))
                self.visual_cache.set(render_id, "scale", float(visual.get("scale", 1)))
                self.visual_cache.set(render_id, "alpha", float(visual.get("alpha", 1)))
                self.visual_cache.set(render_id, "sprite", str(visual.get("sprite", "")))

            self.objects_cache[id] = object

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

        visual = {
            field_name: self.visual_cache.get(render_id, field_name)
            for field_name in ("x", "y", "rotation", "scale", "alpha", "sprite")
        }

        self.render_in_stream.put({
            "command": "add",
            "render_id": render_id,
            "path": visual["sprite"],
            "x": visual["x"],
            "y": visual["y"],
            "scale": visual["scale"],
            "rotation": visual["rotation"],
            "alpha": visual["alpha"],
        })

    def __remove(self, data):
        id = data["id"]

        with self.lock:
            object = self.objects_cache.pop(id, None)

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
                    self.camera_queue,
                    self.visual_cache_data,
                    self.id_cache_data,
                    self.object_lock
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
        camera_queue,
        visual_cache,
        id_cache,
        object_lock
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
        self.camera_queue = camera_queue 
        self.visual_cache_data = visual_cache
        self.id_cache_data = id_cache
        self.visual_cache = ObjectCache(*visual_cache)
        self.id_cache = ObjectCache(*id_cache)
        self.object_lock = object_lock

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

        GameObject(
            {
            "scripting": {
                "scripts" : ["access\\scripts\\camera.py"],
                "network": []
                }
            }
        ).register()

        scene_path = Path(__file__).parent / "access" / "scenes" / "scene1.json"
        self.__init_scene_objects(scene_path)

        while True:
            # self.print(Time)
            time.sleep(1)
