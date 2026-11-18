"""Shared engine constants.

Keeping these values in one place prevents the simulation clock, renderer and
script loop from drifting apart.
"""

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
COMPONENTS_DIR = PROJECT_DIR / "access" / "components"


def resolve_project_path(path):
    """Resolve a scene/script/asset path against the project directory."""

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

# Scripts used to ask the Manager whether their object still exists on every
# loop iteration.  That is one remote call per script per frame; at a few
# dozen scripts it starts to dominate the loop.  Checking every 50 ms is
# enough to stop a removed script promptly without touching the Manager in
# the common case.
OBJECT_LIVENESS_PERIOD = 0.05

MAX_CACHE_STRING_BYTES = 64
MAX_CATCH_UP_STEPS = 5

# Per-stripe ring of segment ids that became dirty since the last poll.  Its
# purpose is to make the common "a few scripts moved" frame proportional to
# the number of dirty sprites instead of the number of active sprites in every
# dirty stripe.  When a stripe ring overflows, the reader falls back to a full
# scan of that stripe and resets the ring.
DIRTY_RING_CAPACITY = 256

# Pointer snapshot shared through one ObjectCache segment.  Scripts read it
# directly from shared memory instead of asking the Manager for an event list
# on every mouse move.  ``hovered`` is -1 when no sprite is under the pointer.
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

# Keyboard events live in a shared-memory ring.  Every script used to copy
# the whole Manager event list on each key press; with many scripts that both
# saturated the Manager and made old events replay in a burst.
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

# The engine clock, tick, input counters and the currently pressed keys used
# to live in six separate ValueCache segments.  Every script loop iteration
# locked each of them separately, and key state was additionally polled from
# the Manager by a background thread per worker.  One segment lets a script
# read the whole hot path with a single get_many() call.
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
    """Size of one ObjectCache segment for a {name: type} description."""

    return 4 + sum(
        MAX_CACHE_STRING_BYTES if value_type is str else 4
        for value_type in struct.values()
    )


ENGINE_STATE_SEGMENT_BYTES = _struct_segment_bytes(ENGINE_STATE_STRUCT)


def pack_pressed_keys(keys):
    """Pack pressed key names into the fixed-size shared string fields.

    Chunks are kept below the per-field string limit.  The number of used
    chunks is published separately, so stale chunks from a previous snapshot
    are simply ignored by readers.
    """

    chunks = []
    current = ""
    for key in sorted(keys):
        if len(key.encode("utf-8")) > MAX_CACHE_STRING_BYTES:
            raise ValueError(
                f"Key name exceeds {MAX_CACHE_STRING_BYTES} bytes: {key!r}"
            )
        candidate = key if not current else current + PRESSED_KEY_SEPARATOR + key
        if len(candidate.encode("utf-8")) > MAX_CACHE_STRING_BYTES:
            chunks.append(current)
            if len(chunks) > PRESSED_KEY_CHUNKS:
                raise ValueError(
                    "Too many pressed keys to publish to shared memory"
                )
            current = key
        else:
            current = candidate
    if current:
        chunks.append(current)
    if len(chunks) > PRESSED_KEY_CHUNKS:
        raise ValueError("Too many pressed keys to publish to shared memory")
    return chunks


def unpack_pressed_keys(chunks):
    """Rebuild pressed key names from published chunks."""

    keys = []
    for chunk in chunks:
        if not chunk:
            continue
        keys.extend(chunk.split(PRESSED_KEY_SEPARATOR))
    return frozenset(keys)



def unpack_input_state(value):
    """Accept the shared input-state dict and the older key tuple form."""

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
