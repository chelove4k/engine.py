from __future__ import annotations

import multiprocessing
import os
import queue
import threading
import time
import traceback
from bisect import bisect_left
from collections import deque
from pathlib import Path

import arcade
import pyglet

import console
import stream
from collision import transform_polygon, validate_convex_polygon
from config import (
    DIRTY_RING_CAPACITY,
    ENGINE_STATE_SEGMENT_BYTES,
    ENGINE_STATE_STRUCT,
    FIXED_DT,
    KEYBOARD_EVENT_BYTES,
    KEYBOARD_EVENT_STRUCT,
    MOUSE_MOTION_BYTES,
    MOUSE_MOTION_STRUCT,
    POSITION_HISTORY_SIZE as CONFIG_POSITION_HISTORY_SIZE,
    POSITION_INTERPOLATION_DELAY as CONFIG_POSITION_INTERPOLATION_DELAY,
    SCRIPT_UPDATE_PERIOD,
    SNAPSHOT_ACTIVE_WINDOW,
)
from memory_lib import FieldView, ObjectCache, ValueCache


NO_SPRITE = "None"

# Mouse hit-testing used to walk every render id on every motion event.  A
# uniform grid keeps that cost proportional to the sprites near the pointer.
# Dense scenes benefit from small cells: at 32 units a 1000-sprite grid leaves
# only a handful of candidates per query instead of a hundred-plus.
HIT_TEST_CELL_SIZE = 32.0
# Huge sprites (backgrounds) would touch thousands of cells.  Keep them in a
# separate short list that is checked on every query instead.
HIT_TEST_MAX_CELLS = 32
# The info overlay reads identity/script data from the Manager once per second
# instead of once per sprite per frame.  Overlay data is debug output; one
# second of staleness is harmless, while thousands of Manager reads per frame
# were enough to sink the frame rate.
INFO_OVERLAY_CACHE_TTL = 1.0


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
    # An object that has not published a position for this long is static.
    INTERPOLATION_IDLE_TICKS = max(
        1, int(round(ACTIVE_SNAPSHOT_WINDOW / SIMULATION_TICK))
    )
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

        self.Time = Time if hasattr(Time, "get") else ValueCache(Time)
        self.tick_cache = (
            tick_cache
            if tick_cache is None or hasattr(tick_cache, "get")
            else ValueCache(tick_cache)
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
        # When the cache exposes stripe counters, keep active render ids
        # pre-grouped by stripe.  Polling then visits only dirty stripes and
        # does not rebuild a 10k-entry grouping dictionary every frame.
        self._render_ids_by_stripe = None
        if self.visual_cache.dirty_counts is not None:
            self._render_ids_by_stripe = [
                set() for _ in range(len(self.visual_cache.locks))
            ]
        self.camera_target_cache = data[4]
        self.camera_scale_cache = data[5]
        # When present, pointer snapshots are published straight to shared
        # memory.  Sending every coalesced motion event through the Manager
        # queue added a full IPC round-trip per rendered frame.
        self.motion_cache = data[6] if len(data) > 6 else None
        self._motion_sequence = (
            int(self.motion_cache.get(0, "seq"))
            if self.motion_cache is not None else 0
        )

        self.renders = arcade.SpriteList()
        self._sprites_by_render_id = {}
        self._render_order_dirty = False
        self._hit_cells = {}
        self._hit_geometry_by_render_id = {}
        self._hit_bounds_by_render_id = {}
        self._hit_max_extent = 0.0
        self._hit_max_count = 0
        self._hit_large_ids = set()
        self._hit_dirty_ids = set()
        self._world_data_by_render_id = {}
        self._position_history_by_render_id = {}
        self._moving_render_ids = set()
        self._latest_tick_by_render_id = {}
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
        self._info_static_cache = {}
        self._camera_target_id = -1
        self._fps_elapsed = 0.0
        self._pending_mouse_motion = None

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
        self._flush_mouse_motion()



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
        # Rebuilds only happen on add/remove/layer changes.  Each sprite caches
        # its (layer, -render_id) key so a re-sort does not rebuild the same
        # tuple and dictionary lookup for every sprite on every rebuild.  A
        # bucket sort would still have to rebuild arcade's GPU index buffer,
        # so the small Python-side win is not worth touching SpriteList
        # internals.
        if not self._render_order_dirty:
            return
        self.renders.sort(key=lambda sprite: sprite._engine_sort_key)
        self._render_order_dirty = False

    def _hit_drop_cells(self, render_id):
        """Remove one sprite from the spatial hit-test grid."""

        geometry = self._hit_geometry_by_render_id.pop(render_id, None)
        bounds = self._hit_bounds_by_render_id.pop(render_id, None)
        if bounds is not None:
            extent = max(-bounds[0], bounds[1], -bounds[2], bounds[3])
            if extent == self._hit_max_extent and self._hit_max_count:
                self._hit_max_count -= 1
        if geometry is None:
            self._hit_large_ids.discard(render_id)
            return
        cells = self._hit_cells
        for cell_x in range(geometry[0], geometry[2] + 1):
            for cell_y in range(geometry[1], geometry[3] + 1):
                cell = (cell_x, cell_y)
                ids = cells.get(cell)
                if ids is None:
                    continue
                ids.discard(render_id)
                if not ids:
                    cells.pop(cell, None)

    def _hit_remove(self, render_id):
        self._hit_drop_cells(render_id)
        self._hit_dirty_ids.discard(render_id)
        # Recompute only once the last sprite at the current maximum is gone.
        # Ties and re-indexing must not turn unloading a large scene into an
        # O(n^2) full scan of every remaining hit box.
        if self._hit_max_count:
            return
        best_extent = 0.0
        best_count = 0
        for bounds in self._hit_bounds_by_render_id.values():
            extent = max(-bounds[0], bounds[1], -bounds[2], bounds[3])
            if extent > best_extent:
                best_extent = extent
                best_count = 1
            elif extent == best_extent:
                best_count += 1
        self._hit_max_extent = best_extent
        self._hit_max_count = best_count

    def _hit_mark_dirty(self, render_id):
        """Queue a sprite for re-indexing on the next hit-test query.

        Positions change every frame for moving sprites, so indexing eagerly
        would add work even when the pointer never moves.  The dirty set is
        only flushed when somebody actually asks what is under the cursor.
        """

        if render_id in self._sprites_by_render_id:
            self._hit_dirty_ids.add(render_id)

    def _hit_mark_position(self, render_id, x, y):
        """Queue a moved sprite only when its hit box leaves its grid cells.

        Moving within the same cells cannot change which pointer cell contains
        the sprite, so a full remove/re-add round trip is wasted work.  The
        packed geometry stores the centre range that stays inside the indexed
        cells, so checking a moved sprite costs four comparisons instead of
        four floor divisions and multiplications.
        """

        if render_id in self._hit_dirty_ids:
            return
        # Large sprites are checked directly on every query, so their movement
        # never needs to touch the grid.
        if render_id in self._hit_large_ids:
            return

        geometry = self._hit_geometry_by_render_id.get(render_id)
        if geometry is None:
            self._hit_mark_dirty(render_id)
            return

        # The packed tuple already stores the range of centres for which the
        # sprite's hit box stays inside its indexed cells.
        if (geometry[4] <= x < geometry[5]
                and geometry[6] <= y < geometry[7]):
            return
        self._hit_mark_dirty(render_id)

    def _hit_index_dirty(self, point=None):
        dirty = self._hit_dirty_ids
        moving = self._moving_render_ids
        if moving:
            # Position updates and interpolation only track motion.  The
            # grid is refreshed lazily, when a pointer query actually needs
            # it, so an idle pointer does not pay a cell check per sprite
            # per drawn frame.
            sprites = self._sprites_by_render_id
            geometry_by_id = self._hit_geometry_by_render_id
            large_ids = self._hit_large_ids
            if point is None:
                pointer_x = pointer_y = 0.0
                max_extent = float("inf")
            else:
                pointer_x, pointer_y = point
                max_extent = self._hit_max_extent
            # Membership checks are only needed when those sets are non-empty.
            # In the common case both are empty and this loop stays a tight
            # distance test over the moving sprites.
            check_dirty = bool(dirty)
            check_large = bool(large_ids)
            for render_id in moving:
                if check_dirty and render_id in dirty:
                    continue
                if check_large and render_id in large_ids:
                    continue
                sprite = sprites.get(render_id)
                if sprite is None:
                    continue
                x, y = sprite.position
                # A sprite farther from the pointer than the largest hit
                # half-size cannot cover it.  Its stale grid entry cannot
                # change this query; it is refreshed once the pointer comes
                # close, or by the next unpinned index pass.
                if (abs(x - pointer_x) > max_extent
                        or abs(y - pointer_y) > max_extent):
                    continue
                geometry = geometry_by_id.get(render_id)
                if geometry is None:
                    dirty.add(render_id)
                    continue
                if not (geometry[4] <= x < geometry[5]
                        and geometry[6] <= y < geometry[7]):
                    dirty.add(render_id)

        if not dirty:
            return
        self._hit_dirty_ids = set()
        cell_size = HIT_TEST_CELL_SIZE
        for render_id in dirty:
            self._hit_drop_cells(render_id)
            sprite = self._sprites_by_render_id.get(render_id)
            if sprite is None:
                continue

            points = sprite.hit_box.get_adjusted_points()
            if not points:
                continue
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            min_cell_x = int(min_x // cell_size)
            max_cell_x = int(max_x // cell_size)
            min_cell_y = int(min_y // cell_size)
            max_cell_y = int(max_y // cell_size)
            center_x = sprite.center_x
            center_y = sprite.center_y
            local_left = min_x - center_x
            local_right = max_x - center_x
            local_bottom = min_y - center_y
            local_top = max_y - center_y
            self._hit_bounds_by_render_id[render_id] = (
                local_left, local_right, local_bottom, local_top,
            )
            # Largest distance from a sprite centre to its hit-box edge.
            # _hit_test() uses it to skip sprites that cannot cover the
            # pointer before touching their full polygon.
            extent = max(
                -local_left, local_right, -local_bottom, local_top,
            )
            if extent > self._hit_max_extent:
                self._hit_max_extent = extent
                self._hit_max_count = 1
            elif extent == self._hit_max_extent:
                self._hit_max_count += 1
            if ((max_cell_x - min_cell_x + 1)
                    * (max_cell_y - min_cell_y + 1) > HIT_TEST_MAX_CELLS):
                self._hit_large_ids.add(render_id)
                continue

            for cell_x in range(min_cell_x, max_cell_x + 1):
                for cell_y in range(min_cell_y, max_cell_y + 1):
                    self._hit_cells.setdefault(
                        (cell_x, cell_y), set()
                    ).add(render_id)
            # (cell_min_x, cell_min_y, cell_max_x, cell_max_y,
            #  centre_min_x, centre_max_x, centre_min_y, centre_max_y).
            # The centre range is precomputed so a moving sprite only has to
            # compare four floats instead of multiplying cell bounds by the
            # cell size and adding its local hit-box offsets on every query.
            self._hit_geometry_by_render_id[render_id] = (
                min_cell_x,
                min_cell_y,
                max_cell_x,
                max_cell_y,
                min_cell_x * cell_size - local_left,
                (max_cell_x + 1) * cell_size - local_right,
                min_cell_y * cell_size - local_bottom,
                (max_cell_y + 1) * cell_size - local_top,
            )

    def _hit_test(self, point):
        """Return the topmost visible render id under a world-space point."""

        self._hit_index_dirty(point)
        cell_size = HIT_TEST_CELL_SIZE
        cell = (int(point[0] // cell_size), int(point[1] // cell_size))
        candidates = self._hit_cells.get(cell, ())
        best_id = None
        best_key = None
        sprites = self._sprites_by_render_id
        world_data = self._world_data_by_render_id
        bounds_by_id = self._hit_bounds_by_render_id
        pointer_x, pointer_y = point

        def consider(render_id):
            nonlocal best_id, best_key
            sprite = sprites[render_id]
            if sprite.alpha <= 0:
                return
            bounds = bounds_by_id.get(render_id)
            if bounds is not None:
                # Cheap AABB rejection first: one polygon test costs more
                # than every comparison in a dense cell combined.
                offset_x = pointer_x - sprite.center_x
                if offset_x < bounds[0] or offset_x > bounds[1]:
                    return
                offset_y = pointer_y - sprite.center_y
                if offset_y < bounds[2] or offset_y > bounds[3]:
                    return
            if not sprite.collides_with_point(point):
                return
            # Same ordering as the sprite list: highest layer wins, and a
            # lower render id wins a layer tie.
            layer = world_data[render_id]["layer"]
            key = (layer, -render_id)
            if best_key is None or key > best_key:
                best_key = key
                best_id = render_id

        for render_id in candidates:
            consider(render_id)
        for render_id in self._hit_large_ids:
            consider(render_id)
        return best_id

    def _publish_pointer_snapshot(self, event_type, render_id, point,
                                  screen_x, screen_y, dx, dy):
        """Write the latest pointer state to shared memory.

        Returns ``True`` when the snapshot was published.  Motion events then
        do not need the Manager queue at all; discrete events still go there
        for the click/keyboard event lists.
        """

        motion_cache = getattr(self, "motion_cache", None)
        if motion_cache is None:
            return False
        if event_type == "mouse_move":
            self._motion_sequence += 1
        motion_cache.set_many(0, {
            "seq": self._motion_sequence,
            "x": point[0],
            "y": point[1],
            "dx": dx,
            "dy": dy,
            "screen_x": screen_x,
            "screen_y": screen_y,
            "hovered": -1 if render_id is None else int(render_id),
        })
        return True

    def _send_mouse_event(self, event_type, x, y, button=None, dx=0, dy=0, scroll_x=0, scroll_y=0):
        world_position = self.camera.unproject((x, y))
        point = (world_position.x, world_position.y)
        target_render_id = self._hit_test(point)
        published = self._publish_pointer_snapshot(
            event_type, target_render_id, point, x, y, dx, dy
        )
        if event_type == "mouse_move" and published:
            return
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
            "pointer_published": published,
        })

    def _queue_mouse_motion(self, x, y, dx, dy):
        """Coalesce motion events so hit-testing runs once per frame.

        A high-polling-rate mouse can produce hundreds of motion events per
        frame.  Testing every sprite for every event made the renderer spend
        all its time inside ``on_mouse_motion`` and dropped FPS to zero on a
        large scene.  Keeping only the newest position and the accumulated
        delta preserves the frame-level meaning of the event.
        """

        pending = self._pending_mouse_motion
        if pending is None:
            self._pending_mouse_motion = [x, y, dx, dy]
        else:
            pending[0] = x
            pending[1] = y
            pending[2] += dx
            pending[3] += dy

    def _flush_mouse_motion(self):
        if self._pending_mouse_motion is None:
            return
        x, y, dx, dy = self._pending_mouse_motion
        self._pending_mouse_motion = None
        self._send_mouse_event("mouse_move", x, y, dx=dx, dy=dy)

    def on_mouse_press(self, x, y, button, modifiers):
        # Preserve enter/exit ordering before a click at the same position.
        self._flush_mouse_motion()
        self._send_mouse_event("mouse_down", x, y, button)

    def on_mouse_release(self, x, y, button, modifiers):
        self._flush_mouse_motion()
        self._send_mouse_event("mouse_up", x, y, button)

    def on_mouse_motion(self, x, y, dx, dy):
        self._queue_mouse_motion(x, y, dx, dy)

    def on_mouse_drag(self, x, y, dx, dy, buttons, modifiers):
        self._queue_mouse_motion(x, y, dx, dy)

    def on_mouse_scroll(self, x, y, scroll_x, scroll_y):
        self._flush_mouse_motion()
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

        # Check active sprites only; scanning the full cache capacity each
        # frame is costly.  With stripe counters the active ids are kept
        # pre-grouped, so a dirty frame touches only dirty stripes.
        buckets = getattr(self, "_render_ids_by_stripe", None)
        if self._sprites_by_render_id:
            counts = getattr(self.visual_cache, "dirty_counts", None)
            if buckets is not None and counts is not None:
                if any(counts):
                    updates = self.visual_cache.get_changed_values_by_stripe(
                        buckets
                    )
                else:
                    updates = {}
            else:
                has_pending = getattr(
                    self.visual_cache, "has_pending_changes", None
                )
                if has_pending is None or has_pending():
                    updates = self.visual_cache.get_changed_values_many(
                        self._sprites_by_render_id
                    )
                else:
                    updates = {}
            if updates:
                sprites_by_id = self._sprites_by_render_id
                apply_update = self._apply_update
                for render_id, entry in updates.items():
                    sprite = sprites_by_id.get(render_id)
                    if sprite is None:
                        continue
                    try:
                        apply_update(render_id, sprite, entry)
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

    def _append_position_sample(self, render_id, tick, x, y):
        """Append an already-normalised sample to one position history.

        This is the hot path used by ``_apply_update``: values come from the
        visual cache or from ``world_data``, so they are already numeric and
        do not need float/int coercions on every moving sprite.
        """

        history = self._position_history_by_render_id.get(render_id)
        if history is None:
            history = deque(maxlen=self.POSITION_HISTORY_SIZE)
            self._position_history_by_render_id[render_id] = history
        elif history:
            last_tick = history[-1][0]
            if tick < last_tick:
                return
            if tick == last_tick:
                history[-1] = (tick, x, y)
                return
        history.append((tick, x, y))

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
        elif history:
            last_tick = history[-1][0]
            if tick < last_tick:
                return
            if tick == last_tick:
                history[-1] = (tick, x, y)
                return
        history.append((tick, x, y))

    @staticmethod
    def _position_from_history(history, query_tick):
        """Interpolate one history without another dictionary lookup."""

        if not history:
            return None

        # Clamp first: most queries fall outside or near the newest sample,
        # and the common clamp path avoids bisect plus range checks.
        last = history[-1]
        if query_tick >= last[0]:
            return last[1], last[2]
        first = history[0]
        if query_tick <= first[0]:
            return first[1], first[2]

        index = bisect_left(history, (query_tick,))
        current = history[index]
        previous = history[index - 1]
        interval = current[0] - previous[0]
        if interval <= 0:
            return current[1], current[2]
        fraction = (query_tick - previous[0]) / interval
        return (
            previous[1] + (current[1] - previous[1]) * fraction,
            previous[2] + (current[2] - previous[2]) * fraction,
        )

    def _position_at_tick(self, render_id, query_tick):
        """Return the position of an entity at one shared simulation tick."""

        return self._position_from_history(
            self._position_history_by_render_id.get(render_id),
            query_tick,
        )

    def _resolve_render_tick(self):
        """Return the shared interpolation tick for this frame.

        The render clock is a smooth wall-clock timeline.  Each sprite is
        sampled at this tick; ``_position_at_tick`` holds the newest sample
        for an object that has not published that far yet.  A script that
        falls behind therefore holds only its own objects instead of
        freezing the whole frame and jumping forward later.
        """

        now = time.monotonic()
        render_tick = (
            (now - self.clock_origin) / self.SIMULATION_TICK
            - self.POSITION_INTERPOLATION_DELAY / self.SIMULATION_TICK
        )
        return max(0.0, render_tick)

    def _interpolate_sprite_positions(self, render_tick):
        """Interpolate only sprites that published a position recently.

        Static sprites already sit at their final position, so touching every
        sprite each frame is wasted work: a scene with thousands of walls paid
        the same per-frame cost as a scene where everything moves.
        """

        moving = self._moving_render_ids
        if not moving:
            return

        sprites = self._sprites_by_render_id
        histories = self._position_history_by_render_id
        interpolate = self._position_from_history
        mark_position = self._hit_mark_position
        idle_before = render_tick - self.INTERPOLATION_IDLE_TICKS
        stale = None
        for render_id in moving:
            history = histories.get(render_id)
            if not history:
                if stale is None:
                    stale = []
                stale.append(render_id)
                continue

            sprite = sprites.get(render_id)
            if sprite is not None:
                position = interpolate(history, render_tick)
                if sprite.position != position:
                    # One position setter updates arcade's hit box and sprite
                    # list slot once; center_x/center_y would do it twice.
                    # Hit-grid refresh is lazy: _hit_index_dirty() checks the
                    # current center of every moving sprite on query.
                    sprite.position = position

            if history[-1][0] < idle_before:
                if stale is None:
                    stale = []
                stale.append(render_id)

        if not stale:
            return

        world_positions = self._world_data_by_render_id
        for render_id in stale:
            moving.discard(render_id)
            sprite = sprites.get(render_id)
            world_data = world_positions.get(render_id)
            if sprite is not None and world_data is not None:
                # Snap to the last received sample before going static.
                sprite.position = world_data["position"]
                # Interpolation is skipped for this id from now on, so the
                # hit grid must be refreshed here instead.
                mark_position(
                    render_id, sprite.center_x, sprite.center_y
                )

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

    def _info_static_for(self, render_id, now):
        """Return cached identity/scripts for the info overlay."""

        cached = self._info_static_cache.get(render_id)
        if cached is not None and cached[0] >= now:
            return cached[1]

        index_key = (stream.Stream.INDEX_PREFIX, "render_id", render_id)
        record_id = self.object_cache.get(index_key)
        record = self.object_cache.get(record_id) if record_id is not None else None
        if record is None:
            return None

        data = record.get("dict_data", {})
        identity = data.get("_identity", {})
        scripts = data.get("scripting", {}).get("scripts", [])
        payload = {
            "object_id": record.get("_id", "?"),
            "name": identity.get("name", "-"),
            "class": identity.get("class", "-"),
            "type": identity.get("type", data.get("object_type", "-")),
            "scripts": ", ".join(Path(path).name for path in scripts) or "-",
        }
        self._info_static_cache[render_id] = (
            now + INFO_OVERLAY_CACHE_TTL, payload,
        )
        return payload

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

        now = time.monotonic()
        for render_id, sprite in self._sprites_by_render_id.items():
            if sprite.alpha <= 0:
                continue
            static = self._info_static_for(render_id, now)
            if static is None:
                continue
            world = self._world_data_by_render_id.get(render_id, {})
            position = self._position_at_tick(render_id, render_tick)
            if position is None:
                position = world.get("position", (0, 0))

            lines = []
            if enabled["id"]:
                lines.append(
                    f"ID: {static['object_id']} / render {render_id}"
                )
            if enabled["name"]:
                lines.append(
                    f"{static['name']} | {static['class']} | "
                    f"{static['type']} | "
                    f"layer: {world.get('layer', 0):g}"
                )
            if enabled["coords"]:
                lines.append(f"x={position[0]:.0f}, y={position[1]:.0f}")
            if enabled["scripts"]:
                lines.append(f"scripts: {static['scripts']}")
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
        clear_changes = getattr(self.visual_cache, "clear_changes", None)
        if clear_changes is not None:
            for render_id in self._sprites_by_render_id:
                clear_changes(render_id)
        self.renders.clear()

        self._sprites_by_render_id.clear()
        buckets = getattr(self, "_render_ids_by_stripe", None)
        if buckets is not None:
            for bucket in buckets:
                bucket.clear()
        self._render_order_dirty = False
        self._hit_cells.clear()
        self._hit_geometry_by_render_id.clear()
        self._hit_bounds_by_render_id.clear()
        self._hit_max_extent = 0.0
        self._hit_max_count = 0
        self._hit_large_ids.clear()
        self._hit_dirty_ids.clear()
        self._world_data_by_render_id.clear()
        self._position_history_by_render_id.clear()
        self._moving_render_ids.clear()
        self._latest_tick_by_render_id.clear()
        self._scripted_render_ids.clear()
        self._waiting_updates.clear()
        self._colliders_by_render_id.clear()
        self._info_text_by_render_id.clear()
        self._info_static_cache.clear()
        self._failed_textures.clear()
        self._camera_target_id = -1
        self.camera_target_cache.set(-1)

    def _visual_bucket_add(self, render_id):
        buckets = getattr(self, "_render_ids_by_stripe", None)
        if buckets is not None:
            buckets[render_id % len(buckets)].add(render_id)

    def _visual_bucket_discard(self, render_id):
        buckets = getattr(self, "_render_ids_by_stripe", None)
        if buckets is not None:
            buckets[render_id % len(buckets)].discard(render_id)

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
        sprite._engine_sort_key = (world_data['layer'], -render_id)

        self.renders.append(sprite)

        self._sprites_by_render_id[render_id] = sprite
        self._world_data_by_render_id[render_id] = world_data
        self._visual_bucket_add(render_id)
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
        self._hit_mark_dirty(render_id)

    def _remove_sprite(self, data, reset_camera=True):

        render_id = data['render_id']
        clear_changes = getattr(self.visual_cache, "clear_changes", None)
        if clear_changes is not None:
            clear_changes(render_id)
        sprite = (self._sprites_by_render_id.pop(render_id, None))
        self._visual_bucket_discard(render_id)

        if sprite is not None:
            self.renders.remove(sprite)
            self._render_order_dirty = True

        if reset_camera and self._camera_target_id == render_id:
            self._camera_target_id = -1
            self.camera_target_cache.set(-1)

        self._hit_remove(render_id)
        self._info_static_cache.pop(render_id, None)
        self._world_data_by_render_id.pop(render_id, None)
        self._position_history_by_render_id.pop(render_id, None)
        self._moving_render_ids.discard(render_id)
        self._latest_tick_by_render_id.pop(render_id, None)
        self._scripted_render_ids.discard(render_id)
        self._waiting_updates.pop(render_id, None)
        self._colliders_by_render_id.pop(render_id, None)
        self._info_text_by_render_id.pop(render_id, None)

    def _apply_update(self, id, sprite, object_data):
        world_data = self._world_data_by_render_id.get(id)
        if world_data is None:
            return

        # Moving sprites publish exactly {'x', 'y'} or {'x', 'y', 'tick'}.
        # Keep that case free of the nine dictionary membership tests and of
        # the float/int coercions in _record_position.
        key_count = len(object_data)

        # Single-field updates are common for non-position changes (layer
        # switches, alpha fades, a collider rotation).  Handle them before the
        # generic path so they skip seven membership tests and the position
        # bookkeeping that cannot apply to them.
        if key_count == 1:
            if 'tick' in object_data:
                tick = int(object_data['tick'])
                latest_tick = self._latest_tick_by_render_id.get(id)
                if latest_tick is None or tick > latest_tick:
                    self._latest_tick_by_render_id[id] = tick
                return
            if 'rotation' in object_data:
                rotation = object_data['rotation']
                if rotation != world_data['rotation']:
                    world_data['rotation'] = rotation
                    self._hit_mark_dirty(id)
                sprite.angle = rotation
                return
            if 'scale' in object_data:
                scale = object_data['scale']
                if scale != world_data['scale']:
                    world_data['scale'] = scale
                    self._hit_mark_dirty(id)
                sprite.scale = scale
                return
            if 'alpha' in object_data:
                alpha = object_data['alpha']
                world_data['alpha'] = alpha
                sprite.alpha = int(alpha * 255)
                return
            if 'layer' in object_data:
                layer = float(object_data['layer'])
                world_data['layer'] = layer
                sprite._engine_sort_key = (layer, -id)
                self._render_order_dirty = True
                return
            if 'sprite' in object_data:
                sprite.texture = self._texture_for_path(object_data['sprite'])
                self._hit_mark_dirty(id)
                return

        if (key_count <= 3
                and 'x' in object_data
                and 'y' in object_data
                and (key_count == 2 or 'tick' in object_data)):
            tick = object_data.get('tick')
            latest_ticks = self._latest_tick_by_render_id
            if tick is not None:
                tick = int(tick)
                latest_tick = latest_ticks.get(id)
                if latest_tick is None or tick > latest_tick:
                    latest_ticks[id] = tick

            current_x, current_y = world_data['position']
            new_x = object_data['x']
            new_y = object_data['y']
            if new_x != current_x or new_y != current_y:
                world_data['position'] = (new_x, new_y)
                self._moving_render_ids.add(id)
                if tick is None:
                    tick = latest_ticks.get(id)
                    if tick is None:
                        tick = int(self._current_tick())
                self._append_position_sample(id, tick, new_x, new_y)
                # The hit grid is refreshed lazily on the next pointer query;
                # marking here would repeat the same cell check per update.
            return

        if 'sprite' in object_data:
            sprite.texture = self._texture_for_path(object_data['sprite'])

        tick = object_data.get('tick')
        if tick is not None:
            tick = int(tick)
            latest_tick = self._latest_tick_by_render_id.get(id)
            if latest_tick is None or tick > latest_tick:
                self._latest_tick_by_render_id[id] = tick

        current_x, current_y = world_data['position']
        position_changed = False

        if 'x' in object_data:
            new_x = object_data['x']
            if new_x != current_x:
                current_x = new_x
                position_changed = True

        if 'y' in object_data:
            new_y = object_data['y']
            if new_y != current_y:
                current_y = new_y
                position_changed = True

        world_data['position'] = (current_x, current_y)

        geometry_changed = 'sprite' in object_data

        if 'scale' in object_data:
            if object_data['scale'] != world_data['scale']:
                geometry_changed = True
            world_data['scale'] = object_data['scale']

        if 'rotation' in object_data:
            if object_data['rotation'] != world_data['rotation']:
                geometry_changed = True
            world_data['rotation'] = object_data['rotation']

        if 'alpha' in object_data:
            world_data['alpha'] = object_data['alpha']

        if 'layer' in object_data:
            layer = float(object_data['layer'])
            world_data['layer'] = layer
            sprite._engine_sort_key = (layer, -id)
            self._render_order_dirty = True

        if position_changed:
            # Repeated identical positions are heartbeat noise, not motion.
            # Sending them to history would keep a static sprite in the
            # interpolation set forever.
            self._moving_render_ids.add(id)
            record_tick = tick
            if record_tick is None:
                record_tick = self._latest_tick_by_render_id.get(id)
            self._record_position(id, world_data['position'], record_tick)

        if 'scale' in object_data:
            sprite.scale = world_data['scale']
        if 'rotation' in object_data:
            sprite.angle = world_data['rotation']
        if 'alpha' in object_data:
            sprite.alpha = int(world_data['alpha'] * 255)
        if geometry_changed:
            self._hit_mark_dirty(id)
        # Position-only updates are re-indexed lazily by _hit_index_dirty();
        # see the fast path above.

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

def tact_counter(start, tact_time, EngineState, stop_event):

    while not stop_event.wait(tact_time):
        elapsed = time.monotonic() - start
        EngineState.set_many(0, {
            "time": round(elapsed, 5),
            "tick": int(elapsed / tact_time),
        })

def main():

    manager = multiprocessing.Manager()

    render_in_stream = RenderInQueue(manager)
    render_out_stream = RenderOutQueue(manager)
    main_in_stream = MainInQueue(manager)
    main_out_stream = MainOutQueue(manager)

    cpu_count = os.cpu_count() or 1
    worker_count = max(1, cpu_count - 3)
    worker_queues = [ProccesInQueue(manager) for _ in range(worker_count)]
    worker_ready = manager.list([False] * worker_count)

    objects_cache = manager.dict()
    object_lock = manager.Lock()
    visual_struct =  {
        'x' : float,
        'y' : float,
        # tick follows x/y so the hottest visual write is one contiguous
        # span and ObjectCache.set_position() can pack it in one call.
        'tick' : int,
        'sim_time': float,
        'rotation' : float,
        'scale' : float,
        'alpha' : float,
        'sprite' : str,
        'layer' : float,
    }
    visual_locks = [multiprocessing.Lock() for _ in range(64)]
    visual_dirty_counts = multiprocessing.Array(
        "i", len(visual_locks), lock=False
    )
    visual_dirty_queue = multiprocessing.Array(
        "i",
        len(visual_locks) * (3 + DIRTY_RING_CAPACITY),
        lock=False,
    )
    raw_visual_cache = VisualCache(
        1024 * 1024,
        visual_struct,
        visual_locks,
        visual_dirty_counts,
        dirty_queue=visual_dirty_queue,
        dirty_ring_size=DIRTY_RING_CAPACITY,
    )
    visual_cache = [
        raw_visual_cache.key,
        visual_struct,
        visual_locks,
        visual_dirty_counts,
        raw_visual_cache.capacity,
        visual_dirty_queue,
        DIRTY_RING_CAPACITY,
    ]

    id_struct =  {
        'name' : str,
        'type' : str,
        'class' : str,
        'id' : str,
    }
    id_locks = [multiprocessing.Lock() for _ in range(64)]
    raw_id_cache = IdCache(1024*256, id_struct, id_locks)
    id_cache = [
        raw_id_cache.key, id_struct, id_locks, None, raw_id_cache.capacity,
    ]

    proccess_status_info = manager.list()
    # All engine counters share one segment; scripts read them in one lock.
    raw_engine_state = ObjectCache(
        ENGINE_STATE_SEGMENT_BYTES,
        ENGINE_STATE_STRUCT,
        [multiprocessing.Lock() for _ in range(4)],
    )
    engine_state = [
        raw_engine_state.key,
        ENGINE_STATE_STRUCT,
        raw_engine_state.locks,
        None,
        raw_engine_state.capacity,
    ]
    raw_motion_cache = ObjectCache(
        MOUSE_MOTION_BYTES,
        MOUSE_MOTION_STRUCT,
        [multiprocessing.Lock() for _ in range(8)],
    )
    # Zeroed shared memory would report ``hovered=0`` before the first mouse
    # event, which looks like a hover over render_id 0 to the scripts.
    raw_motion_cache.set_many(0, {
        "seq": 0,
        "x": 0.0,
        "y": 0.0,
        "dx": 0.0,
        "dy": 0.0,
        "screen_x": 0.0,
        "screen_y": 0.0,
        "hovered": -1,
    })
    motion_cache_data = [
        raw_motion_cache.key,
        MOUSE_MOTION_STRUCT,
        raw_motion_cache.locks,
        None,
        raw_motion_cache.capacity,
    ]
    raw_keyboard_events = ObjectCache(
        KEYBOARD_EVENT_BYTES,
        KEYBOARD_EVENT_STRUCT,
        [multiprocessing.Lock() for _ in range(16)],
    )
    keyboard_ring_data = [
        raw_keyboard_events.key,
        KEYBOARD_EVENT_STRUCT,
        raw_keyboard_events.locks,
        None,
        raw_keyboard_events.capacity,
    ]
    camera_target_cache = ValueCache(-1)
    camera_scale_cache = ValueCache(1.0)
    camera_state = [camera_target_cache.name, camera_scale_cache.name]
    overlay_settings = console.OverlaySettings()
    lock = manager.Lock()

    logic_process = multiprocessing.Process(
        target=stream.Stream,
        args=(
            engine_state,
            render_in_stream,
            render_out_stream,
            main_in_stream,
            main_out_stream,
            objects_cache,
            proccess_status_info,
            worker_queues,
            worker_ready,
            lock,
            visual_cache,
            id_cache,
            object_lock,
            camera_state,
            motion_cache_data,
            keyboard_ring_data,
            manager.address,
            manager._authkey,
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
            main_in_stream,
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
            raw_engine_state,
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
            raw_motion_cache,
        ], FieldView(raw_engine_state, "time"),
            width=400, height=400, clock_origin=game_clock_origin,
            tick_cache=FieldView(raw_engine_state, "tick"),
            overlay_settings=overlay_settings)
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
        raw_engine_state.close()
        raw_motion_cache.close()
        raw_keyboard_events.close()
        camera_target_cache.close()
        camera_scale_cache.close()


if __name__ == '__main__':
    main()
