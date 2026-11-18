"""Двигает спрайт по орбите вокруг точки появления в визуальной стресс-сцене."""

from math import cos, sin

from pyengine import Component


class stress_mover(Component):
    """Двигает один спрайт по круговой траектории.

    Компонент используется только когда контроллер сцены включает
    ``per_object_scripts``.  В режиме режиссёра по умолчанию контроллер
    сам обновляет каждый спрайт, что сохраняет плавность движения и при этом
    нагружает общий кэш позиций и пакеты рендера.
    """

    radius = 4.0
    speed = 10000
    phase = 0.0

    def start(self):
        visual = self.GameObject.Visual
        if visual is None:
            return
        position = visual.position
        self._center_x = position.x
        self._center_y = position.y

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
