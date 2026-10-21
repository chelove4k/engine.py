from __future__ import annotations

import multiprocessing
import queue
import threading
import time
from collections import deque
from pathlib import Path

import arcade
import pyglet

import console
import stream
from memory_lib import ObjectCache, ValueCache


class Game(arcade.Window):
    POSITION_INTERPOLATION_DELAY = 0.02
    POSITION_HISTORY_SIZE = 8
    SIMULATION_TICK = 1 / 100
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
    ):
        super().__init__(
            width,
            height,
            title,
            vsync=False,
            update_rate=1 / 120,
            draw_rate=1 / 120,
        )

        self.Time = Time if isinstance(Time, ValueCache) else ValueCache(Time)
        self.clock_origin = (
            clock_origin
            if clock_origin is not None
            else time.monotonic() - self.Time.get()
        )

        self.render_in_stream = data[0]
        self.main_in_stream = data[1]
        self.object_cache = data[2]
        self.visual_cache = ObjectCache(*data[3])
        self.camera_target_cache = data[4]
        self.camera_scale_cache = data[5]

        self.renders = arcade.SpriteList()
        self._sprites_by_render_id = {}
        self._world_data_by_render_id = {}
        self._position_history_by_render_id = {}
        self._waiting_updates = {}
        self._textures = {}
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
            'key': key_name
        })

    def on_key_release(self, key, modifiers):

        try:
            key_name = (pyglet.window.key.symbol_string(key).upper().replace('_', ''))
        except ValueError:
            key_name = str(key)

        self.main_in_stream.put({
            'command': 'input',
            'type': 'key_up',
            'key': key_name
        })

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
                    self._add_sprite(data)

                case 'remove':
                    self._remove_sprite(data)

                case 'clear':
                    self._clear(data)

        # Check active sprites only; scanning the full cache capacity each frame is costly.
        for render_id, sprite in self._sprites_by_render_id.items():
            entry = self.visual_cache.get_changed_values(render_id)
            if entry and sprite is not None:
                self._apply_update(render_id, sprite, entry)

    def _read_camera_state(self):
        self._camera_target_id = self.camera_target_cache.get()
        scale = max(0.01, self.camera_scale_cache.get())
        if self.camera.zoom != scale:
            self.camera.zoom = scale

    def _record_position(self, render_id, position, timestamp=None):
        if timestamp is None:
            timestamp = time.monotonic() - self.clock_origin
        timestamp = float(timestamp)
        x, y = float(position[0]), float(position[1])
        history = self._position_history_by_render_id.get(render_id)
        if history is None:
            history = deque(maxlen=self.POSITION_HISTORY_SIZE)
            self._position_history_by_render_id[render_id] = history
        if history:
            last_time, last_x, last_y = history[-1]
            if timestamp < last_time:
                return
            if timestamp == last_time:
                history[-1] = (timestamp, x, y)
                return
            if timestamp - last_time > self.SIMULATION_TICK * 1.5:
                held_state_time = timestamp - self.SIMULATION_TICK
                if held_state_time > last_time:
                    history.append((held_state_time, last_x, last_y))
        history.append((timestamp, x, y))

    def _interpolated_position(self, render_id, render_time):
        history = self._position_history_by_render_id.get(render_id)
        if not history:
            return None

        samples = iter(history)
        previous = next(samples)
        if render_time <= previous[0]:
            return previous[1], previous[2]

        for current in samples:
            if render_time <= current[0]:
                interval = current[0] - previous[0]
                if interval <= 0:
                    return current[1], current[2]
                fraction = (render_time - previous[0]) / interval
                return (
                    previous[1] + (current[1] - previous[1]) * fraction,
                    previous[2] + (current[2] - previous[2]) * fraction,
                )
            previous = current
        return previous[1], previous[2]

    def _interpolate_sprite_positions(self, render_time):
        for render_id, sprite in self._sprites_by_render_id.items():
            position = self._interpolated_position(render_id, render_time)
            if position is not None:
                sprite.center_x, sprite.center_y = position

    def _track_camera_target(self, render_time):
        if self._camera_target_id >= 0:
            position = self._interpolated_position(self._camera_target_id, render_time)
            if position is not None:
                self.camera.position = position

    def _resolve_asset_path(self, path):
        asset_path = Path(path).expanduser()
        if not asset_path.is_absolute():
            asset_path = self.PROJECT_DIR / asset_path
        return str(asset_path.resolve())

    def _clear(self, data):
        self.renders.clear()

        self._sprites_by_render_id.clear()
        self._world_data_by_render_id.clear()
        self._position_history_by_render_id.clear()
        self._waiting_updates.clear()

    def _add_sprite(self, data):

        render_id = data['render_id']
        self._remove_sprite(data)

        world_data = {
            'position': (data['x'], data['y']),
            'scale':    data.get('scale',1.0),
            'rotation': data.get('rotation', 0.0),
            'alpha':    data.get('alpha', 1.0),
        }

        image_path = self._resolve_asset_path(data['path'])
        texture = self._textures.get(image_path)
        if texture is None:
            texture = arcade.load_texture(image_path)
            self._textures[image_path] = texture
        sprite = arcade.Sprite(
            path_or_texture=texture,
            scale=world_data['scale'],
            angle=world_data['rotation'],
        )

        self.renders.append(sprite)

        self._sprites_by_render_id[render_id] = sprite
        self._world_data_by_render_id[render_id] = world_data
        self._record_position(
            render_id,
            world_data['position'],
            data.get('sim_time', self.Time.get()),
        )

        self._apply_world_transform(sprite, world_data)

    def _remove_sprite(self, data):

        render_id = data['render_id']
        sprite = (self._sprites_by_render_id.pop(render_id, None))

        if sprite is not None:
            self.renders.remove(sprite)

        self._world_data_by_render_id.pop(render_id, None)
        self._position_history_by_render_id.pop(render_id, None)
        self._waiting_updates.pop(render_id, None)

    def _apply_update(self, id, sprite, object_data):
        world_data = self._world_data_by_render_id.get(id)
        if world_data is None:
            return
        if 'sprite' in object_data:
            path = self._resolve_asset_path(object_data['sprite'])
            texture = self._textures.get(path)

            if texture is None:
                texture = arcade.load_texture(path)
                self._textures[path] = texture

            sprite.texture = texture

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

        if 'x' in object_data or 'y' in object_data:
            self._record_position(
                id,
                world_data['position'],
                object_data.get('sim_time', self.Time.get()),
            )

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

        render_time = (
            time.monotonic()
            - self.clock_origin
            - self.POSITION_INTERPOLATION_DELAY
        )
        self._interpolate_sprite_positions(render_time)
        self._track_camera_target(render_time)

        self.camera.use()
        self.renders.draw()

        self.gui_camera.use()
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

def tact_counter(start, tact_time, Time, stop_event):

    while not stop_event.wait(tact_time):
        Time.set(round(time.monotonic() - start, 5))

def main():

    render_manager = multiprocessing.Manager()
    main_manager = multiprocessing.Manager()
    objects_manager = multiprocessing.Manager()
    universal_manager = multiprocessing.Manager()


    render_in_stream = RenderInQueue(render_manager)
    render_out_stream = RenderOutQueue(render_manager)
    main_in_stream = MainInQueue(main_manager)
    main_out_stream = MainOutQueue(main_manager) 

    procces_in_stream = ProccesInQueue(objects_manager)

    objects_cache = objects_manager.dict()
    object_lock = objects_manager.Lock()
    visual_struct =  {
        'x' : float,
        'y' : float,
        'sim_time': float,
        'rotation' : float,
        'scale' : float,
        'alpha' : float,
        'sprite' : str
    }
    visual_locks = [multiprocessing.Lock() for _ in range(64)]
    raw_visual_cache = VisualCache(1024 * 256, visual_struct, visual_locks)
    visual_cache = [raw_visual_cache.key, visual_struct, visual_locks]

    id_struct =  {
        'name' : str,
        'layer' : str,
        'type' : str,
        'class' : str,
        'id' : str,
    }
    id_locks = [multiprocessing.Lock() for _ in range(64)]
    raw_id_cache = IdCache(1024*256, id_struct, id_locks)
    id_cache = [raw_id_cache.key, id_struct, id_locks]

    proccess_status_info = universal_manager.list()
    keyboard_input = universal_manager.list()
    raw_time = ValueCache(0.0)
    time_key = raw_time.name
    camera_target_cache = ValueCache(-1)
    camera_scale_cache = ValueCache(1.0)
    camera_state = [camera_target_cache.name, camera_scale_cache.name]
    lock = universal_manager.Lock()

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
            procces_in_stream,
            lock,
            keyboard_input,
            visual_cache,
            id_cache,
            object_lock,
            camera_state
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
            id_cache
        ),
        daemon=True,
    ).start()

    tact_time = 1 / 100
    game_clock_origin = time.monotonic()
    tact_stop_event = threading.Event()
    tact_thread = threading.Thread(
        target=tact_counter,
        args=(
            game_clock_origin,
            tact_time,
            raw_time,
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
        ], time_key, width=400, height=400, clock_origin=game_clock_origin)
        arcade.run()

    finally:
        tact_stop_event.set()
        tact_thread.join()

        if logic_process.is_alive():
            logic_process.terminate()

        logic_process.join(timeout=2)

        objects_manager.shutdown()
        render_manager.shutdown()
        main_manager.shutdown()
        universal_manager.shutdown()
        raw_visual_cache.close()
        raw_id_cache.close()
        raw_time.close()
        camera_target_cache.close()
        camera_scale_cache.close()


if __name__ == '__main__':
    main()
