import importlib.util
import queue
import sys
import time
import traceback

from caches import IdCache, VisualCache
from config import (
    ENGINE_STATE_LOOP_FIELDS,
    FIXED_DT,
    PROJECT_DIR,
    SCRIPT_UPDATE_PERIOD,
    resolve_project_path,
)
from memory_lib import FieldView, ObjectCache
from net.simulation import ServerSimulation


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
        find_object_via_stream=False,
        net_executed_tick=None,
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
        self.find_object_via_stream = bool(find_object_via_stream)
        # Сетевой режим включается командой net_start: тиком управляет сервер,
        # а ввод приходит кадрами из GameServer.
        self.net: ServerSimulation | None = None
        self.net_executed_tick = net_executed_tick

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
            if self.net is None:
                if not self.__run_timed_until_net():
                    return
            if not self.__run_net_until_stopped():
                return

    def __run_timed_until_net(self) -> bool:
        """100 Гц по дедлайну, пока не включат серверный режим."""

        # Планировщик держит 100 Гц по дедлайну, а не «работа + sleep».
        # Иначе время прохода прибавлялось к периоду, и при 500 скриптах
        # сцена автоматически замедлялась вдвое даже без нехватки CPU.
        period = SCRIPT_UPDATE_PERIOD
        next_deadline = time.perf_counter()
        while self.net is None:
            next_deadline += period
            try:
                self.__run_once()
            except (EOFError, BrokenPipeError, OSError):
                # Manager или общая очередь уже закрыты: штатное завершение.
                return False
            now = time.perf_counter()
            delay = next_deadline - now
            if delay > 0:
                time.sleep(delay)
            elif delay < -period:
                # Проход дольше периода: не копим задолженность, иначе
                # планировщик будет постоянно догонять сам себя.
                next_deadline = now
        return True

    def __run_net_until_stopped(self) -> bool:
        """Серверный режим: проход только по команде ``net_tick``.

        Очередь блокирующая: тик обрабатывается сразу после прихода, без
        опроса и без лишней задержки.  Если сервер прислал несколько тиков
        подряд, выполняются только последний (пропущенные тики не
        моделируются дважды), но ввод из всех кадров сохраняется.
        """

        while self.net is not None:
            latest = None
            inputs: list[tuple[int, bytes]] = []
            while True:
                try:
                    data = self.worker_queue.get_nowait()
                except queue.Empty:
                    break
                except (EOFError, BrokenPipeError, OSError):
                    return False
                if data is None or not isinstance(data, dict):
                    continue
                if data.get("proc_id") != self.id:
                    continue
                if data.get("command") == "net_tick":
                    latest = data
                    inputs.extend(data.get("inputs") or ())
                else:
                    self.__handle_worker_message(data)

            if latest is not None:
                self.__step_net(latest, inputs)
                continue

            try:
                data = self.worker_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            except (EOFError, BrokenPipeError, OSError):
                return False
            if data is None or not isinstance(data, dict):
                continue
            if data.get("proc_id") != self.id:
                continue
            if data.get("command") == "net_tick":
                self.__step_net(data, list(data.get("inputs") or ()))
            else:
                self.__handle_worker_message(data)
        return True

    def __run_once(self):
        """Один проход: принять команды и шагнуть каждый живой скрипт.

        Часы, tick и счётчики ввода читаются один раз на весь проход. Все
        скрипты видят одинаковый снимок состояния и один и тот же tick, а не
        шесть полей shared memory на каждый скрипт.

        Ошибка одного скрипта отправляет в ``shutdown()`` только его: общий
        поток симуляции и остальные скрипты продолжают работу.
        """

        self.__drain_commands()
        engine_state = getattr(self, "engine_state", None)
        state = (
            engine_state.get_many_lockless(0, ENGINE_STATE_LOOP_FIELDS)
            if engine_state is not None else None
        )
        self.__step_scripts(state)
        self.__request_trigger_evaluation(
            state.get("tick") if isinstance(state, dict) else None
        )

    def __step_scripts(self, state):
        for token, entry in list(self._scripts.items()):
            instance = entry[0]
            try:
                # Тестовые раннеры могут собирать объект без engine_state
                # и использовать скрипты со старым step() без аргументов.
                alive = (
                    instance.step(state) if state is not None
                    else instance.step()
                )
            except (EOFError, BrokenPipeError, OSError):
                raise
            except Exception:
                traceback.print_exc()
                alive = False
            if not alive:
                self.__stop_script(token)

    def __step_net(self, data, inputs):
        """Применяет ввод и делает ровно один серверный тик."""

        tick = int(data.get("tick", 0))
        if tick <= self.net.tick:
            return
        for client_id, payload in inputs:
            self.net.push(int(client_id), payload)
        self.engine_state.set_many(0, {
            "time": tick * FIXED_DT,
            "tick": tick,
        })
        self.net.begin_tick(tick)
        state = self.engine_state.get_many_lockless(
            0, ENGINE_STATE_LOOP_FIELDS
        )
        self.__step_scripts(state)
        # Клик доставляем после step(): к этому моменту скрипты уже
        # активированы (start выполнен), а объект, созданный обработчиком,
        # успевает зарегистрироваться до подтверждения тика.
        self.__dispatch_network_clicks()
        self.__request_trigger_evaluation(tick)
        if self.net_executed_tick is not None:
            self.net_executed_tick.value = tick

    def __dispatch_network_clicks(self):
        """Раздаёт клики всем объектам, а не только владельцу ввода.

        Компонент может быть прикреплён к реплицируемому объекту без
        ``owner``; такой объект не получает канал ввода игрока, но клик по
        нему обязан сработать на сервере один раз для любого клиента.
        """

        if self.net is None:
            return
        clicks = self.net.take_clicks()
        if not clicks:
            return
        for instance, _complexity, _path in list(self._scripts.values()):
            dispatch = getattr(instance, "dispatch_network_click", None)
            if dispatch is None:
                continue
            for _client_id, snapshot, render_id, button in clicks:
                dispatch(snapshot, render_id, button)

    def __request_trigger_evaluation(self, tick):
        """Просит Stream пересчитать триггеры после тика.

        События вернутся командой ``trigger_events`` и будут разосланы только
        скриптам объекта, который пересёк зону.
        """

        stream = getattr(self, "main_in_stream", None)
        if stream is None:
            return
        try:
            stream.put({
                "command": "trigger_tick",
                "tick": None if tick is None else int(tick),
            })
        except (EOFError, BrokenPipeError, OSError, ValueError):
            pass

    def __dispatch_trigger_events(self, events):
        if not events:
            return
        scripts = list(getattr(self, "_scripts", {}).values())
        if not scripts:
            return
        for data in events:
            if not isinstance(data, dict):
                continue
            target = data.get("object") or {}
            try:
                render_id = int(target.get("render_id") or 0)
            except (TypeError, ValueError):
                continue
            if not render_id:
                continue
            for instance, _complexity, _path in scripts:
                game_object = getattr(instance, "GameObject", None)
                if getattr(game_object, "_render_id", 0) != render_id:
                    continue
                dispatch = getattr(instance, "dispatch_trigger_event", None)
                if dispatch is None:
                    continue
                try:
                    dispatch(data)
                except Exception:
                    traceback.print_exc()

    def __drain_commands(self):
        while True:
            try:
                data = self.worker_queue.get_nowait()
            except queue.Empty:
                return
            except (EOFError, BrokenPipeError, OSError):
                return
            if data is None or not isinstance(data, dict):
                continue
            if data.get("proc_id") != self.id:
                continue
            self.__handle_worker_message(data)

    def __handle_worker_message(self, data):
        command = data.get("command")
        if command == "add_script":
            self.__start_script(data)
        elif command == "net_start":
            self.__start_net(data)
        elif command == "net_join":
            self.__join_net(
                int(data.get("client_id", 0)),
                int(data.get("render_id", 0) or 0),
            )
        elif command == "net_leave":
            self.__leave_net(int(data.get("client_id", 0)))
        elif command == "trigger_events":
            self.__dispatch_trigger_events(data.get("events") or ())
        elif command == "net_input":
            if self.net is not None:
                self.net.push(
                    int(data.get("client_id", 0)),
                    data.get("payload", b""),
                )
        elif command == "net_stop":
            self.net = None
        elif command == "net_stats":
            payload = (
                self.net.stats() if self.net is not None
                else {"enabled": False}
            )
            payload["enabled"] = self.net is not None
            request_id = data.get("request_id")
            if request_id is not None:
                self.main_out_stream.put({request_id: payload})

    def __start_net(self, data):
        if self.net is None:
            self.net = ServerSimulation(
                repeat_ticks=int(data.get("repeat_ticks", 5) or 0)
            )
        for instance, _complexity, _path in self._scripts.values():
            instance.set_strict_tick(True)
            source = self.net.source_for_render(
                getattr(instance.GameObject, "_render_id", 0)
            )
            if source is not None:
                instance.bind_input(source)

    def __join_net(self, client_id, render_id):
        if self.net is None:
            return
        source = self.net.join(client_id, render_id)
        if not render_id:
            return
        for instance, _complexity, _path in self._scripts.values():
            if getattr(instance.GameObject, "_render_id", 0) == render_id:
                instance.bind_input(source)

    def __leave_net(self, client_id):
        if self.net is None:
            return
        self.net.leave(client_id)
        for instance, _complexity, _path in self._scripts.values():
            if getattr(instance.GameObject, "_render_id", 0) in self.net.bindings:
                continue
            instance.bind_input(None)

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
                find_object_via_stream=self.find_object_via_stream,
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
        if self.net is not None:
            instance.set_strict_tick(True)
            source = self.net.source_for_render(
                getattr(instance.GameObject, "_render_id", 0)
            )
            if source is not None:
                instance.bind_input(source)
        self.print(f"Запущен {path}")
