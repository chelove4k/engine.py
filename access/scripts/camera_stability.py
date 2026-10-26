from pyengine import Vector2, script


class camera_stability(script):
    SPEED = 2.0

    def start(self):
        self.GameObject.render()
        self.position = self.GameObject.Visual.position

    def fixed_update(self):
        movement = Vector2(self.SPEED, 0)
        self.position = self.position + movement
        self.GameObject.Visual.position = self.position
        self.Camera.move_camera(movement)
