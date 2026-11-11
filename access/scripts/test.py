import time
import math
from pyengine import *
import random

class test(script):


    def start(self):
        position = self.GameObject.Visual.position
        position.y = random.randint(0, 400)
        self.GameObject.Visual.position = position
        self.k = 1
        self.tilt_trigger = self.find_object("n", "tilt_trigger")
        self.is_touching_trigger = False


    def fixed_update(self):
        if self.GameObject.Visual.position.x > 300 or self.GameObject.Visual.position.x < -100:
            self.k *= -1

        self.GameObject.Visual.alpha = abs(
            math.sin(self.k)
        )

        self.GameObject.Visual.position = (
            self.GameObject.Visual.position
            + Vector2(2, 0) * self.k
        )

