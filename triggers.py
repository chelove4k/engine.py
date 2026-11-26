"""Схема и геометрия триггер-зон.

Триггер — отдельный объект сцены без визуальной части.  Его форма задаётся
выпуклым полигоном так же, как у ``transform``: локальные вершины плюс
``position``/``rotation``/``scale``.  Фильтр ``activates`` устроен как
``find_object``: поля объединяются через AND, значения внутри списка — через
OR, пустой фильтр пропускает все подходящие объекты.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from collision import transform_polygon, validate_convex_polygon


TRIGGER_FIELDS = frozenset({
    "polygon",
    "position",
    "rotation",
    "scale",
    "activates",
})
TRIGGER_FILTER_FIELDS = ("class", "name", "type", "layer")


def _finite_float(value, field_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} должно быть числом") from error
    if not math.isfinite(result):
        raise ValueError(f"{field_name} должно быть конечным числом")
    return result


def _position(value, field_name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{field_name} должно содержать два числа")
    return (
        _finite_float(value[0], f"{field_name}[0]"),
        _finite_float(value[1], f"{field_name}[1]"),
    )


def validate_trigger(trigger, field_name: str = "trigger") -> dict:
    """Проверяет описание триггера и возвращает нормализованную копию."""

    if not isinstance(trigger, dict):
        raise ValueError(f"{field_name} должен быть объектом")

    unknown = set(trigger) - TRIGGER_FIELDS
    if unknown:
        raise ValueError(
            f"{field_name}: неизвестные поля: {', '.join(sorted(unknown))}"
        )
    if "polygon" not in trigger:
        raise ValueError(f"{field_name}.polygon обязателен")

    try:
        polygon = validate_convex_polygon(trigger["polygon"])
    except ValueError as error:
        raise ValueError(f"{field_name}.polygon: {error}") from error

    position = _position(
        trigger.get("position", [0, 0]), f"{field_name}.position"
    )
    rotation = _finite_float(
        trigger.get("rotation", 0), f"{field_name}.rotation"
    )
    scale = _finite_float(trigger.get("scale", 1), f"{field_name}.scale")

    activates = trigger.get("activates", {})
    if not isinstance(activates, dict):
        raise ValueError(f"{field_name}.activates должен быть объектом")

    unknown = set(activates) - set(TRIGGER_FILTER_FIELDS)
    if unknown:
        raise ValueError(
            f"{field_name}.activates: неизвестные поля: "
            f"{', '.join(sorted(unknown))}"
        )

    normalized_filter = {}
    for filter_field, values in activates.items():
        path = f"{field_name}.activates.{filter_field}"
        if not isinstance(values, (list, tuple)):
            raise ValueError(f"{path} должен быть списком")
        if not values:
            raise ValueError(f"{path} не должен быть пустым списком")
        normalized = []
        for index, value in enumerate(values):
            if filter_field == "layer":
                normalized.append(_finite_float(value, f"{path}[{index}]"))
            else:
                if not isinstance(value, str) or not value:
                    raise ValueError(
                        f"{path}[{index}] должно быть непустой строкой"
                    )
                normalized.append(value)
        normalized_filter[filter_field] = tuple(normalized)

    return {
        "polygon": polygon,
        "position": position,
        "rotation": rotation,
        "scale": scale,
        "activates": normalized_filter,
    }


def trigger_world_polygon(trigger: dict):
    """Мировые вершины нормализованного триггера."""

    return transform_polygon(
        trigger["polygon"],
        trigger["position"],
        trigger["rotation"],
        trigger["scale"],
    )


def filter_matches(activates: dict, identity: dict) -> bool:
    """Проверяет объект по фильтру ``activates``.

    ``activates`` уже нормализован ``validate_trigger``.  Пустой словарь
    означает «любой объект».
    """

    for field_name, allowed in activates.items():
        value = identity.get(field_name)
        if value is None:
            return False
        if field_name == "layer":
            value = float(value)
            if not any(abs(value - candidate) <= 1e-9 for candidate in allowed):
                return False
        elif value not in allowed:
            return False
    return True


def convex_polygon_contains_point(point, polygon) -> bool:
    """Содержит ли выпуклый многоугольник точку (включая границу)."""

    if not polygon:
        return False
    x, y = float(point[0]), float(point[1])
    direction = 0
    for index, current in enumerate(polygon):
        following = polygon[(index + 1) % len(polygon)]
        cross = (
            (following[0] - current[0]) * (y - current[1])
            - (following[1] - current[1]) * (x - current[0])
        )
        if abs(cross) <= 1e-9:
            continue
        sign = 1 if cross > 0 else -1
        if direction and sign != direction:
            return False
        direction = sign
    return True


@dataclass(frozen=True, slots=True)
class TriggerTarget:
    """Идентичность и визуальная часть объекта, попавшего под фильтр."""

    render_id: int
    object_id: str
    name: str
    class_name: str
    type_name: str
    layer: float
    x: float
    y: float
    rotation: float
    scale: float
    polygon: tuple | None

    def identity(self) -> dict:
        return {
            "name": self.name,
            "class": self.class_name,
            "type": self.type_name,
            "layer": self.layer,
        }
