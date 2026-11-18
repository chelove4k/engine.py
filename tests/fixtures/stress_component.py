"""Компонент нагрузки на CPU и кэш для ``tests/stress_test.py``.

Компонент намеренно простой: каждая итерация цикла скрипта выполняет
настраиваемое количество арифметических операций, а затем публикует одно
визуальное изменение.  Это позволяет стресс-раннеру измерять реальный цикл
скрипта вместе с записями в общий кэш.
"""

from pyengine import Component


class stress_component(Component):
    """Выполняет ``iterations`` арифметических шагов и публикует одно обновление поворота."""

    iterations = 100
    stride = 1.0

    def update(self):
        total = 0.0
        for index in range(int(self.iterations)):
            total += (index & 7) * 0.125
        # Оставляем результат доступным, чтобы цикл нельзя было выбросить при оптимизации.
        self._last_total = total

        visual = self.GameObject.Visual
        if visual is not None:
            visual.rotation += float(self.stride)

    def on_key_press(self, event):
        # Стресс-раннер отправляет одну клавишу после замера частоты обновлений
        # и проверяет, что каждый скрипт получил её через общее кольцо.
        self.key_presses = getattr(self, "key_presses", 0) + 1
        visual = self.GameObject.Visual
        if visual is not None:
            visual.scale = 1.0 + self.key_presses
