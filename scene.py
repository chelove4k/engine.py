"""Scene object schema and validation.

The rules live in their own module so both ``stream`` and ``pyengine`` can use
them without importing the rendering stack or creating circular imports.
"""

from __future__ import annotations

import math

from collision import validate_convex_polygon
from config import MAX_CACHE_STRING_BYTES


OBJECT_TYPE_RULES = {
    "static_object": {
        "allowed": frozenset({"id", "visual", "transform", "components"}),
        "required": frozenset({"id"}),
    },
    "dynamic_object": {
        "allowed": frozenset({"id", "visual", "scripting", "transform", "components"}),
        "required": frozenset(),
    },
    "background_object": {
        "allowed": frozenset({"visual", "transform", "components"}),
        "required": frozenset({"visual"}),
    },
    "camera": {
        "allowed": frozenset({"id", "visual", "transform", "components"}),
        "required": frozenset({"id", "visual"}),
    },
    "manager": {
        "allowed": frozenset({"id", "scripting", "components"}),
        "required": frozenset({"id", "scripting"}),
    },
}

_ENGINE_FIELDS = frozenset(
    {"object_type", "id", "visual", "scripting", "transform", "components",
     "_id", "object_id", "render_id"}
)
_COMPONENT_FIELDS = frozenset({"id", "visual", "scripting", "transform", "components"})


def validate_cache_string(value: str, field_name: str, *, allow_empty: bool = False) -> None:
    """Validate a string that will be stored in a 64-byte shared-memory slot."""

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{field_name} must not be empty")
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_CACHE_STRING_BYTES:
        raise ValueError(
            f"{field_name} is too long: {len(encoded)} bytes "
            f"(maximum {MAX_CACHE_STRING_BYTES} bytes in UTF-8)"
        )


def _finite_float(value, field_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a number") from error
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be a finite number")
    return result


def validate_object_data(data: dict) -> None:
    if not isinstance(data, dict):
        raise TypeError("Описание объекта должно быть JSON-объектом")

    identity = data.get("id")
    object_type = data.get("object_type")
    if not isinstance(object_type, str) or not object_type:
        raise ValueError("У объекта должно быть непустое текстовое поле 'object_type'")
    if identity is not None and not isinstance(identity, dict):
        raise ValueError("Компонент 'id' должен быть объектом")
    if identity is not None and identity.get("type", object_type) != object_type:
        raise ValueError("Поле 'id.type' должно совпадать с 'object_type'")
    rules = OBJECT_TYPE_RULES.get(object_type)
    if rules is None:
        raise ValueError(
            f"Неизвестный тип объекта {object_type!r}. "
            f"Допустимые типы: {', '.join(OBJECT_TYPE_RULES)}"
        )

    components = set(data) & _COMPONENT_FIELDS
    unknown = set(data) - _ENGINE_FIELDS
    if unknown:
        raise ValueError(f"Неизвестные компоненты объекта: {', '.join(sorted(unknown))}")

    forbidden = components - rules["allowed"]
    missing = rules["required"] - components
    if forbidden or missing:
        details = []
        if forbidden:
            details.append(f"запрещены компоненты: {', '.join(sorted(forbidden))}")
        if missing:
            details.append(f"обязательны компоненты: {', '.join(sorted(missing))}")
        raise ValueError(f"Тип {object_type!r}: {'; '.join(details)}")

    if identity is not None:
        required_identity = {"name", "class"}
        missing_identity = required_identity - set(identity)
        if missing_identity:
            raise ValueError(
                f"В компоненте 'id' отсутствуют поля: {', '.join(sorted(missing_identity))}"
            )
        if "layer" in identity:
            raise ValueError("id.layer was moved to visual.layer")
        validate_cache_string(identity["name"], "id.name")
        validate_cache_string(identity["class"], "id.class")
        if "type" in identity:
            validate_cache_string(identity["type"], "id.type")

    if "visual" in data:
        visual = data["visual"]
        if not isinstance(visual, dict):
            raise ValueError("Компонент 'visual' должен быть объектом")
        required = {"sprite", "position", "scale", "rotation", "alpha"}
        missing_visual = required - set(visual)
        if missing_visual:
            raise ValueError(
                f"В компоненте 'visual' отсутствуют поля: {', '.join(sorted(missing_visual))}"
            )
        validate_cache_string(visual["sprite"], "visual.sprite")

        position = visual["position"]
        if not isinstance(position, (list, tuple)) or len(position) != 2:
            raise ValueError("Поле 'visual.position' должно содержать два числа")
        _finite_float(position[0], "visual.position[0]")
        _finite_float(position[1], "visual.position[1]")
        _finite_float(visual["scale"], "visual.scale")
        _finite_float(visual["rotation"], "visual.rotation")
        _finite_float(visual["alpha"], "visual.alpha")
        _finite_float(visual.get("layer", 0), "visual.layer")

    if "transform" in data:
        transform = data["transform"]
        if not isinstance(transform, dict) or set(transform) != {"polygon"}:
            raise ValueError("transform must contain only a polygon field")
        if "visual" not in data:
            raise ValueError("transform collider requires visual for position, rotation, and scale")
        validate_convex_polygon(transform["polygon"])

    if "components" in data:
        specs = data["components"]
        if not isinstance(specs, list):
            raise ValueError("components must be a list of component definitions")
        for index, spec in enumerate(specs):
            if not isinstance(spec, dict):
                raise ValueError(f"components[{index}] must be an object")
            if not isinstance(spec.get("path"), str) or not spec["path"].endswith(".py"):
                raise ValueError(f"components[{index}].path must be a Python file path")
            if not isinstance(spec.get("properties", {}), dict):
                raise ValueError(f"components[{index}].properties must be an object")

    if "scripting" in data:
        scripting = data["scripting"]
        scripts = scripting.get("scripts", []) if isinstance(scripting, dict) else None
        network = scripting.get("network", []) if isinstance(scripting, dict) else None
        if (not isinstance(scripting, dict)
                or not isinstance(scripts, list)
                or any(not isinstance(path, str) or not path for path in scripts)):
            raise ValueError("Компонент 'scripting.scripts' должен быть списком путей")
        if not isinstance(network, list) or any(not isinstance(item, str) for item in network):
            raise ValueError("Компонент 'scripting.network' должен быть списком строк")
