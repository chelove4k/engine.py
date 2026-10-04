from __future__ import annotations

import multiprocessing
import queue
import threading
import time

import arcade
import pyglet

import console
import stream


class Camera:
    def __init__(self, width: int, height: int):
        self.x = 0.0
        self.y = 0.0

        self.zoom = 1.0

        self.width = width
        self.height = height

    @property
    def screen_center_x(self):
        return self.width / 2

    @property
    def screen_center_y(self):
        return self.height / 2

    def world_to_screen(self, x: float, y: float):

        screen_x = (x - self.x) * self.zoom + self.screen_center_x
        screen_y = (y - self.y) * self.zoom + self.screen_center_y
        return screen_x, screen_y

    def set_position(self, x: float, y: float):
        self.x = x
        self.y = y

    def set_zoom(self, zoom: float):
        self.zoom = max(0.01, zoom)

    def resize(self, width: int, height: int):
        self.width = width
        self.height = height


class Game(arcade.Window):
    def print(self, *args):
        print(f"[RENDER  : {self.Time.value:.4f}]", *args, flush=True)

    def __init__(self, data, Time, width=1280, height=720, title='Arcade Window'):
        super().__init__(
            width,
            height,
            title,
            vsync=False,
            update_rate=1 / 120,
            draw_rate=1 / 120,
        )

        self.start = time.time()
        self.Time = Time

        self.render_in_stream = data[0]
        self.main_in_stream = data[1]
        self.object_cache = data[2]
        self.camera_in_stream = data[3]

        self.camera = Camera(
            width=self.width,
            height=self.height
        )

        self.renders = arcade.SpriteList()
        self._sprites_by_render_id = {}
        self._world_data_by_render_id = {}
        self._waiting_updates = {}
        self._textures = {}

        self.fps_text = arcade.Text(
            text='FPS: 0',
            x=10,
            y=height - 25,
            color=arcade.color.GREEN,
            font_size=14,
            bold=True,
        )

    def on_update(self, delta_time):

        if delta_time > 0:
            self.fps_text.text = (
                f'FPS: {round(1 / delta_time)}'
            )

        self.fps_text.y = self.height - 25

        self._camera_queue()
        self._render_queue()

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

        for _, object in self.object_cache.items():

            if object["_is_changed"]:

                sprite = self._sprites_by_render_id.get(object["_render_id"])

                if sprite is None:
                    continue
                self._apply_update(sprite, object)

    def _camera_queue(self):
        while True:
            try:
                data = self.camera_in_stream.get_nowait()
            except queue.Empty:
                break
            except (EOFError, BrokenPipeError, OSError):
                return
            changed = False

            self.print(data)

            if 'x' in data:
                self.camera.x = data['x']
                changed = True

            if 'y' in data:
                self.camera.y = data['y']
                changed = True

            if 'zoom' in data:
                self.camera.set_zoom(data['zoom'])
                changed = True

            if changed:
                self._update_all_screen_positions()

    def _update_all_screen_positions(self):

        for render_id, sprite in (self._sprites_by_render_id.items()):

            world_data = (self._world_data_by_render_id.get(render_id))
            if world_data is None:
                continue

            self._apply_world_transform(sprite, world_data)

    def _clear(self, data):
        self.renders.clear()

        self._sprites_by_render_id.clear()
        self._world_data_by_render_id.clear()
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

        sprite = arcade.Sprite(
            path_or_texture=data['path'],
            scale=world_data['scale'],
            angle=world_data['rotation'],
        )

        self.renders.append(sprite)

        self._sprites_by_render_id[render_id] = sprite
        self._world_data_by_render_id[render_id] = world_data

        self._apply_world_transform(sprite, world_data)

    def _remove_sprite(self, data):

        render_id = data['render_id']
        sprite = (self._sprites_by_render_id.pop(render_id, None))

        if sprite is not None:
            self.renders.remove(sprite)

        self._world_data_by_render_id.pop(render_id, None)
        self._waiting_updates.pop(render_id, None)

    def _apply_update(self, sprite, object):

        render_id = object["_render_id"]
        world_data = self._world_data_by_render_id.get(render_id)

        if world_data is None:
            return

        if 'sprite' in object['changes']:
            path = object['_visual']['sprite']
            texture = self._textures.get(path)

            if texture is None:
                texture = arcade.load_texture(path)
                self._textures[path] = texture

            sprite.texture = texture

        if 'position' in object['changes']:
            position = object['_visual']['position']
            world_data['position'] = position.x, position.y

        if 'scale' in object['changes']:
            world_data['scale'] = object['_visual']['scale']

        if 'rotation' in object['changes']:
            world_data['rotation'] = object['_visual']['rotation']

        if 'alpha' in object['changes']:
            world_data['alpha'] = object['_visual']['alpha']
        object['_is_changed'] = False

        self._apply_world_transform(sprite, world_data)

    def _apply_world_transform( self, sprite, world_data):

        world_x, world_y = world_data['position']
        screen_x, screen_y = self.camera.world_to_screen(world_x, world_y)

        sprite.center_x = screen_x
        sprite.center_y = screen_y
        sprite.scale = world_data['scale'] * self.camera.zoom
        sprite.angle = world_data['rotation']
        sprite.alpha = int(world_data['alpha'] * 255)

    def on_resize(self, width, height):

        super().on_resize(width, height)

        self.camera.resize(width, height)
        self._update_all_screen_positions()

    def on_draw(self):

        self.clear()

        self.renders.draw()
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

class CameraQueue(ManagedQueue):
    pass


def tact_counter(start, tact_time, Time):

    while True:
        time.sleep(tact_time)
        Time.value = time.time() - start

def main():

    manager = multiprocessing.Manager()

    render_in_stream = RenderInQueue(manager)
    render_out_stream = RenderOutQueue(manager)

    main_in_stream = MainInQueue(manager)
    main_out_stream = MainOutQueue(manager)

    procces_in_stream = ProccesInQueue(manager)
    camera_queue = CameraQueue(manager)

    keyboard_input = manager.list()
    objects_cache = manager.dict()
    managers_stream = manager.dict()
    proccess_status_info = manager.list()
    Time = manager.Value('d', 0.0)
    lock = manager.Lock()

    logic_process = multiprocessing.Process(
        target=stream.Stream,
        args=(
            Time,
            render_in_stream,
            render_out_stream,
            main_in_stream,
            main_out_stream,
            objects_cache,
            proccess_status_info,
            procces_in_stream,
            managers_stream,
            lock,
            keyboard_input,
            camera_queue,
        ),
        daemon=False,
    )
    logic_process.start()

    threading.Thread(
        target=console.console,
        args=(
            render_in_stream,
            objects_cache,
            proccess_status_info
        ),
        daemon=True,
    ).start()

    tact_time = 1 / 100
    threading.Thread(
        target=tact_counter,
        args=(
            time.time(),
            tact_time,
            Time
        ),
        daemon=True,
    ).start()

    try:
        Game([render_in_stream, main_in_stream, objects_cache, camera_queue], Time, width=400, height=400)
        arcade.run()

    finally:
        if logic_process.is_alive():
            logic_process.terminate()

        logic_process.join(timeout=2)
        manager.shutdown()


if __name__ == '__main__':
    main()