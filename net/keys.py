"""Таблица клавиш для битовой маски сетевого ввода.

Скрипты двигателя работают с нормализованными именами клавиш (``A``,
``SPACE``, ``LEFT``) и сравнивают строки. Передавать строки на каждый тик
дорого, поэтому горячий путь использует 256-битную маску, а неизвестные имена
(редкий случай) уходят в дополнительный список строк.
"""

from __future__ import annotations

BITSET_BYTES = 32
MAX_EXTRA_KEYS = 8
MAX_KEY_BYTES = 32

_LETTERS = tuple(chr(code) for code in range(ord("A"), ord("Z") + 1))
_DIGITS = tuple(str(digit) for digit in range(10))
_FUNCTION = tuple(f"F{index}" for index in range(1, 25))
_NAMED = (
    "SPACE",
    "ENTER",
    "RETURN",
    "ESCAPE",
    "TAB",
    "BACKSPACE",
    "DELETE",
    "INSERT",
    "HOME",
    "END",
    "PAGEUP",
    "PAGEDOWN",
    "LEFT",
    "RIGHT",
    "UP",
    "DOWN",
    "SHIFT",
    "LSHIFT",
    "RSHIFT",
    "CTRL",
    "LCTRL",
    "RCTRL",
    "ALT",
    "LALT",
    "RALT",
    "CAPSLOCK",
    "NUMLOCK",
    "SCROLLLOCK",
    "PAUSE",
    "PRINTSCREEN",
    "MENU",
    "MINUS",
    "EQUAL",
    "BRACKETLEFT",
    "BRACKETRIGHT",
    "BACKSLASH",
    "SEMICOLON",
    "APOSTROPHE",
    "GRAVE",
    "COMMA",
    "PERIOD",
    "SLASH",
    "NUMPAD0",
    "NUMPAD1",
    "NUMPAD2",
    "NUMPAD3",
    "NUMPAD4",
    "NUMPAD5",
    "NUMPAD6",
    "NUMPAD7",
    "NUMPAD8",
    "NUMPAD9",
    "NUMPADADD",
    "NUMPADSUBTRACT",
    "NUMPADMULTIPLY",
    "NUMPADDIVIDE",
    "NUMPADDECIMAL",
    "NUMPADENTER",
)

# Порядок фиксирован: индекс бита — часть протокола.  Менять порядок можно
# только вместе с повышением PROTOCOL_VERSION.
KEY_NAMES = tuple(dict.fromkeys(_LETTERS + _DIGITS + _FUNCTION + _NAMED))
if len(KEY_NAMES) > BITSET_BYTES * 8:
    raise RuntimeError("Таблица клавиш не помещается в битовую маску протокола")

KEY_INDEX = {name: index for index, name in enumerate(KEY_NAMES)}


def normalize_key(name: str) -> str:
    """Повторяет нормализацию движка: верхний регистр без подчёркиваний."""

    return str(name).upper().replace("_", "")


def pack_keys(keys) -> tuple[bytes, tuple[str, ...]]:
    """Кодирует набор клавиш в маску и список неизвестных имён.

    Возвращает ``(bits, extra)``. ``bits`` всегда ровно ``BITSET_BYTES``.
    Неизвестные имена ограничены по количеству и длине, чтобы клиент не мог
    раздуть пакет ввода.
    """

    bits = bytearray(BITSET_BYTES)
    extra: list[str] = []
    for raw in keys:
        name = normalize_key(raw)
        if not name:
            continue
        index = KEY_INDEX.get(name)
        if index is not None:
            bits[index >> 3] |= 1 << (index & 7)
            continue
        if len(extra) >= MAX_EXTRA_KEYS:
            continue
        if len(name.encode("utf-8")) > MAX_KEY_BYTES:
            continue
        if name not in extra:
            extra.append(name)
    return bytes(bits), tuple(extra)


def unpack_keys(bits: bytes | bytearray | memoryview, extra=()) -> frozenset[str]:
    """Восстанавливает набор клавиш из маски и дополнительного списка."""

    keys = {str(name) for name in extra}
    for byte_index, value in enumerate(bits[:BITSET_BYTES]):
        while value:
            low = value & -value
            bit = low.bit_length() - 1
            keys.add(KEY_NAMES[(byte_index << 3) + bit])
            value ^= low
    return frozenset(keys)
