"""Разбор ``scripting.network``.

Поле описывает объект целиком, а не отдельный скрипт.  Вместо списка флагов
используются четыре независимые оси с явными значениями по умолчанию::

    {
      "authority": "server",     # local | server
      "replication": "all",      # none | all
      "prediction": "none",      # none | owner
      "input": "none"            # none | owner
    }

Отсутствие поля или пустой объект означают локальный объект одиночной игры.
Сервер и клиент разбирают одну и ту же сцену, поэтому их роли совпадают без
отдельного протокола.
"""

from __future__ import annotations

from dataclasses import dataclass


AXIS_VALUES = {
    "authority": ("local", "server"),
    "replication": ("none", "all"),
    "prediction": ("none", "owner"),
    "input": ("none", "owner"),
}
DEFAULT_NETWORK = {
    "authority": "local",
    "replication": "none",
    "prediction": "none",
    "input": "none",
}


@dataclass(slots=True)
class NetworkRole:
    authority: str = "local"
    replication: str = "none"
    prediction: str = "none"
    input: str = "none"

    @property
    def networked(self) -> bool:
        """Объект существует в серверной симуляции."""

        return self.authority == "server"

    @property
    def replicated(self) -> bool:
        """Состояние объекта попадает в снапшоты клиентам."""

        return self.replication == "all"

    @property
    def owner(self) -> bool:
        """Объекту нужен слот игрока (предсказание или ввод)."""

        return self.prediction == "owner" or self.input == "owner"

    @property
    def predict(self) -> bool:
        """Клиент-владелец предсказывает объект локально."""

        return self.prediction == "owner"

    @property
    def takes_input(self) -> bool:
        """Объект читает ввод назначенного игрока."""

        return self.input == "owner"

    @property
    def server_only(self) -> bool:
        return self.authority == "server" and self.replication == "none"

    @property
    def replicate(self) -> bool:
        return self.replication == "all"


EMPTY_ROLE = NetworkRole()


def parse_network_directives(values) -> NetworkRole:
    """Проверяет объект ``scripting.network`` и возвращает роль.

    ``None`` и ``{}`` дают роль по умолчанию.  Список старого формата намеренно
    не поддерживается: молчаливое преобразование скрывало бы противоречивые
    комбинации вроде ``["server", "replicate"]``.
    """

    if values is None:
        return NetworkRole()
    if not isinstance(values, dict):
        raise TypeError("scripting.network должен быть объектом")

    unknown = set(values) - set(AXIS_VALUES)
    if unknown:
        raise ValueError(
            "scripting.network: неизвестные оси: "
            + ", ".join(sorted(unknown))
        )

    normalized = {}
    for axis, allowed in AXIS_VALUES.items():
        raw = values.get(axis, DEFAULT_NETWORK[axis])
        if not isinstance(raw, str):
            raise TypeError(f"scripting.network.{axis} должен быть строкой")
        value = raw.strip().lower()
        if value not in allowed:
            raise ValueError(
                f"scripting.network.{axis}: допустимые значения "
                f"{', '.join(allowed)}, получено {raw!r}"
            )
        normalized[axis] = value

    role = NetworkRole(**normalized)
    if role.authority == "local":
        if role.replication != "none":
            raise ValueError(
                "scripting.network.replication=all требует authority=server"
            )
        if role.prediction != "none":
            raise ValueError(
                "scripting.network.prediction=owner требует authority=server"
            )
        if role.input != "none":
            raise ValueError(
                "scripting.network.input=owner требует authority=server"
            )
    if role.prediction == "owner":
        if role.authority != "server":
            raise ValueError(
                "scripting.network.prediction=owner требует authority=server"
            )
        if role.replication != "all":
            raise ValueError(
                "scripting.network.prediction=owner требует replication=all"
            )
    if role.input == "owner" and role.authority != "server":
        raise ValueError(
            "scripting.network.input=owner требует authority=server"
        )
    return role


def role_from_object(data: dict) -> NetworkRole:
    """Достаёт роль из данных объекта сцены."""

    scripting = data.get("scripting") if isinstance(data, dict) else None
    if not isinstance(scripting, dict):
        return NetworkRole()
    network = scripting.get("network")
    if network is None:
        return NetworkRole()
    return parse_network_directives(network)
