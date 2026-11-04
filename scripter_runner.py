import importlib.util
import queue
import sys
import threading
import traceback

from config import PROJECT_DIR, resolve_project_path
from memory_lib import ObjectCache, ValueCache


class KeyboardSnapshot:
    """Keep a local key snapshot so scripts don't poll a Manager proxy."""

    def __init__(self, keyboard_input, poll_rate=1 / 120):
        self._keyboard_input = keyboard_input
        self._pressed = frozenset(keyboard_input[:])
        self._poll_rate = poll_rate
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._poll,
            name="keyboard-snapshot",
            daemon=True,
        )
        self._thread.start()

    def _poll(self):
        while not self._stop_event.wait(self._poll_rate):
            try:
                self._pressed = frozenset(self._keyboard_input[:])
            except (EOFError, BrokenPipeError, OSError, AttributeError):
                return

    def stop(self):
        self._stop_event.set()

    def snapshot(self):
        return self._pressed


class VisualCache(ObjectCache):
    pass

class IdCache(ObjectCache):
    pass


class scripter_runner:

    def print(self, *args, **kwargs):
        prefix = f"[CORE {self.id:02d} : {self.core_time.get():.4f}]"
        print(prefix, *args, **kwargs)

    def _release_status(self, complexity):
        if complexity <= 0:
            return
        try:
            with self.lock:
                current = self.proccess_status_info[self.id]
                self.proccess_status_info[self.id] = max(0, current - complexity)
        except (EOFError, BrokenPipeError, OSError):
            return

    def wrapper(self, func, arg, complexity, data):
        path = data.get("path", "?")
        self.print(f"Запущен {path}")

        try:
            func(*arg)
        except Exception:
            traceback.print_exc()
        finally:
            self._release_status(complexity)
            self.print(f"Остановлен {path}")

    def __init__(
        self,
        time_key,
        id,
        render_in_stream,
        change_in_stream,
        main_in_stream,
        main_out_stream,
        objects_cache,
        worker_queue,
        worker_ready,
        lock,
        keyboard_input,
        proccess_status_info,
        visual_cache,
        id_cache,
        object_lock,
        camera_state,
        tick_key
    ):
        self.core_time = ValueCache(time_key)
        self.id = id
        self.time_key = time_key
        self.tick_key = tick_key
        self._script_modules = {}
        if str(PROJECT_DIR) not in sys.path:
            sys.path.insert(0, str(PROJECT_DIR))
        self.render_in_stream = render_in_stream
        self.change_in_stream = change_in_stream
        self.main_in_stream = main_in_stream
        self.main_out_stream = main_out_stream
        self.objects_cache = objects_cache
        self.worker_queue = worker_queue
        self.worker_ready = worker_ready
        self.proccess_status_info = proccess_status_info
        self.lock = lock
        self.keyboard_input = keyboard_input
        self.keyboard_snapshot = KeyboardSnapshot(keyboard_input)
        self.visual_cache = visual_cache
        self.id_cache = id_cache
        self.object_lock = object_lock
        self.camera_state = camera_state

        self.worker_ready[id] = True
        try:
            while True:
                try:
                    data = self.worker_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                except (EOFError, BrokenPipeError, OSError):
                    return
                if data is None:
                    continue
                if data.get("proc_id") != id:
                    continue
                self.__start_script(data)
        finally:
            try:
                self.worker_ready[id] = False
            except (EOFError, BrokenPipeError, OSError):
                pass
            self.keyboard_snapshot.stop()
            self.core_time.detach()

    def __load_script_module(self, path):
        """Load a script by file path, independent of the current directory."""

        resolved_path = resolve_project_path(path)
        cache_key = str(resolved_path)
        module = self._script_modules.get(cache_key)
        if module is not None:
            return module

        module_name = f"_arcade_script_{abs(hash(cache_key)):x}"
        spec = importlib.util.spec_from_file_location(module_name, cache_key)
        if spec is None or spec.loader is None:
            raise ImportError(f"Не удалось загрузить скрипт {cache_key!r}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise

        self._script_modules[cache_key] = module
        return module

    def __start_script(self, data):
        visual_attachment = None
        id_attachment = None
        complexity = int(data.get("complexity", 0) or 0)

        try:
            resolved_path = resolve_project_path(data["path"])
            name = resolved_path.stem
            obj_data = data["script_data"]

            module_obj = self.__load_script_module(resolved_path)
            script_class = getattr(module_obj, name)

            visual_attachment = VisualCache(*self.visual_cache)
            id_attachment = IdCache(*self.id_cache)

            thread = threading.Thread(
                target=self.wrapper,
                args=(
                    script_class,
                    (
                        self.render_in_stream,
                        self.change_in_stream,
                        self.main_in_stream,
                        self.main_out_stream,
                        obj_data,
                        self.objects_cache,
                        self.lock,
                        self.keyboard_snapshot,
                        self.time_key,
                        visual_attachment,
                        self.visual_cache,
                        id_attachment,
                        self.id_cache,
                        self.object_lock,
                        self.camera_state,
                        self.tick_key,
                    ),
                    complexity,
                    data,
                ),
                daemon=True,
            )
            thread.start()
        except Exception as error:
            if visual_attachment is not None:
                visual_attachment.detach()
            if id_attachment is not None:
                id_attachment.detach()
            self._release_status(complexity)
            self.print(f"Ошибка создания script потока {data.get('path', '?')}: {error}")
            traceback.print_exc()
