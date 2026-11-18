import time
import math
from pyengine import *

ZOOM_MIN = 0.01
ZOOM_MAX = 8.0
ZOOM_RATE = 1.2  # экспоненциальных приростов в секунду
ZOOM_STEP = math.exp(ZOOM_RATE * FIXED_DT)


class camera(script):

    def start(self):
        self._last_switch = None
        self._zoom = self.Camera.scale

    def fixed_update(self):
        # Один снимок нажатых клавиш дешевле, чем вызов is_pressed()
        # для каждой клавиши: каждый вызов заново нормализует имя клавиши.
        pressed = self.get_pressed_keys()
        camera = self.Camera

        dx = 0
        dy = 0
        if 'W' in pressed:
            dy += 1
        if 'S' in pressed:
            dy -= 1
        if 'A' in pressed:
            dx -= 1
        if 'D' in pressed:
            dx += 1

        if dx or dy:
            # Двигаемся на том же игровом тике, что и объекты с fixed_update.
            direction_length = math.hypot(dx, dy)
            camera.move_camera(dx * 2 / direction_length, dy * 2 / direction_length)

        # Q отдаляет, E приближает.  Экспоненциальный шаг делает зум
        # плавным и независимым от частоты кадров.  ZOOM_STEP вычисляется
        # один раз при импорте, поэтому горячий путь только умножает или делит.
        zoom_direction = 0
        if 'Q' in pressed:
            zoom_direction -= 1
        if 'E' in pressed:
            zoom_direction += 1

        if zoom_direction:
            if zoom_direction > 0:
                zoom = self._zoom * ZOOM_STEP
            else:
                zoom = self._zoom / ZOOM_STEP
            zoom = min(ZOOM_MAX, max(ZOOM_MIN, zoom))
            if zoom != self._zoom:
                self._zoom = zoom
                camera.scale_camera(zoom)

        # 1 и 2 выбирают именованные цели камеры в примере сцены.
        pressed_one = '1' in pressed
        pressed_two = '2' in pressed
        if pressed_one and self._last_switch != 'alpha':
            camera.switch('alpha')
            self._last_switch = 'alpha'
        elif pressed_two and self._last_switch != 'beta':
            camera.swich('beta')
            self._last_switch = 'beta'

        if not pressed_one and not pressed_two:
            self._last_switch = None
