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


class Stream:
    def print(self, *args):
        print(f"[STREAM  : {self.stream_time.value:.4f}]", *args, flush=True)

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
            self.objects_cache[id] = object

        self.main_out_stream.put({id: (object_id, render_id)})

    def __add_script(self, data):
        self.procces_in_stream.put({
            "path": data["path"],
            "id": data["id"],
            "obj_id" : data["obj_id"],
        })

    def __render(self, data):
        id = data["id"]

        with self.lock:
            object = self.objects_cache.get(id)
            if object is None:
                return
            
            object["_is_render"] = True
            self.objects_cache[id] = object
            visual = dict(object["_visual"])
            render_id = object["_render_id"]

        self.render_in_stream.put({
            "command": "add",
            "render_id": render_id,
            "path": visual["sprite"],
            "x": visual["position"].x,
            "y": visual["position"].y,
            "scale": visual["scale"],
            "rotation": visual["rotation"],
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
                    self.stream_time,
                    worker_id,
                    self.render_in_stream,
                    self.render_out_stream,
                    self.main_in_stream,
                    self.main_out_stream,
                    self.objects_cache,
                    self.procces_in_stream,
                    self.managers_stream,
                    self.lock,
                    self.keyboard_input,
                    self.proccess_status_info,
                    self.camera_queue,
                ),
                daemon=True,
            )
            worker.start()
            workers.append(worker)
        self.print(f"Процесы в колличестве {process_count} запущены")

    def __init_scene_objects(self, path: Path):
        self.print('Инициализация обьектов...')
        for i in range(100):
            for data in json.load(path.open(encoding="utf-8")):

                GameObject(data).register()
        self.print('Все обьекты инициализированы')

    def __check_camera_queue(self):
        while True:
            try:
                data = self.camera_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            except (EOFError, BrokenPipeError, OSError):
                return

            if type(data) != dict:
                continue

            try:
                match data.get("command"):
                    case "camera_move":
                        self.camera_x += data["x"]
                        self.camera_y += data["y"]

                        self.__send_camera_update()

                    case "camera_scale":
                        self.camera_scale = max(0.01, data["scale"])
                        self.__send_camera_update()

            except Exception:
                traceback.print_exc()
                self.print("Ошибка ChangeQueue")

    def __send_camera_update(self):
        self.render_in_stream.put({
            "command":  "camera_update",
            "position": [self.camera_x, self.camera_y],
            "rotation": self.camera_rotation,
            "scale":    self.camera_scale
        })

    def __init__(
        self,
        Time,
        render_in_stream,
        render_out_stream,
        main_in_stream,
        main_out_stream,
        objects_cache,
        proccess_status_info,
        procces_in_stream,
        managers_stream,
        lock,
        keyboard_input,
        camera_queue,
    ):
        self.render_in_stream = render_in_stream
        self.render_out_stream = render_out_stream
        self.main_in_stream = main_in_stream
        self.main_out_stream = main_out_stream
        self.objects_cache = objects_cache
        self.proccess_status_info = proccess_status_info
        self.procces_in_stream = procces_in_stream
        self.managers_stream = managers_stream
        self.lock = lock
        self.keyboard_input = keyboard_input
        self.stream_time = Time

        self.camera_queue = camera_queue 
        self.camera_x = 0.0
        self.camera_y = 0.0
        self.camera_rotation = 0.0
        self.camera_scale = 1.0

        self._next_object_id = 0
        self._next_render_id = 0

        cpu_count = os.cpu_count() or 1
        proccess_count = 5
        worker_count = max(1, min(proccess_count, cpu_count - 1))
        self.__start_scripter_runners(worker_count)

        threading.Thread(target=self.__check_queue, daemon=True).start()
        threading.Thread(target=self.__check_camera_queue, daemon=True).start()


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