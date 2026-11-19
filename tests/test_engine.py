import contextlib
import io
import json
import math
import multiprocessing
import os
import queue
import tempfile
import threading
import time
import unittest
from collections import deque
from unittest.mock import patch

import console
import render
from collision import convex_polygons_overlap, polygon_bounds, transform_polygon
from memory_lib import FieldView, ObjectCache, ValueCache
from pyengine import (
    GameObject,
    IdCache,
    Keyboard,
    Mouse,
    Vector2,
    VisualCache,
    _Camera,
    create_object,
    creat_object,
    script,
)
from scene import validate_object_data
from stream import INDEX_BUCKET_PROMOTE_AT, Stream
from config import (
    ENGINE_STATE_LOOP_FIELDS,
    ENGINE_STATE_SEGMENT_BYTES,
    ENGINE_STATE_STRUCT,
    KEYBOARD_EVENT_DOWN,
    KEYBOARD_EVENT_FIELDS,
    MAX_CACHE_STRING_BYTES,
    PRESSED_KEY_FIELDS,
    PROJECT_DIR,
    pack_pressed_keys,
    resolve_project_path,
    unpack_input_state,
    unpack_pressed_keys,
)


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
    def __init__(self, responses, render_id=0, object_id=0, generation=1):
        self.responses = responses
        self.render_id = render_id
        self.object_id = object_id
        self.generation = generation
        self.messages = []

    def put(self, message):
        self.messages.append(message)
        if message["command"] == "register":
            self.responses.put({
                message["request_id"]: {
                    "object_id": self.object_id,
                    "render_id": self.render_id,
                    "generation": self.generation,
                }
            })
        elif message["command"] == "register_many":
            results = [
                {
                    "object_id": self.object_id + offset,
                    "render_id": self.render_id + offset,
                    "generation": self.generation + offset,
                }
                for offset, _item in enumerate(message["objects"])
            ]
            self.responses.put({
                message["request_id"]: {"objects": results},
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


class NoSpriteTests(unittest.TestCase):
    class FakeGame:
        _no_sprite_texture = object()

        def _resolve_asset_path(self, path):
            return "resolved:" + path

        def _load_texture(self, path):
            return ("loaded", path)

        _texture_for_path = render.Game._texture_for_path

    def test_none_sprite_uses_transparent_texture(self):
        game = self.FakeGame()
        self.assertIs(game._texture_for_path("None"), game._no_sprite_texture)
        self.assertIs(game._texture_for_path("  none  "), game._no_sprite_texture)

    def test_real_sprite_path_is_still_loaded(self):
        game = self.FakeGame()
        self.assertEqual(
            game._texture_for_path("img.png"),
            ("loaded", "resolved:img.png"),
        )


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


class InputStateTests(unittest.TestCase):
    def test_dict_form_keeps_every_shared_handle(self):
        state = unpack_input_state({
            "sequence": "seq",
            "motion": ["motion"],
            "generation": "gen",
            "keyboard_events": ["ring"],
            "keyboard_sequence": "key_seq",
        })
        self.assertEqual(state["sequence"], "seq")
        self.assertEqual(state["motion"], ["motion"])
        self.assertEqual(state["generation"], "gen")
        self.assertEqual(state["keyboard_events"], ["ring"])
        self.assertEqual(state["keyboard_sequence"], "key_seq")

    def test_legacy_tuple_and_string_forms_are_still_accepted(self):
        state = unpack_input_state(("seq", ["motion"], "gen"))
        self.assertEqual(state["sequence"], "seq")
        self.assertEqual(state["motion"], ["motion"])
        self.assertEqual(state["generation"], "gen")
        self.assertIsNone(state["keyboard_events"])
        self.assertIsNone(state["keyboard_sequence"])

        legacy = unpack_input_state("seq")
        self.assertEqual(legacy["sequence"], "seq")
        self.assertIsNone(legacy["motion"])
        self.assertIsNone(legacy["keyboard_events"])


class EngineStateTests(unittest.TestCase):
    def test_engine_counters_share_one_segment(self):
        cache = ObjectCache(
            ENGINE_STATE_SEGMENT_BYTES,
            ENGINE_STATE_STRUCT,
            [threading.Lock()],
        )
        self.addCleanup(cache.close)
        self.assertEqual(cache.capacity, 1)

        cache.set_many(0, {
            "time": 3.5,
            "tick": 350,
            "input_sequence": 4,
            "object_generation": 2,
            "keyboard_sequence": 7,
            "key_count": 1,
            "keys_0": "W",
        })
        state = cache.get_many(0, ENGINE_STATE_LOOP_FIELDS)
        self.assertEqual(state["time"], 3.5)
        self.assertEqual(state["tick"], 350)
        self.assertEqual(state["input_sequence"], 4)
        self.assertEqual(state["object_generation"], 2)
        self.assertEqual(state["keyboard_sequence"], 7)
        self.assertEqual(state["key_count"], 1)

    def test_lockless_read_matches_locked_read_without_the_lock(self):
        cache = ObjectCache(
            ENGINE_STATE_SEGMENT_BYTES,
            ENGINE_STATE_STRUCT,
            [threading.Lock()],
        )
        self.addCleanup(cache.close)
        cache.set_many(0, {
            "time": 3.5,
            "tick": 350,
            "input_sequence": 4,
            "object_generation": 2,
            "keyboard_sequence": 7,
            "key_count": 1,
        })

        # Удержание блокировки сегмента доказывает, что чтение без блокировки
        # её никогда не берёт; обычный threading.Lock не реентерабелен,
        # поэтому чтение с блокировкой зависло бы здесь навсегда.
        with cache.locks[0]:
            state = cache.get_many_lockless(0, ENGINE_STATE_LOOP_FIELDS)
        self.assertEqual(state["time"], 3.5)
        self.assertEqual(state["tick"], 350)
        self.assertEqual(state["input_sequence"], 4)
        self.assertEqual(state["object_generation"], 2)
        self.assertEqual(state["keyboard_sequence"], 7)
        self.assertEqual(state["key_count"], 1)

    def test_lockless_read_rejects_string_fields(self):
        cache = ObjectCache(
            ENGINE_STATE_SEGMENT_BYTES,
            ENGINE_STATE_STRUCT,
            [threading.Lock()],
        )
        self.addCleanup(cache.close)
        with self.assertRaises(TypeError):
            cache.get_many_lockless(0, ("keys_0",))

    def test_pressed_keys_round_trip_across_chunks(self):
        keys = {
            "W", "A", "S", "D", "LEFTSHIFT", "SPACE", "LEFTCTRL",
            "Q", "E", "R", "F", "TAB", "ESCAPE", "ENTER",
        }
        chunks = pack_pressed_keys(keys)
        self.assertLessEqual(len(chunks), len(PRESSED_KEY_FIELDS))
        self.assertTrue(all(
            len(chunk.encode("utf-8")) <= MAX_CACHE_STRING_BYTES
            for chunk in chunks
        ))
        self.assertEqual(unpack_pressed_keys(chunks), keys)
        self.assertEqual(unpack_pressed_keys([]), frozenset())


class MouseMotionTests(unittest.TestCase):
    def test_motion_events_are_coalesced_per_frame(self):
        game = render.Game.__new__(render.Game)
        game._pending_mouse_motion = None
        calls = []
        game._send_mouse_event = lambda *args, **kwargs: calls.append((args, kwargs))

        for index in range(1000):
            game.on_mouse_motion(index, index * 2, 1, 2)

        self.assertEqual(calls, [])
        game._flush_mouse_motion()
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args, ("mouse_move", 999, 1998))
        self.assertEqual(kwargs, {"dx": 1000, "dy": 2000})
        self.assertIsNone(game._pending_mouse_motion)

    def test_click_flushes_pending_motion_first(self):
        game = render.Game.__new__(render.Game)
        game._pending_mouse_motion = None
        calls = []
        game._send_mouse_event = lambda *args, **kwargs: calls.append((args, kwargs))

        game.on_mouse_motion(10, 20, 1, 2)
        game.on_mouse_press(10, 20, 1, 0)

        self.assertEqual(
            [call[0][0] for call in calls],
            ["mouse_move", "mouse_down"],
        )


class FakeMotionCache:
    def __init__(self):
        self.values = {}
        self.writes = []

    def get(self, segment_id, field):
        return self.values.get(field, 0)

    def get_many(self, segment_id, fields):
        return {name: self.values.get(name, 0) for name in fields}

    def set_many(self, segment_id, values):
        self.writes.append(dict(values))
        self.values.update(values)


class FakeInputSequence:
    def __init__(self):
        self.value = 0

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeEngineState:
    """Заменитель общего сегмента состояния движка в памяти."""

    def __init__(self, **values):
        self.values = {
            "time": 0.0,
            "tick": 0,
            "input_sequence": 0,
            "object_generation": 0,
            "keyboard_sequence": 0,
            "key_count": 0,
        }
        self.values.update(values)

    def _default(self, field):
        return "" if field.startswith("keys_") else 0

    def _field(self, field):
        # FieldView просит кэш проверить имя поля.
        if field not in self.values:
            raise TypeError(f"Запрошен незаданый обьект: {field!r}")
        return (0, 0)

    def get(self, segment_id, field):
        return self.values.get(field, self._default(field))

    def set(self, segment_id, field, value):
        self.values[field] = value

    def get_many(self, segment_id, fields):
        return {
            name: self.values.get(name, self._default(name))
            for name in fields
        }

    def set_many(self, segment_id, values):
        self.values.update(values)


class FakeKeyboardRing:
    def __init__(self, capacity=8):
        self.capacity = capacity
        self.segments = {}

    def set_many(self, segment_id, values):
        self.segments[segment_id] = dict(values)

    def get_many(self, segment_id, fields):
        values = self.segments[segment_id]
        return {
            name: values.get(name, "" if name == "key" else 0)
            for name in fields
        }


class MouseEventCoalescingTests(unittest.TestCase):
    def make_stream(self, motion=True):
        stream = Stream.__new__(Stream)
        stream.objects_cache = {}
        stream.engine_state = FakeEngineState()
        stream._pressed_keys = set()
        stream._input_sequence = 0
        stream._object_generation = 0
        stream.motion_cache = FakeMotionCache() if motion else None
        stream._motion_sequence = 0
        stream.keyboard_ring = None
        stream._keyboard_sequence = 0
        return stream

    def mouse_event(self, event_type, x, button=None, render_id=None):
        return {
            "type": event_type,
            "render_id": render_id,
            "x": x,
            "y": 0.0,
            "screen_x": x,
            "screen_y": 0.0,
            "button": button,
            "delta_x": 1.0,
            "delta_y": 0.0,
            "scroll_x": 0.0,
            "scroll_y": 0.0,
        }

    def test_motion_uses_shared_memory_without_waking_scripts(self):
        stream = self.make_stream()
        register = stream._Stream__register_input_button

        register(self.mouse_event("mouse_move", 10))
        register(self.mouse_event("mouse_move", 20))

        self.assertNotIn(Mouse.EVENTS_KEY, stream.objects_cache)
        self.assertEqual(stream.motion_cache.values["seq"], 2)
        self.assertEqual(stream.motion_cache.values["x"], 20.0)
        self.assertEqual(stream.motion_cache.values["dx"], 1.0)
        self.assertEqual(stream.engine_state.get(0, "input_sequence"), 0)

    def test_clicks_are_not_dropped_and_still_wake_scripts(self):
        stream = self.make_stream()
        register = stream._Stream__register_input_button

        register(self.mouse_event("mouse_move", 10))
        register(self.mouse_event("mouse_down", 10, button="left", render_id=7))
        self.assertEqual(stream.motion_cache.values["hovered"], 7)
        register(self.mouse_event("mouse_move", 20))

        events = stream.objects_cache[Mouse.EVENTS_KEY]
        self.assertEqual([event["type"] for event in events], ["mouse_down"])
        self.assertEqual(stream.motion_cache.values["seq"], 2)
        self.assertEqual(stream.motion_cache.values["hovered"], -1)
        self.assertEqual(stream.engine_state.get(0, "input_sequence"), 1)

    def test_direct_pointer_publication_is_not_repeated(self):
        stream = self.make_stream()
        event = self.mouse_event("mouse_move", 10, render_id=7)
        event["pointer_published"] = True

        stream._Stream__register_input_button(event)

        self.assertEqual(stream.motion_cache.writes, [])
        self.assertNotIn(Mouse.EVENTS_KEY, stream.objects_cache)
        self.assertEqual(stream.engine_state.get(0, "input_sequence"), 0)

    def test_keyboard_events_still_wake_scripts(self):
        stream = self.make_stream()
        register = stream._Stream__register_input_button

        register({"type": "key_down", "key": "W", "modifiers": 0})
        register({"type": "key_up", "key": "W", "modifiers": 0})

        events = stream.objects_cache[Keyboard.EVENTS_KEY]
        self.assertEqual([event["type"] for event in events], ["key_down", "key_up"])
        self.assertEqual(stream.engine_state.get(0, "input_sequence"), 2)
        self.assertEqual(stream.engine_state.get(0, "key_count"), 0)

    def test_keyboard_events_use_shared_ring_when_attached(self):
        stream = self.make_stream()
        stream.keyboard_ring = FakeKeyboardRing(capacity=8)
        register = stream._Stream__register_input_button

        register({"type": "key_down", "key": "W", "modifiers": 5})
        self.assertEqual(stream.engine_state.get(0, "key_count"), 1)
        self.assertEqual(
            stream.engine_state.get(0, "keys_0"), "W"
        )
        self.assertIn("W", stream._pressed_keys)
        register({"type": "key_up", "key": "W", "modifiers": 0})

        self.assertNotIn(Keyboard.EVENTS_KEY, stream.objects_cache)
        self.assertEqual(stream.engine_state.get(0, "keyboard_sequence"), 2)
        self.assertEqual(stream.engine_state.get(0, "key_count"), 0)
        self.assertNotIn("W", stream._pressed_keys)
        first = stream.keyboard_ring.get_many(1, KEYBOARD_EVENT_FIELDS)
        second = stream.keyboard_ring.get_many(2, KEYBOARD_EVENT_FIELDS)
        self.assertEqual(first["sequence"], 1)
        self.assertEqual(first["type"], KEYBOARD_EVENT_DOWN)
        self.assertEqual(first["key"], "W")
        self.assertEqual(first["modifiers"], 5)
        self.assertEqual(second["type"], 2)
        self.assertEqual(stream.engine_state.get(0, "input_sequence"), 2)

    def test_legacy_path_keeps_only_latest_mouse_move(self):
        stream = self.make_stream(motion=False)
        register = stream._Stream__register_input_button

        register(self.mouse_event("mouse_move", 1))
        first = stream.objects_cache[Mouse.EVENTS_KEY]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["x"], 1.0)

        register(self.mouse_event("mouse_move", 2))
        second = stream.objects_cache[Mouse.EVENTS_KEY]
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["x"], 2.0)
        self.assertGreater(second[0]["sequence"], first[0]["sequence"])
        self.assertEqual(stream.engine_state.get(0, "input_sequence"), 2)


class MouseSharedStateTests(unittest.TestCase):
    def make_mouse(self, **values):
        cache = FakeMotionCache()
        cache.values.update({
            "seq": 0,
            "x": 0.0,
            "y": 0.0,
            "dx": 0.0,
            "dy": 0.0,
            "screen_x": 0.0,
            "screen_y": 0.0,
            "hovered": -1,
        })
        cache.values.update(values)
        return Mouse(None, {}, cache), cache

    def test_poll_motion_returns_each_snapshot_once(self):
        mouse, cache = self.make_mouse()
        cache.values.update({"seq": 1, "x": 5.0, "y": 6.0, "hovered": 3})

        event = mouse._poll_motion()
        self.assertEqual(event["type"], "mouse_move")
        self.assertEqual((event["x"], event["y"]), (5.0, 6.0))
        self.assertEqual(event["render_id"], 3)
        self.assertIsNone(mouse._poll_motion())

        cache.values.update({"seq": 2, "x": 7.0, "hovered": -1})
        event = mouse._poll_motion()
        self.assertEqual(event["x"], 7.0)
        self.assertIsNone(event["render_id"])

    def test_position_and_hover_read_shared_memory(self):
        mouse, cache = self.make_mouse(
            seq=3, x=12.0, y=-4.0, screen_x=100.0, screen_y=50.0, hovered=9,
        )

        self.assertEqual((mouse.position.x, mouse.position.y), (12.0, -4.0))
        self.assertEqual(
            (mouse.screen_position.x, mouse.screen_position.y),
            (100.0, 50.0),
        )
        self.assertTrue(mouse.is_over(type("Target", (), {"_render_id": 9})()))

        cache.values.update({"x": -1.0, "hovered": 4})
        self.assertEqual(mouse.position.x, -1.0)
        self.assertFalse(mouse.is_over(type("Target", (), {"_render_id": 9})()))


class CameraZoomTests(unittest.TestCase):
    class Cache:
        def __init__(self, value=0.0):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    def make_camera(self, scale=1.0):
        owner = type("Owner", (), {})()
        owner.GlobalTime = 0.0
        owner.__dict__["_script__id_cache"] = None
        owner.__dict__["_script__objects_cache"] = {}
        owner.__dict__["_script__visual_cache"] = None
        scale_cache = self.Cache(scale)
        return _Camera(owner, self.Cache(-1), scale_cache), scale_cache

    def test_scale_camera_updates_shared_state_and_clamps_low_values(self):
        camera, cache = self.make_camera()
        camera.scale_camera(2.5)
        self.assertEqual(cache.value, 2.5)
        self.assertEqual(camera.scale, 2.5)

        camera.scale_camera(0.0)
        self.assertEqual(cache.value, 0.01)

    def test_scale_camera_rejects_non_finite_values(self):
        camera, cache = self.make_camera()
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                camera.scale_camera(value)
        self.assertEqual(cache.value, 1.0)

    def test_renderer_applies_shared_scale_to_camera_zoom(self):
        game = render.Game.__new__(render.Game)
        game.camera_target_cache = self.Cache(7)
        game.camera_scale_cache = self.Cache(2.5)
        game.camera = type("Camera", (), {"zoom": 1.0})()
        game._camera_target_id = -1

        game._read_camera_state()
        self.assertEqual(game._camera_target_id, 7)
        self.assertEqual(game.camera.zoom, 2.5)

        game.camera_scale_cache.value = 0.001
        game._read_camera_state()
        self.assertEqual(game.camera.zoom, 0.01)

    def test_zoom_scales_projection_and_unproject_roundtrips(self):
        import arcade
        from types import SimpleNamespace

        fake_window = SimpleNamespace(
            ctx=SimpleNamespace(
                screen=SimpleNamespace(viewport=(0, 0, 800, 600))
            )
        )
        camera = arcade.Camera2D(window=fake_window)
        camera.position = (400, 300)
        self.assertAlmostEqual(camera.project((500, 300)).x, 500.0)

        camera.zoom = 2.0
        zoomed = camera.project((500, 300))
        unprojected = camera.unproject(zoomed)
        self.assertAlmostEqual(zoomed.x, 600.0)
        self.assertAlmostEqual(unprojected.x, 500.0)
        self.assertAlmostEqual(unprojected.y, 300.0)

        camera.zoom = 0.5
        self.assertAlmostEqual(camera.project((500, 300)).x, 450.0)


class KeyboardRingTests(unittest.TestCase):
    class Owner:
        def is_pressed(self, key):
            return False

    def make_stream(self):
        stream = Stream.__new__(Stream)
        stream.objects_cache = {}
        stream.engine_state = FakeEngineState()
        stream._pressed_keys = set()
        stream._input_sequence = 0
        stream._object_generation = 0
        stream.keyboard_ring = FakeKeyboardRing(capacity=8)
        stream._keyboard_sequence = 0
        return stream

    def test_poll_and_was_pressed_read_shared_ring(self):
        stream = self.make_stream()
        register = stream._Stream__register_input_button
        keyboard = Keyboard(
            self.Owner(),
            {},
            stream.keyboard_ring,
            FieldView(stream.engine_state, "keyboard_sequence"),
        )

        register({"type": "key_down", "key": "W", "modifiers": 0})
        events = keyboard._poll_events()
        self.assertEqual([event["type"] for event in events], ["key_down"])
        self.assertFalse(keyboard._poll_events())
        self.assertTrue(keyboard.was_pressed("w"))
        self.assertFalse(keyboard.was_pressed("w"))

        register({"type": "key_up", "key": "W", "modifiers": 0})
        events = keyboard._poll_events()
        self.assertEqual([event["type"] for event in events], ["key_up"])
        self.assertTrue(keyboard.was_released("w"))


class ConsoleClearTests(unittest.TestCase):
    class VisualCache:
        def __init__(self, layers):
            self.layers = layers

        def get(self, render_id, field):
            if field != "layer":
                raise KeyError(field)
            return self.layers[render_id]

    class CommandSink:
        def __init__(self):
            self.commands = []

        def put(self, value):
            self.commands.append(value)

    def setUp(self):
        self.cache = {
            "runner": {
                "_render_id": 1,
                "dict_data": {
                    "object_type": "dynamic_object",
                    "_components": ["id", "visual", "scripting"],
                    "_identity": {
                        "name": "runner",
                        "class": "test",
                        "type": "dynamic_object",
                    },
                },
            },
            "wall": {
                "_render_id": 2,
                "dict_data": {
                    "object_type": "static_object",
                    "_components": ["id", "visual"],
                    "_identity": {"name": "wall", "class": "Wall"},
                },
            },
            "camera_manager": {
                "_render_id": 3,
                "dict_data": {
                    "object_type": "manager",
                    "_components": ["id", "scripting"],
                    "_identity": {
                        "name": "camera_manager",
                        "class": "CameraManager",
                        "type": "manager",
                    },
                },
            },
            ("__engine_index__", "class", "test"): ["runner"],
        }
        self.vcache = self.VisualCache({1: 1.0, 2: 2.0})
        self.sink = self.CommandSink()

    def run_clear(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            console._clear_command(
                list(args), self.cache, self.vcache, self.sink
            )
        return self.sink.commands

    def test_clear_by_class(self):
        self.assertEqual(
            self.run_clear("class", "test"),
            [{"command": "remove", "id": "runner"}],
        )

    def test_clear_by_name(self):
        self.assertEqual(
            self.run_clear("name", "wall"),
            [{"command": "remove", "id": "wall"}],
        )

    def test_clear_by_layer(self):
        self.assertEqual(
            self.run_clear("layer", "2"),
            [{"command": "remove", "id": "wall"}],
        )

    def test_clear_by_type_uses_object_type_fallback(self):
        self.assertEqual(
            self.run_clear("type", "static_object"),
            [{"command": "remove", "id": "wall"}],
        )

    def test_short_selector_and_russian_alias(self):
        self.assertEqual(
            self.run_clear("c", "test"),
            [{"command": "remove", "id": "runner"}],
        )
        self.sink.commands.clear()
        self.assertEqual(
            self.run_clear("класс", "Wall"),
            [{"command": "remove", "id": "wall"}],
        )

    def test_unknown_selector_and_bad_layer_do_nothing(self):
        self.assertEqual(self.run_clear("unknown", "x"), [])
        self.assertEqual(self.run_clear("layer", "not-a-number"), [])
        self.assertEqual(self.run_clear("class"), [])


class ValidationTests(unittest.TestCase):
    def test_valid_static_object(self):
        validate_object_data({
            "object_type": "static_object",
            "id": {"name": "wall", "class": "Wall"},
        })

    def test_engine_owned_fields_are_rejected(self):
        for field_name in (
            "_id", "object_id", "render_id", "_object_id", "_render_id",
            "_generation",
        ):
            with self.subTest(field=field_name):
                with self.assertRaises(ValueError):
                    validate_object_data({
                        "object_type": "static_object",
                        "id": {"name": "wall", "class": "Wall"},
                        field_name: 1,
                    })

    def test_no_sprite_marker_is_valid(self):
        validate_object_data({
            "object_type": "background_object",
            "visual": {
                "sprite": "None",
                "position": [0, 0],
                "scale": 1,
                "rotation": 0,
                "alpha": 1,
            },
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

    def test_top_level_component_is_valid(self):
        validate_object_data({
            "object_type": "static_object",
            "id": {"name": "tilt_trigger", "class": "TiltTrigger"},
            "rotate_on_click": {"degrees": 90},
        })

    def test_top_level_component_properties_must_be_object(self):
        with self.assertRaises(ValueError):
            validate_object_data({
                "object_type": "static_object",
                "id": {"name": "tilt_trigger", "class": "TiltTrigger"},
                "rotate_on_click": [90],
            })

    def test_unknown_component_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_object_data({
                "object_type": "static_object",
                "id": {"name": "wall", "class": "Wall"},
                "definitely_missing_component": {},
            })

    def test_component_properties_wrapper_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_object_data({
                "object_type": "static_object",
                "id": {"name": "tilt_trigger", "class": "TiltTrigger"},
                "rotate_on_click": {"properties": {"degrees": 90}},
            })

    def test_component_path_wrapper_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_object_data({
                "object_type": "static_object",
                "id": {"name": "tilt_trigger", "class": "TiltTrigger"},
                "rotate_on_click": {
                    "path": "access/components/rotate_on_click.py",
                    "degrees": 90,
                },
            })

    def test_reserved_component_property_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_object_data({
                "object_type": "static_object",
                "id": {"name": "tilt_trigger", "class": "TiltTrigger"},
                "rotate_on_click": {"update": 90},
            })

    def test_legacy_components_list_is_still_valid(self):
        validate_object_data({
            "object_type": "static_object",
            "id": {"name": "tilt_trigger", "class": "TiltTrigger"},
            "components": [{
                "path": "access/components/rotate_on_click.py",
                "properties": {"degrees": 90},
            }],
        })

    def test_stress_motion_scene_is_valid(self):
        scene_path = (
            PROJECT_DIR / "access" / "scenes" / "stress_motion.json"
        )
        with scene_path.open(encoding="utf-8") as scene_file:
            scene = json.load(scene_file)

        self.assertEqual(
            [item["object_type"] for item in scene],
            ["camera", "manager"],
        )
        for item in scene:
            validate_object_data(item)

        config = scene[1]["stress_scene"]
        self.assertGreaterEqual(config["count"], 100)
        self.assertGreater(config["columns"], 0)
        self.assertFalse(config["per_object_scripts"])


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

    def test_object_cache_position_fast_path_roundtrip(self):
        cache = ObjectCache(100 * 4, VISUAL_STRUCT)
        self.addCleanup(cache.close)
        self.assertIsNotNone(cache._position_struct)

        cache.get_changed_values_many(range(4))
        cache.set_position(1, 12.5, -3.25, 7)
        self.assertEqual(cache.get_position(1), (12.5, -3.25, 7))
        changes = cache.get_changed_values(1)
        self.assertEqual(set(changes), {"x", "y", "tick"})
        self.assertEqual(cache.get_changed_values(1), {})

        # Запись того же упакованного участка не должна снова помечать сегмент изменённым.
        cache.set_position(1, 12.5, -3.25, 7)
        self.assertEqual(cache.get_changed_values(1), {})

        self.assertEqual(cache.move_position(1, 0.5, 2.0), (13.0, -1.25))
        self.assertEqual(cache.get_position(1), (13.0, -1.25, 7))
        self.assertEqual(set(cache.get_changed_values(1)), {"x", "y", "tick"})

        # Отсутствующий tick сохраняет текущий под той же блокировкой.
        self.assertEqual(cache.move_position(1, -1.0, 1.0), (12.0, -0.25))
        self.assertEqual(cache.get_position(1), (12.0, -0.25, 7))

    def test_object_cache_position_falls_back_for_noncontiguous_layout(self):
        struct_def = {
            "x": float,
            "scale": float,
            "y": float,
            "tick": int,
            "sprite": str,
        }
        cache = ObjectCache(100 * 2, struct_def)
        self.addCleanup(cache.close)
        self.assertIsNone(cache._position_struct)

        cache.set_position(0, 1.5, 2.5, 3)
        self.assertEqual(cache.get_position(0), (1.5, 2.5, 3))
        self.assertEqual(cache.move_position(0, 1.0, -0.5), (2.5, 2.0))
        self.assertEqual(cache.get_position(0), (2.5, 2.0, 3))


        # 130 сегментов делят 64 блокировки полос, поэтому 0 и 64 должны
        # читаться в рамках одного захвата блокировки.
        cache = ObjectCache(100 * 130, VISUAL_STRUCT)
        self.addCleanup(cache.close)
        cache.set_many(0, {"x": 1.0, "y": 2.0, "sprite": "a.png"})
        cache.set_many(64, {"x": 3.0, "rotation": 45.0})
        cache.set_many(2, {"y": -4.0})

        changes = cache.get_changed_values_many((0, 1, 2, 64))
        self.assertEqual(set(changes), {0, 2, 64})
        self.assertEqual(changes[0]["x"], 1.0)
        self.assertEqual(changes[0]["y"], 2.0)
        self.assertEqual(changes[0]["sprite"], "a.png")
        self.assertEqual(changes[2]["y"], -4.0)
        self.assertEqual(changes[64]["x"], 3.0)
        self.assertEqual(changes[64]["rotation"], 45.0)

        self.assertEqual(cache.get_changed_values_many((0, 1, 2, 64)), {})
        with self.assertRaises(IndexError):
            cache.get_changed_values_many((0, 130))

    def test_dirty_counters_follow_stripe_transitions(self):
        locks = [multiprocessing.Lock() for _ in range(8)]
        counts = multiprocessing.Array("i", len(locks), lock=False)
        cache = ObjectCache(100 * 16, VISUAL_STRUCT, locks, counts)
        self.addCleanup(cache.close)
        self.assertIs(cache.dirty_counts, counts)

        # Сегменты 0 и 8 делят полосу 0.
        cache.set(0, "x", 1.0)
        cache.set(8, "y", 2.0)
        self.assertEqual(counts[0], 2)
        self.assertEqual(counts[1], 0)

        # Вторая запись в уже изменённый сегмент не должна учитываться дважды.
        cache.set_many(0, {"x": 2.0, "rotation": 5.0})
        self.assertEqual(counts[0], 2)

        changes = cache.get_changed_values_many((0, 8))
        self.assertEqual(set(changes), {0, 8})
        self.assertEqual(counts[0], 0)

        # Запись неизменённых значений должна оставлять полосу чистой.
        cache.set_many(0, {"x": 2.0, "rotation": 5.0, "y": 0.0})
        self.assertEqual(counts[0], 0)
        self.assertEqual(cache.get_changed_values_many((0, 8)), {})

    def test_changed_values_many_skips_clean_stripes(self):
        locks = [multiprocessing.Lock() for _ in range(4)]
        counts = multiprocessing.Array("i", len(locks), lock=False)
        cache = ObjectCache(100 * 8, VISUAL_STRUCT, locks, counts)
        self.addCleanup(cache.close)
        cache.set_many(0, {"x": 1.0})

        scanned = []
        original = cache._changed_values_locked

        def spy(segment_id, current_mask=None, lock_index=None):
            scanned.append(segment_id)
            return original(segment_id, current_mask, lock_index)

        cache._changed_values_locked = spy

        # 0 и 4 соответствуют полосе 0, 1 и 5 — полосе 1.  Сканироваться
        # должна только изменённая полоса.
        changes = cache.get_changed_values_many((0, 1, 4, 5))
        self.assertEqual(set(changes), {0})
        self.assertNotIn(1, scanned)
        self.assertNotIn(5, scanned)

    def test_get_changed_values_by_stripe_skips_clean_groups(self):
        locks = [multiprocessing.Lock() for _ in range(4)]
        counts = multiprocessing.Array("i", len(locks), lock=False)
        cache = ObjectCache(100 * 8, VISUAL_STRUCT, locks, counts)
        self.addCleanup(cache.close)
        groups = [set(), set(), set(), set()]
        groups[0].update((0, 4))
        groups[1].update((1, 5))
        groups[2].add(2)
        groups[3].add(3)

        cache.set_many(0, {"x": 1.0})
        cache.set_many(5, {"y": 2.0})

        changes = cache.get_changed_values_by_stripe(groups)
        self.assertEqual(set(changes), {0, 5})
        self.assertEqual(changes[0]["x"], 1.0)
        self.assertEqual(changes[5]["y"], 2.0)
        self.assertEqual(counts[0], 0)
        self.assertEqual(counts[1], 0)
        self.assertEqual(cache.get_changed_values_by_stripe(groups), {})

        with self.assertRaises(ValueError):
            cache.get_changed_values_by_stripe([set()])

    def test_dirty_queue_visits_only_newly_dirty_segments(self):
        locks = [multiprocessing.Lock() for _ in range(4)]
        counts = multiprocessing.Array("i", len(locks), lock=False)
        ring_size = 8
        queue_data = multiprocessing.Array(
            "i", len(locks) * (3 + ring_size), lock=False
        )
        cache = ObjectCache(
            100 * 8,
            VISUAL_STRUCT,
            locks,
            counts,
            dirty_queue=queue_data,
            dirty_ring_size=ring_size,
        )
        self.addCleanup(cache.close)
        groups = [set() for _ in range(len(locks))]
        # Держим полосу разреженной относительно её активного набора, чтобы
        # использовался путь через кольцо, а не плотное последовательное сканирование.
        groups[1].update((1, 5, 9, 13, 17))
        stride = 3 + ring_size

        cache.set(1, "x", 1.0)
        cache.set(1, "y", 2.0)
        # Вторая запись — не новый переход в изменённое состояние, поэтому
        # для сегмента 1 должна существовать только одна запись кольца.
        self.assertEqual(queue_data[stride], 0)
        self.assertEqual(queue_data[stride + 1], 1)
        changes = cache.get_changed_values_by_stripe(groups)
        self.assertEqual(set(changes), {1})
        self.assertEqual(changes[1]["x"], 1.0)
        self.assertEqual(changes[1]["y"], 2.0)
        self.assertEqual(queue_data[stride:stride + 3], [0, 0, 0])

        cache.set(5, "rotation", 45.0)
        self.assertEqual(queue_data[stride + 1], 1)
        changes = cache.get_changed_values_by_stripe(groups)
        self.assertEqual(set(changes), {5})
        self.assertEqual(changes[5]["rotation"], 45.0)
        self.assertEqual(cache.get_changed_values_by_stripe(groups), {})

    def test_dirty_queue_overflow_scans_whole_stripe(self):
        locks = [multiprocessing.Lock() for _ in range(2)]
        counts = multiprocessing.Array("i", len(locks), lock=False)
        ring_size = 1
        queue_data = multiprocessing.Array(
            "i", len(locks) * (3 + ring_size), lock=False
        )
        cache = ObjectCache(
            100 * 8,
            VISUAL_STRUCT,
            locks,
            counts,
            dirty_queue=queue_data,
            dirty_ring_size=ring_size,
        )
        self.addCleanup(cache.close)
        groups = [set(), set()]

        # 0 и 2 делят полосу 0.  Кольцо вмещает один id, поэтому второй
        # переход ставит флаг переполнения вместо перезаписи.
        cache.set(0, "x", 1.0)
        cache.set(2, "x", 2.0)
        self.assertEqual(queue_data[2], 1)

        changes = cache.get_changed_values_by_stripe(groups)
        self.assertEqual(set(changes), {0, 2})
        self.assertEqual(changes[0]["x"], 1.0)
        self.assertEqual(changes[2]["x"], 2.0)
        self.assertEqual(queue_data[0:3], [0, 0, 0])
        self.assertEqual(counts[0], 0)
        self.assertEqual(cache.get_changed_values_by_stripe(groups), {})

    def test_dense_stripe_scans_inactive_segments_and_resets_ring(self):
        locks = [multiprocessing.Lock() for _ in range(4)]
        counts = multiprocessing.Array("i", len(locks), lock=False)
        ring_size = 8
        queue_data = multiprocessing.Array(
            "i", len(locks) * (3 + ring_size), lock=False
        )
        cache = ObjectCache(
            100 * 8,
            VISUAL_STRUCT,
            locks,
            counts,
            dirty_queue=queue_data,
            dirty_ring_size=ring_size,
        )
        self.addCleanup(cache.close)
        groups = [set(), set(), set(), set()]
        groups[0].add(0)

        # Сегмент 4 находится в той же полосе, но вне активного набора вызывающего кода.
        # Плотная полоса сканируется последовательно, и это тоже должно её очищать.
        cache.set(0, "x", 1.0)
        cache.set(4, "y", 2.0)
        self.assertEqual(counts[0], 2)

        changes = cache.get_changed_values_by_stripe(groups)
        self.assertEqual(set(changes), {0, 4})
        self.assertEqual(changes[0]["x"], 1.0)
        self.assertEqual(changes[4]["y"], 2.0)
        self.assertEqual(counts[0], 0)

        base = 0
        self.assertEqual(queue_data[base:base + 3], [0, 0, 0])
        self.assertEqual(cache.get_changed_values_by_stripe(groups), {})

    def test_attached_cache_shares_dirty_queue(self):
        locks = [multiprocessing.Lock() for _ in range(4)]
        counts = multiprocessing.Array("i", len(locks), lock=False)
        ring_size = 4
        queue_data = multiprocessing.Array(
            "i", len(locks) * (3 + ring_size), lock=False
        )
        owner = ObjectCache(
            100 * 8,
            VISUAL_STRUCT,
            locks,
            counts,
            dirty_queue=queue_data,
            dirty_ring_size=ring_size,
        )
        self.addCleanup(owner.close)
        attached = ObjectCache(
            owner.key,
            VISUAL_STRUCT,
            locks,
            counts,
            owner.capacity,
            queue_data,
            ring_size,
        )
        self.addCleanup(attached.detach)
        groups = [set() for _ in range(len(locks))]
        groups[0].update((0, 4, 8))

        attached.set(0, "x", 3.0)
        self.assertEqual(
            owner.get_changed_values_by_stripe(groups),
            {0: {"x": 3.0}},
        )
        self.assertEqual(counts[0], 0)

    def test_dirty_queue_requires_matching_ring_size(self):
        locks = [multiprocessing.Lock() for _ in range(4)]
        counts = multiprocessing.Array("i", len(locks), lock=False)
        with self.assertRaises(ValueError):
            ObjectCache(
                100 * 8,
                VISUAL_STRUCT,
                locks,
                counts,
                dirty_queue=multiprocessing.Array("i", 4, lock=False),
                dirty_ring_size=8,
            )
        with self.assertRaises(ValueError):
            ObjectCache(
                100 * 8, VISUAL_STRUCT, locks, counts, dirty_ring_size=8
            )

    def test_clear_changes_clears_mask_and_stripe_counter(self):
        locks = [multiprocessing.Lock() for _ in range(4)]
        counts = multiprocessing.Array("i", len(locks), lock=False)
        cache = ObjectCache(100 * 8, VISUAL_STRUCT, locks, counts)
        self.addCleanup(cache.close)

        cache.set_many(2, {"x": 5.0})
        self.assertEqual(counts[2], 1)
        self.assertTrue(cache.clear_changes(2))
        self.assertEqual(counts[2], 0)
        self.assertFalse(cache.clear_changes(2))
        self.assertEqual(cache.get_changed_values_many((2,)), {})

    def test_attached_cache_shares_dirty_counters(self):
        locks = [multiprocessing.Lock() for _ in range(4)]
        counts = multiprocessing.Array("i", len(locks), lock=False)
        owner = ObjectCache(100 * 8, VISUAL_STRUCT, locks, counts)
        self.addCleanup(owner.close)
        attached = ObjectCache(
            owner.key, VISUAL_STRUCT, locks, counts, owner.capacity
        )
        self.addCleanup(attached.detach)

        attached.set_many(0, {"x": 3.0})
        self.assertEqual(counts[0], 1)
        self.assertEqual(
            owner.get_changed_values_many((0,)),
            {0: {"x": 3.0}},
        )
        self.assertEqual(counts[0], 0)

    def test_cache_without_counters_falls_back_to_full_scan(self):
        cache = ObjectCache(100 * 4, VISUAL_STRUCT)
        self.addCleanup(cache.close)
        self.assertIsNone(cache.dirty_counts)
        self.assertTrue(cache.has_pending_changes())

        cache.set_many(0, {"x": 1.0})
        self.assertEqual(
            cache.get_changed_values_many((0,)),
            {0: {"x": 1.0}},
        )

    def test_has_pending_changes_reads_counters(self):
        locks = [multiprocessing.Lock() for _ in range(4)]
        counts = multiprocessing.Array("i", len(locks), lock=False)
        cache = ObjectCache(100 * 8, VISUAL_STRUCT, locks, counts)
        self.addCleanup(cache.close)

        self.assertFalse(cache.has_pending_changes())
        cache.set_many(0, {"x": 1.0})
        self.assertTrue(cache.has_pending_changes())
        cache.get_changed_values_many((0,))
        self.assertFalse(cache.has_pending_changes())

        cache.set_many(2, {"y": 2.0})
        self.assertTrue(cache.has_pending_changes())
        cache.clear_changes(2)
        self.assertFalse(cache.has_pending_changes())

    def test_dirty_counts_must_match_lock_stripes(self):
        locks = [multiprocessing.Lock() for _ in range(4)]
        counts = multiprocessing.Array("i", 3, lock=False)
        with self.assertRaises(ValueError):
            ObjectCache(100 * 8, VISUAL_STRUCT, locks, counts)

    def test_object_cache_get_many_reuses_plan_across_segments(self):
        cache = ObjectCache(100 * 8, VISUAL_STRUCT)
        self.addCleanup(cache.close)
        fields = ("x", "y", "sprite")
        cache.set_many(0, {"x": 1.0, "y": 2.0, "sprite": "a.png"})
        cache.set_many(5, {"x": 3.0, "y": 4.0, "sprite": "b.png"})

        self.assertEqual(
            cache.get_many(0, fields),
            {"x": 1.0, "y": 2.0, "sprite": "a.png"},
        )
        plan = cache._many_plans[fields]
        self.assertEqual(
            cache.get_many(5, fields),
            {"x": 3.0, "y": 4.0, "sprite": "b.png"},
        )
        self.assertIs(cache._many_plans[fields], plan)

    def test_object_cache_set_many_marks_only_changed_fields(self):
        cache = ObjectCache(100 * 4, VISUAL_STRUCT)
        self.addCleanup(cache.close)
        cache.set_many(1, {"x": 1.0, "y": 2.0})
        self.assertEqual(set(cache.get_changed_values(1)), {"x", "y"})

        # Повторение тех же значений не должно ничего помечать как изменённое.
        cache.set_many(1, {"x": 1.0, "y": 2.0})
        self.assertEqual(cache.get_changed_values(1), {})

        cache.set_many(1, {"x": 1.0, "rotation": 45.0})
        self.assertEqual(set(cache.get_changed_values(1)), {"rotation"})
        self.assertEqual(cache.get(1, "x"), 1.0)

    def test_object_cache_set_many_validates_before_writing(self):
        cache = ObjectCache(100 * 4, VISUAL_STRUCT)
        self.addCleanup(cache.close)
        with self.assertRaises(TypeError):
            cache.set_many(0, {"x": 9.0, "missing": 1})
        self.assertEqual(cache.get(0, "x"), 0.0)
        self.assertEqual(cache.get_changed_values(0), {})

    def test_object_cache_single_set_and_get(self):
        cache = ObjectCache(100 * 4, VISUAL_STRUCT)
        self.addCleanup(cache.close)
        cache.set(3, "sprite", "hero.png")
        cache.set(3, "x", 7.5)
        self.assertEqual(cache.get(3, "sprite"), "hero.png")
        self.assertEqual(cache.get(3, "x"), 7.5)
        with self.assertRaises(IndexError):
            cache.get(4, "x")
        with self.assertRaises(TypeError):
            cache.get(0, "missing")

    def test_value_cache_attach_keeps_format(self):
        owner = ValueCache(2.5)
        self.addCleanup(owner.close)
        attached = ValueCache(owner.name)
        self.addCleanup(attached.detach)
        self.assertEqual(attached.get(), 2.5)
        attached.set(4.5)
        self.assertEqual(owner.get(), 4.5)

        text_owner = ValueCache("start")
        self.addCleanup(text_owner.close)
        text = ValueCache(text_owner.name)
        self.addCleanup(text.detach)
        self.assertEqual(text.get(), "start")
        text.set("next")
        self.assertEqual(text_owner.get(), "next")

    def test_field_view_reads_one_engine_segment(self):
        cache = ObjectCache(
            ENGINE_STATE_SEGMENT_BYTES,
            ENGINE_STATE_STRUCT,
        )
        self.addCleanup(cache.close)
        view = FieldView(cache, "time")
        view.set(12.5)
        self.assertEqual(view.get(), 12.5)
        self.assertEqual(cache.get(0, "time"), 12.5)
        self.assertEqual(view.name, (cache.key, 0, "time"))

    def test_missing_field_does_not_poison_get_many_plan(self):
        cache = ObjectCache(100 * 2, VISUAL_STRUCT)
        self.addCleanup(cache.close)
        with self.assertRaises(TypeError):
            cache.get_many(0, ("x", "missing"))
        self.assertNotIn(("x", "missing"), cache._many_plans)
        self.assertEqual(cache.get_many(0, ("x",)), {"x": 0.0})

    def test_string_limit(self):
        cache = ValueCache("ok")
        self.addCleanup(cache.close)
        cache.set("short")
        self.assertEqual(cache.get(), "short")
        with self.assertRaises(ValueError):
            cache.set("x" * (MAX_CACHE_STRING_BYTES + 1))


class CollisionTests(unittest.TestCase):
    square = ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0))

    def test_polygon_bounds_and_precomputed_bounds(self):
        left = transform_polygon(self.square, (0, 0), 0, 1)
        touching = transform_polygon(self.square, (2, 0), 0, 1)
        apart = transform_polygon(self.square, (5, 0), 0, 1)

        self.assertEqual(polygon_bounds(left), (-1.0, 1.0, -1.0, 1.0))
        self.assertTrue(convex_polygons_overlap(
            left, touching, polygon_bounds(left), polygon_bounds(touching)
        ))
        self.assertFalse(convex_polygons_overlap(
            left, apart, polygon_bounds(left), polygon_bounds(apart)
        ))


class ApplyUpdateTests(unittest.TestCase):
    class Sprite:
        def __init__(self):
            self.texture = None
            self.angle = 0.0
            self.scale = 1.0
            self.alpha = 255

    def make_game(self):
        game = render.Game.__new__(render.Game)
        game._world_data_by_render_id = {1: {
            "position": (0.0, 0.0),
            "scale": 1.0,
            "rotation": 0.0,
            "alpha": 1.0,
            "layer": 0.0,
        }}
        game._latest_tick_by_render_id = {}
        game._moving_render_ids = set()
        game._position_history_by_render_id = {}
        game._render_order_dirty = False
        game._dirty_hits = set()
        game._hit_mark_dirty = game._dirty_hits.add
        game._texture_for_path = lambda path: ("texture", path)
        return game

    def test_tick_only_update_does_not_touch_sprite(self):
        game = self.make_game()
        sprite = self.Sprite()

        game._apply_update(1, sprite, {"tick": 12})

        self.assertEqual(game._latest_tick_by_render_id, {1: 12})
        self.assertEqual(sprite.angle, 0.0)
        self.assertEqual(sprite.scale, 1.0)
        self.assertNotIn(1, game._moving_render_ids)
        self.assertEqual(
            game._world_data_by_render_id[1]["position"], (0.0, 0.0)
        )

    def test_single_field_updates_reuse_generic_semantics(self):
        game = self.make_game()
        sprite = self.Sprite()
        world = game._world_data_by_render_id[1]

        game._apply_update(1, sprite, {"rotation": 1.5})
        self.assertEqual(world["rotation"], 1.5)
        self.assertEqual(sprite.angle, 1.5)
        self.assertIn(1, game._dirty_hits)

        game._dirty_hits.clear()
        game._apply_update(1, sprite, {"rotation": 1.5})
        self.assertNotIn(1, game._dirty_hits)

        game._apply_update(1, sprite, {"scale": 2.0})
        self.assertEqual(world["scale"], 2.0)
        self.assertEqual(sprite.scale, 2.0)
        self.assertIn(1, game._dirty_hits)

        game._apply_update(1, sprite, {"alpha": 0.5})
        self.assertEqual(world["alpha"], 0.5)
        self.assertEqual(sprite.alpha, 127)

        game._apply_update(1, sprite, {"layer": 3})
        self.assertEqual(world["layer"], 3.0)
        self.assertEqual(sprite._engine_sort_key, (3.0, -1))
        self.assertTrue(game._render_order_dirty)

        game._dirty_hits.clear()
        game._apply_update(1, sprite, {"sprite": "new.png"})
        self.assertEqual(sprite.texture, ("texture", "new.png"))
        self.assertIn(1, game._dirty_hits)

    def test_sort_render_layers_uses_cached_keys(self):
        class RenderList:
            def __init__(self):
                self.sprites = []

            def sort(self, *, key):
                self.sprites.sort(key=key)

        class SortedSprite:
            def __init__(self, render_id, layer):
                self._engine_render_id = render_id
                self._engine_sort_key = (layer, -render_id)

        game = render.Game.__new__(render.Game)
        game.renders = RenderList()
        game.renders.sprites = [
            SortedSprite(1, 0.0),
            SortedSprite(2, 2.0),
            SortedSprite(3, 2.0),
        ]
        game._render_order_dirty = True

        game._sort_render_layers()

        self.assertEqual(
            [sprite._engine_render_id for sprite in game.renders.sprites],
            [1, 3, 2],
        )
        self.assertFalse(game._render_order_dirty)


class MouseHitTestTests(unittest.TestCase):
    class HitBox:
        def __init__(self, bounds):
            self.bounds = bounds

        def get_adjusted_points(self):
            left, bottom, right, top = self.bounds
            return [
                (left, bottom), (right, bottom),
                (right, top), (left, top),
            ]

    class Sprite:
        def __init__(self, bounds, alpha=255):
            self.bounds = bounds
            self.hit_box = MouseHitTestTests.HitBox(bounds)
            self.alpha = alpha
            self.collide_calls = 0
            self.center_x = (bounds[0] + bounds[2]) * 0.5
            self.center_y = (bounds[1] + bounds[3]) * 0.5

        def place(self, bounds):
            self.bounds = bounds
            self.hit_box = MouseHitTestTests.HitBox(bounds)
            self.center_x = (bounds[0] + bounds[2]) * 0.5
            self.center_y = (bounds[1] + bounds[3]) * 0.5

        def collides_with_point(self, point):
            self.collide_calls += 1
            x, y = point
            left, bottom, right, top = self.bounds
            return left <= x <= right and bottom <= y <= top

        @property
        def position(self):
            return (self.center_x, self.center_y)

        @position.setter
        def position(self, value):
            self.center_x, self.center_y = value

    def make_game(self):
        game = render.Game.__new__(render.Game)
        game._sprites_by_render_id = {}
        game._world_data_by_render_id = {}
        game._hit_cells = {}
        game._hit_geometry_by_render_id = {}
        game._hit_bounds_by_render_id = {}
        game._hit_max_extent = 0.0
        game._hit_max_count = 0
        game._hit_large_ids = set()
        game._hit_dirty_ids = set()
        game._position_history_by_render_id = {}
        game._moving_render_ids = set()
        game._latest_tick_by_render_id = {}
        game._last_tick_update_by_render_id = {}
        return game

    def add(self, game, render_id, bounds, layer=0.0, alpha=255):
        sprite = self.Sprite(bounds, alpha)
        game._sprites_by_render_id[render_id] = sprite
        game._world_data_by_render_id[render_id] = {"layer": layer}
        game._hit_mark_dirty(render_id)
        return sprite

    def test_only_sprites_in_the_pointer_cell_are_checked(self):
        game = self.make_game()
        near = self.add(game, 1, (10, 10, 40, 40))
        for render_id in range(2, 102):
            offset = render_id * 512
            self.add(
                game, render_id,
                (offset, offset, offset + 40, offset + 40),
            )

        self.assertEqual(game._hit_test((20, 20)), 1)
        self.assertEqual(near.collide_calls, 1)
        self.assertEqual(
            sum(sprite.collide_calls
                for sprite in game._sprites_by_render_id.values()),
            1,
        )

    def test_aabb_rejects_candidates_before_polygon_test(self):
        game = self.make_game()
        hit = self.add(game, 1, (0, 0, 10, 10))
        missed = self.add(game, 2, (0, 20, 10, 30))

        self.assertEqual(game._hit_test((5, 5)), 1)
        self.assertEqual(hit.collide_calls, 1)
        self.assertEqual(missed.collide_calls, 0)

    def test_far_stale_sprite_is_reindexed_when_pointer_approaches(self):
        game = self.make_game()
        sprite = self.add(game, 1, (0, 0, 10, 10))
        self.assertEqual(game._hit_test((5, 5)), 1)

        # Спрайт переместился без явной пометки об изменении, как это делает
        # интерполяция.  Дальний запрос не должен тратиться на его переиндексацию,
        # но при приближении к новой позиции он всё равно должен находиться.
        sprite.place((3000, 3000, 3010, 3010))
        game._moving_render_ids.add(1)

        self.assertIsNone(game._hit_test((5, 5)))
        self.assertEqual(game._hit_test((3005, 3005)), 1)

    def test_highest_layer_wins_and_render_id_breaks_ties(self):
        game = self.make_game()
        self.add(game, 5, (0, 0, 100, 100), layer=2)
        self.add(game, 7, (0, 0, 100, 100), layer=3)
        self.assertEqual(game._hit_test((50, 50)), 7)

        self.add(game, 2, (0, 0, 100, 100), layer=4)
        self.add(game, 9, (0, 0, 100, 100), layer=4)
        self.assertEqual(game._hit_test((50, 50)), 2)

    def test_transparent_sprites_are_not_hit(self):
        game = self.make_game()
        self.add(game, 1, (0, 0, 100, 100), alpha=0)
        self.assertIsNone(game._hit_test((50, 50)))

    def test_moved_sprite_is_reindexed_on_next_query(self):
        game = self.make_game()
        sprite = self.add(game, 1, (0, 0, 40, 40))
        self.assertEqual(game._hit_test((10, 10)), 1)

        sprite.place((5000, 5000, 5040, 5040))
        game._hit_mark_dirty(1)
        self.assertIsNone(game._hit_test((10, 10)))
        self.assertEqual(game._hit_test((5010, 5010)), 1)

    def test_movement_inside_the_same_cells_does_not_reindex(self):
        game = self.make_game()
        sprite = self.add(game, 1, (10, 10, 40, 40))
        self.assertEqual(game._hit_test((20, 20)), 1)
        self.assertNotIn(1, game._hit_dirty_ids)
        initial_cells = game._hit_geometry_by_render_id[1][:4]

        sprite.place((20, 20, 50, 50))
        game._hit_mark_position(1, sprite.center_x, sprite.center_y)

        self.assertNotIn(1, game._hit_dirty_ids)
        self.assertEqual(
            game._hit_geometry_by_render_id[1][:4], initial_cells
        )
        self.assertEqual(game._hit_test((25, 25)), 1)
        # Набор кандидатов всё ещё действителен; решает сам hit box.
        self.assertEqual(game._hit_test((45, 45)), 1)
        self.assertIsNone(game._hit_test((15, 15)))

    def test_position_update_inside_same_cells_does_not_reindex(self):
        game = self.make_game()
        sprite = self.add(game, 1, (10, 10, 40, 40))
        game._world_data_by_render_id[1].update({
            "position": (25.0, 25.0),
            "scale": 1.0,
            "rotation": 0.0,
            "alpha": 1.0,
        })
        self.assertEqual(game._hit_test((20, 20)), 1)
        initial_cells = game._hit_geometry_by_render_id[1][:4]

        game._apply_update(1, sprite, {"x": 26.0, "tick": 1})

        self.assertNotIn(1, game._hit_dirty_ids)
        # Ленивое обновление указателя тоже должно увидеть, что спрайт остался
        # в своих кэшированных ячейках, и не трогать индекс.
        game._hit_index_dirty()
        self.assertEqual(
            game._hit_geometry_by_render_id[1][:4], initial_cells
        )
        self.assertNotIn(1, game._hit_dirty_ids)

    def test_position_update_is_indexed_lazily_on_hit_query(self):
        game = self.make_game()
        sprite = self.add(game, 1, (10, 10, 40, 40))
        game._world_data_by_render_id[1].update({
            "position": (25.0, 25.0),
            "scale": 1.0,
            "rotation": 0.0,
            "alpha": 1.0,
        })
        self.assertEqual(game._hit_test((20, 20)), 1)
        old_geometry = game._hit_geometry_by_render_id[1]

        game._apply_update(
            1, sprite, {"x": 3000.0, "y": 3000.0, "tick": 1}
        )
        # Обновление только записывает сэмпл.  Интерполяция рисует спрайт
        # в новой позиции, но ни то, ни другое не трогает сетку попаданий.
        self.assertNotIn(1, game._hit_dirty_ids)
        game._interpolate_sprite_positions(1.0)
        self.assertEqual((sprite.center_x, sprite.center_y), (3000.0, 3000.0))
        self.assertNotIn(1, game._hit_dirty_ids)
        self.assertEqual(game._hit_geometry_by_render_id[1], old_geometry)

        # Запрос указателя переводит перемещённый спрайт в активные и переиндексирует его.
        game._hit_index_dirty()
        new_geometry = game._hit_geometry_by_render_id[1]
        self.assertNotEqual(new_geometry, old_geometry)
        cell_size = render.HIT_TEST_CELL_SIZE
        local_left = new_geometry[0] * cell_size - new_geometry[4]
        self.assertAlmostEqual(local_left, 10.0 - 3000.0)

    def test_stale_snap_refreshes_hit_grid(self):
        game = self.make_game()
        sprite = self.add(game, 1, (10, 10, 40, 40))
        game._world_data_by_render_id[1].update({
            "position": (25.0, 25.0),
            "scale": 1.0,
            "rotation": 0.0,
            "alpha": 1.0,
        })
        self.assertEqual(game._hit_test((20, 20)), 1)

        game._apply_update(
            1, sprite, {"x": 3000.0, "y": 3000.0, "tick": 1}
        )
        game._interpolate_sprite_positions(1.0)
        self.assertNotIn(1, game._hit_dirty_ids)

        game._interpolate_sprite_positions(1000.0)
        self.assertNotIn(1, game._moving_render_ids)
        self.assertIn(1, game._hit_dirty_ids)

    def test_movement_across_cells_reindexes_on_next_query(self):
        game = self.make_game()
        sprite = self.add(game, 1, (10, 10, 40, 40))
        self.assertEqual(game._hit_test((20, 20)), 1)

        sprite.place((300, 300, 340, 340))
        game._hit_mark_position(1, sprite.center_x, sprite.center_y)

        self.assertIn(1, game._hit_dirty_ids)
        self.assertIsNone(game._hit_test((20, 20)))
        self.assertEqual(game._hit_test((310, 310)), 1)

    def test_large_sprite_motion_does_not_need_grid_reindexing(self):
        game = self.make_game()
        sprite = self.add(game, 1, (0, 0, 4000, 4000))
        self.assertEqual(game._hit_test((3210, 1230)), 1)
        self.assertIn(1, game._hit_large_ids)

        sprite.place((1000, 1000, 5000, 5000))
        game._hit_mark_position(1, sprite.center_x, sprite.center_y)

        self.assertNotIn(1, game._hit_dirty_ids)
        self.assertIn(1, game._hit_large_ids)
        self.assertEqual(game._hit_test((4210, 2230)), 1)

    def test_scale_change_forces_reindex_even_inside_the_same_cells(self):
        game = self.make_game()
        sprite = self.add(game, 1, (10, 10, 40, 40))
        game._world_data_by_render_id[1].update({
            "position": (25.0, 25.0),
            "scale": 1.0,
            "rotation": 0.0,
            "alpha": 1.0,
        })
        self.assertEqual(game._hit_test((20, 20)), 1)

        game._apply_update(1, sprite, {"scale": 2.0})

        self.assertIn(1, game._hit_dirty_ids)

    def test_huge_sprite_uses_the_large_sprite_fallback(self):
        game = self.make_game()
        self.add(game, 1, (0, 0, 4000, 4000))
        self.assertEqual(game._hit_test((3210, 1230)), 1)
        self.assertIn(1, game._hit_large_ids)
        self.assertNotIn(1, game._hit_geometry_by_render_id)

        game._hit_remove(1)
        self.assertEqual(game._hit_test((3210, 1230)), None)
        self.assertNotIn(1, game._hit_large_ids)

    def test_removing_largest_sprite_recomputes_max_extent(self):
        game = self.make_game()
        self.add(game, 1, (0, 0, 100, 100))
        self.add(game, 2, (200, 200, 220, 220))
        self.assertEqual(game._hit_test((50, 50)), 1)
        self.assertEqual(game._hit_max_extent, 50.0)

        game._hit_remove(1)
        self.assertEqual(game._hit_max_extent, 10.0)
        self.assertEqual(game._hit_max_count, 1)

    class Camera:
        @staticmethod
        def unproject(point):
            return Vector2(point[0], point[1])

    class Stream:
        def __init__(self):
            self.messages = []

        def put(self, message):
            self.messages.append(message)

    def prepare_send(self, game):
        game.camera = self.Camera()
        game.main_in_stream = self.Stream()
        game.motion_cache = FakeMotionCache()
        game._motion_sequence = 0
        return game.main_in_stream

    def test_send_mouse_event_uses_spatial_index(self):
        game = self.make_game()
        self.add(game, 3, (0, 0, 100, 100))
        stream = self.prepare_send(game)
        game._send_mouse_event("mouse_move", 50, 60, dx=1, dy=2)

        # Движение публикуется напрямую и не трогает очередь Manager.
        self.assertEqual(stream.messages, [])
        self.assertEqual(game.motion_cache.values["hovered"], 3)
        self.assertEqual(game.motion_cache.values["x"], 50.0)
        self.assertEqual(game.motion_cache.values["y"], 60.0)
        self.assertEqual(game.motion_cache.values["seq"], 1)

        game._send_mouse_event("mouse_move", 51, 61, dx=2, dy=3)
        self.assertEqual(game.motion_cache.values["seq"], 2)
        self.assertEqual(game.motion_cache.values["dx"], 2.0)

    def test_click_publishes_snapshot_but_still_queues_the_event(self):
        game = self.make_game()
        self.add(game, 3, (0, 0, 100, 100))
        stream = self.prepare_send(game)

        game._send_mouse_event("mouse_down", 50, 60, button=1)
        self.assertEqual(len(stream.messages), 1)
        message = stream.messages[0]
        self.assertTrue(message["pointer_published"])
        self.assertEqual(message["button"], "left")
        self.assertEqual(game.motion_cache.values["hovered"], 3)
        # Только движение продвигает последовательность указателя.
        self.assertEqual(game.motion_cache.values["seq"], 0)


class InfoOverlayCacheTests(unittest.TestCase):
    def make_game(self):
        class Cache:
            def __init__(self, values):
                self.values = values
                self.gets = 0

            def get(self, key):
                self.gets += 1
                return self.values.get(key)

        game = render.Game.__new__(render.Game)
        game._info_static_cache = {}
        game.object_cache = Cache({
            ("__engine_index__", "render_id", 4): "object-1",
            "object-1": {
                "_id": "object-1",
                "dict_data": {
                    "_identity": {
                        "name": "hero", "class": "Player", "type": "dynamic",
                    },
                    "scripting": {
                        "scripts": [
                            "access/scripts/move.py", "access/scripts/shoot.py",
                        ],
                    },
                },
            },
        })
        return game

    def test_static_info_is_cached_between_frames(self):
        game = self.make_game()
        first = game._info_static_for(4, 100.0)
        self.assertEqual(first, {
            "object_id": "object-1",
            "name": "hero",
            "class": "Player",
            "type": "dynamic",
            "scripts": "move.py, shoot.py",
        })
        self.assertEqual(game.object_cache.gets, 2)

        self.assertIs(game._info_static_for(4, 100.5), first)
        self.assertEqual(game.object_cache.gets, 2)

        game._info_static_for(4, 101.5)
        self.assertEqual(game.object_cache.gets, 4)

    def test_missing_record_is_retried_next_frame(self):
        game = self.make_game()
        game.object_cache.values.clear()
        self.assertIsNone(game._info_static_for(4, 100.0))
        self.assertNotIn(4, game._info_static_cache)


class RenderQueueTests(unittest.TestCase):
    def test_render_queue_reads_dirty_segments_in_one_batch(self):
        calls = []

        class VisualCache:
            def get_changed_values_many(self, segment_ids):
                calls.append(tuple(segment_ids))
                return {0: {"x": 5.0}}

        class EmptyQueue:
            def get_nowait(self):
                raise queue.Empty

        game = render.Game.__new__(render.Game)
        game.render_in_stream = EmptyQueue()
        game._sprites_by_render_id = {0: object(), 1: object()}
        game.visual_cache = VisualCache()
        game._sort_render_layers = lambda: None
        game.print = lambda *args, **kwargs: None
        applied = []
        game._apply_update = lambda render_id, sprite, entry: applied.append(
            (render_id, entry)
        )

        game._render_queue()

        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]), {0, 1})
        self.assertEqual(applied, [(0, {"x": 5.0})])

    def test_idle_frame_skips_dirty_batch_when_counters_are_clean(self):
        calls = []

        class VisualCache:
            def has_pending_changes(self):
                return False

            def get_changed_values_many(self, segment_ids):
                calls.append(tuple(segment_ids))
                return {}

        class EmptyQueue:
            def get_nowait(self):
                raise queue.Empty

        game = render.Game.__new__(render.Game)
        game.render_in_stream = EmptyQueue()
        game._sprites_by_render_id = {0: object()}
        game.visual_cache = VisualCache()
        game._sort_render_layers = lambda: None
        game.print = lambda *args, **kwargs: None
        game._apply_update = lambda *args, **kwargs: None

        game._render_queue()

        self.assertEqual(calls, [])

    def test_render_queue_uses_pre_grouped_stripe_buckets(self):
        calls = []

        class VisualCache:
            dirty_counts = [1, 0, 0, 0]

            def get_changed_values_by_stripe(self, groups):
                calls.append([set(group) for group in groups])
                return {0: {"x": 5.0}}

            def get_changed_values_many(self, segment_ids):
                raise AssertionError("должен использоваться путь с предварительной группировкой")

        class EmptyQueue:
            def get_nowait(self):
                raise queue.Empty

        game = render.Game.__new__(render.Game)
        game.render_in_stream = EmptyQueue()
        game._sprites_by_render_id = {0: object()}
        game._render_ids_by_stripe = [{0}, {1}, set(), set()]
        game.visual_cache = VisualCache()
        game._sort_render_layers = lambda: None
        game.print = lambda *args, **kwargs: None
        applied = []
        game._apply_update = lambda render_id, sprite, entry: applied.append(
            (render_id, entry)
        )

        game._render_queue()

        self.assertEqual(calls, [[{0}, {1}, set(), set()]])
        self.assertEqual(applied, [(0, {"x": 5.0})])


class SnapshotInterpolationTests(unittest.TestCase):
    class Probe:
        SIMULATION_TICK = render.Game.SIMULATION_TICK
        POSITION_HISTORY_SIZE = render.Game.POSITION_HISTORY_SIZE
        POSITION_INTERPOLATION_DELAY = render.Game.POSITION_INTERPOLATION_DELAY
        ACTIVE_SNAPSHOT_WINDOW = render.Game.ACTIVE_SNAPSHOT_WINDOW
        INTERPOLATION_IDLE_TICKS = render.Game.INTERPOLATION_IDLE_TICKS

        def __init__(self, clock_origin=None):
            self.clock_origin = (
                clock_origin if clock_origin is not None else time.monotonic()
            )
            self._position_history_by_render_id = {}
            self._latest_tick_by_render_id = {}
            self._last_tick_update_by_render_id = {}
            self._scripted_render_ids = set()
            self._sprites_by_render_id = {}
            self._world_data_by_render_id = {}
            self._moving_render_ids = set()

        _record_position = render.Game._record_position
        _append_position_sample = render.Game._append_position_sample
        _position_from_history = render.Game.__dict__["_position_from_history"]
        _position_at_tick = render.Game._position_at_tick
        _resolve_render_tick = render.Game._resolve_render_tick
        _interpolate_sprite_positions = render.Game._interpolate_sprite_positions
        _apply_update = render.Game._apply_update

        def _hit_mark_dirty(self, render_id):
            pass

        def _hit_mark_position(self, render_id, x, y):
            pass

    class Sprite:
        center_x = 0.0
        center_y = 0.0

        @property
        def position(self):
            return (self.center_x, self.center_y)

        @position.setter
        def position(self, value):
            self.center_x, self.center_y = value

    def test_common_tick_keeps_relative_offset_stable(self):
        probe = self.Probe()

        # Объект и цель камеры движутся с одинаковой скоростью.  Сэмплы цели
        # записываются с другим паттерном поступления, но оба несут одинаковые
        # номера общего tick.
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

    def test_slow_script_does_not_freeze_shared_render_tick(self):
        origin = time.monotonic() - 1.0
        probe = self.Probe(clock_origin=origin)
        now = time.monotonic()
        probe._scripted_render_ids = {1, 2}
        probe._latest_tick_by_render_id = {1: 100, 2: 90}
        probe._last_tick_update_by_render_id = {1: now, 2: now}

        # Объект 2 отстаёт на 10 тиков, но общие часы должны продолжать идти,
        # чтобы остальные объекты оставались плавными.  Только объект 2 держит сэмпл.
        render_tick = probe._resolve_render_tick()
        self.assertGreater(render_tick, 89.0)
        probe._position_history_by_render_id[2] = deque(
            [(90, 5.0, 7.0)], maxlen=self.Probe.POSITION_HISTORY_SIZE
        )
        self.assertEqual(probe._position_at_tick(2, render_tick), (5.0, 7.0))

    def test_idle_unscripted_visual_does_not_hold_render_tick(self):
        origin = time.monotonic() - 1.0
        probe = self.Probe(clock_origin=origin)
        now = time.monotonic()
        probe._scripted_render_ids = {1}
        probe._latest_tick_by_render_id = {1: 100, 2: 50}
        probe._last_tick_update_by_render_id = {1: now, 2: now}

        # Render id 2 — простаивающая цель камеры без собственного цикла скрипта.
        # Её устаревший tick не должен замораживать весь кадр и вызывать скачок.
        render_tick = probe._resolve_render_tick()
        self.assertGreater(render_tick, 90.0)

    def test_only_moving_sprites_are_interpolated(self):
        probe = self.Probe()
        static_sprite = self.Sprite()
        static_sprite.center_x = 11.0
        static_sprite.center_y = 22.0
        moving_sprite = self.Sprite()
        probe._sprites_by_render_id = {1: static_sprite, 2: moving_sprite}
        probe._world_data_by_render_id = {
            1: {"position": (11.0, 22.0)},
            2: {"position": (10.0, -10.0)},
        }
        probe._position_history_by_render_id[2] = deque(
            [(0, 0.0, 0.0), (10, 10.0, -10.0)],
            maxlen=probe.POSITION_HISTORY_SIZE,
        )
        probe._moving_render_ids = {2}

        probe._interpolate_sprite_positions(5.0)

        self.assertEqual(
            (static_sprite.center_x, static_sprite.center_y), (11.0, 22.0)
        )
        self.assertAlmostEqual(moving_sprite.center_x, 5.0)
        self.assertAlmostEqual(moving_sprite.center_y, -5.0)

        # Долгая тишина убирает спрайт из набора движущихся и перемещает его
        # к последнему полученному сэмплу, а не оставляет интерполированным.
        probe._interpolate_sprite_positions(1000.0)
        self.assertNotIn(2, probe._moving_render_ids)
        self.assertEqual(
            (moving_sprite.center_x, moving_sprite.center_y), (10.0, -10.0)
        )

    def test_equal_position_updates_do_not_mark_sprite_moving(self):
        probe = self.Probe()
        sprite = self.Sprite()
        probe._sprites_by_render_id = {3: sprite}
        probe._world_data_by_render_id = {3: {"position": (5.0, 5.0)}}

        probe._apply_update(3, sprite, {"x": 5.0, "y": 5.0, "tick": 7})
        self.assertNotIn(3, probe._moving_render_ids)

        probe._apply_update(3, sprite, {"x": 6.0, "y": 5.0, "tick": 8})
        self.assertIn(3, probe._moving_render_ids)
        self.assertEqual(
            list(probe._position_history_by_render_id[3]),
            [(8, 6.0, 5.0)],
        )


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

        # Обновление позиции не должно перезаписывать sim_time: рендерер
        # использует общий tick, а поле совместимости нужно только при регистрации.
        visual.set(0, "sim_time", 123.0)
        game_object.Visual.position = Vector2(5, 6)
        self.assertEqual(visual.get(0, "tick"), 17)
        self.assertEqual(visual.get(0, "x"), 5.0)
        self.assertEqual(visual.get(0, "sim_time"), 123.0)

    def test_visual_move_updates_position_and_tick(self):
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

        visual.set_many(0, {"x": 1.0, "y": 2.0, "tick": 17})
        position = game_object.Visual.move(2.5, -1.5)
        self.assertEqual(visual.get_position(0), (3.5, 0.5, 17))
        self.assertEqual((position.x, position.y), (3.5, 0.5))

        # Вариант с парой и возвращаемое значение без fallback.
        position = game_object.Visual.move((-1.0, 2.0))
        self.assertEqual(visual.get_position(0), (2.5, 2.5, 17))
        self.assertEqual((position.x, position.y), (2.5, 2.5))

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

        # Скрипт может двигать другой объект через find_object().  Каждая
        # запись позиции должна нести текущий tick того объекта, к которому
        # она относится, а не объекта самого скрипта.
        first.Visual.position = Vector2(1, 2)
        second.Visual.position = Vector2(3, 4)

        self.assertEqual(visual.get(0, "tick"), 23)
        self.assertEqual(visual.get(1, "tick"), 23)
        self.assertEqual(visual.get_position(0), (1.0, 2.0, 23))
        self.assertEqual(visual.get_position(1), (3.0, 4.0, 23))

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

    def test_transform_caches_world_geometry_until_pose_changes(self):
        visual, identity = self.make_caches()
        deps, _, responses = self.make_deps(visual, identity)
        data = {
            "object_type": "background_object",
            "visual": {
                "sprite": "img.png",
                "position": [0, 0],
                "scale": 1,
                "rotation": 0,
                "alpha": 1,
            },
            "transform": {"polygon": [[-1, -1], [1, -1], [1, 1], [-1, 1]]},
        }
        first = create_object(dict(data), deps=deps)
        second_requests = FakeRequestQueue(responses, render_id=1, object_id=1)
        second = create_object(
            dict(data), deps=dict(deps, main_in_stream=second_requests)
        )
        visual.set_many(0, {
            "x": 0.0, "y": 0.0, "rotation": 0.0, "scale": 1.0,
        })
        visual.set_many(1, {
            "x": 1.0, "y": 0.0, "rotation": 0.0, "scale": 1.0,
        })

        with patch("pyengine.transform_polygon", wraps=transform_polygon) as mocked:
            self.assertTrue(first.Transform.overlaps(second))
            self.assertEqual(mocked.call_count, 2)

            # Оба трансформа сохраняют свой мировой полигон и AABB между проверками.
            self.assertTrue(first.Transform.overlaps(second))
            self.assertEqual(mocked.call_count, 2)

            # Изменение позы сбрасывает кэш только у сдвинувшегося трансформа.
            visual.set_many(0, {"x": 10.0})
            self.assertFalse(first.Transform.overlaps(second))
            self.assertEqual(mocked.call_count, 3)

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

    def test_top_level_component_is_registered_with_direct_properties(self):
        visual, identity = self.make_caches()
        deps, requests, _ = self.make_deps(visual, identity)

        game_object = create_object({
            "object_type": "static_object",
            "id": {"name": "tilt_trigger", "class": "TiltTrigger"},
            "rotate_on_click": {"degrees": 90},
        }, deps=deps)

        self.assertEqual(game_object.component_specs, [{
            "name": "rotate_on_click",
            "path": "access/components/rotate_on_click.py",
            "properties": {"degrees": 90},
        }])
        add_script = [
            message for message in requests.messages
            if message["command"] == "add_script"
        ]
        self.assertEqual(len(add_script), 1)
        self.assertEqual(
            add_script[0]["path"],
            "access/components/rotate_on_click.py",
        )
        self.assertEqual(
            add_script[0]["script_data"]["_component_config"],
            {"degrees": 90},
        )

    def test_legacy_components_are_still_registered(self):
        visual, identity = self.make_caches()
        deps, requests, _ = self.make_deps(visual, identity)

        create_object({
            "object_type": "static_object",
            "id": {"name": "tilt_trigger", "class": "TiltTrigger"},
            "components": [{
                "path": "access/components/rotate_on_click.py",
                "properties": {"degrees": 45},
            }],
        }, deps=deps)

        add_script = [
            message for message in requests.messages
            if message["command"] == "add_script"
        ]
        self.assertEqual(len(add_script), 1)
        self.assertEqual(
            add_script[0]["script_data"]["_component_config"],
            {"degrees": 45},
        )

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

    def test_create_list_registers_with_one_batch_request(self):
        visual, identity = self.make_caches()
        deps, requests, _ = self.make_deps(visual, identity)

        objects = create_object([
            {"object_type": "static_object",
             "id": {"name": "one", "class": "Wall"}},
            {"object_type": "static_object",
             "id": {"name": "two", "class": "Wall"}},
        ], deps=deps)

        self.assertEqual(
            [message["command"] for message in requests.messages],
            ["register_many"],
        )
        self.assertEqual(len(requests.messages[0]["objects"]), 2)
        self.assertEqual([obj._object_id for obj in objects], [0, 1])
        self.assertEqual(identity.get(0, "name"), "one")
        self.assertEqual(identity.get(1, "name"), "two")

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

    def test_register_is_idempotent_only_for_live_generation(self):
        visual, identity = self.make_caches()
        deps, requests, _ = self.make_deps(visual, identity)

        game_object = create_object({
            "object_type": "static_object",
            "id": {"name": "wall", "class": "Wall"},
        }, deps=deps)
        self.assertEqual(game_object._generation, 1)
        # Служебные поля не попадают в пользовательский dict_data, иначе
        # повторная регистрация не прошла бы валидацию сцены.
        self.assertNotIn("_generation", game_object.dict_data)
        self.assertNotIn("object_id", game_object.dict_data)
        self.assertNotIn("render_id", game_object.dict_data)

        def register_count():
            return sum(
                message["command"] == "register"
                for message in requests.messages
            )

        deps["objects_cache"][game_object._id] = {
            "_generation": game_object._generation,
        }
        game_object.register()
        self.assertEqual(register_count(), 1)

        # После удаления запись исчезает, и старые идентификаторы нельзя
        # считать живыми: слот мог достаться другому объекту.
        deps["objects_cache"].pop(game_object._id)
        requests.generation = 2
        requests.render_id = 5
        game_object.register()

        self.assertEqual(register_count(), 2)
        self.assertEqual(game_object._render_id, 5)
        self.assertEqual(game_object._generation, 2)

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
        stream_object.engine_state = FakeEngineState()
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

    def test_registration_populates_and_removes_layer_bucket(self):
        visual, identity = self.make_caches()
        responses = FakeResponseQueue()
        stream_object = Stream.__new__(Stream)
        stream_object.main_out_stream = responses
        stream_object.render_in_stream = FakeRequestQueue(responses)
        stream_object.object_lock = threading.Lock()
        stream_object.objects_cache = {}
        stream_object.visual_cache = visual
        stream_object.id_cache = identity
        stream_object.engine_state = FakeEngineState()
        stream_object.camera_target_cache = ValueCache(-1)
        self.addCleanup(stream_object.camera_target_cache.close)
        stream_object._next_object_id = 0
        stream_object._next_render_id = 0

        game_object = GameObject({
            "object_type": "static_object",
            "id": {"name": "sky", "class": "Background"},
            "visual": {
                "sprite": "img.png",
                "position": [0, 0],
                "scale": 1,
                "rotation": 0,
                "alpha": 1,
                "layer": 3,
            },
        }, deps={
            "visual_cache": visual,
            "id_cache": identity,
            "objects_cache": stream_object.objects_cache,
            "lock": stream_object.object_lock,
        })
        stream_object._Stream__register({
            "id": game_object._id,
            "request_id": "layer",
            "obj": game_object._GameObject__cache_record(),
            "script_data": game_object.dict_data,
        })

        layer_key = (Stream.INDEX_PREFIX, "layer", 3.0)
        self.assertEqual(
            stream_object.objects_cache[layer_key], [game_object._id]
        )

        stream_object._Stream__remove_identity_indexes(game_object._id, 0)
        self.assertNotIn(layer_key, stream_object.objects_cache)

    def test_visual_layer_change_moves_index_bucket(self):
        visual, identity = self.make_caches()
        deps, _, _ = self.make_deps(visual, identity)
        game_object = create_object({
            "object_type": "background_object",
            "visual": {
                "sprite": "img.png",
                "position": [0, 0],
                "scale": 1,
                "rotation": 0,
                "alpha": 1,
                "layer": 2,
            },
        }, deps=deps)

        old_key = (Stream.INDEX_PREFIX, "layer", 2.0)
        new_key = (Stream.INDEX_PREFIX, "layer", 4.5)
        visual.set_many(0, {"layer": 2.0})
        # Регистрация в живом Stream заполняет эту корзину; фиктивный поток
        # только подтверждает регистрацию.
        deps["objects_cache"][old_key] = [game_object._id]

        game_object.Visual.layer = 4.5
        self.assertEqual(visual.get(0, "layer"), 4.5)
        self.assertNotIn(old_key, deps["objects_cache"])
        self.assertEqual(deps["objects_cache"][new_key], [game_object._id])

        # Повторная установка того же слоя не должна дублировать объект.
        game_object.Visual.layer = 4.5
        self.assertEqual(deps["objects_cache"][new_key], [game_object._id])


class RenderSlotLifecycleTests(CacheTestCase):
    """Переиспользование render_id и защита от команд старого поколения."""

    def make_stream(self):
        visual, identity = self.make_caches()
        responses = FakeResponseQueue()
        renders = FakeRequestQueue(responses)
        stream_object = Stream.__new__(Stream)
        stream_object.main_out_stream = responses
        stream_object.render_in_stream = renders
        stream_object.object_lock = threading.Lock()
        stream_object.objects_cache = {}
        stream_object.visual_cache = visual
        stream_object.id_cache = identity
        stream_object.engine_state = FakeEngineState()
        stream_object.camera_target_cache = ValueCache(-1)
        self.addCleanup(stream_object.camera_target_cache.close)
        stream_object._next_object_id = 0
        stream_object._next_render_id = 0
        stream_object._object_generation = 0
        return stream_object, responses, renders

    @staticmethod
    def make_game(name):
        return GameObject({
            "object_type": "static_object",
            "id": {"name": name, "class": "Wall"},
            "visual": {
                "sprite": "img.png",
                "position": [0, 0],
                "scale": 1,
                "rotation": 0,
                "alpha": 1,
            },
        })

    @staticmethod
    def register(stream_object, responses, game_object, request_id):
        stream_object._Stream__register({
            "id": game_object._id,
            "request_id": request_id,
            "obj": game_object._GameObject__cache_record(),
            "script_data": game_object.dict_data,
        })
        return responses.items[-1][request_id]

    def test_removed_render_slot_is_reused_with_new_generation(self):
        stream_object, responses, renders = self.make_stream()
        first = self.make_game("first")
        first_payload = self.register(stream_object, responses, first, "first")
        self.assertEqual(first_payload["render_id"], 0)
        self.assertEqual(first_payload["generation"], 1)

        stream_object._Stream__remove({"id": first._id, "generation": 1})
        self.assertNotIn(first._id, stream_object.objects_cache)
        self.assertEqual(list(stream_object._free_render_ids), [0])
        self.assertEqual(
            renders.messages[-1],
            {"command": "remove", "render_id": 0},
        )

        second = self.make_game("second")
        second_payload = self.register(stream_object, responses, second, "second")
        self.assertEqual(second_payload["render_id"], 0)
        self.assertEqual(second_payload["generation"], 2)
        self.assertEqual(list(stream_object._free_render_ids), [])

    def test_free_list_prevents_slot_exhaustion(self):
        stream_object, responses, _ = self.make_stream()
        capacity = min(
            stream_object.visual_cache.capacity,
            stream_object.id_cache.capacity,
        )
        stream_object._next_render_id = capacity - 1

        # Много циклов create/remove на последнем слоте: без free-list
        # следующая регистрация упёрлась бы в границу capacity.
        for index in range(50):
            game_object = self.make_game(f"unit_{index}")
            payload = self.register(
                stream_object, responses, game_object, f"request_{index}"
            )
            self.assertEqual(payload["render_id"], capacity - 1)
            stream_object._Stream__remove({
                "id": game_object._id,
                "generation": payload["generation"],
            })

        self.assertEqual(list(stream_object._free_render_ids), [capacity - 1])

    def test_stale_remove_and_render_are_ignored(self):
        stream_object, responses, renders = self.make_stream()
        first = self.make_game("first")
        self.register(stream_object, responses, first, "first")
        stream_object._Stream__remove({"id": first._id, "generation": 1})

        second = self.make_game("second")
        second_payload = self.register(stream_object, responses, second, "second")
        generation = second_payload["generation"]
        renders.messages.clear()

        # Команды старого поколения не должны трогать занятый слот.
        stream_object._Stream__remove({
            "id": second._id,
            "generation": generation - 1,
        })
        self.assertIn(second._id, stream_object.objects_cache)

        stream_object._Stream__render({
            "id": second._id,
            "generation": generation - 1,
        })
        self.assertEqual(renders.messages, [])

        stream_object._Stream__render({
            "id": second._id,
            "generation": generation,
        })
        self.assertEqual(len(renders.messages), 1)
        self.assertEqual(renders.messages[0]["command"], "add")
        self.assertEqual(renders.messages[0]["render_id"], 0)

        stream_object._Stream__remove({
            "id": second._id,
            "generation": generation,
        })
        self.assertNotIn(second._id, stream_object.objects_cache)
        self.assertEqual(list(stream_object._free_render_ids), [0])


class IdentityIndexTests(CacheTestCase):
    """Индексы идентичности должны быть O(1)-корзинами, а не перезаписью целых списков."""

    @classmethod
    def setUpClass(cls):
        cls.manager = multiprocessing.Manager()

    @classmethod
    def tearDownClass(cls):
        cls.manager.shutdown()

    def make_stream(self, objects_cache=None, visual_cache=None):
        stream_object = Stream.__new__(Stream)
        stream_object.objects_cache = (
            objects_cache if objects_cache is not None else self.manager.dict()
        )
        stream_object._index_manager = self.manager
        stream_object._index_buckets = {}
        if visual_cache is not None:
            stream_object.visual_cache = visual_cache
        return stream_object

    def test_small_buckets_stay_lists_and_shared_buckets_promote(self):
        visual, identity = self.make_caches()
        stream_object = self.make_stream()
        stream_object.id_cache = identity

        class_key = (Stream.INDEX_PREFIX, "class", "Unit")
        type_key = (Stream.INDEX_PREFIX, "type", "dynamic_object")
        hero_key = (Stream.INDEX_PREFIX, "name", "hero")

        for index in range(INDEX_BUCKET_PROMOTE_AT):
            stream_object._Stream__add_identity_indexes(
                f"unit_{index}",
                {"name": f"unit_{index}", "class": "Unit"},
                "dynamic_object",
            )

        # Общие корзины class/type были переведены в прокси manager.list.
        class_bucket = stream_object.objects_cache[class_key]
        self.assertNotIsInstance(class_bucket, list)
        self.assertEqual(
            class_bucket[:],
            [f"unit_{index}" for index in range(INDEX_BUCKET_PROMOTE_AT)],
        )
        self.assertNotIsInstance(stream_object.objects_cache[type_key], list)

        # Имя, использованное один раз, остаётся обычным списком и не повышается до прокси.
        self.assertNotIn(hero_key, stream_object.objects_cache)
        stream_object._Stream__add_identity_indexes(
            "solo", {"name": "hero", "class": "Unit"}, "dynamic_object"
        )
        self.assertIsInstance(stream_object.objects_cache[hero_key], list)

        stream_object._Stream__add_identity_indexes(
            "extra", {"name": "extra", "class": "Unit"}, "dynamic_object"
        )
        self.assertEqual(len(class_bucket), INDEX_BUCKET_PROMOTE_AT + 2)

        added_ids = [
            f"unit_{index}" for index in range(INDEX_BUCKET_PROMOTE_AT)
        ] + ["solo", "extra"]
        for render_id, object_id in enumerate(added_ids):
            identity.set_many(render_id, {
                "name": "hero" if object_id == "solo" else object_id,
                "class": "Unit",
                "type": "dynamic_object",
                "id": object_id,
            })

        # Удаляем из обычной корзины (name) и из повышенных (class/type).
        stream_object._Stream__remove_identity_indexes("unit_0", 0)
        stream_object._Stream__remove_identity_indexes("solo", 16)
        self.assertEqual(class_bucket[:], added_ids[1:16] + ["extra"])
        self.assertNotIn(hero_key, stream_object.objects_cache)

        # Пустые повышенные корзины удаляются из индекса и локального кэша.
        for render_id, object_id in enumerate(added_ids):
            if object_id in {"unit_0", "solo"}:
                continue
            stream_object._Stream__remove_identity_indexes(object_id, render_id)
        self.assertNotIn(class_key, stream_object.objects_cache)
        self.assertNotIn(type_key, stream_object.objects_cache)
        self.assertNotIn(class_key, stream_object._index_buckets)

    def test_layer_buckets_track_add_and_remove(self):
        visual, identity = self.make_caches()
        stream_object = self.make_stream(visual_cache=visual)
        stream_object.id_cache = identity

        layers = {0: 0.0, 1: 2.0, 2: 2.0, 3: 5.0}
        for render_id, layer in layers.items():
            object_id = f"unit_{render_id}"
            visual.set_many(render_id, {"layer": layer})
            identity.set_many(render_id, {
                "name": object_id,
                "class": "Unit",
                "type": "dynamic_object",
                "id": object_id,
            })
            stream_object._Stream__add_index_value(
                (Stream.INDEX_PREFIX, "layer", layer), object_id
            )

        layer_two = (Stream.INDEX_PREFIX, "layer", 2.0)
        self.assertEqual(
            stream_object.objects_cache[layer_two], ["unit_1", "unit_2"]
        )

        stream_object._Stream__remove_identity_indexes("unit_1", 1)
        self.assertEqual(stream_object.objects_cache[layer_two], ["unit_2"])

        stream_object._Stream__remove_identity_indexes("unit_2", 2)
        self.assertNotIn(layer_two, stream_object.objects_cache)
        # Остальные слои не затрагиваются.
        self.assertEqual(
            stream_object.objects_cache[
                (Stream.INDEX_PREFIX, "layer", 5.0)
            ],
            ["unit_3"],
        )

    def test_find_object_layer_uses_index_bucket(self):
        visual, identity = self.make_caches()
        visual.set_many(0, {"layer": 2.0})
        identity.set_many(0, {
            "name": "wall", "class": "Wall",
            "type": "static_object", "id": "wall",
        })
        objects_cache = self.manager.dict()
        objects_cache[(Stream.INDEX_PREFIX, "layer", "__ready__")] = True
        objects_cache[(Stream.INDEX_PREFIX, "layer", 2.0)] = (
            self.manager.list(["wall"])
        )
        objects_cache["wall"] = {
            "_object_id": 0,
            "_render_id": 0,
            "dict_data": {
                "object_type": "static_object",
                "_components": ["id", "visual"],
            },
        }
        probe = script.__new__(script)
        probe.__dict__["_script__objects_cache"] = objects_cache
        probe.__dict__["_script__id_cache"] = identity
        probe.__dict__["_script__visual_cache"] = visual
        probe.__dict__["_script__dependencies"] = {
            "objects_cache": objects_cache,
            "lock": self.manager.Lock(),
            "visual_cache": visual,
            "id_cache": identity,
        }

        found = script.find_object(probe, "layer", 2.0)
        self.assertIsNotNone(found)
        self.assertEqual(found._id, "wall")

    def test_find_object_layer_falls_back_without_index_marker(self):
        visual, identity = self.make_caches()
        visual.set_many(0, {"layer": 2.0})
        identity.set_many(0, {
            "name": "wall", "class": "Wall",
            "type": "static_object", "id": "wall",
        })
        objects_cache = {
            "wall": {
                "_object_id": 0,
                "_render_id": 0,
                "dict_data": {
                    "object_type": "static_object",
                    "_components": ["id", "visual"],
                },
            },
        }
        probe = script.__new__(script)
        probe.__dict__["_script__objects_cache"] = objects_cache
        probe.__dict__["_script__id_cache"] = identity
        probe.__dict__["_script__visual_cache"] = visual
        probe.__dict__["_script__dependencies"] = {
            "objects_cache": objects_cache,
            "lock": threading.Lock(),
            "visual_cache": visual,
            "id_cache": identity,
        }

        self.assertIsNone(script.find_object(probe, "layer", 5.0))
        found = script.find_object(probe, "layer", 2.0)
        self.assertIsNotNone(found)
        self.assertEqual(found._id, "wall")

    def test_find_object_snapshots_manager_list_bucket(self):
        visual, identity = self.make_caches()
        identity.set_many(0, {
            "name": "hero", "class": "Unit", "type": "dynamic_object", "id": "a",
        })
        visual.set_many(0, {
            "sprite": "None", "x": 0.0, "y": 0.0, "scale": 1.0,
            "rotation": 0.0, "alpha": 1.0, "layer": 0.0,
        })
        objects_cache = self.manager.dict()
        objects_cache[(Stream.INDEX_PREFIX, "class", "Unit")] = (
            self.manager.list(["a"])
        )
        objects_cache["a"] = {
            "_object_id": 0,
            "_render_id": 0,
            "dict_data": {
                "object_type": "dynamic_object",
                "_components": ["id", "visual"],
            },
        }
        deps = {
            "objects_cache": objects_cache,
            "lock": self.manager.Lock(),
            "visual_cache": visual,
            "id_cache": identity,
        }
        probe = script.__new__(script)
        probe.__dict__["_script__objects_cache"] = objects_cache
        probe.__dict__["_script__id_cache"] = identity
        probe.__dict__["_script__visual_cache"] = visual
        probe.__dict__["_script__dependencies"] = deps

        found = script.find_object(probe, "class", "Unit")
        self.assertIsNotNone(found)
        self.assertEqual(found._object_id, 0)


class FindObjectComponentTests(CacheTestCase):
    def test_find_object_preserves_top_level_component(self):
        visual, identity = self.make_caches()
        identity.set_many(0, {
            "name": "tilt_trigger",
            "class": "TiltTrigger",
            "type": "static_object",
            "id": "object-id",
        })
        visual.set_many(0, {
            "sprite": "None",
            "x": 10.0,
            "y": 20.0,
            "scale": 1.0,
            "rotation": 0.0,
            "alpha": 1.0,
            "layer": 0.0,
        })
        objects_cache = {
            ("__engine_index__", "name", "tilt_trigger"): ["object-id"],
            "object-id": {
                "_object_id": 7,
                "_render_id": 0,
                "_generation": 3,
                "dict_data": {
                    "object_type": "static_object",
                    "_components": ["id", "visual", "rotate_on_click"],
                    "rotate_on_click": {"degrees": 90},
                },
            },
        }
        deps = {
            "objects_cache": objects_cache,
            "lock": threading.Lock(),
            "visual_cache": visual,
            "id_cache": identity,
        }

        probe = script.__new__(script)
        probe.__dict__["_script__objects_cache"] = objects_cache
        probe.__dict__["_script__id_cache"] = identity
        probe.__dict__["_script__visual_cache"] = visual
        probe.__dict__["_script__dependencies"] = deps

        found = script.find_object(probe, "name", "tilt_trigger")
        self.assertIsNotNone(found)
        self.assertEqual(found._object_id, 7)
        self.assertEqual(found._generation, 3)
        self.assertEqual(found.component_specs, [{
            "name": "rotate_on_click",
            "path": "access/components/rotate_on_click.py",
            "properties": {"degrees": 90},
        }])


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
