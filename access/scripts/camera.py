import time
import math
from pyengine import *

class camera(script):

    def fixed_update(self):
        dx = 0
        dy = 0

        if self.is_pressed('w'):
            dy += 2

        if self.is_pressed('s'):
            dy -= 2

        if self.is_pressed('a'):
            dx -= 2

        if self.is_pressed('d'):
            dx += 2

        if dx != 0 or dy != 0:
            self.Camera.move_camera([dx, dy])
