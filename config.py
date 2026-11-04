"""Shared engine constants.

Keeping these values in one place prevents the simulation clock, renderer and
script loop from drifting apart.
"""

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent


def resolve_project_path(path):
    """Resolve a scene/script/asset path against the project directory."""

    result = Path(path).expanduser()
    if not result.is_absolute():
        result = PROJECT_DIR / result
    return result.resolve()


SIMULATION_HZ = 100
FIXED_DT = 1.0 / SIMULATION_HZ

SCRIPT_UPDATE_HZ = 500
SCRIPT_UPDATE_PERIOD = 1.0 / SCRIPT_UPDATE_HZ

POSITION_INTERPOLATION_DELAY = 0.04
POSITION_HISTORY_SIZE = 10

SNAPSHOT_ACTIVE_WINDOW = 0.1

MAX_CACHE_STRING_BYTES = 64
MAX_CATCH_UP_STEPS = 5

REGISTRATION_TIMEOUT = 10.0
REGISTRATION_POLL_INTERVAL = 0.05
WORKER_READY_TIMEOUT = 10.0
