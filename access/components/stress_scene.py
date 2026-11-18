"""Spawn and animate the visual performance stress scene.

The controller is attached to the manager object in
``access/scenes/stress_motion.json``.  It creates a dense grid of small
sprites and moves all of them on circular paths.

Two modes are available:

* director mode (default) - one ``fixed_update`` writes every position.  This
  is the smooth visual benchmark for the shared position cache, polling,
  interpolation and render batching.
* per-object script mode - set ``per_object_scripts`` to ``true``.  Every
  sprite receives its own ``stress_mover`` component, which additionally
  stresses the worker script loops and is expected to saturate the workers
  long before the renderer becomes the bottleneck.

Run the scene directly with::

    $env:PYENGINE_SCENE = "stress_motion.json"
    python render.py
"""

from math import cos, sin

from pyengine import Component


class stress_scene(Component):
    count = 1000
    columns = 36
    spacing = 10.5
    radius = 4.0
    speed = 1.2
    sprite_scale = 0.024
    layer = 1.0
    per_object_scripts = False
    chunk_size = 100

    def start(self):
        count = max(1, int(self.count))
        columns = max(1, int(self.columns))
        spacing = float(self.spacing)
        radius = float(self.radius)
        speed = float(self.speed)
        sprite_scale = float(self.sprite_scale)
        layer = float(self.layer)
        per_object = bool(self.per_object_scripts)
        chunk_size = max(1, int(self.chunk_size))

        rows = (count + columns - 1) // columns
        start_x = -((columns - 1) * spacing) / 2.0
        start_y = ((rows - 1) * spacing) / 2.0

        self._objects = []
        self._visuals = []
        self._centers = []
        self._phases = []

        # Batch registration keeps scene creation to one Stream round trip per
        # chunk instead of one per object.  Chunks also let worker scripts
        # start while the rest of the grid is still being registered.
        for chunk_start in range(0, count, chunk_size):
            chunk_end = min(count, chunk_start + chunk_size)
            entries = []
            for index in range(chunk_start, chunk_end):
                column = index % columns
                row = index // columns
                phase = column * 0.45 + row * 0.65
                entry = {
                    "object_type": "dynamic_object",
                    "id": {
                        "name": f"stress_{index:04d}",
                        "class": "StressMover",
                        "type": "dynamic_object",
                    },
                    "visual": {
                        "sprite": "access/resources/img.png",
                        "position": [
                            start_x + column * spacing,
                            start_y - row * spacing,
                        ],
                        "scale": sprite_scale,
                        "rotation": 0,
                        "alpha": 1,
                        "layer": layer,
                    },
                }
                if per_object:
                    entry["stress_mover"] = {
                        "radius": radius,
                        "speed": speed,
                        "phase": phase,
                    }
                entries.append(entry)

            created = self.create_object(entries)
            self._objects.extend(created)
            print(
                f"[STRESS] registered {chunk_end}/{count} sprites",
                flush=True,
            )
            if per_object:
                continue

            for offset, game_object in enumerate(created):
                index = chunk_start + offset
                visual = game_object.Visual
                if visual is None:
                    continue
                column = index % columns
                row = index // columns
                self._visuals.append(visual)
                self._centers.append((
                    start_x + column * spacing,
                    start_y - row * spacing,
                ))
                self._phases.append(column * 0.45 + row * 0.65)

        self.Camera.moveto_camera(0.0, 0.0)
        mode = "per-object scripts" if per_object else "director"
        print(f"[STRESS] {count} moving sprites, {mode} mode")

    def fixed_update(self):
        visuals = self._visuals
        if not visuals:
            return

        radius = float(self.radius)
        speed = float(self.speed)
        time_value = float(self.GlobalTime)
        phases = self._phases
        centers = self._centers

        for index, visual in enumerate(visuals):
            angle = phases[index] + time_value * speed
            center_x, center_y = centers[index]
            visual.position = (
                center_x + cos(angle) * radius,
                center_y + sin(angle) * radius,
            )
