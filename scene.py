"""Scene object schema and validation.

The rules live in their own module so both ``stream`` and ``pyengine`` can use
them without importing the rendering stack or creating circular imports.
"""

from __future__ import annotations

import copy
import math

from collision import validate_convex_polygon
from config import COMPONENTS_DIR, MAX_CACHE_STRING_BYTES, PROJECT_DIR


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

ENGINE_COMPONENT_NAMES = frozenset(
    {"id", "visual", "scripting", "transform", "components"}
)
ENGINE_FIELDS = frozenset(
    {"object_type", *ENGINE_COMPONENT_NAMES, "_id", "object_id", "render_id"}
)

# These names belong to the engine or to the script lifecycle and cannot be
# overwritten by custom component properties.
RESERVED_COMPONENT_PROPERTIES = frozenset({
    "GameObject", "Camera", "Mouse", "Keyboard", "ComponentConfig", "GlobalTime",
    "start", "update", "fixed_update", "stop", "is_pressed",
    "get_mouse_clicks", "find_object", "create_object", "creat_object",
})


def custom_component_names(data: dict) -> list[str]:
    """Return top-level keys that describe custom components."""

    if not isinstance(data, dict):
        return []
    return [
        name
        for name in data
        if isinstance(name, str)
        and not name.startswith("_")
        and name not in ENGINE_FIELDS
    ]


def resolve_component_path(name: str):
    """Resolve a component name to access/components/<name>.py."""

    if not isinstance(name, str) or not name.isidentifier():
        return None
    if not COMPONENTS_DIR.is_dir():
        return None

    exact = COMPONENTS_DIR / f"{name}.py"
    if exact.is_file():
        return exact.resolve()

    # Be forgiving about filename case on case-sensitive filesystems while
    # still passing the real file name to the script loader (the class name
    # must match the file stem).
    matches = sorted(
        path
        for path in COMPONENTS_DIR.glob("*.py")
        if path.stem.casefold() == name.casefold()
    )
    if len(matches) == 1:
        return matches[0].resolve()
    return None


def _component_path_for_spec(path) -> str:
    try:
        return path.relative_to(PROJECT_DIR).as_posix()
    except ValueError:
        return str(path)


def validate_component_properties(component_name: str, properties) -> None:
    """Validate properties written directly inside a component entry."""

    if not isinstance(properties, dict):
        raise ValueError(
            f"Компонент {component_name!r} должен быть объектом со свойствами"
        )
    for property_name in properties:
        if property_name in {"path", "properties"}:
            raise ValueError(
                f"Свойства компонента {component_name!r} задаются напрямую, "
                f"без поля {property_name!r}"
            )
        if (not isinstance(property_name, str)
                or not property_name.isidentifier()
                or property_name.startswith("_")
                or property_name in RESERVED_COMPONENT_PROPERTIES):
            raise ValueError(
                f"Недопустимое свойство {property_name!r} "
                f"у компонента {component_name!r}"
            )


def component_specs_from_data(data: dict) -> list[dict]:
    """Collect legacy ``components`` entries and top-level components."""

    specs = []
    legacy_specs = data.get("components", []) or []
    if not isinstance(legacy_specs, list):
        raise ValueError("components must be a list of component definitions")
    for index, spec in enumerate(legacy_specs):
        if not isinstance(spec, dict) or not isinstance(spec.get("path"), str):
            raise ValueError(f"components[{index}] must contain a path")
        specs.append({
            "path": spec["path"],
            "properties": copy.deepcopy(spec.get("properties", {})),
        })

    for name in custom_component_names(data):
        resolved = resolve_component_path(name)
        if resolved is None:
            raise ValueError(
                f"Неизвестный компонент {name!r}: "
                f"нет файла access/components/{name}.py"
            )
        properties = data[name]
        validate_component_properties(name, properties)
        specs.append({
            "name": name,
            "path": _component_path_for_spec(resolved),
            "properties": copy.deepcopy(properties),
        })
    return specs


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

    components = set(data) & ENGINE_COMPONENT_NAMES

    forbidden = components - rules["allowed"]
    missing = rules["required"] - components
    if forbidden or missing:
        details = []
        if forbidden:
            details.append(f"запрещены компоненты: {', '.join(sorted(forbidden))}")
        if missing:
            details.append(f"обязательны компоненты: {', '.join(sorted(missing))}")
        raise ValueError(f"Тип {object_type!r}: {'; '.join(details)}")

    # Any other top-level key is a custom component.  Its name must match a
    # Python file in access/components, and its value holds the properties
    # directly (no "path"/"properties" wrapper).
    for name in data:
        if name in ENGINE_FIELDS:
            continue
        if (not isinstance(name, str)
                or not name.isidentifier()
                or name.startswith("_")):
            raise ValueError(
                f"Недопустимое имя компонента {name!r}: используйте имя "
                f"класса и файла из access/components"
            )
        if resolve_component_path(name) is None:
            raise ValueError(
                f"Неизвестный компонент {name!r}: "
                f"нет файла access/components/{name}.py"
            )
        validate_component_properties(name, data[name])

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
