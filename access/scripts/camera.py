import time
import math
from pyengine import *

class camera(script):

    def start(self):
        self._last_switch = None

    def fixed_update(self):
        dx = 0
        dy = 0

        if self.is_pressed('w'):
            dy += 1
        if self.is_pressed('s'):
            dy -= 1
        if self.is_pressed('a'):
            dx -= 1
        if self.is_pressed('d'):
            dx += 1

        direction_length = math.hypot(dx, dy)
        if direction_length:
            # Move on the same game tick as objects using fixed_update.
            self.Camera.move_camera(dx * 2 / direction_length, dy * 2 / direction_length)

        # 1 and 2 select the named camera targets in the sample scene.
        if self.is_pressed('1') and self._last_switch != 'alpha':
            self.Camera.switch('alpha')
            self._last_switch = 'alpha'
        elif self.is_pressed('2') and self._last_switch != 'beta':
            self.Camera.swich('beta')
            self._last_switch = 'beta'

        if not self.is_pressed('1') and not self.is_pressed('2'):
            self._last_switch = None
