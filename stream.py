from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import queue
import threading
import time
import traceback

from config import WORKER_READY_TIMEOUT, resolve_project_path
from pyengine import GameObject
from scene import (
    OBJECT_TYPE_RULES,
    custom_component_names,
    validate_cache_string,
    validate_object_data,
)
import scripter_runner
from memory_lib import ObjectCache, ValueCache

from radon.visitors import ComplexityVisitor


# Choose the scene loaded when the engine starts.
SCENE_FILE = "scene1.json"


class Stream:
    INDEX_PREFIX = "__engine_index__"

    def __add_identity_indexes(self, object_id, identity, object_type):
        if identity is None:
            return
        indexed_values = dict(identity)
        indexed_values.setdefault("type", object_type)
        for field_name in ("name", "class", "type"):
            value = indexed_values.get(field_name)
            if value is None:
                continue
            key = (self.INDEX_PREFIX, field_name, value)
            object_ids = list(self.objects_cache.get(key, ()))
            if object_id not in object_ids:
                object_ids.append(object_id)
            self.objects_cache[key] = object_ids

    def __add_render_index(self, object_id, render_id):
        self.objects_cache[(self.INDEX_PREFIX, "render_id", render_id)] = object_id

    def __remove_identity_indexes(self, object_id, render_id):
        render_key = (self.INDEX_PREFIX, "render_id", render_id)
        if self.objects_cache.get(render_key) == object_id:
            self.objects_cache.pop(render_key, None)
        for field_name in ("name", "class", "type"):
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
                data = self.main_in_stream.get(timeout=0.5)
            except queue.Empty:
                continue
            except (EOFError, BrokenPipeError, OSError):
                return
            if data is None or not isinstance(data, dict):
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
        event_type = data.get("type", "")
        if event_type.startswith("mouse_"):
            events_key = ("__engine_mouse__", "events")
            state_key = ("__engine_mouse__", "state")
            events = list(self.objects_cache.get(events_key) or ())
            sequence = events[-1]["sequence"] + 1 if events else 1
            event = {
                "sequence": sequence,
                "type": event_type,
                "render_id": data.get("render_id"),
                "x": float(data.get("x", 0)),
                "y": float(data.get("y", 0)),
                "screen_x": float(data.get("screen_x", 0)),
                "screen_y": float(data.get("screen_y", 0)),
                "button": data.get("button"),
                "delta_x": float(data.get("delta_x", 0)),
                "delta_y": float(data.get("delta_y", 0)),
                "scroll_x": float(data.get("scroll_x", 0)),
                "scroll_y": float(data.get("scroll_y", 0)),
            }
            events.append(event)
            self.objects_cache[events_key] = events[-128:]

            state = dict(self.objects_cache.get(state_key) or {})
            buttons = set(state.get("buttons", ()))
            if event_type == "mouse_down" and event["button"]:
                buttons.add(event["button"])
            elif event_type == "mouse_up" and event["button"]:
                buttons.discard(event["button"])
            state.update({
                "position": (event["x"], event["y"]),
                "screen_position": (event["screen_x"], event["screen_y"]),
                "hovered_render_id": event["render_id"],
                "buttons": tuple(buttons),
            })
            self.objects_cache[state_key] = state
            return

        key = data["key"]
        if data["type"] == "key_down" and key not in self.keyboard_input:
            self.keyboard_input.append(key)
        elif data["type"] == "key_up" and key in self.keyboard_input:
            self.keyboard_input.remove(key)

        if data["type"] in {"key_down", "key_up"}:
            events_key = ("__engine_keyboard__", "events")
            events = list(self.objects_cache.get(events_key) or ())
            sequence = events[-1]["sequence"] + 1 if events else 1
            events.append({
                "sequence": sequence,
                "type": data["type"],
                "key": key,
                "modifiers": data.get("modifiers", 0),
            })
            self.objects_cache[events_key] = events[-128:]

    def __register(self, data):
        request_id = data.get("request_id") or data.get("id")
        object_key = data.get("id")
        object = data.get("obj")

        def reply(payload):
            if request_id is not None:
                self.main_out_stream.put({request_id: payload})

        try:
            if not isinstance(request_id, str) or not request_id:
                raise ValueError("Регистрация требует непустой request_id")
            if not isinstance(object_key, str) or not object_key:
                raise ValueError("Регистрация требует непустой id")
            validate_cache_string(object_key, "id")
            if not isinstance(object, dict):
                raise ValueError("Регистрация требует объект 'obj'")
            dict_data = object.get("dict_data")
            if not isinstance(dict_data, dict):
                raise ValueError("Регистрация требует dict_data")

            script_data = data.get("script_data", dict_data)
            validate_object_data(script_data)

            with self.object_lock:
                if self.objects_cache.get(object_key) is not None:
                    raise ValueError(
                        f"Объект с id {object_key!r} уже зарегистрирован"
                    )
                capacity = min(self.visual_cache.capacity, self.id_cache.capacity)
                if self._next_render_id >= capacity:
                    raise ValueError(
                        "Закончились слоты VisualCache/IdCache. "
                        "Увеличьте размер общего кэша или уменьшите число объектов."
                    )

                numeric_object_id = self._next_object_id
                render_id = self._next_render_id
                self._next_object_id += 1
                self._next_render_id += 1

                object["_id"] = object_key
                object["_object_id"] = numeric_object_id
                object["_render_id"] = render_id
                object["_is_render"] = False
                object["_is_changed"] = False
                object["changes"] = []
                object["dict_data"].update({
                    "object_id": numeric_object_id,
                    "render_id": render_id,
                })
                object["dict_data"]["_id"] = object_key
                object["dict_data"]["_components"] = [
                    component
                    for component in ("id", "visual", "scripting", "transform", "components")
                    if component in object["dict_data"]
                ] + custom_component_names(object["dict_data"])
                object_type = object["dict_data"].get("object_type")
                identity = object["dict_data"].pop("id", None)
                object["dict_data"]["_identity"] = identity or {}
                visual = object["dict_data"].pop("visual", None)

                if identity is not None:
                    for field_name in ("name", "type", "class"):
                        value = identity.get(
                            field_name,
                            object_type if field_name == "type" else None,
                        )
                        if value is not None:
                            self.id_cache.set(render_id, field_name, value)
                    self.id_cache.set(render_id, "id", object_key)
                    self.__add_identity_indexes(object_key, identity, object_type)

                if visual is not None:
                    position = visual.get("position", [0, 0])
                    self.visual_cache.set_many(render_id, {
                        "x": float(position[0]),
                        "y": float(position[1]),
                        "sim_time": self.stream_time.get(),
                        "tick": int(self.tick_cache.get()),
                        "rotation": float(visual.get("rotation", 0)),
                        "scale": float(visual.get("scale", 1)),
                        "alpha": float(visual.get("alpha", 1)),
                        "sprite": str(visual.get("sprite", "")),
                        "layer": float(visual.get("layer", 0)),
                    })

                self.objects_cache[object_key] = object
                self.__add_render_index(object_key, render_id)
        except (TypeError, ValueError) as error:
            reply({"error": str(error)})
        except Exception as error:
            traceback.print_exc()
            reply({"error": f"Ошибка регистрации: {error}"})
        else:
            reply({
                "object_id": numeric_object_id,
                "render_id": render_id,
            })

    def __add_script(self, data):
        path = data.get("path")
        if not isinstance(path, str) or not path:
            self.print("add_script: не указан путь к скрипту")
            return

        try:
            # Script paths in scenes are project-relative, not cwd-relative.
            resolved_path = resolve_project_path(path)
            complexity = self.get_script_diff(resolved_path)
        except Exception as error:
            self.print(f"Не удалось прочитать скрипт {path!r}: {error}")
            traceback.print_exc()
            return

        status_info = list(self.proccess_status_info)
        if not status_info:
            self.print("Нет доступных процессов для скриптов")
            return

        available = [
            worker_id
            for worker_id in range(len(status_info))
            if worker_id < len(self.worker_ready) and self.worker_ready[worker_id]
        ]
        if not available:
            self.print("Нет живых процессов для скриптов")
            return

        worker_id = min(available, key=status_info.__getitem__)
        with self.lock:
            self.proccess_status_info[worker_id] += complexity

        try:
            self.worker_queues[worker_id].put({
                'proc_id': worker_id,
                "path": str(resolved_path),
                "id": data["id"],
                "obj_id": data["obj_id"],
                "script_data": data["script_data"],
                "complexity": complexity,
            })
        except Exception as error:
            with self.lock:
                current = self.proccess_status_info[worker_id]
                self.proccess_status_info[worker_id] = max(0, current - complexity)
            self.print(f"Не удалось передать скрипт {path!r} воркеру: {error}")
            traceback.print_exc()

    def __render(self, data):
        id = data["id"]

        with self.object_lock:
            object = self.objects_cache.get(id)
            if object is None:
                return

            dict_data = object.get("dict_data", {})
            components = dict_data.get("_components", ())
            if "visual" not in components:
                return

            object["_is_render"] = True
            self.objects_cache[id] = object
            render_id = object["_render_id"]

            # Only objects with their own script loop publish a tick
            # heartbeat every fixed step.  Sporadically updated visuals
            # (for example an idle camera target) must not hold back the
            # shared render tick.
            scripting = dict_data.get("scripting") or {}
            scripted = (
                bool(scripting.get("scripts"))
                or bool(dict_data.get("components"))
                or bool(custom_component_names(dict_data))
            )

        visual = self.visual_cache.get_many(
            render_id,
            ("x", "y", "sim_time", "tick", "rotation", "scale", "alpha", "sprite", "layer"),
        )

        self.render_in_stream.put({
            "command": "add",
            "render_id": render_id,
            "path": visual["sprite"],
            "x": visual["x"],
            "y": visual["y"],
            "sim_time": visual["sim_time"],
            "tick": visual["tick"],
            "scripted": scripted,
            "scale": visual["scale"],
            "rotation": visual["rotation"],
            "alpha": visual["alpha"],
            "layer": visual["layer"],
        })

    def __remove(self, data):
        id = data["id"]

        with self.object_lock:
            object = self.objects_cache.pop(id, None)
            render_id = None
            if object is not None:
                render_id = object["_render_id"]
                self.__remove_identity_indexes(id, render_id)
                if self.camera_target_cache.get() == render_id:
                    self.camera_target_cache.set(-1)

        if object is not None:
            self.render_in_stream.put({
                "command": "remove",
                "render_id": render_id,
            })

    def __start_scripter_runners(self):
        worker_count = len(self.worker_queues)
        while len(self.proccess_status_info) < worker_count:
            self.proccess_status_info.append(0)
        while len(self.worker_ready) < worker_count:
            self.worker_ready.append(False)

        workers = []
        for worker_id, worker_queue in enumerate(self.worker_queues):
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
                    worker_queue,
                    self.worker_ready,
                    self.lock,
                    self.keyboard_input,
                    self.proccess_status_info,
                    self.visual_cache_data,
                    self.id_cache_data,
                    self.object_lock,
                    self.camera_state,
                    self.tick_key
                ),
                daemon=True,
            )
            worker.start()
            workers.append(worker)

        deadline = time.monotonic() + WORKER_READY_TIMEOUT
        while worker_count and not all(
            self.worker_ready[worker_id] for worker_id in range(worker_count)
        ):
            if not all(worker.is_alive() for worker in workers):
                self.print("Скриптовый процесс завершился во время запуска")
                break
            if time.monotonic() > deadline:
                self.print("Не все скриптовые процессы сообщили о готовности")
                break
            time.sleep(0.05)

        self.print(f"Процессы в количестве {worker_count} запущены")

    def __dependencies(self):
        return {
            "render_in_stream": self.render_in_stream,
            "main_in_stream": self.main_in_stream,
            "main_out_stream": self.main_out_stream,
            "objects_cache": self.objects_cache,
            "lock": self.object_lock,
            "visual_cache": self.visual_cache,
            "id_cache": self.id_cache,
            "time_cache": self.stream_time,
            "tick_cache": self.tick_cache,
        }

    def __init_scene_objects(self, path: Path):
        self.print('Инициализация обьектов...')
        with path.open(encoding="utf-8") as scene_file:
            scene = json.load(scene_file)
        if not isinstance(scene, list):
            raise TypeError("Файл сцены должен содержать JSON-массив объектов")

        dependencies = self.__dependencies()
        for index, data in enumerate(scene):
            try:
                validate_object_data(data)
                GameObject(data, deps=dependencies).register()
            except Exception as error:
                self.print(f"Объект #{index} не загружен: {error}")
        self.print('Все обьекты инициализированы')

    def get_script_diff(self, path):
        resolved_path = resolve_project_path(path)
        cache_key = str(resolved_path)
        if cache_key in self._script_complexity_cache:
            return self._script_complexity_cache[cache_key]

        with resolved_path.open(encoding="utf-8") as source_file:
            source_code = source_file.read()

        visitor = ComplexityVisitor.from_code(source_code)

        sum_index = 0
        for block in visitor.blocks:
            sum_index += block.complexity

        self._script_complexity_cache[cache_key] = sum_index
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
        worker_queues,
        worker_ready,
        lock,
        keyboard_input,
        visual_cache,
        id_cache,
        object_lock,
        camera_state,
        tick_key
    ):
        self.render_in_stream = render_in_stream
        self.change_in_stream = change_in_stream
        self.main_in_stream = main_in_stream
        self.main_out_stream = main_out_stream
        self.objects_cache = objects_cache
        self.proccess_status_info = proccess_status_info
        self.worker_queues = worker_queues
        self.worker_ready = worker_ready
        self.lock = lock
        self.keyboard_input = keyboard_input
        self.time_key = time_key
        self.stream_time = ValueCache(time_key)
        self.tick_key = tick_key
        self.tick_cache = ValueCache(tick_key)
        self.visual_cache_data = visual_cache
        self.id_cache_data = id_cache
        self.visual_cache = ObjectCache(*visual_cache)
        self.id_cache = ObjectCache(*id_cache)
        self.object_lock = object_lock
        self.camera_state = camera_state
        self.camera_target_cache = ValueCache(camera_state[0])

        self.camera_rotation = 0.0
        self.camera_scale = 1.0

        self._next_object_id = 0
        self._next_render_id = 0

        self._script_complexity_cache = {}

        self.__start_scripter_runners()

        threading.Thread(target=self.__check_queue, daemon=True).start()

        scene_path = Path(__file__).parent / "access" / "scenes" / SCENE_FILE
        try:
            self.__init_scene_objects(scene_path)
            while True:
                time.sleep(1)
        finally:
            self.stream_time.detach()
            self.tick_cache.detach()
            self.visual_cache.detach()
            self.id_cache.detach()
            self.camera_target_cache.detach()
