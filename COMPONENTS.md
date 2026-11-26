# Компоненты Python

> Часть проекта **pyengine**. Обзор и быстрый старт — в [README.MD](README.MD),
> полный API скриптов — в [SCRIPTING_API.md](SCRIPTING_API.md).

Полный API скриптов — включая методы жизненного цикла, ввод, управление камерой, поиск объектов и коллизии — см. в [SCRIPTING_API.md](SCRIPTING_API.md).

Повторно используемое поведение оформляется как класс, производный от `Component`. Имя класса должно совпадать с именем Python-файла в `access/components`.

```python
from pyengine import Component


class rotate_on_click(Component):
    degrees = 45

    def on_mouse_click(self, event):
        self.GameObject.Visual.rotation += float(self.degrees)
```

Подключите его к любому объекту сцены как верхнеуровневый компонент. Ключ должен совпадать с классом компонента и его именем файла в `access/components`, а значение содержит свойства напрямую:

```json
"rotate_on_click": {
  "degrees": 45
}
```

В этой форме нет обёрток `path` и `properties`. Старый массив `components` с `path`/`properties` по-прежнему принимается для совместимости.

Компоненты и обычные скрипты получают `self.GameObject`, `self.Mouse` и `self.Keyboard`. Значения из объекта компонента доступны напрямую как атрибуты экземпляра (`self.degrees`) и переопределяют значения класса по умолчанию. Обратные вызовы мыши включают `on_mouse_click`, `on_mouse_press`, `on_mouse_release`, `on_mouse_move`, `on_mouse_enter`, `on_mouse_exit` и `on_mouse_scroll`. `self.Mouse.position` даёт мировые координаты, а `self.Mouse.screen_position` — координаты окна; `self.Mouse.is_over()`, `self.Mouse.is_pressed("left")` и `self.Mouse.was_clicked("left")` предоставляют типичные проверки. Позиции событий мыши доступны как `event.position` и `event.screen_position`.

Обратные вызовы клавиатуры `on_key_press(event)` и `on_key_release(event)` выполняются для каждого события клавиши. Используйте `self.Keyboard.is_pressed("A")` для удерживаемых клавиш, `self.Keyboard.was_pressed("SPACE")` / `was_released("SPACE")` для одноразовых проверок и `self.Keyboard.get_pressed()` для чтения множества нажатых клавиш. Событие содержит `event.key` и `event.modifiers`. Более старый помощник `self.is_pressed("A")` остаётся доступным. Существующие записи `scripting` продолжают работать вместе с компонентами.

## Слои Visual

Задайте числовой `layer` внутри компонента `visual` объекта, чтобы управлять порядком отрисовки; по умолчанию он равен `0`. Большие значения рисуются поверх меньших. Если у двух объектов одинаковый слой, объект, созданный раньше, рисуется поверх.

```json
"visual": {
  "sprite": "access/resources/img.png",
  "position": [0, 0],
  "scale": 1,
  "rotation": 0,
  "alpha": 1,
  "layer": 2
}
```
