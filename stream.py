from __future__ import annotations

import json
import multiprocessing
import os
from collections import deque
from multiprocessing.managers import SyncManager
from pathlib import Path
import queue
import threading
import time
import traceback

from config import (
    KEYBOARD_EVENT_DOWN,
    KEYBOARD_EVENT_UP,
    MOUSE_MOTION_STRUCT,
    PRESSED_KEY_FIELDS,
    WORKER_READY_TIMEOUT,
    pack_pressed_keys,
    resolve_project_path,
    unpack_pressed_keys,
)
from pyengine import GameObject
from scene import (
    OBJECT_TYPE_RULES,
    custom_component_names,
    validate_cache_string,
    validate_object_data,
)
import scripter_runner
from memory_lib import FieldView, ObjectCache, ValueCache

from radon.visitors import ComplexityVisitor


# Выбор сцены, загружаемой при старте движка. Переопределить для одного
# запуска: PYENGINE_SCENE=stress_motion.json (файл лежит в access/scenes).
SCENE_FILE = os.environ.get("PYENGINE_SCENE") or "scene1.json"


# Небольшие buckets идентичности остаются обычными списками: создание
# manager.list стоит нового подключения к Manager (~3 мс в Windows), что
# дороже копирования короткого списка. Когда значение разделяют достаточно
# объектов, bucket один раз повышается, и каждое следующее добавление — O(1).
INDEX_BUCKET_PROMOTE_AT = 16


class Stream:
    INDEX_PREFIX = "__engine_index__"

    @staticmethod
    def __connect_index_manager(address, authkey):
        """Создать клиент Manager для buckets индекса идентичности.

        Процесс Stream получает прокси, созданные рендерером, но не может
        создавать через них новые *серверные* объекты: прокси, созданный в
        процессе Stream, был бы его собственностью и исчез бы с его выходом.
        Второй клиент, подключённый к тому же серверу, создаёт buckets
        ``manager.list``, которые живут в сервере Manager и остаются
        действительными для всех читателей.
        """

        if address is None or authkey is None:
            return None
        try:
            manager = SyncManager(address=address, authkey=authkey)
            manager.connect()
            return manager
        except Exception as error:
            print(
                f"[STREAM  ] Manager недоступен для индексов, "
                f"используется совместимый режим: {error}",
                flush=True,
            )
            return None

    def __add_index_value(self, key, object_id):
        """Добавить в bucket идентичности, не переписывая большие buckets.

        Небольшие buckets — обычные списки: создание прокси ``manager.list``
        стоит нового подключения к Manager, что медленнее копирования
        нескольких id. Когда значение становится общим, bucket один раз
        повышается, и каждое следующее добавление переносит только один id
        вместо всего списка.
        """

        manager = getattr(self, "_index_manager", None)
        if manager is None:
            # Запасной путь в процессе для unit-тестов и нестандартных встраиваний.
            bucket = list(self.objects_cache.get(key, ()))
            if object_id not in bucket:
                bucket.append(object_id)
            self.objects_cache[key] = bucket
            return

        # Stream — единственный писатель, поэтому он может сам хранить
        # повышенные прокси и обходиться без одного запроса к Manager на объект.
        buckets = getattr(self, "_index_buckets", None)
        if buckets is None:
            buckets = {}
            self._index_buckets = buckets

        bucket = buckets.get(key)
        if bucket is not None:
            bucket.append(object_id)
            return

        values = self.objects_cache.get(key)
        if values is None:
            self.objects_cache[key] = [object_id]
            return

        if not isinstance(values, list):
            # Уже повышено, но этот процесс потерял свой локальный кэш.
            buckets[key] = values
            values.append(object_id)
            return

        values = list(values)
        values.append(object_id)
        if len(values) >= INDEX_BUCKET_PROMOTE_AT:
            bucket = manager.list(values)
            # Публикуем повышенный bucket до отказа от обычного списка.
            self.objects_cache[key] = bucket
            buckets[key] = bucket
        else:
            self.objects_cache[key] = values

    def __remove_index_value(self, key, object_id):
        manager = getattr(self, "_index_manager", None)
        buckets = getattr(self, "_index_buckets", None) or {}
        bucket = buckets.get(key)
        if bucket is None:
            bucket = self.objects_cache.get(key)
        if bucket is None:
            return

        if manager is None or isinstance(bucket, list):
            bucket = list(bucket)
            if object_id not in bucket:
                return
            bucket.remove(object_id)
            if bucket:
                self.objects_cache[key] = bucket
            else:
                self.objects_cache.pop(key, None)
            return

        try:
            bucket.remove(object_id)
        except ValueError:
            return
        if not bucket:
            self.objects_cache.pop(key, None)
            buckets.pop(key, None)

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
            self.__add_index_value(key, object_id)

    def __add_render_index(self, object_id, render_id):
        self.objects_cache[(self.INDEX_PREFIX, "render_id", render_id)] = object_id

    def __remove_identity_indexes(self, object_id, render_id):
        render_key = (self.INDEX_PREFIX, "render_id", render_id)
        if self.objects_cache.get(render_key) == object_id:
            self.objects_cache.pop(render_key, None)
        # Тесты и клиенты совместимости могут собрать Stream без визуального
        # кэша; buckets слоёв существуют, только если он был зарегистрирован.
        visual_cache = getattr(self, "visual_cache", None)
        if visual_cache is not None:
            layer = visual_cache.get(render_id, "layer")
            if layer is not None:
                self.__remove_index_value(
                    (self.INDEX_PREFIX, "layer", float(layer)), object_id
                )
        for field_name in ("name", "class", "type"):
            value = self.id_cache.get(render_id, field_name)
            if value is None:
                continue
            key = (self.INDEX_PREFIX, field_name, value)
            self.__remove_index_value(key, object_id)

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

                    case "register_many":
                        self.__register_many(data)

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
            event = {
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
            is_motion = event_type == "mouse_move"
            # Новые рендереры публикуют снимок указателя прямо в shared
            # memory и отмечают событие, поэтому лишнюю запись здесь делают
            # только устаревшие клиенты.
            if not data.get("pointer_published"):
                self.__publish_pointer(event, is_motion)

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

            if is_motion and self.motion_cache is not None:
                # Движение передаётся через shared memory; вне списка событий
                # Manager оно не будит путь клавиатуры и не заставляет каждый
                # скрипт копировать события.
                return

            events = list(self.objects_cache.get(events_key) or ())
            sequence = events[-1]["sequence"] + 1 if events else 1
            event["sequence"] = sequence
            # Скриптам нужна только новейшая позиция указателя. Хранение
            # всех событий движения заставляло каждый скрипт копировать из
            # Manager полный список из 128 событий и проигрывать устаревшие.
            if is_motion:
                events = [
                    pending for pending in events
                    if pending["type"] != "mouse_move"
                ]
            events.append(event)
            self.objects_cache[events_key] = events[-128:]
            self.__bump_input_sequence()
            return

        key = data["key"]
        event_type = data["type"]
        if event_type == "key_down":
            self._pressed_keys.add(key)
        elif event_type == "key_up":
            self._pressed_keys.discard(key)

        if event_type in {"key_down", "key_up"}:
            if self.keyboard_ring is not None:
                self.__publish_keyboard_event(data)
            else:
                events_key = ("__engine_keyboard__", "events")
                events = list(self.objects_cache.get(events_key) or ())
                sequence = events[-1]["sequence"] + 1 if events else 1
                events.append({
                    "sequence": sequence,
                    "type": event_type,
                    "key": key,
                    "modifiers": data.get("modifiers", 0),
                })
                self.objects_cache[events_key] = events[-128:]
                self._keyboard_sequence += 1
                self.__publish_pressed_keys(self._keyboard_sequence)
        self.__bump_input_sequence()

    def __publish_pressed_keys(self, sequence):
        """Опубликовать снимок нажатых клавиш и его последовательность атомарно.

        Скрипты читают количество, а затем ровно столько строковых полей,
        поэтому устаревшие фрагменты прошлого снимка не попадают в набор
        клавиш.
        """

        chunks = pack_pressed_keys(self._pressed_keys)
        values = {
            "key_count": len(chunks),
            "keyboard_sequence": sequence,
        }
        for index, chunk in enumerate(chunks):
            values[PRESSED_KEY_FIELDS[index]] = chunk
        self.engine_state.set_many(0, values)

    def __publish_keyboard_event(self, data):
        """Добавить одно событие клавиши в кольцо shared memory."""

        self._keyboard_sequence += 1
        segment_id = self._keyboard_sequence % self.keyboard_ring.capacity
        self.keyboard_ring.set_many(segment_id, {
            "sequence": self._keyboard_sequence,
            "type": (
                KEYBOARD_EVENT_DOWN
                if data["type"] == "key_down"
                else KEYBOARD_EVENT_UP
            ),
            "key": data["key"],
            "modifiers": int(data.get("modifiers", 0)),
        })
        # Пишем событие в кольцо до продвижения последовательности для скриптов,
        # чтобы новая последовательность всегда указывала на видимое событие.
        self.__publish_pressed_keys(self._keyboard_sequence)

    def __publish_pointer(self, event, motion):
        """Опубликовать последний снимок указателя в shared memory.

        ``seq`` продвигается только при движении, поэтому скрипты могут
        отличать перемещение от кликов и событий колеса, не обращаясь
        к Manager.
        """

        if self.motion_cache is None:
            return
        if motion:
            # Рендерер мог уже продвинуть последовательность через прямой
            # путь; никогда не двигаем её назад для устаревшего клиента.
            observed = int(self.motion_cache.get(0, "seq"))
            self._motion_sequence = max(self._motion_sequence, observed) + 1
        self.motion_cache.set_many(0, {
            "seq": self._motion_sequence,
            "x": event["x"],
            "y": event["y"],
            "dx": event["delta_x"],
            "dy": event["delta_y"],
            "screen_x": event["screen_x"],
            "screen_y": event["screen_y"],
            "hovered": (
                -1 if event["render_id"] is None else int(event["render_id"])
            ),
        })

    def __bump_input_sequence(self):
        """Будить циклы скриптов, только когда пришло реальное событие ввода."""

        self._input_sequence += 1
        try:
            self.engine_state.set(0, "input_sequence", self._input_sequence)
        except (EOFError, BrokenPipeError, OSError):
            pass

    def __bump_object_generation(self):
        """Будить циклы скриптов, когда объект мог быть удалён.

        Проверки живости раньше стоили одного вызова Manager на скрипт каждые
        ``OBJECT_LIVENESS_PERIOD``. Теперь скрипты обращаются к Manager, только
        когда меняется этот общий счётчик, а происходит это при реальных
        удалениях.
        """

        self._object_generation += 1
        try:
            self.engine_state.set(
                0, "object_generation", self._object_generation
            )
        except (EOFError, BrokenPipeError, OSError):
            pass

    def __prepare_registration(self, data):
        request_id = data.get("request_id") or data.get("id")
        object_key = data.get("id")
        object = data.get("obj")

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

        # Старые клиенты присылали вторую копию объекта в ``script_data``.
        # Запись уже содержит тот же dict, поэтому новые клиенты его опускают.
        script_data = data.get("script_data", dict_data)
        validate_object_data(script_data)
        return object_key, object

    def __ensure_id_state(self):
        """Лениво создаёт состояние free-list для тестовых Stream без __init__."""

        if not hasattr(self, "_free_render_ids"):
            self._free_render_ids = deque()
        if not hasattr(self, "_render_generations"):
            self._render_generations = {}

    def __available_render_slots(self, capacity):
        self.__ensure_id_state()
        return len(self._free_render_ids) + max(
            0, capacity - self._next_render_id
        )

    def __allocate_render_id(self, capacity):
        self.__ensure_id_state()
        if self._free_render_ids:
            render_id = self._free_render_ids.popleft()
        else:
            if self._next_render_id >= capacity:
                raise ValueError(
                    "Закончились слоты VisualCache/IdCache. "
                    "Увеличьте размер общего кэша или уменьшите число объектов."
                )
            render_id = self._next_render_id
            self._next_render_id += 1

        generation = self._render_generations.get(render_id, 0) + 1
        self._render_generations[render_id] = generation
        return render_id, generation

    def __register_one(self, object_key, object, check_duplicate=True, state=None):
        dict_data = object["dict_data"]

        if check_duplicate and self.objects_cache.get(object_key) is not None:
            raise ValueError(
                f"Объект с id {object_key!r} уже зарегистрирован"
            )
        capacity = min(self.visual_cache.capacity, self.id_cache.capacity)
        render_id, generation = self.__allocate_render_id(capacity)

        numeric_object_id = self._next_object_id
        self._next_object_id += 1

        object["_id"] = object_key
        object["_object_id"] = numeric_object_id
        object["_render_id"] = render_id
        object["_generation"] = generation
        object["_is_render"] = False
        object["_is_changed"] = False
        object["changes"] = []
        object["dict_data"].update({
            "object_id": numeric_object_id,
            "render_id": render_id,
            "_generation": generation,
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
            if state is None:
                state = self.engine_state.get_many(0, ("time", "tick"))
            layer = float(visual.get("layer", 0))
            self.visual_cache.set_many(render_id, {
                "x": float(position[0]),
                "y": float(position[1]),
                "sim_time": state["time"],
                "tick": int(state["tick"]),
                "rotation": float(visual.get("rotation", 0)),
                "scale": float(visual.get("scale", 1)),
                "alpha": float(visual.get("alpha", 1)),
                "sprite": str(visual.get("sprite", "")),
                "layer": layer,
            })
            self.__add_index_value(
                (self.INDEX_PREFIX, "layer", layer), object_key
            )

        self.objects_cache[object_key] = object
        self.__add_render_index(object_key, render_id)
        return {
            "object_id": numeric_object_id,
            "render_id": render_id,
            "generation": generation,
        }

    def __register(self, data):
        request_id = data.get("request_id") or data.get("id")
        try:
            object_key, object = self.__prepare_registration(data)
            state = self.engine_state.get_many(0, ("time", "tick"))
            with self.object_lock:
                payload = self.__register_one(object_key, object, state=state)
        except (TypeError, ValueError) as error:
            payload = {"error": str(error)}
        except Exception as error:
            traceback.print_exc()
            payload = {"error": f"Ошибка регистрации: {error}"}

        if request_id is not None:
            self.main_out_stream.put({request_id: payload})

    def __register_many(self, data):
        request_id = data.get("request_id")
        objects = data.get("objects")
        try:
            if not isinstance(request_id, str) or not request_id:
                raise ValueError("Пакетная регистрация требует непустой request_id")
            if not isinstance(objects, list) or not objects:
                raise ValueError(
                    "Пакетная регистрация требует непустой список objects"
                )

            # Один снимок часов на весь пакет. Отдельные чтения добавили бы
            # тысячи проходов блокировки shared memory при загрузке сцены.
            state = self.engine_state.get_many(0, ("time", "tick"))

            with self.object_lock:
                # Проверяем весь пакет до изменения общего состояния, чтобы
                # дубликат или опечатка не оставили пакет наполовину
                # зарегистрированным.
                prepared = []
                seen = set()
                for index, item in enumerate(objects):
                    if not isinstance(item, dict):
                        raise ValueError(
                            f"Элемент списка #{index} должен быть JSON-объектом"
                        )
                    try:
                        object_key, cached_object = self.__prepare_registration(
                            item
                        )
                    except (TypeError, ValueError) as error:
                        raise ValueError(
                            f"Объект #{index}: {error}"
                        ) from error
                    if object_key in seen:
                        raise ValueError(
                            f"Объект {object_key!r} повторяется в пакете"
                        )
                    seen.add(object_key)
                    if self.objects_cache.get(object_key) is not None:
                        raise ValueError(
                            f"Объект с id {object_key!r} уже зарегистрирован"
                        )
                    prepared.append((object_key, cached_object))

                capacity = min(self.visual_cache.capacity, self.id_cache.capacity)
                if len(prepared) > self.__available_render_slots(capacity):
                    raise ValueError(
                        "Закончились слоты VisualCache/IdCache. "
                        "Увеличьте размер общего кэша или уменьшите число объектов."
                    )

                results = []
                for object_key, cached_object in prepared:
                    try:
                        results.append(
                            self.__register_one(
                                object_key,
                                cached_object,
                                check_duplicate=False,
                                state=state,
                            )
                        )
                    except Exception as error:
                        traceback.print_exc()
                        results.append({
                            "error": f"Ошибка регистрации: {error}",
                        })
            payload = {"objects": results}
        except (TypeError, ValueError) as error:
            payload = {"error": str(error)}
        except Exception as error:
            traceback.print_exc()
            payload = {"error": f"Ошибка пакетной регистрации: {error}"}

        if request_id is not None:
            self.main_out_stream.put({request_id: payload})

    def __add_script(self, data):
        path = data.get("path")
        if not isinstance(path, str) or not path:
            self.print("add_script: не указан путь к скрипту")
            return

        try:
            # Пути скриптов в сценах относительны проекта, а не текущего каталога.
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
        generation = data.get("generation")

        with self.object_lock:
            object = self.objects_cache.get(id)
            if object is None:
                return
            if (generation is not None
                    and object.get("_generation") != generation):
                return

            dict_data = object.get("dict_data", {})
            components = dict_data.get("_components", ())
            if "visual" not in components:
                return

            object["_is_render"] = True
            self.objects_cache[id] = object
            render_id = object["_render_id"]

            # Владение скриптом по-прежнему публикуется для совместимости, но
            # общий render tick больше не зависит от heartbeat каждого объекта.
            # Простаивающий объект просто перестаёт давать визуальные обновления.
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
        generation = data.get("generation")

        with self.object_lock:
            object = self.objects_cache.get(id)
            if object is None:
                return
            # Команда от старого скрипта не должна удалять новый объект,
            # занявший тот же слот после переиспользования render_id.
            if (generation is not None
                    and object.get("_generation") != generation):
                return
            object = self.objects_cache.pop(id, None)
            render_id = None
            if object is not None:
                render_id = object["_render_id"]
                self.__remove_identity_indexes(id, render_id)
                if self.camera_target_cache.get() == render_id:
                    self.camera_target_cache.set(-1)

        if object is not None:
            self.__bump_object_generation()
            self.render_in_stream.put({
                "command": "remove",
                "render_id": render_id,
            })
            if render_id is not None:
                self.__ensure_id_state()
                self._free_render_ids.append(render_id)

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
                    self.engine_state_data,
                    self.motion_cache_data,
                    self.keyboard_ring_data,
                    worker_id,
                    self.render_in_stream,
                    self.change_in_stream,
                    self.main_in_stream,
                    self.main_out_stream,
                    self.objects_cache,
                    worker_queue,
                    self.worker_ready,
                    self.lock,
                    self.proccess_status_info,
                    self.visual_cache_data,
                    self.id_cache_data,
                    self.object_lock,
                    self.camera_state,
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
        objects = []
        for index, data in enumerate(scene):
            try:
                validate_object_data(data)
                objects.append(GameObject(data, deps=dependencies))
            except Exception as error:
                self.print(f"Объект #{index} не загружен: {error}")

        try:
            # Один проход регистрации вместо одного на каждый объект сцены.
            GameObject.register_many(objects)
        except Exception as error:
            self.print(f"Пакетная регистрация не удалась: {error}")
            for index, game_object in enumerate(objects):
                try:
                    game_object.register()
                except Exception as register_error:
                    self.print(f"Объект #{index} не загружен: {register_error}")
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
        engine_state,
        render_in_stream,
        change_in_stream,
        main_in_stream,
        main_out_stream,
        objects_cache,
        proccess_status_info,
        worker_queues,
        worker_ready,
        lock,
        visual_cache,
        id_cache,
        object_lock,
        camera_state,
        motion_cache_data=None,
        keyboard_ring_data=None,
        manager_address=None,
        manager_authkey=None,
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
        self.engine_state_data = engine_state
        self.engine_state = ObjectCache(*engine_state)
        self.stream_time = FieldView(self.engine_state, "time")
        self.tick_cache = FieldView(self.engine_state, "tick")
        self.motion_cache_data = motion_cache_data
        self.keyboard_ring_data = keyboard_ring_data
        self.motion_cache = (
            ObjectCache(*motion_cache_data)
            if motion_cache_data is not None else None
        )
        self.keyboard_ring = (
            ObjectCache(*keyboard_ring_data)
            if keyboard_ring_data is not None else None
        )
        self._input_sequence = int(
            self.engine_state.get(0, "input_sequence")
        )
        self._object_generation = int(
            self.engine_state.get(0, "object_generation")
        )
        self._keyboard_sequence = int(
            self.engine_state.get(0, "keyboard_sequence")
        )
        self._motion_sequence = (
            int(self.motion_cache.get(0, "seq"))
            if self.motion_cache is not None else 0
        )
        # Перезагруженный Stream может подключиться к сегменту, где клавиши
        # уже удерживаются. Заполняем локальный набор из опубликованного снимка.
        self._pressed_keys = set()
        key_count = int(self.engine_state.get(0, "key_count"))
        if key_count > 0:
            self._pressed_keys = set(unpack_pressed_keys(
                self.engine_state.get_many(
                    0, PRESSED_KEY_FIELDS[:key_count]
                ).values()
            ))
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
        # Свободные render_id переиспользуются, а не выдаются монотонно.
        # Поколение на слот защищает от stale-скриптов, которые пережили
        # удаление своего объекта.
        self._free_render_ids = deque()
        self._render_generations = {}

        self._script_complexity_cache = {}
        self._index_manager = self.__connect_index_manager(
            manager_address, manager_authkey
        )
        # Сообщает find_object("layer") на стороне скриптов, что buckets слоёв есть.
        self.objects_cache[(self.INDEX_PREFIX, "layer", "__ready__")] = True

        self.__start_scripter_runners()

        threading.Thread(target=self.__check_queue, daemon=True).start()

        scene_path = Path(__file__).parent / "access" / "scenes" / SCENE_FILE
        try:
            self.__init_scene_objects(scene_path)
            while True:
                time.sleep(1)
        finally:
            self.engine_state.detach()
            if self.motion_cache is not None:
                self.motion_cache.detach()
            if self.keyboard_ring is not None:
                self.keyboard_ring.detach()
            self.visual_cache.detach()
            self.id_cache.detach()
            self.camera_target_cache.detach()
