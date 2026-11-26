"""Разбор директив ``scripting.network``.

Директивы — часть данных сцены, а не сети: сервер и клиент разбирают их
одинаково, поэтому роли объектов совпадают без дополнительного протокола.

Пустой список (или отсутствие поля) сохраняет поведение одиночной игры.
"""

from __future__ import annotations

from dataclasses import dataclass, field

KNOWN_DIRECTIVES = (
    "replicate",
    "owner",
    "input",
    "server",
    "predict",
)

SERVER_ALIASES = {
    "server": "server",
    "server_only": "server",
    "serveronly": "server",
}


@dataclass(slots=True)
class NetworkRole:
    replicate: bool = False
    owner: bool = False
    input: bool = False
    server_only: bool = False
    predict: bool = False
    rooms: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()

    @property
    def networked(self) -> bool:
        return (
            self.replicate
            or self.owner
            or self.input
            or self.server_only
            or self.predict
            or bool(self.rooms)
        )

    @property
    def replicated(self) -> bool:
        """Объект попадает в снапшоты клиентам."""

        return not self.server_only and (
            self.replicate or self.owner or self.predict
        )

    @property
    def takes_input(self) -> bool:
        """Объект читает ввод назначенного игрока."""

        return self.owner or self.input

    def visible_to(self, room: str | None) -> bool:
        if not self.replicated:
            return False
        if not self.rooms:
            return True
        return room is not None and room in self.rooms


EMPTY_ROLE = NetworkRole()


def parse_network_directives(values) -> NetworkRole:
    """Превращает список строк сцены в структурированные роли."""

    if not values:
        return NetworkRole()
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise TypeError("scripting.network должен быть списком строк")

    role = NetworkRole()
    rooms: list[str] = []
    unknown: list[str] = []

    for raw in values:
        if not isinstance(raw, str):
            raise TypeError("Элементы scripting.network должны быть строками")
        name = raw.strip().lower()
        if not name:
            continue
        if name.startswith("room:"):
            room = name[5:].strip()
            if room and room not in rooms:
                rooms.append(room)
            continue
        normalized = SERVER_ALIASES.get(name, name)
        if normalized == "replicate":
            role.replicate = True
        elif normalized == "owner":
            role.owner = True
            role.replicate = True
            role.input = True
        elif normalized == "input":
            role.input = True
        elif normalized == "server":
            role.server_only = True
        elif normalized == "predict":
            role.predict = True
            role.replicate = True
        else:
            unknown.append(raw)

    role.rooms = tuple(rooms)
    role.unknown = tuple(unknown)
    return role


def role_from_object(data: dict) -> NetworkRole:
    """Достаёт роль из данных объекта сцены."""

    scripting = data.get("scripting") if isinstance(data, dict) else None
    if not isinstance(scripting, dict):
        return NetworkRole()
    return parse_network_directives(scripting.get("network") or ())
