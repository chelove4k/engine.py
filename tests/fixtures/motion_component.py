"""Компонент движущегося объекта для живого стресс-сценария ``motion``.

На каждом фиксированном шаге объект смещается на постоянное расстояние.
Затем стресс-раннер проверяет, что общий render tick продолжает плавно
идти, а каждый объект публикует обновления позиции внутри окна интерполяции.
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
