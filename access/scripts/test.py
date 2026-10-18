import time
import math
from pyengine import *
import random

class test(script):

    
    def start(self):
        self.GameObject.render()
        self.GameObject.Visual.position.y = random.randint(0, 400)
        self.k = 1

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
    