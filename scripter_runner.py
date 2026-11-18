import importlib.util
import queue
import sys
import threading
import traceback

from caches import IdCache, VisualCache
from config import PROJECT_DIR, resolve_project_path
from memory_lib import FieldView, ObjectCache


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
        engine_state,
        motion_cache,
        keyboard_ring,
        id,
        render_in_stream,
        change_in_stream,
        main_in_stream,
        main_out_stream,
        objects_cache,
        worker_queue,
        worker_ready,
        lock,
        proccess_status_info,
        visual_cache,
        id_cache,
        object_lock,
        camera_state,
    ):
        self.engine_state = ObjectCache(*engine_state)
        self.core_time = FieldView(self.engine_state, "time")
        self.motion_cache = (
            ObjectCache(*motion_cache) if motion_cache is not None else None
        )
        self.keyboard_ring = (
            ObjectCache(*keyboard_ring) if keyboard_ring is not None else None
        )
        self.id = id
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
            self.engine_state.detach()
            if self.motion_cache is not None:
                self.motion_cache.detach()
            if self.keyboard_ring is not None:
                self.keyboard_ring.detach()

    def __load_script_module(self, path):
        """Загружает скрипт по пути к файлу независимо от текущего каталога."""

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
                        self.engine_state,
                        self.motion_cache,
                        self.keyboard_ring,
                        visual_attachment,
                        self.visual_cache,
                        id_attachment,
                        self.id_cache,
                        self.object_lock,
                        self.camera_state,
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
