"""Headless-сервер мультиплеера.

Запуск::

    python server_main.py access/scenes/net_demo.json --max-players 8

Внутри — тот же движок, что и в одиночной игре: процесс ``Stream`` с реестром
объектов и процесс ``scripter_runner`` со скриптами.  Отличие только в
источнике ввода (сеть) и в том, что тик публикует серверный цикл, а не
рендерер.  Окно Arcade не создаётся.
"""

from __future__ import annotations

import argparse
import copy
import json
import multiprocessing
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from caches import IdCache, VisualCache
from config import (
    DIRTY_RING_CAPACITY,
    ENGINE_STATE_SEGMENT_BYTES,
    ENGINE_STATE_STRUCT,
    KEYBOARD_EVENT_BYTES,
    KEYBOARD_EVENT_STRUCT,
    MOUSE_MOTION_BYTES,
    MOUSE_MOTION_STRUCT,
    WORKER_READY_TIMEOUT,
)
from memory_lib import ObjectCache, ValueCache
from net.config import (
    DEFAULT_ANTICHEAT,
    DEFAULT_HOST,
    DEFAULT_MAX_PLAYERS,
    DEFAULT_PORT,
    INPUT_REPEAT_TICKS,
    AnticheatPolicy,
)
from net.directives import parse_network_directives
from net.engine_world import EngineWorld
from net.hashes import compute_hashes, scene_file_path


class CommandQueue:
    """Минимальная обёртка над ``multiprocessing.Queue`` для команд."""

    def __init__(self, maxsize=1024):
        self._queue = multiprocessing.Queue(maxsize=maxsize)

    def put(self, item, *args, **kwargs):
        return self._queue.put(item, *args, **kwargs)

    def get(self, *args, **kwargs):
        return self._queue.get(*args, **kwargs)

    def get_nowait(self):
        return self._queue.get(False)

    def close(self):
        return self._queue.close()


class Runtime:
    """Общие кэши и процессы ``Stream`` + ``scripter_runner``."""

    def __init__(self, scene_file: str | None):
        self.scene_file = scene_file
        self.manager = multiprocessing.Manager()
        self.input_cache = self.manager.dict()
        self.lock = multiprocessing.Lock()
        self.object_lock = multiprocessing.Lock()

        visual_struct = {
            "x": float,
            "y": float,
            "tick": int,
            "sim_time": float,
            "rotation": float,
            "scale": float,
            "alpha": float,
            "sprite": str,
            "layer": float,
        }
        self.raw_visual = VisualCache(
            1024 * 1024, visual_struct, [multiprocessing.Lock() for _ in range(64)]
        )
        self.visual_data = [
            self.raw_visual.key,
            visual_struct,
            self.raw_visual.locks,
            None,
            self.raw_visual.capacity,
        ]
        id_struct = {
            "name": str,
            "type": str,
            "class": str,
            "id": str,
            "generation": int,
        }
        self.raw_id = IdCache(1024 * 256, id_struct, [multiprocessing.Lock() for _ in range(64)])
        self.id_data = [
            self.raw_id.key,
            id_struct,
            self.raw_id.locks,
            None,
            self.raw_id.capacity,
        ]
        self.raw_engine_state = ObjectCache(
            ENGINE_STATE_SEGMENT_BYTES,
            ENGINE_STATE_STRUCT,
            [multiprocessing.Lock() for _ in range(4)],
        )
        self.engine_state_data = [
            self.raw_engine_state.key,
            ENGINE_STATE_STRUCT,
            self.raw_engine_state.locks,
            None,
            self.raw_engine_state.capacity,
        ]
        self.raw_motion = ObjectCache(
            MOUSE_MOTION_BYTES,
            MOUSE_MOTION_STRUCT,
            [multiprocessing.Lock() for _ in range(8)],
        )
        self.raw_motion.set_many(0, {
            "seq": 0, "x": 0.0, "y": 0.0, "dx": 0.0, "dy": 0.0,
            "screen_x": 0.0, "screen_y": 0.0, "hovered": -1,
        })
        self.motion_data = [
            self.raw_motion.key, MOUSE_MOTION_STRUCT, self.raw_motion.locks,
            None, self.raw_motion.capacity,
        ]
        self.raw_keyboard = ObjectCache(
            KEYBOARD_EVENT_BYTES,
            KEYBOARD_EVENT_STRUCT,
            [multiprocessing.Lock() for _ in range(16)],
        )
        self.keyboard_data = [
            self.raw_keyboard.key, KEYBOARD_EVENT_STRUCT, self.raw_keyboard.locks,
            None, self.raw_keyboard.capacity,
        ]

        self.render_in = CommandQueue()
        self.render_out = CommandQueue()
        self.main_in = CommandQueue()
        self.main_out = CommandQueue()
        self.console_out = CommandQueue()
        self.worker_queues = [CommandQueue()]
        self.worker_ready = multiprocessing.Array("b", 1, lock=False)
        self.proccess_status_info = multiprocessing.Array("i", 1, lock=False)
        self.camera_target_cache = ValueCache(-1)
        self.camera_scale_cache = ValueCache(1.0)
        # Тик, который процесс скриптов реально выполнил. Сервер ждёт его,
        # прежде чем проверять заявки и снимать снапшот.
        self.executed_tick = multiprocessing.Value("q", 0)
        # Динамически созданные сетевые объекты: Stream кладёт сюда событие
        # при регистрации, GameServer добавляет render_id в снапшоты.
        self.net_object_events = multiprocessing.Queue()

        scene = scene_file or os.environ.get("PYENGINE_SCENE") or "scene1.json"
        self.scene_file = scene
        os.environ["PYENGINE_SCENE"] = scene
        import stream

        self.scene_hash, self.scripts_hash = compute_hashes(scene)
        self.stream_process = multiprocessing.Process(
            target=stream.Stream,
            args=(
                self.engine_state_data,
                self.render_in,
                self.render_out,
                self.main_in,
                self.main_out,
                self.input_cache,
                self.proccess_status_info,
                self.worker_queues,
                self.worker_ready,
                self.lock,
                self.visual_data,
                self.id_data,
                self.object_lock,
                [
                    self.camera_target_cache.name,
                    self.camera_scale_cache.name,
                ],
                self.motion_data,
                self.keyboard_data,
                self.console_out,
                self.executed_tick,
            ),
            kwargs={
                "net_object_events": self.net_object_events,
                # Headless-сервер не исполняет клиентские (локальные) скрипты:
                # только объекты с ``scripting.network``.
                "net_role": "server",
            },
            daemon=False,
        )
        self.world_visual = VisualCache(*self.visual_data)
        self._request_counter = 0

    def start(self) -> None:
        self.stream_process.start()

    def submit(self, payload: dict) -> None:
        self.main_in.put(payload)

    def request(self, payload: dict, timeout: float = 2.0):
        self._request_counter += 1
        request_id = f"srv:{os.getpid()}:{self._request_counter}"
        message = dict(payload)
        message["request_id"] = request_id
        self.main_in.put(message)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                reply = self.main_out.get(timeout=0.05)
            except queue.Empty:
                continue
            if isinstance(reply, dict) and request_id in reply:
                return reply[request_id]
        raise TimeoutError(f"Stream не ответил на {payload.get('command')!r}")

    def net_objects(self):
        payload = self.request({"command": "net_objects"})
        if not isinstance(payload, dict) or "objects" not in payload:
            raise RuntimeError(f"Некорректный ответ net_objects: {payload!r}")
        return payload["objects"]

    def close(self) -> None:
        try:
            self.stream_process.terminate()
            self.stream_process.join(timeout=2.0)
        except (OSError, ValueError):
            pass
        for cache in (
            self.world_visual, self.raw_visual, self.raw_id,
            self.raw_engine_state, self.raw_motion, self.raw_keyboard,
            self.camera_target_cache, self.camera_scale_cache,
        ):
            try:
                cache.close()
            except (OSError, ValueError, BufferError):
                pass
        try:
            self.manager.shutdown()
        except Exception:
            pass


class ServerConsole:
    """Команды отладки сервера (см. NETWORK_ARCHITECTURE.md, раздел 19)."""

    def __init__(self, runtime: Runtime, server, world, stop_event):
        self.runtime = runtime
        self.server = server
        self.world = world
        self.stop_event = stop_event
        self.debug_objects: dict[int, dict] = {}
        self.debug_counter = 0

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                line = input("server> ")
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            try:
                self.execute(line)
            except Exception:
                traceback.print_exc()
        self.stop_event.set()

    def execute(self, line: str) -> None:
        parts = line.split(maxsplit=2)
        command = parts[0].lower()
        argument = parts[1] if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""

        if command == "stop":
            self.stop_event.set()
            self.server.submit(self.server.stop)
        elif command == "players":
            self._print_players()
        elif command == "kick":
            if not argument.isdigit():
                print("использование: kick <client_id> [reason]")
                return
            client_id = int(argument)
            reason = rest or "kicked"
            self.server.submit(
                lambda: self.server.kick(client_id, reason)
            )
        elif command == "anticheat":
            if argument:
                mode = self.server.set_anticheat(argument)
                print(f"античит: {mode}")
            else:
                stats = self.server.stats()
                print(f"античит: {stats['anticheat']}")
        elif command == "netstat":
            self._print_netstat()
        elif command == "resync":
            if argument.isdigit():
                client_id = int(argument)
                self.server.submit(
                    lambda: self._force_full(client_id)
                )
        elif command == "script":
            self._add_debug_script(argument, rest)
        elif command == "scripts":
            for object_id, info in self.debug_objects.items():
                print(f"{object_id}: {info['path']} ({info['name']})")
        elif command == "script_stop":
            if argument.isdigit():
                self._stop_debug_script(int(argument))
        elif command in {"help", "?"}:
            print(
                "script <path> [json] | scripts | script_stop <object_id> | "
                "players | kick <id> [reason] | anticheat [log|kick] | "
                "netstat | resync <id> | stop"
            )
        else:
            print(f"неизвестная команда: {command}")

    def _print_players(self) -> None:
        stats = self.server.stats()
        print(f"тик {stats['tick']}, игроков {stats['clients']}/{stats['max_players']}")
        for player in stats["players"]:
            print(
                f"  #{player['client_id']} {player['name']} "
                f"rtt={player['rtt_us'] / 1000:.1f}мс "
                f"desync={player['desyncs']} "
                f"violations={player['violations']} "
                f"seq={player['last_executed_seq']}"
            )

    def _print_netstat(self) -> None:
        stats = self.server.stats()
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        print(f"world: missed_ticks={self.world.missed_ticks}")
        try:
            worker = self.runtime.worker_queues[0]
            request_id = f"console:{time.monotonic_ns()}"
            worker.put({
                "proc_id": 0, "command": "net_stats", "request_id": request_id,
            })
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                try:
                    reply = self.runtime.main_out.get(timeout=0.05)
                except queue.Empty:
                    continue
                if isinstance(reply, dict) and request_id in reply:
                    print(json.dumps(reply[request_id], ensure_ascii=False, indent=2))
                    break
        except Exception as error:
            print(f"статистика симуляции недоступна: {error}")

    def _force_full(self, client_id: int) -> None:
        session = self.server.by_id.get(client_id)
        if session is not None:
            session.force_full = True
            print(f"resync отправлен игроку {client_id}")

    def _add_debug_script(self, path: str, json_text: str) -> None:
        if not path:
            print("использование: script <path> [json]")
            return
        self.debug_counter += 1
        name = f"debug_{self.debug_counter}"
        data = {
            "object_type": "manager",
            "id": {"name": name, "class": name, "type": "manager"},
            "scripting": {
                "scripts": [path],
                "network": {"authority": "server"},
            },
        }
        if json_text.strip():
            extra = json.loads(json_text)
            if not isinstance(extra, dict):
                raise ValueError("json-свойства должны быть объектом")
            data.update(extra)
        # Stream ждёт запись в формате GameObject: dict_data + служебные поля.
        # После ответа скрипт запускается отдельной командой add_script —
        # ровно так же, как это делает _publish_registration в pyengine.
        record_data = copy.deepcopy(data)
        reply = self.runtime.request({
            "command": "register",
            "id": name,
            "obj": {
                "dict_data": copy.deepcopy(record_data),
                "_id": name,
                "_object_id": None,
                "_render_id": None,
                "_generation": 0,
                "_is_render": False,
            },
            "script_data": record_data,
        })
        if not isinstance(reply, dict) or "error" in reply:
            print(f"ошибка регистрации: {reply}")
            return
        object_id = reply.get("object_id")
        if object_id is None:
            object_id = reply.get("id")
        if object_id is None:
            print(f"Stream не вернул object_id: {reply}")
            return
        script_data = copy.deepcopy(data)
        script_data["_id"] = name
        script_data["object_id"] = int(object_id)
        script_data["render_id"] = int(reply.get("render_id", 0) or 0)
        script_data["_generation"] = int(reply.get("generation", 0) or 0)
        self.runtime.submit({
            "command": "add_script",
            "path": path,
            "obj_id": int(object_id),
            "id": name,
            "script_data": script_data,
        })
        self.debug_objects[int(object_id)] = {
            "name": name, "path": path, "key": name,
        }
        print(f"запущен {name} (object_id={object_id})")

    def _stop_debug_script(self, object_id: int) -> None:
        info = self.debug_objects.pop(object_id, None)
        if info is None:
            print(f"нет отладочн��го скрипта {object_id}")
            return
        self.runtime.submit({"command": "remove", "id": info["key"]})
        print(f"остановлен {info['name']}")


def _wait_scene(runtime: Runtime, timeout: float = 30.0) -> list[dict]:
    """Ждёт регистрации сцены и возвращает объекты с сетевыми ролями."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not runtime.worker_ready[0]:
            time.sleep(0.05)
            continue
        try:
            objects = runtime.net_objects()
            if objects:
                return objects
            stats = runtime.request({"command": "stats"}, timeout=0.5)
            if isinstance(stats, dict) and stats.get("objects", 0) > 0:
                return objects
        except TimeoutError:
            pass
        time.sleep(0.05)
    raise TimeoutError("Сцена не зарегистрировалась за отведённое время")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Headless-сервер pyengine")
    parser.add_argument("scene", nargs="?", default=None,
                        help="файл сцены (по умолчанию PYENGINE_SCENE)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--max-players", type=int, default=DEFAULT_MAX_PLAYERS)
    parser.add_argument("--token", default="")
    parser.add_argument("--anticheat", choices=("log", "kick"), default="log")
    parser.add_argument("--no-console", action="store_true")
    args = parser.parse_args(argv)

    if args.scene:
        os.environ["PYENGINE_SCENE"] = args.scene

    runtime = Runtime(args.scene)
    server = None
    stop_event = threading.Event()
    try:
        runtime.start()
        objects = _wait_scene(runtime)

        owners = []
        replicated = []
        for item in objects:
            role = parse_network_directives(item.get("network"))
            if not item.get("has_visual", False):
                continue
            if role.owner:
                owners.append(int(item["render_id"]))
            if role.replicated:
                replicated.append(int(item["render_id"]))

        executed_tick = runtime.executed_tick
        world = EngineWorld(
            worker_queue=runtime.worker_queues[0],
            visual_cache=runtime.world_visual,
            owner_slots=owners,
            replicated_ids=replicated,
            executed_tick=executed_tick,
            network_events=runtime.net_object_events,
        )
        runtime.worker_queues[0].put({
            "proc_id": 0,
            "command": "net_start",
            "repeat_ticks": INPUT_REPEAT_TICKS,
        })

        from net.server import GameServer

        server = GameServer(
            args.host,
            args.port,
            max_players=args.max_players,
            world=world,
            anticheat=AnticheatPolicy(args.anticheat),
            scene_hash=runtime.scene_hash,
            scripts_hash=runtime.scripts_hash,
            token=args.token,
            verbose=True,
        )
        print(
            f"[NET] сцена {runtime.scene_file}: "
            f"owner={owners}, replicated={replicated}",
            flush=True,
        )
        print(
            f"[NET] слушаю {args.host}:{server.port}, "
            f"до {args.max_players} игроков",
            flush=True,
        )
        server_thread = threading.Thread(
            target=server.run_forever, args=(0.003,), daemon=True
        )
        server_thread.start()

        console = ServerConsole(runtime, server, world, stop_event)
        if args.no_console:
            try:
                while not stop_event.wait(0.5):
                    pass
            except KeyboardInterrupt:
                stop_event.set()
        else:
            console.run()
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        stop_event.set()
        if server is not None:
            server.submit(server.stop)
            time.sleep(0.1)
        try:
            runtime.worker_queues[0].put({"proc_id": 0, "command": "net_stop"})
        except Exception:
            pass
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
