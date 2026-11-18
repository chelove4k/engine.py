"""Общие константы движка.

Хранение этих значений в одном месте не позволяет часам симуляции, рендереру и
циклу скриптов расходиться.
"""

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
COMPONENTS_DIR = PROJECT_DIR / "access" / "components"


def resolve_project_path(path):
    """Разрешает путь к сцене/скрипту/ресурсу относительно каталога проекта."""

    result = Path(path).expanduser()
    if not result.is_absolute():
        result = PROJECT_DIR / result
    return result.resolve()


SIMULATION_HZ = 100
FIXED_DT = 1.0 / SIMULATION_HZ

SCRIPT_UPDATE_HZ = 100
SCRIPT_UPDATE_PERIOD = 1.0 / SCRIPT_UPDATE_HZ

POSITION_INTERPOLATION_DELAY = 0.04
POSITION_HISTORY_SIZE = 10

SNAPSHOT_ACTIVE_WINDOW = 0.1

# Раньше скрипты на каждой итерации цикла спрашивали у Manager, существует ли
# ещё их объект.  Это один удалённый вызов на скрипт за кадр; уже при
# нескольких десятках скриптов он начинает доминировать в цикле.  Проверки
# каждые 50 мс достаточно, чтобы своевременно остановить удалённый скрипт,
# не обращаясь к Manager в обычном случае.
OBJECT_LIVENESS_PERIOD = 0.05

MAX_CACHE_STRING_BYTES = 64
MAX_CATCH_UP_STEPS = 5

# Кольцевой буфер на каждую полосу (stripe) с id сегментов, ставших dirty с
# момента последнего опроса.  Его цель — сделать обычный кадр «сдвинулось
# несколько скриптов» пропорциональным числу грязных спрайтов, а не числу
# активных спрайтов в каждой грязной полосе.  При переполнении кольца полосы
# читатель переходит к полному сканированию этой полосы и сбрасывает кольцо.
DIRTY_RING_CAPACITY = 256

# Снимок состояния указателя, передаваемый через один сегмент ObjectCache.
# Скрипты читают его напрямую из shared memory вместо запроса списка событий у
# Manager на каждое движение мыши.  ``hovered`` равен -1, если под указателем
# нет спрайта.
MOUSE_MOTION_STRUCT = {
    "seq": int,
    "x": float,
    "y": float,
    "dx": float,
    "dy": float,
    "screen_x": float,
    "screen_y": float,
    "hovered": int,
}
MOUSE_MOTION_FIELDS = tuple(MOUSE_MOTION_STRUCT)
MOUSE_MOTION_BYTES = 4 + 4 * len(MOUSE_MOTION_STRUCT)

# События клавиатуры хранятся в кольцевом буфере в shared memory.  Раньше
# каждый скрипт копировал весь список событий Manager при каждом нажатии
# клавиши; при большом числе скриптов это и перегружало Manager, и приводило
# к пачке повторно проигранных старых событий.
KEYBOARD_EVENT_DOWN = 1
KEYBOARD_EVENT_UP = 2
KEYBOARD_EVENT_CAPACITY = 128
KEYBOARD_EVENT_STRUCT = {
    "sequence": int,
    "type": int,
    "key": str,
    "modifiers": int,
}
KEYBOARD_EVENT_FIELDS = tuple(KEYBOARD_EVENT_STRUCT)
KEYBOARD_EVENT_SEGMENT_BYTES = 4 + 4 + 4 + MAX_CACHE_STRING_BYTES + 4
KEYBOARD_EVENT_BYTES = KEYBOARD_EVENT_SEGMENT_BYTES * KEYBOARD_EVENT_CAPACITY

# Часы движка, tick, счётчики ввода и текущие нажатые клавиши раньше жили в
# шести отдельных сегментах ValueCache.  Каждая итерация цикла скрипта
# блокировала каждый из них по отдельности, а состояние клавиш дополнительно
# опрашивалось у Manager фоновым потоком на каждый worker.  Один сегмент
# позволяет скрипту прочитать весь горячий путь одним вызовом get_many().
PRESSED_KEY_CHUNKS = 8
PRESSED_KEY_SEPARATOR = "\x1f"
ENGINE_STATE_STRUCT = {
    "time": float,
    "tick": int,
    "input_sequence": int,
    "object_generation": int,
    "keyboard_sequence": int,
    "key_count": int,
    **{
        f"keys_{index}": str
        for index in range(PRESSED_KEY_CHUNKS)
    },
}
ENGINE_STATE_FIELDS = tuple(ENGINE_STATE_STRUCT)
ENGINE_STATE_LOOP_FIELDS = (
    "time",
    "tick",
    "input_sequence",
    "object_generation",
    "keyboard_sequence",
    "key_count",
)
PRESSED_KEY_FIELDS = tuple(
    f"keys_{index}" for index in range(PRESSED_KEY_CHUNKS)
)


def _struct_segment_bytes(struct):
    """Размер одного сегмента ObjectCache для описания {name: type}."""

    return 4 + sum(
        MAX_CACHE_STRING_BYTES if value_type is str else 4
        for value_type in struct.values()
    )


ENGINE_STATE_SEGMENT_BYTES = _struct_segment_bytes(ENGINE_STATE_STRUCT)


def pack_pressed_keys(keys):
    """Упаковывает имена нажатых клавиш в строковые поля shared memory фиксированного размера.

    Фрагменты (chunks) не превышают лимит строки на одно поле.  Число занятых
    фрагментов публикуется отдельно, поэтому устаревшие фрагменты от
    предыдущего снимка читатели просто игнорируют.
    """

    chunks = []
    current = ""
    for key in sorted(keys):
        if len(key.encode("utf-8")) > MAX_CACHE_STRING_BYTES:
            raise ValueError(
                f"Имя клавиши превышает {MAX_CACHE_STRING_BYTES} байт: {key!r}"
            )
        candidate = key if not current else current + PRESSED_KEY_SEPARATOR + key
        if len(candidate.encode("utf-8")) > MAX_CACHE_STRING_BYTES:
            chunks.append(current)
            if len(chunks) > PRESSED_KEY_CHUNKS:
                raise ValueError(
                    "Слишком много нажатых клавиш для публикации в shared memory"
                )
            current = key
        else:
            current = candidate
    if current:
        chunks.append(current)
    if len(chunks) > PRESSED_KEY_CHUNKS:
        raise ValueError("Слишком много нажатых клавиш для публикации в shared memory")
    return chunks


def unpack_pressed_keys(chunks):
    """Восстанавливает имена нажатых клавиш из опубликованных фрагментов."""

    keys = []
    for chunk in chunks:
        if not chunk:
            continue
        keys.extend(chunk.split(PRESSED_KEY_SEPARATOR))
    return frozenset(keys)



def unpack_input_state(value):
    """Принимает общий словарь состояния ввода и старую форму в виде кортежа ключей."""

    if isinstance(value, dict):
        return {
            "sequence": value.get("sequence"),
            "motion": value.get("motion"),
            "generation": value.get("generation"),
            "keyboard_events": value.get("keyboard_events"),
            "keyboard_sequence": value.get("keyboard_sequence"),
        }
    if isinstance(value, tuple):
        parts = list(value) + [None] * 5
        return {
            "sequence": parts[0],
            "motion": parts[1],
            "generation": parts[2],
            "keyboard_events": parts[3],
            "keyboard_sequence": parts[4],
        }
    return {
        "sequence": value,
        "motion": None,
        "generation": None,
        "keyboard_events": None,
        "keyboard_sequence": None,
    }

REGISTRATION_TIMEOUT = 10.0
REGISTRATION_POLL_INTERVAL = 0.05
WORKER_READY_TIMEOUT = 10.0
