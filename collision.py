"""Convex polygon collision helpers used by Transform colliders."""

from math import cos, isfinite, radians, sin


_EPSILON = 1e-9


def validate_convex_polygon(points):
    """Return normalized local-space vertices or raise ValueError."""
    if not isinstance(points, (list, tuple)) or len(points) < 3:
        raise ValueError("transform.polygon must contain at least three [x, y] points")

    vertices = []
    for index, point in enumerate(points):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"transform.polygon[{index}] must be a pair [x, y]")
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError) as error:
            raise ValueError(f"transform.polygon[{index}] must contain numbers") from error
        if not isfinite(x) or not isfinite(y):
            raise ValueError(f"transform.polygon[{index}] must contain finite numbers")
        vertices.append((x, y))

    count = len(vertices)
    for index, point in enumerate(vertices):
        following = vertices[(index + 1) % count]
        if abs(point[0] - following[0]) <= _EPSILON and abs(point[1] - following[1]) <= _EPSILON:
            raise ValueError("transform.polygon must not contain duplicate adjacent points")

    area_twice = sum(
        vertices[index][0] * vertices[(index + 1) % count][1]
        - vertices[(index + 1) % count][0] * vertices[index][1]
        for index in range(count)
    )
    if abs(area_twice) <= _EPSILON:
        raise ValueError("transform.polygon must have non-zero area")

    direction = 0
    for index in range(count):
        a, b, c = vertices[index], vertices[(index + 1) % count], vertices[(index + 2) % count]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) <= _EPSILON:
            continue
        current = 1 if cross > 0 else -1
        if direction and current != direction:
            raise ValueError("transform.polygon vertices must describe a convex polygon in order")
        direction = current
    if direction == 0:
        raise ValueError("transform.polygon must have at least three non-collinear vertices")
    return tuple(vertices)


def transform_polygon(vertices, position, rotation_degrees, scale):
    """Convert local polygon vertices to world-space coordinates."""
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


def convex_polygons_overlap(first, second):
    """Return whether two convex polygons overlap or touch (SAT)."""
    if not first or not second:
        return False
    bounds = []
    for polygon in (first, second):
        bounds.append((min(p[0] for p in polygon), max(p[0] for p in polygon),
                       min(p[1] for p in polygon), max(p[1] for p in polygon)))
    a, b = bounds
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
