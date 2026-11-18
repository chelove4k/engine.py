"""Orbit one sprite around its spawn point in the visual stress scene."""

from math import cos, sin

from pyengine import Component


class stress_mover(Component):
    """Move a single sprite on a circular path.

    The component is used only when the scene controller enables
    ``per_object_scripts``.  In the default director mode the controller
    updates every sprite itself, which keeps the motion smooth while still
    exercising the shared position cache and the render batches.
    """

    radius = 4.0
    speed = 10000
    phase = 0.0

    def start(self):
        visual = self.GameObject.Visual
        if visual is None:
            return
        position = visual.position
        self._center_x = position.x
        self._center_y = position.y

    def fixed_update(self):
        visual = self.GameObject.Visual
        if visual is None:
            return

        angle = float(self.phase) + float(self.GlobalTime) * float(self.speed)
        radius = float(self.radius)
        visual.position = (
            self._center_x + cos(angle) * radius,
            self._center_y + sin(angle) * radius,
        )
