"""Moving-object component used by the live ``motion`` stress scenario.

Each fixed step moves the object by a constant distance.  The stress runner
then checks that the shared render tick keeps advancing smoothly and that
every object publishes heartbeats inside the interpolation window.
"""

from pyengine import Component

from config import FIXED_DT


class motion_component(Component):
    speed = 120.0
    half_range = 300.0

    def fixed_update(self):
        visual = self.GameObject.Visual
        if visual is None:
            return
        position = visual.position
        position.x += float(self.speed) * FIXED_DT
        if position.x > float(self.half_range):
            position.x = -float(self.half_range)
        visual.position = position
