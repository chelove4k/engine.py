"""CPU and cache load component used by ``tests/stress_test.py``.

The component is intentionally simple: every script-loop iteration performs a
configurable amount of arithmetic and then publishes one visual change.  That
lets the stress runner measure the real script loop together with its shared
cache writes.
"""

from pyengine import Component


class stress_component(Component):
    """Burn ``iterations`` arithmetic steps and publish one rotation update."""

    iterations = 100
    stride = 1.0

    def update(self):
        total = 0.0
        for index in range(int(self.iterations)):
            total += (index & 7) * 0.125
        # Keep the result reachable so the loop cannot be folded away.
        self._last_total = total

        visual = self.GameObject.Visual
        if visual is not None:
            visual.rotation += float(self.stride)

    def on_key_press(self, event):
        # The stress runner sends one key after measuring update rates and
        # checks that every script received it through the shared ring.
        self.key_presses = getattr(self, "key_presses", 0) + 1
        visual = self.GameObject.Visual
        if visual is not None:
            visual.scale = 1.0 + self.key_presses
