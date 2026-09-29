import time
import math
from pyengine import *

class test(script):

    
    def start(self):
        self.GameObject.render()
        pass
        self.start_pos = (self.GameObject.Visual.position.x, self.GameObject.Visual.position.y-100)

        self.k = 1
        self.GameObject.ahsfdlaslfdasdssssssssssssssssssssjlkj = 123

    def fixed_update(self):
        if self.GameObject.Visual.position.x > 300 or self.GameObject.Visual.position.x < -100:
            self.k *= -1


        self.GameObject.Visual.alpha = abs(
            math.sin(self.k)
        )

        self.GameObject.Visual.position = self.GameObject.Visual.position + Vector2(2, 0) * self.k


            
        