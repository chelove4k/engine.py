from pyengine import *


class net_player(script):
    """Детерминированный игрок для сетевого демо и тестов.

    Одинаковый код крутится на сервере и на клиенте, поэтому предсказание
    сходится без физики и случайности.
    """

    SPEED = 120.0

    def fixed_update(self):
        pressed = self.get_pressed_keys()
        dx = (1 if "D" in pressed else 0) - (1 if "A" in pressed else 0)
        dy = (1 if "W" in pressed else 0) - (1 if "S" in pressed else 0)
        if not dx and not dy:
            return
        position = self.GameObject.Visual.position
        self.GameObject.Visual.position = Vector2(
            position.x + dx * self.SPEED * FIXED_DT,
            position.y + dy * self.SPEED * FIXED_DT,
        )
