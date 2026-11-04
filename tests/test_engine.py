import math
import os
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import render
from memory_lib import ObjectCache, ValueCache
from pyengine import (
    GameObject,
    IdCache,
    Keyboard,
    Vector2,
    VisualCache,
    create_object,
    creat_object,
    script,
)
from scene import validate_object_data
from stream import Stream
from config import MAX_CACHE_STRING_BYTES, PROJECT_DIR, resolve_project_path


VISUAL_STRUCT = {
    "x": float,
    "y": float,
    "sim_time": float,
    "tick": int,
    "rotation": float,
    "scale": float,
    "alpha": float,
    "sprite": str,
    "layer": float,
}
ID_STRUCT = {
    "name": str,
    "type": str,
    "class": str,
    "id": str,
}


class FakeResponseQueue:
    def __init__(self):
        self.items = []
        self._queue = queue.Queue()

    def put(self, value):
        self.items.append(value)
        self._queue.put(value)

    def get(self, timeout=None):
        return self._queue.get(timeout=timeout)


class FakeRequestQueue:
    def __init__(self, responses, render_id=0, object_id=0):
        self.responses = responses
        self.render_id = render_id
        self.object_id = object_id
        self.messages = []

    def put(self, message):
        self.messages.append(message)
        if message["command"] == "register":
            self.responses.put({
                message["request_id"]: {
                    "object_id": self.object_id,
                    "render_id": self.render_id,
                }
            })


class CacheTestCase(unittest.TestCase):
    def make_caches(self, size=1024 * 8):
        visual = VisualCache(size, VISUAL_STRUCT)
        identity = IdCache(size, ID_STRUCT)
        self.addCleanup(visual.close)
        self.addCleanup(identity.close)
        return visual, identity

    def make_deps(self, visual, identity):
        responses = FakeResponseQueue()
        requests = FakeRequestQueue(responses)
        deps = {
            "main_in_stream": requests,
            "main_out_stream": responses,
            "objects_cache": {},
            "lock": threading.Lock(),
            "visual_cache": visual,
            "id_cache": identity,
        }
        return deps, requests, responses


class Vector2Tests(unittest.TestCase):
    def test_add_and_subtract_pairs(self):
        self.assertEqual((Vector2(1, 2) + [3, 4]).__repr__(), "Vector2(4, 6)")
        self.assertEqual((Vector2(1, 2) - (3, 4)).__repr__(), "Vector2(-2, -2)")

    def test_invalid_operand_raises_type_error(self):
        with self.assertRaises(TypeError):
            Vector2(1, 2) + 3
        with self.assertRaises(TypeError):
            Vector2(1, 2) + [1, 2, 3]


class ProjectPathTests(unittest.TestCase):
    def test_script_paths_work_from_another_working_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                stream = Stream.__new__(Stream)
                stream._script_complexity_cache = {}

                resolved = resolve_project_path("access/scripts/test.py")
                self.assertEqual(
                    resolved,
                    PROJECT_DIR / "access" / "scripts" / "test.py",
                )
                self.assertGreater(
                    stream.get_script_diff("access/scripts/test.py"), 0
                )

                from scripter_runner import scripter_runner as Runner
                runner = Runner.__new__(Runner)
                runner._script_modules = {}
                module = runner._scripter_runner__load_script_module(
                    "access/scripts/test.py"
                )
                self.assertTrue(hasattr(module, "test"))
            finally:
                os.chdir(original_cwd)


class TextureFailureTests(unittest.TestCase):
    class FakeGame:
        def __init__(self):
            self._textures = {}
            self._failed_textures = set()
            self._fallback_texture = object()
            self.messages = []

        def print(self, *args):
            self.messages.append(args)

    def test_failed_texture_returns_placeholder_and_is_cached(self):
        game = self.FakeGame()
        with patch(
            "render.arcade.load_texture",
            side_effect=RuntimeError("broken texture"),
        ) as loader:
            first = render.Game._load_texture(game, "bad.png")
            second = render.Game._load_texture(game, "bad.png")

        self.assertIs(first, game._fallback_texture)
        self.assertIs(second, game._fallback_texture)
        loader.assert_called_once()
        self.assertTrue(game.messages)


class KeyboardTests(unittest.TestCase):
    class Owner:
        def is_pressed(self, key):
            return False

    def test_was_pressed_does_not_use_dispatch_cursor(self):
        cache = {Keyboard.EVENTS_KEY: []}
        keyboard = Keyboard(self.Owner(), cache)
        cache[Keyboard.EVENTS_KEY] = [
            {"sequence": 1, "type": "key_down", "key": "SPACE"},
            {"sequence": 2, "type": "key_up", "key": "SPACE"},
        ]
        keyboard._poll_events()

        self.assertTrue(keyboard.was_pressed("space"))
        self.assertFalse(keyboard.was_pressed("space"))


class ValidationTests(unittest.TestCase):
    def test_valid_static_object(self):
        validate_object_data({
            "object_type": "static_object",
            "id": {"name": "wall", "class": "Wall"},
        })

    def test_type_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_object_data({
                "object_type": "static_object",
                "id": {"name": "wall", "class": "Wall", "type": "dynamic_object"},
            })

    def test_non_finite_position_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_object_data({
                "object_type": "background_object",
                "visual": {
                    "sprite": "img.png",
                    "position": [math.inf, 0],
                    "scale": 1,
                    "rotation": 0,
                    "alpha": 1,
                },
            })

    def test_too_long_identity_name_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_object_data({
                "object_type": "static_object",
                "id": {
                    "name": "x" * (MAX_CACHE_STRING_BYTES + 1),
                    "class": "Wall",
                },
            })

    def test_network_must_be_a_list(self):
        with self.assertRaises(ValueError):
            validate_object_data({
                "object_type": "manager",
                "id": {"name": "manager", "class": "Manager"},
                "scripting": {"scripts": [], "network": "tcp"},
            })


class MemoryCacheTests(unittest.TestCase):
    def test_object_cache_roundtrip_and_capacity(self):
        cache = ObjectCache(100 * 4, VISUAL_STRUCT)
        self.addCleanup(cache.close)
        self.assertEqual(cache.capacity, 4)

        cache.set_many(2, {"x": 1.5, "y": -2.0, "sprite": "img.png"})
        values = cache.get_many(2, ("x", "y", "sprite"))
        self.assertEqual(values, {"x": 1.5, "y": -2.0, "sprite": "img.png"})

        changes = cache.get_changed_values(2)
        self.assertEqual(changes["x"], 1.5)
        self.assertEqual(cache.get_changed_values(2), {})

    def test_string_limit(self):
        cache = ValueCache("ok")
        self.addCleanup(cache.close)
        cache.set("short")
        self.assertEqual(cache.get(), "short")
        with self.assertRaises(ValueError):
            cache.set("x" * (MAX_CACHE_STRING_BYTES + 1))


class SnapshotInterpolationTests(unittest.TestCase):
    class Probe:
        SIMULATION_TICK = render.Game.SIMULATION_TICK
        POSITION_HISTORY_SIZE = render.Game.POSITION_HISTORY_SIZE
        POSITION_INTERPOLATION_DELAY = render.Game.POSITION_INTERPOLATION_DELAY
        ACTIVE_SNAPSHOT_WINDOW = render.Game.ACTIVE_SNAPSHOT_WINDOW

        def __init__(self, clock_origin=None):
            self.clock_origin = (
                clock_origin if clock_origin is not None else time.monotonic()
            )
            self._position_history_by_render_id = {}
            self._latest_tick_by_render_id = {}
            self._last_tick_update_by_render_id = {}
            self._scripted_render_ids = set()

        _record_position = render.Game._record_position
        _position_at_tick = render.Game._position_at_tick
        _resolve_render_tick = render.Game._resolve_render_tick

    def test_common_tick_keeps_relative_offset_stable(self):
        probe = self.Probe()

        # The object and the camera target move at the same speed.  The
        # target's samples are recorded with a different arrival pattern, but
        # both carry the same shared tick numbers.
        for tick in range(0, 20):
            probe._record_position(10, (100.0 + tick * 2, 0.0), tick)
        for tick in (0, 3, 6, 9, 12, 15, 18):
            probe._record_position(20, (200.0 + tick * 2, 0.0), tick)

        for query_tick in (10.0, 10.25, 10.5, 10.75, 12.0, 14.5):
            object_position = probe._position_at_tick(10, query_tick)
            target_position = probe._position_at_tick(20, query_tick)
            self.assertIsNotNone(object_position)
            self.assertIsNotNone(target_position)
            self.assertAlmostEqual(
                object_position[0] - target_position[0], -100.0, places=6
            )

    def test_same_tick_replaces_sample_and_old_ticks_are_ignored(self):
        probe = self.Probe()
        probe._record_position(1, (0.0, 0.0), 5)
        probe._record_position(1, (10.0, 0.0), 5)
        probe._record_position(1, (99.0, 0.0), 4)

        self.assertEqual(
            list(probe._position_history_by_render_id[1]),
            [(5, 10.0, 0.0)],
        )

    def test_render_tick_waits_for_slowest_live_script(self):
        origin = time.monotonic() - 1.0
        probe = self.Probe(clock_origin=origin)
        now = time.monotonic()
        probe._scripted_render_ids = {1, 2}
        probe._latest_tick_by_render_id = {1: 100, 2: 90}
        probe._last_tick_update_by_render_id = {1: now, 2: now}

        render_tick = probe._resolve_render_tick()
        self.assertLessEqual(render_tick, 89.0)

        probe._last_tick_update_by_render_id[2] = now - 1.0
        render_tick = probe._resolve_render_tick()
        self.assertGreater(render_tick, 89.0)

    def test_idle_unscripted_visual_does_not_hold_render_tick(self):
        origin = time.monotonic() - 1.0
        probe = self.Probe(clock_origin=origin)
        now = time.monotonic()
        probe._scripted_render_ids = {1}
        probe._latest_tick_by_render_id = {1: 100, 2: 50}
        probe._last_tick_update_by_render_id = {1: now, 2: now}

        # Render id 2 is an idle camera target without its own script loop.
        # Its stale tick must not freeze the whole frame and cause a jump.
        render_tick = probe._resolve_render_tick()
        self.assertGreater(render_tick, 90.0)


class CreateObjectTests(CacheTestCase):
    def test_create_from_json_and_bind_caches(self):
        visual, identity = self.make_caches()
        deps, requests, _ = self.make_deps(visual, identity)

        game_object = create_object(
            '{"object_type": "background_object", "visual": '
            '{"sprite": "img.png", "position": [1, 2], "scale": 1, '
            '"rotation": 0, "alpha": 1}}',
            deps=deps,
        )

        self.assertEqual(game_object._object_id, 0)
        self.assertEqual(game_object._render_id, 0)
        self.assertEqual([m["command"] for m in requests.messages],
                         ["register", "render"])
        visual.set_many(0, {"x": 1.0, "y": 2.0})
        self.assertEqual(game_object.Visual.position.x, 1.0)
        game_object.Visual.position = Vector2(5, 6)
        self.assertEqual(visual.get(0, "x"), 5.0)
        self.assertEqual(visual.get(0, "y"), 6.0)

    def test_visual_position_writes_shared_tick(self):
        visual, identity = self.make_caches()
        deps, _, _ = self.make_deps(visual, identity)
        tick_cache = ValueCache(17)
        self.addCleanup(tick_cache.close)
        deps["tick_cache"] = tick_cache

        game_object = create_object({
            "object_type": "background_object",
            "visual": {
                "sprite": "img.png",
                "position": [1, 2],
                "scale": 1,
                "rotation": 0,
                "alpha": 1,
            },
        }, deps=deps)

        game_object.Visual.position = Vector2(5, 6)
        self.assertEqual(visual.get(0, "tick"), 17)
        self.assertEqual(visual.get(0, "x"), 5.0)

    def test_tick_bookkeeping_is_tracked_per_object(self):
        visual, identity = self.make_caches()
        deps, _, responses = self.make_deps(visual, identity)
        tick_cache = ValueCache(23)
        self.addCleanup(tick_cache.close)
        deps["tick_cache"] = tick_cache

        visual_data = {
            "sprite": "img.png",
            "position": [0, 0],
            "scale": 1,
            "rotation": 0,
            "alpha": 1,
        }
        first = create_object({
            "object_type": "background_object",
            "visual": dict(visual_data),
        }, deps=deps)
        second_requests = FakeRequestQueue(responses, render_id=1, object_id=1)
        second = create_object({
            "object_type": "background_object",
            "visual": dict(visual_data),
        }, deps=dict(deps, main_in_stream=second_requests))

        class Owner:
            GlobalTime = 0.23

        owner = Owner()
        owner.__dict__["_simulation_tick"] = 23
        first.Visual.__dict__["_simulation_time_owner"] = owner
        second.Visual.__dict__["_simulation_time_owner"] = owner

        # A script can move another object through find_object().  Such a
        # write must not be mistaken for a heartbeat of the script's own
        # object, so the bookkeeping is keyed by render_id.
        first.Visual.position = Vector2(1, 2)
        second.Visual.position = Vector2(3, 4)

        self.assertEqual(
            owner.__dict__["_visual_tick_written_by_render_id"],
            {0: 23, 1: 23},
        )
        self.assertEqual(visual.get(0, "tick"), 23)
        self.assertEqual(visual.get(1, "tick"), 23)

    def test_transform_is_rebound_after_registration(self):
        visual, identity = self.make_caches()
        deps, _, _ = self.make_deps(visual, identity)

        game_object = create_object({
            "object_type": "background_object",
            "visual": {
                "sprite": "img.png",
                "position": [10, 20],
                "scale": 1,
                "rotation": 0,
                "alpha": 1,
            },
            "transform": {"polygon": [[-1, -1], [1, -1], [1, 1], [-1, 1]]},
        }, deps=deps)

        visual.set_many(0, {"x": 10.0, "y": 20.0, "rotation": 0.0, "scale": 1.0})
        polygon = game_object.Transform._world_polygon()
        self.assertEqual(len(polygon), 4)

    def test_create_from_dict_and_alias(self):
        visual, identity = self.make_caches()
        deps, requests, _ = self.make_deps(visual, identity)

        game_object = creat_object({
            "object_type": "static_object",
            "id": {"name": "wall", "class": "Wall"},
        }, deps=deps)

        self.assertIs(creat_object, create_object)
        self.assertEqual(identity.get(0, "name"), "wall")
        self.assertEqual([m["command"] for m in requests.messages], ["register"])

    def test_create_list(self):
        visual, identity = self.make_caches()
        deps, requests, responses = self.make_deps(visual, identity)
        second_requests = FakeRequestQueue(responses, render_id=1, object_id=1)
        deps = dict(deps, main_in_stream=second_requests)

        objects = create_object([
            {"object_type": "static_object",
             "id": {"name": "one", "class": "Wall"}},
        ], deps=deps)
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]._object_id, 1)

    def test_list_is_validated_before_creation(self):
        visual, identity = self.make_caches()
        deps, requests, _ = self.make_deps(visual, identity)

        with self.assertRaises(ValueError):
            create_object([
                {"object_type": "static_object",
                 "id": {"name": "one", "class": "Wall"}},
                {"object_type": "no_such_type"},
            ], deps=deps)
        self.assertEqual(requests.messages, [])

    def test_registration_error_is_raised(self):
        visual, identity = self.make_caches()
        deps, _, _ = self.make_deps(visual, identity)
        deps["main_in_stream"] = type("RejectingQueue", (), {
            "put": lambda self, message: deps["main_out_stream"].put({
                message["request_id"]: {"error": "rejected"}
            })
        })()

        with self.assertRaises(ValueError):
            create_object({
                "object_type": "static_object",
                "id": {"name": "wall", "class": "Wall"},
            }, deps=deps)

    def test_duplicate_and_invalid_stream_registration(self):
        visual, identity = self.make_caches()
        responses = FakeResponseQueue()
        stream_object = Stream.__new__(Stream)
        stream_object.main_out_stream = responses
        stream_object.render_in_stream = FakeRequestQueue(responses)
        stream_object.object_lock = threading.Lock()
        stream_object.objects_cache = {}
        stream_object.visual_cache = visual
        stream_object.id_cache = identity
        stream_object.stream_time = ValueCache(0.0)
        self.addCleanup(stream_object.stream_time.close)
        stream_object.tick_cache = ValueCache(0)
        self.addCleanup(stream_object.tick_cache.close)
        stream_object.camera_target_cache = ValueCache(-1)
        self.addCleanup(stream_object.camera_target_cache.close)
        stream_object._next_object_id = 0
        stream_object._next_render_id = 0

        data = {
            "object_type": "background_object",
            "visual": {
                "sprite": "img.png",
                "position": [1, 2],
                "scale": 1,
                "rotation": 0,
                "alpha": 1,
            },
        }
        game_object = GameObject(data, deps={
            "visual_cache": visual,
            "id_cache": identity,
            "objects_cache": stream_object.objects_cache,
            "lock": stream_object.object_lock,
        })
        message = {
            "id": game_object._id,
            "request_id": "one",
            "obj": game_object._GameObject__cache_record(),
            "script_data": game_object.dict_data,
        }
        stream_object._Stream__register(message)
        self.assertIn("one", responses.items[-1])

        stream_object._Stream__register(message)
        self.assertIn("error", responses.items[-1]["one"])

        stream_object._Stream__register({
            "id": "bad",
            "request_id": "two",
            "obj": {"dict_data": {"object_type": "unknown"}},
            "script_data": {"object_type": "unknown"},
        })
        self.assertIn("error", responses.items[-1]["two"])


class StopLifecycleTests(unittest.TestCase):
    class DetachableCache:
        def __init__(self):
            self.detached = False

        def detach(self):
            self.detached = True

    class Probe(script):
        stopped = 0

        def stop(self):
            type(self).stopped += 1

    def test_shutdown_calls_stop_once_and_detaches(self):
        probe = self.Probe.__new__(self.Probe)
        cache = self.DetachableCache()
        probe.__dict__["_script__rawTime"] = cache
        probe.__dict__["_script__camera_target_cache"] = self.DetachableCache()
        probe.__dict__["_script__camera_scale_cache"] = self.DetachableCache()
        probe.__dict__["_script__visual_cache"] = self.DetachableCache()
        probe.__dict__["_script__id_cache"] = self.DetachableCache()

        getattr(probe, "_script__shutdown")()
        getattr(probe, "_script__shutdown")()

        self.assertEqual(self.Probe.stopped, 1)
        self.assertTrue(cache.detached)


if __name__ == "__main__":
    unittest.main()
