import time
from pyengine import *
import random
from math import cos, sin

class test(script):


    

    def start(self):
        visual = self.GameObject.Visual
        if visual is None:
            return
        position = visual.position
        self._center_x = position.x
        self._center_y = position.y
        self.radius = 50
        self.speed = 3
        self.phase = 0.0

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

