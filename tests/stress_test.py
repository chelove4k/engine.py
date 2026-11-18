"""Автономные стресс-тесты ядра движка.

Раннер намеренно не называется ``test_*.py``, чтобы обычный discovery
unittest его не запускал.  Вместо одного синтетического цикла он покрывает
разные нагрузки:

* чистую пропускную способность ``ObjectCache`` / ``ValueCache``;
* конкуренцию многопроцессного кэша через shared memory и блокировки сегментов;
* математику выпуклых коллизий (transform + SAT);
* историю позиций по render tick и интерполяцию;
* регистрацию объектов в реальном времени через ``Stream``;
* пакетное создание через локальный путь ``create_object``;
* циклы обновления компонентов/скриптов через живой ``Stream``.

Запуск из корня проекта (или из любого другого места; пути разрешаются
относительно проекта):

    python tests/stress_test.py
    python tests/stress_test.py --scenario cache,contention --processes 8
    python tests/stress_test.py --scenario all --objects 2000 --duration 5
    python tests/stress_test.py --json stress-report.json

Живые сценарии запускают настоящие процессы ``Stream`` и рабочие процессы,
поэтому на Windows сохраняйте защиту ``if __name__ == "__main__"`` и
функции-обработчики верхнего уровня неизменными.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
import queue
import statistics
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config import (  # noqa: E402
    DIRTY_RING_CAPACITY,
    ENGINE_STATE_SEGMENT_BYTES,
    ENGINE_STATE_STRUCT,
    FIXED_DT,
    KEYBOARD_EVENT_BYTES,
    KEYBOARD_EVENT_STRUCT,
    MOUSE_MOTION_BYTES,
    MOUSE_MOTION_STRUCT,
    POSITION_HISTORY_SIZE,
    POSITION_INTERPOLATION_DELAY,
    SCRIPT_UPDATE_HZ,
    SIMULATION_HZ,
)
from memory_lib import FieldView, ObjectCache, ValueCache  # noqa: E402


VISUAL_STRUCT = {
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
ID_STRUCT = {"name": str, "type": str, "class": str, "id": str}
CONTENTION_STRUCT = {"value": float, "counter": int}


def _segment_len(struct):
    """Раскладка ObjectCache: 4-байтовая маска изменений + 4/64 байта на поле."""

    return 4 + sum(64 if field_type is str else 4 for field_type in struct.values())


VISUAL_SEGMENT_LEN = _segment_len(VISUAL_STRUCT)  # 100 байт
ID_SEGMENT_LEN = _segment_len(ID_STRUCT)  # 260 байт
CONTENTION_SEGMENT_LEN = _segment_len(CONTENTION_STRUCT)  # 12 байт

SCENE_OBJECT_COUNT = 5  # access/scenes/scene1.json
INTERPOLATION_DELAY_TICKS = int(
    round(POSITION_INTERPOLATION_DELAY / FIXED_DT)
)


@dataclass
class Metric:
    scenario: str
    name: str
    value: float
    unit: str
    note: str = ""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class ScenarioResult:
    name: str
    metrics: list[Metric] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    error: str | None = None
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and all(check.ok for check in self.checks)

    def metric(self, name: str, value: float, unit: str, note: str = "") -> float:
        self.metrics.append(Metric(self.name, name, float(value), unit, note))
        return value

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, bool(ok), detail))
        return bool(ok)


def _rate(count: int, elapsed: float) -> float:
    return float(count) / elapsed if elapsed > 0 else 0.0


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(math.ceil(percent / 100.0 * len(ordered))) - 1
    return ordered[min(len(ordered) - 1, max(0, index))]


def _stress_object(index: int, prefix: str = "stress_object") -> dict:
    return {
        "object_type": "static_object",
        "id": {
            "name": f"{prefix}_{index}",
            "class": "StressObject",
            "type": "static_object",
        },
        "visual": {
            "sprite": "None",
            "layer": 1,
            "position": [float(index % 100), float(index // 100)],
            "scale": 1,
            "rotation": 0,
            "alpha": 1,
        },
    }


def run_cache(args) -> ScenarioResult:
    """Измеряет чистые операции с общим кэшем в одном процессе."""

    result = ScenarioResult("cache")
    cache = None
    value_cache = None
    try:
        cache = ObjectCache(64 * 1024, VISUAL_STRUCT)
        value_cache = ValueCache(0.0)
        segments = cache.capacity
        operations = max(1, int(args.cache_ops))

        started = time.perf_counter()
        for index in range(operations):
            cache.set(index % segments, "x", float(index % 997))
        elapsed = time.perf_counter() - started
        result.metric("object_cache.set", _rate(operations, elapsed), "ops/s",
                      f"{segments} сегментов")

        started = time.perf_counter()
        for index in range(operations):
            cache.get(index % segments, "x")
        elapsed = time.perf_counter() - started
        result.metric("object_cache.get", _rate(operations, elapsed), "ops/s")

        packed = {
            "x": 0.0,
            "y": 0.0,
            "sim_time": 0.0,
            "tick": 0,
            "rotation": 0.0,
            "scale": 1.0,
            "alpha": 1.0,
            "sprite": "None",
            "layer": 1.0,
        }
        started = time.perf_counter()
        for index in range(operations):
            packed["x"] = float(index % 1009)
            packed["y"] = float(index % 1013)
            packed["sim_time"] = index * 0.001
            packed["tick"] = index
            cache.set_many(index % segments, packed)
        elapsed = time.perf_counter() - started
        result.metric("object_cache.set_many", _rate(operations, elapsed), "ops/s",
                      "9 полей")

        started = time.perf_counter()
        for index in range(operations):
            cache.get_many(
                index % segments,
                ("x", "y", "sim_time", "tick", "rotation", "scale",
                 "alpha", "sprite", "layer"),
            )
        elapsed = time.perf_counter() - started
        result.metric("object_cache.get_many", _rate(operations, elapsed), "ops/s",
                      "9 полей")

        cycles = max(1000, operations // 10)
        started = time.perf_counter()
        for index in range(cycles):
            segment = index % segments
            cache.set_many(segment, {"x": float(index), "tick": index})
            cache.get_changed_values(segment)
        elapsed = time.perf_counter() - started
        result.metric("object_cache.dirty_cycle", _rate(cycles, elapsed), "ops/s",
                      "set_many + get_changed_values")

        started = time.perf_counter()
        for index in range(operations):
            value_cache.set(float(index % 4096))
            value_cache.get()
        elapsed = time.perf_counter() - started
        result.metric("value_cache.set+get", _rate(operations, elapsed), "ops/s")

        # Функциональные проверки: бенчмарк не должен выдавать правдоподобное
        # время, портя при этом порядок полей или маску изменений.
        check_segment = 0
        cache.set_many(check_segment, {
            "x": 12.5,
            "tick": 7,
            "sprite": "probe_sprite",
        })
        values = cache.get_many(check_segment, ("x", "tick", "sprite"))
        result.check(
            "cache roundtrip",
            values == {"x": 12.5, "tick": 7, "sprite": "probe_sprite"},
            repr(values),
        )
        changed = cache.get_changed_values(check_segment)
        result.check(
            "dirty mask fields",
            set(changed) == {"x", "tick", "sprite"},
            repr(sorted(changed)),
        )
        result.check(
            "dirty mask cleared",
            cache.get_changed_values(check_segment) == {},
        )
        value_cache.set(42.5)
        result.check(
            "value cache roundtrip",
            math.isclose(value_cache.get(), 42.5, rel_tol=1e-6),
            repr(value_cache.get()),
        )
    finally:
        if cache is not None:
            cache.close()
        if value_cache is not None:
            value_cache.close()
    return result


def _contention_worker(cache_key, struct, locks, capacity, mode, worker_id,
                       start_event, stop_event, ready_queue, result_queue):
    """Функция-обработчик верхнего уровня, чтобы её можно было сериализовать при spawn в Windows."""

    cache = None
    try:
        cache = ObjectCache(cache_key, struct, locks, capacity=capacity)
        segment = worker_id if mode == "distinct" else 0
        counter = 0
        ready_queue.put({"worker": worker_id, "ok": True})
        if not start_event.wait(15.0):
            raise RuntimeError("истекло время ожидания стартового события")

        started = time.perf_counter()
        while True:
            # Проверяем флаг остановки раз за пакет, а не в горячем цикле.
            for _ in range(512):
                counter += 1
                cache.set_many(segment, {
                    "value": float(counter & 0xFFFF) + 0.25,
                    "counter": counter & 0x7FFFFFFF,
                })
            if stop_event.is_set():
                break
        elapsed = time.perf_counter() - started
        result_queue.put({
            "worker": worker_id,
            "iterations": counter,
            "elapsed": elapsed,
        })
    except Exception:
        try:
            result_queue.put({
                "worker": worker_id,
                "error": traceback.format_exc(),
            })
        except Exception:
            pass
    finally:
        if cache is not None:
            try:
                cache.detach()
            except Exception:
                pass


def _run_contention_mode(cache, mode, process_count, duration):
    start_event = multiprocessing.Event()
    stop_event = multiprocessing.Event()
    ready_queue = multiprocessing.Queue()
    result_queue = multiprocessing.Queue()
    workers = []

    try:
        for worker_id in range(process_count):
            process = multiprocessing.Process(
                target=_contention_worker,
                args=(
                    cache.key, CONTENTION_STRUCT, cache.locks, cache.capacity,
                    mode, worker_id,
                    start_event, stop_event, ready_queue, result_queue,
                ),
            )
            process.start()
            workers.append(process)

        ready = []
        deadline = time.monotonic() + 20.0
        while len(ready) < process_count and time.monotonic() < deadline:
            try:
                ready.append(ready_queue.get(timeout=0.25))
            except queue.Empty:
                if not any(process.is_alive() for process in workers):
                    break
        failures = [message for message in ready if not message.get("ok")]
        if len(ready) != process_count or failures:
            raise RuntimeError(
                f"рабочие процессы конкуренции не запустились: ready={len(ready)}/"
                f"{process_count}, failures={failures}"
            )

        wall_started = time.perf_counter()
        start_event.set()
        time.sleep(max(0.05, float(duration)))
        stop_event.set()
        wall_elapsed = time.perf_counter() - wall_started

        results = []
        deadline = time.monotonic() + 20.0
        while len(results) < process_count and time.monotonic() < deadline:
            try:
                results.append(result_queue.get(timeout=0.25))
            except queue.Empty:
                if not any(process.is_alive() for process in workers):
                    break
        errors = [message["error"] for message in results if "error" in message]
        if errors:
            raise RuntimeError("\n".join(errors))
        if len(results) != process_count:
            raise RuntimeError(
                f"рабочие процессы конкуренции вернули {len(results)}/{process_count} результатов"
            )

        return {
            "iterations": sum(int(message["iterations"]) for message in results),
            "wall_elapsed": wall_elapsed,
            "worker_elapsed": [float(message["elapsed"]) for message in results],
        }
    finally:
        stop_event.set()
        for process in workers:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        ready_queue.close()
        result_queue.close()


def run_contention(args) -> ScenarioResult:
    """Измеряет конкуренцию многопроцессного кэша в shared memory."""

    result = ScenarioResult("contention")
    cache = None
    process_count = max(2, int(args.processes))
    try:
        segments = max(8, process_count * 2)
        locks = [multiprocessing.Lock() for _ in range(min(64, segments))]
        cache = ObjectCache(
            CONTENTION_SEGMENT_LEN * segments,
            CONTENTION_STRUCT,
            locks,
        )

        for mode in ("distinct", "shared"):
            data = _run_contention_mode(
                cache, mode, process_count, max(0.1, float(args.duration))
            )
            total = data["iterations"]
            wall = data["wall_elapsed"]
            throughput = _rate(total, wall)
            latency = wall * 1_000_000.0 / total if total else 0.0
            result.metric(
                f"contention.{mode}.throughput",
                throughput,
                "ops/s",
                f"{process_count} процессов",
            )
            result.metric(
                f"contention.{mode}.latency",
                latency,
                "us/op",
                "суммарное реальное время",
            )
            if mode == "distinct":
                result.metric(
                    "contention.distinct.per_process",
                    throughput / process_count,
                    "ops/s",
                )
            result.check(
                f"{mode} mode executed",
                total > 0 and wall > 0,
                f"iterations={total}, wall={wall:.3f}s",
            )

        segment_counters = [
            cache.get(segment, "counter") for segment in range(process_count)
        ]
        result.check(
            "worker segments updated",
            all(counter > 0 for counter in segment_counters),
            repr(segment_counters),
        )
        result.check(
            "cache readable after contention",
            isinstance(cache.get(0, "counter"), int)
            and cache.get(0, "counter") >= 0,
        )
    finally:
        if cache is not None:
            cache.close()
    return result


def run_collision(args) -> ScenarioResult:
    """Измеряет преобразования полигонов и проверки пересечения SAT."""

    from collision import (
        convex_polygons_overlap,
        transform_polygon,
        validate_convex_polygon,
    )

    result = ScenarioResult("collision")
    polygon = validate_convex_polygon(
        [[-10, -10], [10, -10], [10, 10], [-10, 10]]
    )
    pairs = max(1, int(args.pairs))

    left = transform_polygon(polygon, (0, 0), 0, 1)
    touching = transform_polygon(polygon, (10, 0), 0, 1)
    apart = transform_polygon(polygon, (30, 0), 0, 1)
    result.check("SAT detects overlap", convex_polygons_overlap(left, touching))
    result.check("SAT rejects gap", not convex_polygons_overlap(left, apart))

    started = time.perf_counter()
    for index in range(pairs):
        transform_polygon(
            polygon,
            (index % 200, (index * 7) % 200),
            index % 360,
            1.0,
        )
    elapsed = time.perf_counter() - started
    result.metric("collision.transform", _rate(pairs, elapsed), "ops/s",
                  "полигон из 4 вершин")

    variants = [
        transform_polygon(
            polygon,
            ((index % 16) * 10, (index // 16) * 10),
            index * 5,
            1.0,
        )
        for index in range(64)
    ]
    started = time.perf_counter()
    for index in range(pairs):
        convex_polygons_overlap(
            variants[index % 64],
            variants[(index * 7 + 1) % 64],
        )
    elapsed = time.perf_counter() - started
    result.metric("collision.overlap", _rate(pairs, elapsed), "checks/s",
                  "предвычисленные полигоны")

    hits = 0
    started = time.perf_counter()
    for index in range(pairs):
        first_x = index % 100
        # Смещения 0..39 включают как пересекающиеся, так и разделённые пары.
        offset = index % 40
        first = transform_polygon(
            polygon, (first_x, 0), index % 360, 1.0
        )
        second = transform_polygon(
            polygon, (first_x + offset, 10), (index * 3) % 360, 1.0
        )
        if convex_polygons_overlap(first, second):
            hits += 1
    elapsed = time.perf_counter() - started
    result.metric("collision.pipeline", _rate(pairs, elapsed), "pairs/s",
                  "2 трансформа + SAT")
    result.check(
        "collision pipeline produced both outcomes",
        hits > 0 and hits < pairs,
        f"{hits}/{pairs} overlaps",
    )
    return result


class _FakeSprite:
    __slots__ = ("_position",)

    def __init__(self):
        self._position = (0.0, 0.0)

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, value):
        self._position = value

    @property
    def center_x(self):
        return self._position[0]

    @center_x.setter
    def center_x(self, value):
        self._position = (value, self._position[1])

    @property
    def center_y(self):
        return self._position[1]

    @center_y.setter
    def center_y(self, value):
        self._position = (self._position[0], value)


def run_render(args) -> ScenarioResult:
    """Измеряет реальный код истории позиций рендера и интерполяции."""

    import render

    result = ScenarioResult("render")
    objects = max(1, int(args.objects))
    frames = max(1, int(args.frames))
    history_size = int(POSITION_HISTORY_SIZE)

    game = render.Game.__new__(render.Game)
    game._position_history_by_render_id = {}
    game._sprites_by_render_id = {}
    game._moving_render_ids = set()
    game._world_data_by_render_id = {}
    game._hit_cells = {}
    game._hit_geometry_by_render_id = {}
    game._hit_large_ids = set()
    game._hit_dirty_ids = set()

    probe_id = 10 ** 9
    game._position_history_by_render_id[probe_id] = deque(
        [(0, 0.0, 0.0), (1, 10.0, 20.0)],
        maxlen=history_size,
    )
    probe = game._position_at_tick(probe_id, 0.5)
    result.check(
        "linear interpolation",
        probe is not None
        and math.isclose(probe[0], 5.0)
        and math.isclose(probe[1], 10.0),
        repr(probe),
    )
    result.check(
        "clamp before history",
        game._position_at_tick(probe_id, -1) == (0.0, 0.0),
    )
    result.check(
        "clamp after history",
        game._position_at_tick(probe_id, 99) == (10.0, 20.0),
    )
    game._position_history_by_render_id.pop(probe_id, None)

    for render_id in range(objects):
        history = deque(maxlen=history_size)
        for tick in range(history_size):
            history.append((
                tick,
                render_id * 0.01 + tick * 0.1,
                render_id * 0.02 - tick * 0.05,
            ))
        game._position_history_by_render_id[render_id] = history
        game._sprites_by_render_id[render_id] = _FakeSprite()

    # Этот сценарий измеряет худший случай: каждый объект движется каждый кадр.
    game._moving_render_ids = set(range(objects))

    frame_times = []
    started = time.perf_counter()
    for frame in range(frames):
        frame_started = time.perf_counter()
        tick = history_size + frame
        for render_id in range(objects):
            game._record_position(
                render_id,
                (
                    frame * 0.1 + render_id * 0.01,
                    frame * -0.05 + render_id * 0.02,
                ),
                tick,
            )
        game._interpolate_sprite_positions(
            max(0.0, tick - INTERPOLATION_DELAY_TICKS)
        )
        frame_times.append(time.perf_counter() - frame_started)
    elapsed = time.perf_counter() - started

    result.metric("render.interpolation.frames", _rate(frames, elapsed), "frames/s",
                  f"{objects} объектов")
    result.metric(
        "render.interpolation.objects",
        _rate(objects * frames, elapsed),
        "objects/s",
        f"{objects} объектов x {frames} кадров",
    )
    result.metric(
        "render.interpolation.frame_p95",
        _percentile(frame_times, 95) * 1000.0,
        "ms/frame",
    )
    sample = game._sprites_by_render_id[0]
    result.check(
        "sprites updated",
        math.isfinite(sample.center_x) and math.isfinite(sample.center_y),
        f"center=({sample.center_x}, {sample.center_y})",
    )
    result.check(
        "history bounded",
        all(
            len(history) <= history_size
            for history in game._position_history_by_render_id.values()
        ),
    )

    # Статическая сцена не должна тратить время на интерполяцию вообще.
    game._moving_render_ids.clear()
    static_sample = game._sprites_by_render_id[0]
    static_sample.center_x = -12345.0
    static_sample.center_y = -54321.0
    static_started = time.perf_counter()
    for _ in range(frames):
        game._interpolate_sprite_positions(10 ** 9)
    static_elapsed = time.perf_counter() - static_started
    game._moving_render_ids.clear()
    result.metric(
        "render.interpolation.static_frame_p95",
        static_elapsed / max(1, frames) * 1000.0,
        "ms/frame",
        f"{objects} статических объектов",
    )
    result.check(
        "static sprites untouched",
        static_sample.center_x == -12345.0
        and static_sample.center_y == -54321.0,
        f"center=({static_sample.center_x}, {static_sample.center_y})",
    )
    return result


class _LoopbackInStream:
    """Немедленно отвечает на запросы регистрации, без процесса Stream."""

    def __init__(self, out_stream, objects_cache=None):
        self._out_stream = out_stream
        self._objects_cache = objects_cache
        self._next_id = 0
        self.commands = []

    def put(self, message, *args, **kwargs):
        if not isinstance(message, dict):
            return
        command = message.get("command")
        self.commands.append(command)
        if command == "register":
            object_id = self._next_id
            self._next_id += 1
            # Имитируем часть Stream, которая сохраняет зарегистрированную запись;
            # id и записи в кэш выполняет сам GameObject.register.
            if self._objects_cache is not None:
                self._objects_cache[message["id"]] = message["obj"]
            self._out_stream.put({
                message["request_id"]: {
                    "object_id": object_id,
                    "render_id": object_id,
                },
            })
        elif command == "register_many":
            results = []
            for item in message["objects"]:
                object_id = self._next_id
                self._next_id += 1
                if self._objects_cache is not None:
                    self._objects_cache[item["id"]] = item["obj"]
                results.append({
                    "object_id": object_id,
                    "render_id": object_id,
                })
            self._out_stream.put({
                message["request_id"]: {"objects": results},
            })


def run_batch(args) -> ScenarioResult:
    """Измеряет локальный пакетный путь create_object (валидация + кэши)."""

    from pyengine import create_object

    result = ScenarioResult("batch")
    count = max(1, int(args.objects))
    visual_cache = None
    id_cache = None
    try:
        capacity = count + 8
        visual_cache = ObjectCache(
            VISUAL_SEGMENT_LEN * capacity, VISUAL_STRUCT
        )
        id_cache = ObjectCache(ID_SEGMENT_LEN * capacity, ID_STRUCT)

        main_out_stream = queue.Queue()
        objects_cache = {}
        main_in_stream = _LoopbackInStream(main_out_stream, objects_cache)
        deps = {
            "main_in_stream": main_in_stream,
            "main_out_stream": main_out_stream,
            "objects_cache": objects_cache,
            "lock": threading.Lock(),
            "visual_cache": visual_cache,
            "id_cache": id_cache,
        }
        payload = [_stress_object(index, "stress_batch") for index in range(count)]

        started = time.perf_counter()
        created = create_object(payload, deps=deps)
        elapsed = time.perf_counter() - started

        result.metric("batch.objects", _rate(count, elapsed), "objects/s",
                      "один вызов create_object(list)")
        result.metric("batch.per_object", elapsed * 1000.0 / count, "ms/object")

        result.check("batch returned every object", len(created) == count,
                     f"{len(created)}/{count}")
        result.check(
            "all objects registered",
            all(record is not None for record in objects_cache.values())
            and len(objects_cache) == count,
            f"objects_cache={len(objects_cache)}",
        )
        result.check(
            "render ids unique",
            len({game_object._render_id for game_object in created}) == count,
        )
        first = created[0]
        last = created[-1]
        result.check(
            "first visual cache entry",
            math.isclose(visual_cache.get(first._render_id, "x"), 0.0)
            and math.isclose(visual_cache.get(first._render_id, "y"), 0.0),
        )
        result.check(
            "last identity cache entry",
            id_cache.get(last._render_id, "name") == f"stress_batch_{count - 1}",
            repr(id_cache.get(last._render_id, "name")),
        )
        result.check(
            "registration commands emitted",
            main_in_stream.commands.count("register_many") == 1
            and main_in_stream.commands.count("render") == count,
            f"register_many={main_in_stream.commands.count('register_many')}, "
            f"render={main_in_stream.commands.count('render')}",
        )
    finally:
        if visual_cache is not None:
            visual_cache.close()
        if id_cache is not None:
            id_cache.close()
    return result


class LiveStreamHarness:
    """Запускает настоящий процесс Stream и рабочие процессы для живых сценариев."""

    def __init__(self, workers=2, capacity=256, timeout=20.0):
        self.workers = max(1, int(workers))
        self.capacity = max(64, int(capacity))
        self.timeout = max(5.0, float(timeout))
        self._closed = False

    def __enter__(self):
        import render
        import stream

        self.manager = multiprocessing.Manager()
        self.render_in_stream = self.manager.Queue()
        self.render_out_stream = self.manager.Queue()
        self.main_in_stream = self.manager.Queue()
        self.main_out_stream = self.manager.Queue()
        self.objects_cache = self.manager.dict()
        self.object_lock = self.manager.Lock()
        self.status = self.manager.list()
        self.worker_ready = self.manager.list([False] * self.workers)
        self.worker_queues = [
            self.manager.Queue() for _ in range(self.workers)
        ]
        self.lock = self.manager.Lock()

        self.visual_locks = [multiprocessing.Lock() for _ in range(64)]
        self.visual_dirty_counts = multiprocessing.Array(
            "i", len(self.visual_locks), lock=False
        )
        self.visual_dirty_queue = multiprocessing.Array(
            "i",
            len(self.visual_locks) * (3 + DIRTY_RING_CAPACITY),
            lock=False,
        )
        self.raw_visual = ObjectCache(
            VISUAL_SEGMENT_LEN * self.capacity,
            VISUAL_STRUCT,
            self.visual_locks,
            self.visual_dirty_counts,
            dirty_queue=self.visual_dirty_queue,
            dirty_ring_size=DIRTY_RING_CAPACITY,
        )
        self.visual_cache = [
            self.raw_visual.key,
            VISUAL_STRUCT,
            self.visual_locks,
            self.visual_dirty_counts,
            self.raw_visual.capacity,
            self.visual_dirty_queue,
            DIRTY_RING_CAPACITY,
        ]
        self.id_locks = [multiprocessing.Lock() for _ in range(64)]
        self.raw_id = ObjectCache(
            ID_SEGMENT_LEN * self.capacity,
            ID_STRUCT,
            self.id_locks,
        )
        self.id_cache = [
            self.raw_id.key, ID_STRUCT, self.id_locks, None,
            self.raw_id.capacity,
        ]

        self.engine_locks = [multiprocessing.Lock() for _ in range(4)]
        self.raw_engine_state = ObjectCache(
            ENGINE_STATE_SEGMENT_BYTES,
            ENGINE_STATE_STRUCT,
            self.engine_locks,
        )
        self.engine_state = [
            self.raw_engine_state.key,
            ENGINE_STATE_STRUCT,
            self.engine_locks,
            None,
            self.raw_engine_state.capacity,
        ]
        self.time_view = FieldView(self.raw_engine_state, "time")
        self.tick_view = FieldView(self.raw_engine_state, "tick")
        self.motion_locks = [multiprocessing.Lock() for _ in range(8)]
        self.raw_motion = ObjectCache(
            MOUSE_MOTION_BYTES,
            MOUSE_MOTION_STRUCT,
            self.motion_locks,
        )
        self.raw_motion.set_many(0, {
            "seq": 0,
            "x": 0.0,
            "y": 0.0,
            "dx": 0.0,
            "dy": 0.0,
            "screen_x": 0.0,
            "screen_y": 0.0,
            "hovered": -1,
        })
        self.motion_cache = [
            self.raw_motion.key,
            MOUSE_MOTION_STRUCT,
            self.motion_locks,
            None,
            self.raw_motion.capacity,
        ]
        self.keyboard_locks = [multiprocessing.Lock() for _ in range(16)]
        self.raw_keyboard_events = ObjectCache(
            KEYBOARD_EVENT_BYTES,
            KEYBOARD_EVENT_STRUCT,
            self.keyboard_locks,
        )
        self.keyboard_ring_data = [
            self.raw_keyboard_events.key,
            KEYBOARD_EVENT_STRUCT,
            self.keyboard_locks,
            None,
            self.raw_keyboard_events.capacity,
        ]
        self.camera_target = ValueCache(-1)
        self.camera_scale = ValueCache(1.0)
        self.camera_state = [
            self.camera_target.name, self.camera_scale.name,
        ]

        self.clock_origin = time.monotonic()
        self.tact_stop = threading.Event()
        self.tact_thread = threading.Thread(
            target=render.tact_counter,
            args=(
                self.clock_origin,
                FIXED_DT,
                self.raw_engine_state,
                self.tact_stop,
            ),
            daemon=True,
        )
        self.tact_thread.start()

        self.process = multiprocessing.Process(
            target=stream.Stream,
            args=(
                self.engine_state,
                self.render_in_stream,
                self.render_out_stream,
                self.main_in_stream,
                self.main_out_stream,
                self.objects_cache,
                self.status,
                self.worker_queues,
                self.worker_ready,
                self.lock,
                self.visual_cache,
                self.id_cache,
                self.object_lock,
                self.camera_state,
                self.motion_cache,
                self.keyboard_ring_data,
                self.manager.address,
                self.manager._authkey,
            ),
        )
        self.process.start()

        try:
            self._wait_ready()
            self.client_visual = ObjectCache(*self.visual_cache)
            self.client_id = ObjectCache(*self.id_cache)
            self.deps = {
                "main_in_stream": self.main_in_stream,
                "main_out_stream": self.main_out_stream,
                "objects_cache": self.objects_cache,
                "lock": self.object_lock,
                "visual_cache": self.client_visual,
                "id_cache": self.client_id,
                "time_cache": self.time_view,
                "tick_cache": self.tick_view,
            }
        except BaseException:
            self.close()
            raise
        return self

    def _scene_object_count(self):
        return sum(
            1
            for record in self.objects_cache.values()
            if isinstance(record, dict) and "_render_id" in record
        )

    def _drain_render_queue(self):
        while True:
            try:
                self.render_in_stream.get_nowait()
            except queue.Empty:
                return

    def _wait_ready(self):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if not self.process.is_alive():
                raise RuntimeError(
                    "Процесс Stream завершился при запуске; смотрите его traceback"
                )
            if (all(self.worker_ready)
                    and self._scene_object_count() >= SCENE_OBJECT_COUNT):
                self._drain_render_queue()
                return
            time.sleep(0.05)
        raise RuntimeError(
            f"Stream не стал готов за {self.timeout:.1f}с "
            f"(workers={list(self.worker_ready)}, "
            f"scene_objects={self._scene_object_count()})"
        )

    def close(self):
        if self._closed:
            return
        self._closed = True

        for attachment in (
            getattr(self, "client_visual", None),
            getattr(self, "client_id", None),
        ):
            if attachment is not None:
                try:
                    attachment.detach()
                except Exception:
                    pass

        self.tact_stop.set()
        self.tact_thread.join(timeout=2)

        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=5)

        self.manager.shutdown()
        for cache in (
            self.raw_visual,
            self.raw_id,
            self.raw_engine_state,
            self.raw_motion,
            self.raw_keyboard_events,
            self.camera_target,
            self.camera_scale,
        ):
            try:
                cache.close()
            except Exception:
                pass

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def run_registration(args) -> ScenarioResult:
    """Измеряет регистрацию объектов в реальном времени через Stream."""

    from pyengine import create_object

    result = ScenarioResult("registration")
    count = max(1, int(args.objects))
    capacity = count + max(64, SCENE_OBJECT_COUNT * 4)
    with LiveStreamHarness(
        workers=args.workers, capacity=capacity, timeout=args.timeout
    ) as harness:
        latencies = []
        created = []
        started = time.perf_counter()
        for index in range(count):
            data = _stress_object(index, "stress_reg")
            object_started = time.perf_counter()
            created.append(create_object(data, deps=harness.deps))
            latencies.append(time.perf_counter() - object_started)
        elapsed = time.perf_counter() - started

        result.metric("registration.objects", _rate(count, elapsed), "objects/s",
                      f"{count} объектов, {args.workers} рабочих")
        result.metric("registration.p50", _percentile(latencies, 50) * 1000.0,
                      "ms/object")
        result.metric("registration.p95", _percentile(latencies, 95) * 1000.0,
                      "ms/object")
        result.metric("registration.p99", _percentile(latencies, 99) * 1000.0,
                      "ms/object")

        result.check("all objects created", len(created) == count,
                     f"{len(created)}/{count}")
        result.check(
            "object ids unique",
            len({game_object._id for game_object in created}) == count,
        )
        last = created[-1]
        expected_x = float((count - 1) % 100)
        expected_y = float((count - 1) // 100)
        result.check(
            "registered in objects_cache",
            harness.objects_cache.get(last._id) is not None,
        )
        result.check(
            "visual cache updated",
            math.isclose(harness.client_visual.get(last._render_id, "x"), expected_x)
            and math.isclose(harness.client_visual.get(last._render_id, "y"), expected_y),
            f"x={harness.client_visual.get(last._render_id, 'x')}, "
            f"y={harness.client_visual.get(last._render_id, 'y')}",
        )
        result.check(
            "identity cache updated",
            harness.client_id.get(last._render_id, "name")
            == f"stress_reg_{count - 1}",
            repr(harness.client_id.get(last._render_id, "name")),
        )

        add_count = 0
        deadline = time.monotonic() + max(10.0, count * 0.01)
        while time.monotonic() < deadline and add_count < count:
            try:
                command = harness.render_in_stream.get(timeout=0.25)
            except queue.Empty:
                continue
            if command.get("command") == "add":
                add_count += 1
        result.check(
            "render add commands",
            add_count >= count,
            f"{add_count}/{count}",
        )
    return result


def run_scripts(args) -> ScenarioResult:
    """Измеряет циклы обновления живых компонентов и их записи в кэш."""

    from pyengine import create_object

    result = ScenarioResult("scripts")
    count = max(1, int(args.scripts))
    iterations = max(0, int(args.iterations))
    stride = 1.0
    component_path = "tests/fixtures/stress_component.py"
    capacity = count + max(64, SCENE_OBJECT_COUNT * 4)

    with LiveStreamHarness(
        workers=args.workers, capacity=capacity, timeout=args.timeout
    ) as harness:
        created = []
        for index in range(count):
            data = _stress_object(index, "stress_script")
            data["components"] = [{
                "path": component_path,
                "properties": {
                    "iterations": iterations,
                    "stride": stride,
                },
            }]
            created.append(create_object(data, deps=harness.deps))

        deadline = time.monotonic() + max(10.0, args.timeout)
        rotations = []
        while time.monotonic() < deadline:
            rotations = [
                harness.client_visual.get(game_object._render_id, "rotation")
                for game_object in created
            ]
            if all(value > 0 for value in rotations):
                break
            time.sleep(0.05)
        result.check(
            "component loops started",
            all(value > 0 for value in rotations),
            f"rotations={rotations}",
        )

        before = list(rotations)
        started = time.perf_counter()
        time.sleep(max(0.1, float(args.duration)))
        after = [
            harness.client_visual.get(game_object._render_id, "rotation")
            for game_object in created
        ]
        elapsed = time.perf_counter() - started

        rates = [
            max(0.0, current - previous) / stride / elapsed
            for previous, current in zip(before, after)
        ]
        average = sum(rates) / len(rates) if rates else 0.0
        result.metric("scripts.updates", average, "updates/s",
                      f"среднее по {count}, {iterations} итераций/обновление")
        result.metric("scripts.target", float(SCRIPT_UPDATE_HZ), "updates/s",
                      "SCRIPT_UPDATE_HZ")
        result.metric(
            "scripts.efficiency",
            average / SCRIPT_UPDATE_HZ * 100.0 if SCRIPT_UPDATE_HZ > 0 else 0.0,
            "% от цели",
        )
        result.metric("scripts.min_updates", min(rates) if rates else 0.0,
                      "updates/s")
        result.metric("scripts.max_updates", max(rates) if rates else 0.0,
                      "updates/s")
        result.metric(
            "scripts.update_period",
            1000.0 / average if average > 0 else 0.0,
            "ms/update",
        )
        result.check(
            "updates observed",
            bool(rates) and all(rate > 1.0 for rate in rates),
            f"rates={[round(rate, 1) for rate in rates]}",
        )
        result.check(
            "rotation advanced",
            all(current > previous for previous, current in zip(before, after)),
        )

        # Кольцо клавиатуры в shared memory: одно нажатие клавиши должно дойти до каждого скрипта.
        harness.main_in_stream.put({
            "command": "input",
            "type": "key_down",
            "key": "STRESS_KEY",
            "modifiers": 0,
        })
        scales = []
        deadline = time.monotonic() + max(5.0, args.timeout)
        while time.monotonic() < deadline:
            scales = [
                harness.client_visual.get(game_object._render_id, "scale")
                for game_object in created
            ]
            if all(scale > 1.0 for scale in scales):
                break
            time.sleep(0.05)
        harness.main_in_stream.put({
            "command": "input",
            "type": "key_up",
            "key": "STRESS_KEY",
            "modifiers": 0,
        })
        result.check(
            "keyboard callback delivered",
            all(scale > 1.0 for scale in scales),
            f"scales={scales}",
        )
    return result


def run_motion(args) -> ScenarioResult:
    """Проверяет, что множество движущихся скриптов не замораживает общий render tick."""

    import render

    from pyengine import create_object

    result = ScenarioResult("motion")
    count = max(1, int(args.motion_objects))
    component_path = "tests/fixtures/motion_component.py"
    capacity = count + max(64, SCENE_OBJECT_COUNT * 4)
    delay_ticks = POSITION_INTERPOLATION_DELAY / FIXED_DT
    # Записи позиции несут tick из своего fixed_update(), поэтому ожидаемая
    # частота ограничена часами симуляции, а не SCRIPT_UPDATE_HZ.
    min_rate = max(10.0, SIMULATION_HZ * 0.3)

    with LiveStreamHarness(
        workers=args.workers, capacity=capacity, timeout=args.timeout
    ) as harness:
        created = []
        try:
            for index in range(count):
                data = _stress_object(index, f"stress_motion_{index}")
                data["visual"]["position"] = [
                    float(index % 20) * 8.0,
                    float(index // 20) * 8.0,
                ]
                data["components"] = [{
                    "path": component_path,
                    "properties": {"speed": 120.0, "half_range": 300.0},
                }]
                created.append(create_object(data, deps=harness.deps))

            # Даём каждому рабочему процессу запустить свои потоки скриптов и войти в ритм.
            time.sleep(1.5)

            tick_probe = render.Game.__new__(render.Game)
            tick_probe.clock_origin = harness.clock_origin
            tick_probe.SIMULATION_TICK = FIXED_DT
            tick_probe.POSITION_INTERPOLATION_DELAY = POSITION_INTERPOLATION_DELAY

            render_ids = [game_object._render_id for game_object in created]
            first_tick = {
                render_id: harness.client_visual.get(render_id, "tick")
                for render_id in render_ids
            }
            last_tick = dict(first_tick)
            last_change_at = {render_id: time.monotonic() for render_id in render_ids}
            gaps = {render_id: [] for render_id in render_ids}
            render_ticks = []
            sample_period = FIXED_DT
            duration = max(0.5, float(args.duration))
            started = time.perf_counter()
            next_sample = started
            while True:
                now = time.perf_counter()
                if now < next_sample:
                    time.sleep(min(0.002, next_sample - now))
                    continue
                next_sample += sample_period
                if next_sample < now:
                    next_sample = now
                if now - started >= duration:
                    break

                wall = time.monotonic()
                for render_id in render_ids:
                    tick = harness.client_visual.get(render_id, "tick")
                    previous = last_tick[render_id]
                    if tick != previous:
                        if tick > previous:
                            gaps[render_id].append(wall - last_change_at[render_id])
                        last_tick[render_id] = tick
                        last_change_at[render_id] = wall
                render_ticks.append(tick_probe._resolve_render_tick())

            elapsed = time.perf_counter() - started
            steps = [
                current - previous
                for previous, current in zip(render_ticks, render_ticks[1:])
            ]
            frozen = sum(1 for step in steps if step <= 0.01)
            backward = sum(1 for step in steps if step < -0.01)
            big_jumps = sum(1 for step in steps if step > 2.5)
            rates = [
                (last_tick[render_id] - first_tick[render_id]) / elapsed
                for render_id in render_ids
            ]
            all_gaps = [gap for values in gaps.values() for gap in values]

            result.metric("motion.objects", float(count), "objects")
            result.metric("motion.frames", _rate(len(render_ticks), elapsed),
                          "frames/s")
            result.metric("motion.updates_mean",
                          sum(rates) / len(rates) if rates else 0.0, "updates/s")
            result.metric("motion.updates_min", min(rates) if rates else 0.0,
                          "updates/s", f"цель {SCRIPT_UPDATE_HZ}")
            result.metric("motion.gap_p95", _percentile(all_gaps, 95) * 1000.0,
                          "ms", f"{count} объектов")
            result.metric("motion.gap_max",
                          max(all_gaps) * 1000.0 if all_gaps else 0.0, "ms")
            result.metric("motion.render_step_std",
                          statistics.pstdev(steps) if len(steps) > 1 else 0.0,
                          "ticks")
            result.metric(
                "motion.render_frozen",
                100.0 * frozen / len(steps) if steps else 0.0,
                "% от кадров",
            )
            result.metric("motion.render_big_jumps", float(big_jumps),
                          "frames", "шаг > 2 тиков")
            result.metric("motion.render_max_step",
                          max(steps) if steps else 0.0, "ticks")

            result.check(
                "moving objects advance",
                bool(rates) and min(rates) >= min_rate,
                f"min={min(rates) if rates else 0.0:.1f}/s, target>={min_rate:.1f}/s",
            )
            result.check(
                "shared render tick stays smooth",
                bool(steps) and backward == 0
                and 100.0 * frozen / len(steps) <= 5.0,
                f"frozen={frozen}/{len(steps)}, backward={backward}",
            )
            result.check(
                "no multi-tick render jumps",
                bool(steps) and max(steps) <= 3.0,
                f"max_step={max(steps) if steps else 0.0:.2f} ticks "
                f"(delay={delay_ticks:.1f})",
            )
            result.check(
                "heartbeats stay inside interpolation window",
                bool(all_gaps)
                and _percentile(all_gaps, 95) < POSITION_INTERPOLATION_DELAY,
                f"p95={_percentile(all_gaps, 95) * 1000.0:.1f}ms, "
                f"delay={POSITION_INTERPOLATION_DELAY * 1000.0:.1f}ms",
            )
        finally:
            for game_object in created:
                try:
                    game_object.remove()
                except Exception:
                    pass
            time.sleep(0.4)
    return result


SCENARIOS = {
    "cache": run_cache,
    "contention": run_contention,
    "collision": run_collision,
    "render": run_render,
    "registration": run_registration,
    "batch": run_batch,
    "scripts": run_scripts,
    "motion": run_motion,
}


def _format_value(value: float) -> str:
    if not math.isfinite(value):
        return str(value)
    if abs(value) >= 1_000_000:
        return f"{value:,.0f}"
    if abs(value) >= 1000:
        return f"{value:,.1f}"
    if abs(value) >= 10:
        return f"{value:,.2f}"
    return f"{value:,.3f}"


def print_report(results: list[ScenarioResult]) -> None:
    print("\n=== Отчёт стресс-теста ===")
    header = (
        f"{'Сценарий':<14} {'Метрика':<38} {'Значение':>15} "
        f"{'Единица':<12} {'Статус':<6}"
    )
    print(header)
    print("-" * len(header))
    for result in results:
        if result.error:
            print(
                f"{result.name:<14} {'ERROR':<38} {'':>15} "
                f"{'':<12} {'FAIL':<6}"
            )
            for line in result.error.strip().splitlines()[-8:]:
                print(f"  {line}")
            continue
        for metric in result.metrics:
            status = "ok" if result.ok else "FAIL"
            print(
                f"{result.name:<14} {metric.name:<38} "
                f"{_format_value(metric.value):>15} {metric.unit:<12} {status:<6}"
            )
        for check in result.checks:
            if not check.ok:
                print(f"  ПРОВЕРКА НЕ ПРОЙДЕНА: {check.name}: {check.detail}")

    total_checks = sum(len(result.checks) for result in results)
    failed_checks = sum(
        1 for result in results for check in result.checks if not check.ok
    )
    failed_scenarios = [result.name for result in results if not result.ok]
    print(
        f"\nСценарии: {len(results) - len(failed_scenarios)}/{len(results)} пройдено"
    )
    print(f"Проверки: {total_checks - failed_checks}/{total_checks} пройдено")
    if failed_scenarios:
        print("Провалены:", ", ".join(failed_scenarios))
    else:
        print("ALL_STRESS_OK")


def _jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def write_report(path: Path, results: list[ScenarioResult], args) -> None:
    config = {
        key: value for key, value in vars(args).items() if key != "json"
    }
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "config": _jsonable(config),
        "ok": all(result.ok for result in results),
        "results": [
            {
                "name": result.name,
                "ok": result.ok,
                "duration_s": result.duration,
                "error": result.error,
                "metrics": [asdict(metric) for metric in result.metrics],
                "checks": [asdict(check) for check in result.checks],
            }
            for result in results
        ],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"JSON-отчёт: {path}")


def _resolve_scenarios(value: str) -> list[str]:
    requested = [item.strip() for item in value.split(",") if item.strip()]
    if not requested:
        raise ValueError("--scenario не должен быть пустым")
    if "all" in requested:
        return list(SCENARIOS)
    unknown = [name for name in requested if name not in SCENARIOS]
    if unknown:
        raise ValueError(
            f"Неизвестные сценарии: {', '.join(unknown)}. "
            f"Доступные: {', '.join(SCENARIOS)}"
        )
    return requested


def build_parser() -> argparse.ArgumentParser:
    cpu_count = os.cpu_count() or 2
    parser = argparse.ArgumentParser(
        description="Стресс-тестирование ядра движка с несколькими нагрузками.",
    )
    parser.add_argument(
        "--scenario",
        default="all",
        help="all или список через запятую: " + ",".join(SCENARIOS),
    )
    parser.add_argument(
        "--duration", type=float, default=2.0,
        help="секунд на сценарий с замером (по умолчанию: 2.0)",
    )
    parser.add_argument(
        "--objects", type=int, default=1000,
        help="количество объектов для batch/registration/render (по умолчанию: 1000)",
    )
    parser.add_argument(
        "--pairs", type=int, default=100_000,
        help="пар коллизий на бенчмарк (по умолчанию: 100000)",
    )
    parser.add_argument(
        "--frames", type=int, default=60,
        help="кадров рендера (по умолчанию: 60)",
    )
    parser.add_argument(
        "--cache-ops", type=int, default=100_000,
        help="операций на бенчмарк кэша (по умолчанию: 100000)",
    )
    parser.add_argument(
        "--processes", type=int, default=max(2, min(4, cpu_count)),
        help="процессов конкуренции (по умолчанию: 2..4)",
    )
    parser.add_argument(
        "--workers", type=int, default=max(1, min(2, cpu_count - 1)),
        help="рабочих процессов скриптов Stream (по умолчанию: 1..2)",
    )
    parser.add_argument(
        "--scripts", type=int, default=1,
        help="объектов со скриптами в сценарии scripts (по умолчанию: 1)",
    )
    parser.add_argument(
        "--motion-objects", type=int, default=16,
        help="движущихся объектов в сценарии motion (по умолчанию: 16)",
    )
    parser.add_argument(
        "--iterations", type=int, default=100,
        help="арифметических итераций на обновление компонента (по умолчанию: 100)",
    )
    parser.add_argument(
        "--timeout", type=float, default=20.0,
        help="таймаут запуска Stream в секундах (по умолчанию: 20)",
    )
    parser.add_argument(
        "--json", type=Path, default=None,
        help="записать JSON-отчёт по этому пути",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="вывести список сценариев и выйти",
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list:
        for name in SCENARIOS:
            print(name)
        return 0

    try:
        names = _resolve_scenarios(args.scenario)
    except ValueError as error:
        parser.error(str(error))

    results = []
    for name in names:
        print(f"[stress] запуск {name} ...", flush=True)
        started = time.perf_counter()
        try:
            result = SCENARIOS[name](args)
        except Exception:
            result = ScenarioResult(name, error=traceback.format_exc())
        result.duration = time.perf_counter() - started
        results.append(result)
        print(
            f"[stress] {name}: {'ok' if result.ok else 'FAIL'} "
            f"({result.duration:.2f}s)",
            flush=True,
        )

    print_report(results)
    if args.json is not None:
        write_report(args.json, results, args)
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
