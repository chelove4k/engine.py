from __future__ import annotations

import multiprocessing
import os
import queue
import threading
import time
import traceback
from collections import deque
from pathlib import Path

import arcade
import pyglet

import console
import stream
from collision import transform_polygon, validate_convex_polygon
from config import (
    FIXED_DT,
    POSITION_HISTORY_SIZE as CONFIG_POSITION_HISTORY_SIZE,
    POSITION_INTERPOLATION_DELAY as CONFIG_POSITION_INTERPOLATION_DELAY,
    SCRIPT_UPDATE_PERIOD,
    SNAPSHOT_ACTIVE_WINDOW,
)
from memory_lib import ObjectCache, ValueCache


NO_SPRITE = "None"


def _is_no_sprite(path):
    """Return True when the scene uses the textual no-sprite marker."""

    return path is None or (
        isinstance(path, str) and path.strip().casefold() == NO_SPRITE.casefold()
    )


class Game(arcade.Window):
    POSITION_INTERPOLATION_DELAY = CONFIG_POSITION_INTERPOLATION_DELAY
    POSITION_HISTORY_SIZE = CONFIG_POSITION_HISTORY_SIZE
    SIMULATION_TICK = FIXED_DT
    ACTIVE_SNAPSHOT_WINDOW = SNAPSHOT_ACTIVE_WINDOW
    PROJECT_DIR = Path(__file__).resolve().parent

    def print(self, *args):
        print(f"[RENDER  : {self.Time.get():.4f}]", *args, flush=True)

    def __init__(
        self,
        data,
        Time,
        width=1280,
        height=720,
        title='Arcade Window',
        clock_origin=None,
        tick_cache=None,
        overlay_settings=None,
    ):
        super().__init__(
            width,
            height,
            title,
            vsync=False,
            update_rate=SCRIPT_UPDATE_PERIOD,
            draw_rate=SCRIPT_UPDATE_PERIOD,
        )

        self.Time = Time if isinstance(Time, ValueCache) else ValueCache(Time)
        self.tick_cache = (
            tick_cache
            if isinstance(tick_cache, ValueCache)
            else (ValueCache(tick_cache) if tick_cache is not None else None)
        )
        self.clock_origin = (
            clock_origin
            if clock_origin is not None
            else time.monotonic() - self.Time.get()
        )

        self.render_in_stream = data[0]
        self.main_in_stream = data[1]
        self.object_cache = data[2]
        self.overlay_settings = overlay_settings or console.OverlaySettings()
        self.visual_cache = ObjectCache(*data[3])
        self.camera_target_cache = data[4]
        self.camera_scale_cache = data[5]

        self.renders = arcade.SpriteList()
        self._sprites_by_render_id = {}
        self._render_order = []
        self._render_order_dirty = False
        self._world_data_by_render_id = {}
        self._position_history_by_render_id = {}
        self._latest_tick_by_render_id = {}
        self._last_tick_update_by_render_id = {}
        self._scripted_render_ids = set()
        self._waiting_updates = {}
        self._textures = {}
        self._failed_textures = set()
        self._fallback_texture = arcade.make_soft_square_texture(
            64, (255, 0, 255, 255), center_alpha=255, outer_alpha=255
        )
        self._no_sprite_texture = arcade.Texture.create_empty(
            "__engine_no_sprite__", (1, 1)
        )
        self._colliders_by_render_id = {}
        self._info_text_by_render_id = {}
        self._camera_target_id = -1
        self._fps_elapsed = 0.0

        self.fps_text = arcade.Text(
            text='FPS: 0',
            x=10,
            y=height - 25,
            color=arcade.color.GREEN,
            font_size=14,
            bold=True,
        )

        self.camera = arcade.Camera2D()
        self.gui_camera = arcade.Camera2D()

    def on_update(self, delta_time):
        self._fps_elapsed += delta_time
        if self._fps_elapsed >= 0.25 and delta_time > 0:
            self.fps_text.text = (
                f'FPS: {round(1 / delta_time)}'
            )
            self._fps_elapsed %= 0.25

        fps_y = self.height - 25
        if self.fps_text.y != fps_y:
            self.fps_text.y = fps_y

        self._render_queue()
        self._read_camera_state()



    def on_key_press(self, key, modifiers):
        try:
            key_name = (pyglet.window.key.symbol_string(key).upper().replace('_', ''))
        except ValueError:
            key_name = str(key)

        self.main_in_stream.put({
            'command': 'input',
            'type': 'key_down',
            'key': key_name,
            'modifiers': modifiers,
        })

    def on_key_release(self, key, modifiers):

        try:
            key_name = (pyglet.window.key.symbol_string(key).upper().replace('_', ''))
        except ValueError:
            key_name = str(key)

        self.main_in_stream.put({
            'command': 'input',
            'type': 'key_up',
            'key': key_name,
            'modifiers': modifiers,
        })

    @staticmethod
    def _mouse_button_name(button):
        return {1: "left", 2: "middle", 4: "right"}.get(button, str(button))

    def _sort_render_layers(self):
        if not self._render_order_dirty:
            return
        self.renders.sort(
            key=lambda sprite: (
                self._world_data_by_render_id[sprite._engine_render_id]["layer"],
                -sprite._engine_render_id,
            )
        )
        self._render_order = [sprite._engine_render_id for sprite in self.renders]
        self._render_order_dirty = False

    def _send_mouse_event(self, event_type, x, y, button=None, dx=0, dy=0, scroll_x=0, scroll_y=0):
        world_position = self.camera.unproject((x, y))
        point = (world_position.x, world_position.y)
        target_render_id = None
        self._sort_render_layers()
        for render_id in reversed(self._render_order):
            sprite = self._sprites_by_render_id[render_id]
            if sprite.alpha > 0 and sprite.collides_with_point(point):
                target_render_id = render_id
                break
        self.main_in_stream.put({
            "command": "input",
            "type": event_type,
            "render_id": target_render_id,
            "x": point[0],
            "y": point[1],
            "screen_x": x,
            "screen_y": y,
            "button": self._mouse_button_name(button) if button is not None else None,
            "delta_x": dx,
            "delta_y": dy,
            "scroll_x": scroll_x,
            "scroll_y": scroll_y,
        })

    def on_mouse_press(self, x, y, button, modifiers):
        self._send_mouse_event("mouse_down", x, y, button)

    def on_mouse_release(self, x, y, button, modifiers):
        self._send_mouse_event("mouse_up", x, y, button)

    def on_mouse_motion(self, x, y, dx, dy):
        self._send_mouse_event("mouse_move", x, y, dx=dx, dy=dy)

    def on_mouse_drag(self, x, y, dx, dy, buttons, modifiers):
        self._send_mouse_event("mouse_move", x, y, dx=dx, dy=dy)

    def on_mouse_scroll(self, x, y, scroll_x, scroll_y):
        self._send_mouse_event("mouse_scroll", x, y, scroll_x=scroll_x, scroll_y=scroll_y)

    def _render_queue(self):
        while True:
            try:
                data = self.render_in_stream.get_nowait()
            except queue.Empty:
                break
            except (EOFError, BrokenPipeError, OSError):
                return
 
            if data == 'rshow':
                print(self._sprites_by_render_id)
                continue

            if type(data) != dict:
                continue

            command = data.get('command')
            match command:

                case 'add':
                    try:
                        self._add_sprite(data)
                    except Exception:
                        # A bad asset or malformed render command must not kill
                        # the whole render loop.
                        self.print("Ошибка добавления спрайта")
                        traceback.print_exc()

                case 'remove':
                    self._remove_sprite(data)

                case 'clear':
                    self._clear(data)

        # Check active sprites only; scanning the full cache capacity each frame is costly.
        for render_id, sprite in list(self._sprites_by_render_id.items()):
            entry = self.visual_cache.get_changed_values(render_id)
            if entry and sprite is not None:
                try:
                    self._apply_update(render_id, sprite, entry)
                except Exception:
                    self.print(f"Ошибка обновления спрайта {render_id}")
                    traceback.print_exc()
        self._sort_render_layers()

    def _read_camera_state(self):
        self._camera_target_id = self.camera_target_cache.get()
        scale = max(0.01, self.camera_scale_cache.get())
        if self.camera.zoom != scale:
            self.camera.zoom = scale

    def _current_tick(self):
        """Return the current shared simulation tick as a float."""

        if self.tick_cache is not None:
            return float(self.tick_cache.get())
        return (time.monotonic() - self.clock_origin) / self.SIMULATION_TICK

    def _record_position(self, render_id, position, tick=None):
        """Store a position sample keyed by the simulation tick.

        All entities use the same tick domain, so there are no independent
        wall-clock timestamps that could drift apart.
        """

        if tick is None:
            tick = self._current_tick()
        tick = int(tick)
        x, y = float(position[0]), float(position[1])
        history = self._position_history_by_render_id.get(render_id)
        if history is None:
            history = deque(maxlen=self.POSITION_HISTORY_SIZE)
            self._position_history_by_render_id[render_id] = history
        if history:
            last_tick, last_x, last_y = history[-1]
            if tick < last_tick:
                return
            if tick == last_tick:
                history[-1] = (tick, x, y)
                return
        history.append((tick, x, y))

    def _position_at_tick(self, render_id, query_tick):
        """Return the position of an entity at one shared simulation tick."""

        history = self._position_history_by_render_id.get(render_id)
        if not history:
            return None

        previous = history[0]
        if query_tick <= previous[0]:
            return previous[1], previous[2]

        for current in history:
            if query_tick <= current[0]:
                interval = current[0] - previous[0]
                if interval <= 0:
                    return current[1], current[2]
                fraction = (query_tick - previous[0]) / interval
                return (
                    previous[1] + (current[1] - previous[1]) * fraction,
                    previous[2] + (current[2] - previous[2]) * fraction,
                )
            previous = current
        return previous[1], previous[2]

    def _resolve_render_tick(self):
        """Choose the shared tick used by every entity in this frame.

        The usual value is ``current - interpolation delay``.  If a live
        script is late, the tick is clamped to the oldest published tick so
        that no entity is sampled past its newest data.

        Only objects with their own script loop are considered here.  They
        publish a heartbeat every fixed step, so an idle scripted object
        still advances its tick.  Sporadically updated visuals (such as a
        camera target that stopped moving) must not hold back the clock;
        otherwise the whole frame freezes until the activity window expires
        and then jumps forward.
        """

        now = time.monotonic()
        render_tick = (
            (now - self.clock_origin) / self.SIMULATION_TICK
            - self.POSITION_INTERPOLATION_DELAY / self.SIMULATION_TICK
        )

        oldest_tick = None
        for render_id in self._scripted_render_ids:
            latest_tick = self._latest_tick_by_render_id.get(render_id)
            if latest_tick is None:
                continue
            updated_at = self._last_tick_update_by_render_id.get(render_id)
            if updated_at is None:
                continue
            if now - updated_at > self.ACTIVE_SNAPSHOT_WINDOW:
                continue
            if oldest_tick is None or latest_tick < oldest_tick:
                oldest_tick = latest_tick
        if oldest_tick is not None:
            render_tick = min(render_tick, oldest_tick - 1)
        return max(0.0, render_tick)

    def _interpolate_sprite_positions(self, render_tick):
        for render_id, sprite in self._sprites_by_render_id.items():
            position = self._position_at_tick(render_id, render_tick)
            if position is not None:
                sprite.center_x, sprite.center_y = position

    def _track_camera_target(self, render_tick):
        # The tracked object may have been removed without a renderer update
        # (for example while the render queue was being processed). Do not
        # keep a camera on a stale render id.
        if (self._camera_target_id >= 0
                and self._camera_target_id not in self._sprites_by_render_id):
            self._camera_target_id = -1
            self.camera_target_cache.set(-1)

        if self._camera_target_id >= 0:
            position = self._position_at_tick(self._camera_target_id, render_tick)
            if position is not None:
                self.camera.position = position

    def _get_collider_polygon(self, render_id):
        record = self.object_cache.get((stream.Stream.INDEX_PREFIX, "render_id", render_id))
        if record is not None:
            record = self.object_cache.get(record)
        if record is None:
            return None
        transform = record.get("dict_data", {}).get("transform")
        if transform is None:
            return None
        return validate_convex_polygon(transform["polygon"])

    def _draw_collision_overlay(self, render_tick):
        settings = self.overlay_settings.get("collision")
        if settings is None or not settings["enabled"]:
            return
        for render_id, polygon in self._colliders_by_render_id.items():
            world_data = self._world_data_by_render_id.get(render_id)
            if world_data is None:
                continue
            position = self._position_at_tick(render_id, render_tick)
            if position is None:
                position = world_data["position"]
            world_polygon = transform_polygon(
                polygon, position, world_data["rotation"], world_data["scale"]
            )
            if world_polygon:
                arcade.draw_polygon_outline(
                    world_polygon, settings["color"], line_width=settings["width"]
                )

    def _draw_info_categories(self, render_tick):
        categories = ("id", "name", "coords", "scripts", "transform")
        enabled = {
            category: self.overlay_settings.get(f"info_{category}")["enabled"]
            for category in categories
        }
        if self.overlay_settings.get("info")["enabled"]:
            enabled = {category: True for category in categories}
        if not any(enabled.values()):
            return

        for render_id, sprite in self._sprites_by_render_id.items():
            if sprite.alpha <= 0:
                continue
            record_id = self.object_cache.get(
                (stream.Stream.INDEX_PREFIX, "render_id", render_id)
            )
            record = self.object_cache.get(record_id) if record_id is not None else None
            if record is None:
                continue
            data = record.get("dict_data", {})
            identity = data.get("_identity", {})
            world = self._world_data_by_render_id.get(render_id, {})
            position = self._position_at_tick(render_id, render_tick)
            if position is None:
                position = world.get("position", (0, 0))

            lines = []
            if enabled["id"]:
                lines.append(f"ID: {record.get('_id', '?')} / render {render_id}")
            if enabled["name"]:
                lines.append(
                    f"{identity.get('name', '-')} | {identity.get('class', '-')} | "
                    f"{identity.get('type', data.get('object_type', '-'))} | "
                    f"layer: {world.get('layer', 0):g}"
                )
            if enabled["coords"]:
                lines.append(f"x={position[0]:.0f}, y={position[1]:.0f}")
            if enabled["scripts"]:
                paths = data.get("scripting", {}).get("scripts", [])
                lines.append(f"scripts: {', '.join(Path(path).name for path in paths) or '-'}")
            if enabled["transform"]:
                lines.append(
                    f"angle={world.get('rotation', 0):.1f}, "
                    f"scale={world.get('scale', 1):.2f}"
                )
            if not lines:
                continue

            screen_position = self.camera.project(position)
            sprite_top = screen_position.y + sprite.height * self.camera.zoom / 2
            label_y = sprite_top + 5
            label_content = "\n".join(lines)
            label = self._info_text_by_render_id.get(render_id)
            if label is None:
                label = arcade.Text(
                    label_content, screen_position.x, label_y,
                    color=(255, 255, 255, 255), font_size=10,
                    width=max(100, self.width - 12), align="center",
                    multiline=True, anchor_x="center", anchor_y="bottom",
                )
                self._info_text_by_render_id[render_id] = label
            elif label.text != label_content:
                label.text = label_content
            label.x = screen_position.x
            label.y = label_y
            label.draw()

    def _resolve_asset_path(self, path):
        asset_path = Path(path).expanduser()
        if not asset_path.is_absolute():
            asset_path = self.PROJECT_DIR / asset_path
        return str(asset_path.resolve())

    def _texture_for_path(self, path):
        """Resolve a sprite path, or return a transparent texture for "None"."""

        if _is_no_sprite(path):
            return self._no_sprite_texture
        return self._load_texture(self._resolve_asset_path(path))

    def _load_texture(self, path):
        """Load a texture, returning a visible placeholder on failure."""

        texture = self._textures.get(path)
        if texture is not None:
            return texture
        if path in self._failed_textures:
            return self._fallback_texture

        try:
            texture = arcade.load_texture(path)
        except Exception as error:
            self._failed_textures.add(path)
            self.print(f"Не удалось загрузить текстуру {path!r}: {error}")
            return self._fallback_texture

        self._textures[path] = texture
        return texture

    def _clear(self, data):
        self.renders.clear()

        self._sprites_by_render_id.clear()
        self._render_order.clear()
        self._render_order_dirty = False
        self._world_data_by_render_id.clear()
        self._position_history_by_render_id.clear()
        self._latest_tick_by_render_id.clear()
        self._last_tick_update_by_render_id.clear()
        self._scripted_render_ids.clear()
        self._waiting_updates.clear()
        self._colliders_by_render_id.clear()
        self._info_text_by_render_id.clear()
        self._failed_textures.clear()
        self._camera_target_id = -1
        self.camera_target_cache.set(-1)

    def _add_sprite(self, data):

        render_id = data['render_id']
        self._remove_sprite(data, reset_camera=False)

        world_data = {
            'position': (data['x'], data['y']),
            'scale':    data.get('scale',1.0),
            'rotation': data.get('rotation', 0.0),
            'alpha':    data.get('alpha', 1.0),
            'layer':    float(data.get('layer', 0.0)),
        }

        image_path = data.get('path')
        texture = self._texture_for_path(image_path)
        sprite = arcade.Sprite(
            path_or_texture=texture,
            scale=world_data['scale'],
            angle=world_data['rotation'],
        )
        sprite._engine_render_id = render_id

        self.renders.append(sprite)

        self._sprites_by_render_id[render_id] = sprite
        self._world_data_by_render_id[render_id] = world_data
        if data.get('scripted'):
            self._scripted_render_ids.add(render_id)
        self._render_order_dirty = True
        collider_polygon = self._get_collider_polygon(render_id)
        if collider_polygon is not None:
            self._colliders_by_render_id[render_id] = collider_polygon
        self._record_position(
            render_id,
            world_data['position'],
            data.get('tick', round(float(data.get('sim_time', self.Time.get()))
                                   / self.SIMULATION_TICK)),
        )

        self._apply_world_transform(sprite, world_data)

    def _remove_sprite(self, data, reset_camera=True):

        render_id = data['render_id']
        sprite = (self._sprites_by_render_id.pop(render_id, None))

        if sprite is not None:
            self.renders.remove(sprite)
            self._render_order_dirty = True

        if reset_camera and self._camera_target_id == render_id:
            self._camera_target_id = -1
            self.camera_target_cache.set(-1)

        self._world_data_by_render_id.pop(render_id, None)
        self._position_history_by_render_id.pop(render_id, None)
        self._latest_tick_by_render_id.pop(render_id, None)
        self._last_tick_update_by_render_id.pop(render_id, None)
        self._scripted_render_ids.discard(render_id)
        self._waiting_updates.pop(render_id, None)
        self._colliders_by_render_id.pop(render_id, None)
        self._info_text_by_render_id.pop(render_id, None)

    def _apply_update(self, id, sprite, object_data):
        world_data = self._world_data_by_render_id.get(id)
        if world_data is None:
            return
        if 'sprite' in object_data:
            sprite.texture = self._texture_for_path(object_data['sprite'])

        tick = object_data.get('tick')
        if tick is not None:
            tick = int(tick)
            latest_tick = self._latest_tick_by_render_id.get(id)
            if latest_tick is None or tick > latest_tick:
                self._latest_tick_by_render_id[id] = tick
            self._last_tick_update_by_render_id[id] = time.monotonic()

        current_x, current_y = world_data['position']
        
        if 'x' in object_data:
            current_x = object_data['x']

        if 'y' in object_data:
            current_y = object_data['y']
            
        world_data['position'] = (current_x, current_y)

        if 'scale' in object_data:
            world_data['scale'] = object_data['scale']

        if 'rotation' in object_data:
            world_data['rotation'] = object_data['rotation']

        if 'alpha' in object_data:
            world_data['alpha'] = object_data['alpha']

        if 'layer' in object_data:
            world_data['layer'] = float(object_data['layer'])
            self._render_order_dirty = True

        record_tick = tick
        if record_tick is None:
            record_tick = self._latest_tick_by_render_id.get(id)

        if 'x' in object_data or 'y' in object_data:
            self._record_position(id, world_data['position'], record_tick)
        elif tick is not None:
            # Tick heartbeats keep a snapshot for every fixed step even when
            # the entity did not move during that step.
            self._record_position(id, world_data['position'], tick)

        if 'scale' in object_data:
            sprite.scale = world_data['scale']
        if 'rotation' in object_data:
            sprite.angle = world_data['rotation']
        if 'alpha' in object_data:
            sprite.alpha = int(world_data['alpha'] * 255)

    def _apply_world_transform(self, sprite, world_data):
        world_x, world_y = world_data['position']

        sprite.center_x = world_x
        sprite.center_y = world_y
        sprite.scale = world_data['scale']
        sprite.angle = world_data['rotation']
        sprite.alpha = int(world_data['alpha'] * 255)

    def on_draw(self):
        self.clear()

        render_tick = self._resolve_render_tick()
        self._interpolate_sprite_positions(render_tick)
        self._track_camera_target(render_tick)

        self.camera.use()
        self.renders.draw()
        self._draw_collision_overlay(render_tick)

        self.gui_camera.use()
        self._draw_info_categories(render_tick)
        self.fps_text.draw()


class ManagedQueue:

    def __init__(self, manager, maxsize=1024):
        self._queue = manager.Queue(maxsize=maxsize)

    def put(self, item, *args, **kwargs):
        return self._queue.put(item, *args, **kwargs)
    def get(self, *args, **kwargs):
        return self._queue.get(*args, **kwargs)
    def get_nowait(self):
        return self._queue.get(False)
    def empty(self):
        return self._queue.empty()
    def qsize(self):
        return self._queue.qsize()


class RenderInQueue(ManagedQueue):
    pass

class RenderOutQueue(ManagedQueue):
    pass

class MainInQueue(ManagedQueue):
    pass

class MainOutQueue(ManagedQueue):
    pass

class ProccesInQueue(ManagedQueue):
    pass

class VisualCache(ObjectCache):
    pass

class IdCache(ObjectCache):
    pass

def tact_counter(start, tact_time, Time, Tick, stop_event):

    while not stop_event.wait(tact_time):
        elapsed = time.monotonic() - start
        Time.set(round(elapsed, 5))
        Tick.set(int(elapsed / tact_time))

def main():

    manager = multiprocessing.Manager()

    render_in_stream = RenderInQueue(manager)
    render_out_stream = RenderOutQueue(manager)
    main_in_stream = MainInQueue(manager)
    main_out_stream = MainOutQueue(manager)

    cpu_count = os.cpu_count() or 1
    worker_count = max(1, min(4, cpu_count - 1))
    worker_queues = [ProccesInQueue(manager) for _ in range(worker_count)]
    worker_ready = manager.list([False] * worker_count)

    objects_cache = manager.dict()
    object_lock = manager.Lock()
    visual_struct =  {
        'x' : float,
        'y' : float,
        'sim_time': float,
        'tick': int,
        'rotation' : float,
        'scale' : float,
        'alpha' : float,
        'sprite' : str,
        'layer' : float,
    }
    visual_locks = [multiprocessing.Lock() for _ in range(64)]
    raw_visual_cache = VisualCache(1024 * 256, visual_struct, visual_locks)
    visual_cache = [raw_visual_cache.key, visual_struct, visual_locks]

    id_struct =  {
        'name' : str,
        'type' : str,
        'class' : str,
        'id' : str,
    }
    id_locks = [multiprocessing.Lock() for _ in range(64)]
    raw_id_cache = IdCache(1024*256, id_struct, id_locks)
    id_cache = [raw_id_cache.key, id_struct, id_locks]

    proccess_status_info = manager.list()
    keyboard_input = manager.list()
    raw_time = ValueCache(0.0)
    time_key = raw_time.name
    raw_tick = ValueCache(0)
    tick_key = raw_tick.name
    camera_target_cache = ValueCache(-1)
    camera_scale_cache = ValueCache(1.0)
    camera_state = [camera_target_cache.name, camera_scale_cache.name]
    overlay_settings = console.OverlaySettings()
    lock = manager.Lock()

    logic_process = multiprocessing.Process(
        target=stream.Stream,
        args=(
            time_key,
            render_in_stream,
            render_out_stream,
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
        ),
        daemon=False,
    )
    logic_process.start()

    threading.Thread(
        target=console.console,
        args=(
            render_in_stream,
            objects_cache,
            proccess_status_info,
            visual_cache,
            id_cache,
            overlay_settings,
        ),
        daemon=True,
    ).start()

    tact_time = FIXED_DT
    game_clock_origin = time.monotonic()
    tact_stop_event = threading.Event()
    tact_thread = threading.Thread(
        target=tact_counter,
        args=(
            game_clock_origin,
            tact_time,
            raw_time,
            raw_tick,
            tact_stop_event,
        ),
        daemon=True,
    )
    tact_thread.start()

    try:
        Game([
            render_in_stream,
            main_in_stream,
            objects_cache,
            visual_cache,
            camera_target_cache,
            camera_scale_cache,
        ], time_key, width=400, height=400, clock_origin=game_clock_origin,
            tick_cache=raw_tick, overlay_settings=overlay_settings)
        arcade.run()

    finally:
        tact_stop_event.set()
        tact_thread.join()

        if logic_process.is_alive():
            logic_process.terminate()

        logic_process.join(timeout=2)

        manager.shutdown()
        raw_visual_cache.close()
        raw_id_cache.close()
        raw_time.close()
        raw_tick.close()
        camera_target_cache.close()
        camera_scale_cache.close()


if __name__ == '__main__':
    main()
