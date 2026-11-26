"""Хэши сцены и скриптов для проверки совпадения версий при входе.

Клиент и сервер обязаны запускать один и тот же мир.  Хэш сцены — CRC32
байтов файла сцены, хэш скриптов — CRC32 отсортированных пар «путь + файл».
Это дешёво, стабильно между запусками и не требует общего репозитория.
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path

from config import resolve_project_path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def scene_file_path(scene_file: str | None = None) -> Path:
    if scene_file:
        path = Path(scene_file)
        if not path.is_absolute():
            path = PROJECT_DIR / path
        if path.exists():
            return path
        return PROJECT_DIR / "access" / "scenes" / scene_file
    import os

    name = os.environ.get("PYENGINE_SCENE") or "scene1.json"
    return PROJECT_DIR / "access" / "scenes" / name


def _script_paths(data) -> list[str]:
    paths: set[str] = set()
    objects = data if isinstance(data, list) else []
    for item in objects:
        if not isinstance(item, dict):
            continue
        scripting = item.get("scripting")
        if isinstance(scripting, dict):
            for path in scripting.get("scripts") or ():
                if isinstance(path, str) and path:
                    paths.add(path)
        for name in item.get("_components") or ():
            if isinstance(name, str) and name:
                paths.add(f"access/components/{name}.py")
    return sorted(paths)


def compute_hashes(scene_file: str | None = None) -> tuple[int, int]:
    """Возвращает ``(scene_hash, scripts_hash)`` для указанной сцены."""

    path = scene_file_path(scene_file)
    raw = path.read_bytes()
    scene_hash = zlib.crc32(raw) & 0xFFFFFFFF
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return scene_hash, 0

    digest = zlib.crc32(b"")
    for relative in _script_paths(data):
        digest = zlib.crc32(relative.encode("utf-8"), digest)
        try:
            script_path = resolve_project_path(relative)
            if script_path.is_file():
                digest = zlib.crc32(script_path.read_bytes(), digest)
        except (OSError, ValueError):
            continue
    return scene_hash, digest & 0xFFFFFFFF
