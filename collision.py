"""Вспомогательные функции коллизий выпуклых многоугольников для коллайдеров Transform."""

from math import cos, isfinite, radians, sin


_EPSILON = 1e-9


def validate_convex_polygon(points):
    """Возвращает нормализованные вершины в локальном пространстве или вызывает ValueError."""
    if not isinstance(points, (list, tuple)) or len(points) < 3:
        raise ValueError("transform.polygon должен содержать как минимум три точки [x, y]")

    vertices = []
    for index, point in enumerate(points):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"transform.polygon[{index}] должен быть парой [x, y]")
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError) as error:
            raise ValueError(f"transform.polygon[{index}] должен содержать числа") from error
        if not isfinite(x) or not isfinite(y):
            raise ValueError(f"transform.polygon[{index}] должен содержать конечные числа")
        vertices.append((x, y))

    count = len(vertices)
    for index, point in enumerate(vertices):
        following = vertices[(index + 1) % count]
        if abs(point[0] - following[0]) <= _EPSILON and abs(point[1] - following[1]) <= _EPSILON:
            raise ValueError("transform.polygon не должен содержать совпадающие соседние точки")

    area_twice = sum(
        vertices[index][0] * vertices[(index + 1) % count][1]
        - vertices[(index + 1) % count][0] * vertices[index][1]
        for index in range(count)
    )
    if abs(area_twice) <= _EPSILON:
        raise ValueError("transform.polygon должен иметь ненулевую площадь")

    direction = 0
    for index in range(count):
        a, b, c = vertices[index], vertices[(index + 1) % count], vertices[(index + 2) % count]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) <= _EPSILON:
            continue
        current = 1 if cross > 0 else -1
        if direction and current != direction:
            raise ValueError("вершины transform.polygon должны описывать выпуклый многоугольник по порядку")
        direction = current
    if direction == 0:
        raise ValueError("transform.polygon должен иметь как минимум три неколлинеарные вершины")
    return tuple(vertices)


def polygon_bounds(polygon):
    """Возвращает ``(min_x, max_x, min_y, max_y)`` для непустого многоугольника."""

    return (
        min(point[0] for point in polygon),
        max(point[0] for point in polygon),
        min(point[1] for point in polygon),
        max(point[1] for point in polygon),
    )


def transform_polygon(vertices, position, rotation_degrees, scale):
    """Преобразует вершины многоугольника из локальных координат в мировые."""
    angle = radians(float(rotation_degrees))
    cosine, sine = cos(angle), sin(angle)
    scale = float(scale)
    if abs(scale) <= _EPSILON:
        return ()
    origin_x, origin_y = float(position[0]), float(position[1])
    return tuple(
        (origin_x + (x * cosine - y * sine) * scale,
         origin_y + (x * sine + y * cosine) * scale)
        for x, y in vertices
    )


def convex_polygons_overlap(first, second, first_bounds=None,
                            second_bounds=None):
    """Проверяет, пересекаются ли два выпуклых многоугольника или касаются (SAT).

    ``first_bounds``/``second_bounds`` могут содержать ранее вычисленные
    значения ``polygon_bounds()``.  Коллайдеры Transform кэшируют их вместе со
    своим мировым многоугольником, поэтому парная проверка не пересчитывает
    четыре значения min/max для каждой стороны.
    """

    if not first or not second:
        return False
    if first_bounds is None:
        first_bounds = polygon_bounds(first)
    if second_bounds is None:
        second_bounds = polygon_bounds(second)
    a, b = first_bounds, second_bounds
    if (a[1] < b[0] - _EPSILON or b[1] < a[0] - _EPSILON
            or a[3] < b[2] - _EPSILON or b[3] < a[2] - _EPSILON):
        return False

    for polygon in (first, second):
        for index, point in enumerate(polygon):
            following = polygon[(index + 1) % len(polygon)]
            axis_x, axis_y = point[1] - following[1], following[0] - point[0]
            if abs(axis_x) <= _EPSILON and abs(axis_y) <= _EPSILON:
                continue
            projections = []
            for candidate in (first, second):
                values = [x * axis_x + y * axis_y for x, y in candidate]
                projections.append((min(values), max(values)))
            p, q = projections
            if p[1] < q[0] - _EPSILON or q[1] < p[0] - _EPSILON:
                return False
    return True
