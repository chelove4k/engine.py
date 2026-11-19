import importlib.util
import queue
import sys
import time
import traceback

from caches import IdCache, VisualCache
from config import PROJECT_DIR, SCRIPT_UPDATE_PERIOD, resolve_project_path
from memory_lib import FieldView, ObjectCache


class scripter_runner:
    """Один процесс, который кооперативно выполняет все скрипты.

    Раньше каждый скрипт получал собственный поток внутри воркера.  Теперь
    экземпляры скриптов живут в общем списке, а цикл по очереди вызывает
    ``step()`` у каждого.  Это убирает thread-per-script и оставляет ровно
    один поток симуляции на процесс.
    """

    def print(self, *args, **kwargs):
        try:
            now = self.core_time.get()
        except Exception:
            now = 0.0
        prefix = f"[CORE {self.id:02d} : {now:.4f}]"
        kwargs.setdefault("flush", True)
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
        self._scripts = {}
        self._next_script_id = 0
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
            self.__loop()
        finally:
            self.__shutdown_all()
            try:
                self.worker_ready[id] = False
            except (EOFError, BrokenPipeError, OSError):
                pass
            self.engine_state.detach()
            if self.motion_cache is not None:
                self.motion_cache.detach()
            if self.keyboard_ring is not None:
                self.keyboard_ring.detach()

    def __loop(self):
        while True:
            try:
                self.__run_once()
            except (EOFError, BrokenPipeError, OSError):
                # Manager или общая очередь уже закрыты: штатное завершение.
                return
            time.sleep(SCRIPT_UPDATE_PERIOD)

    def __run_once(self):
        """Один проход: принять команды и шагнуть каждый живой скрипт.

        Ошибка одного скрипта отправляет в ``shutdown()`` только его: общий
        поток симуляции и остальные скрипты продолжают работу.
        """

        self.__drain_commands()
        for token, entry in list(self._scripts.items()):
            instance = entry[0]
            try:
                alive = instance.step()
            except (EOFError, BrokenPipeError, OSError):
                raise
            except Exception:
                traceback.print_exc()
                alive = False
            if not alive:
                self.__stop_script(token)

    def __drain_commands(self):
        while True:
            try:
                data = self.worker_queue.get_nowait()
            except queue.Empty:
                return
            except (EOFError, BrokenPipeError, OSError):
                return
            if data is None:
                continue
            if data.get("proc_id") != self.id:
                continue
            self.__start_script(data)

    def __stop_script(self, token):
        entry = self._scripts.pop(token, None)
        if entry is None:
            return
        instance, complexity, path = entry
        try:
            instance.shutdown()
        except Exception:
            traceback.print_exc()
        self._release_status(complexity)
        self.print(f"Остановлен {path}")

    def __shutdown_all(self):
        for token in list(self._scripts):
            self.__stop_script(token)

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
        path = data.get("path", "?")

        try:
            resolved_path = resolve_project_path(path)
            name = resolved_path.stem
            obj_data = data["script_data"]

            module_obj = self.__load_script_module(resolved_path)
            script_class = getattr(module_obj, name)

            visual_attachment = VisualCache(*self.visual_cache)
            id_attachment = IdCache(*self.id_cache)

            instance = script_class(
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
            )
        except Exception as error:
            if visual_attachment is not None:
                visual_attachment.detach()
            if id_attachment is not None:
                id_attachment.detach()
            self._release_status(complexity)
            self.print(f"Ошибка создания скрипта {path}: {error}")
            traceback.print_exc()
            return

        token = self._next_script_id
        self._next_script_id += 1
        self._scripts[token] = (instance, complexity, str(resolved_path))
        self.print(f"Запущен {path}")
