# Arcade test engine

## Формат объекта

Каждый объект сцены — JSON-объект с обязательным `object_type`. Допустимые типы и компоненты объявлены централизованно в `stream.py` в таблице `OBJECT_TYPE_RULES`: для каждого типа там заданы `allowed` и `required`. Чтобы изменить правила или добавить тип, достаточно поправить эту таблицу.

| `object_type` | Допустимые компоненты | Обязательные компоненты |
| --- | --- | --- |
| `static_object` | `id`, `visual` | `id` |
| `dynamic_object` | `id`, `visual`, `scripting` | нет |
| `background_object` | `visual` | `visual` |
| `camera` | `id`, `visual` | `id`, `visual` |
| `manager` | `id`, `scripting` | `id`, `scripting` |

`id.type`, если он указан, должен совпадать с `object_type`. Identity хранится в `IdCache`, визуальные данные — в `VisualCache`; они не дублируются в `objects_cache`. В нём остаются служебные данные и конфигурация скриптов.

Пример цели камеры:

```json
{
  "object_type": "camera",
  "id": {"name": "alpha", "class": "CameraTarget", "layer": "world", "type": "camera"},
  "visual": {
    "sprite": "access/resources/img.png",
    "position": [0, 0], "scale": 0.2, "rotation": 0, "alpha": 1
  }
}
```

`Stream` проверяет тип, неизвестные и лишние компоненты, обязательные поля и базовые типы значений до регистрации объекта. Ошибка возвращается вызывающему коду как `ValueError` с объяснением.

## Типы объектов

- `static_object` — объект с Identity; скриптинг запрещён.
- `dynamic_object` — произвольное сочетание Identity, Visual и Scripting.
- `background_object` — объект только с Visual.
- `camera` — именованная визуальная цель, за которой может следить камера.
- `manager` — объект Identity и Scripting для системного поведения.

## Функции скриптов

- `is_pressed(button)` проверяет, удерживается ли клавиша.
- `find_object(by, value)` возвращает первый объект, совпавший по атрибуту. `by`: `c`/`class`, `n`/`name`, `l`/`layer`, `t`/`type`. Возвращаемый объект — прокси: Visual читает и записывает `VisualCache`.

## Камера

При старте контроллер выбирает объект типа `camera` с первым именем в алфавитном порядке (без учёта регистра). Камера постоянно следует за его координатами.

Скрипт контроллера напрямую изменяет `Visual.position` выбранного объекта. Рендерер только считывает координаты и ID цели из общего состояния камеры; очередь для движения и переключения камеры не используется.

- `Camera.move_camera(dx, dy)` или `Camera.move_camera([dx, dy])` сдвигает выбранную цель.
- `Camera.moveto_camera(x, y)` задаёт координаты цели.
- `Camera.switch(name)` переключает слежение на цель типа `camera` с точным именем; возвращает `True`, если цель найдена.
- `Camera.swich(name)` оставлен как совместимый алиас с написанием из раннего API.
- `Camera.scale_camera(scale)` меняет масштаб отображения.

В примере `access/scripts/camera.py` клавиши WASD двигают выбранный объект, `1` и `2` переключают цели `alpha` и `beta`.
