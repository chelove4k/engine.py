import math
import queue
import threading
import time
from memory_lib import ObjectCache


class OverlaySettings:
    def __init__(self):
        self._lock = threading.Lock()
        self._features = {
            "collision": {"enabled": False, "color": (0, 255, 255, 255), "width": 2.0},
            "info": {"enabled": False, "color": (255, 255, 255, 255)},
            "info_id": {"enabled": False},
            "info_name": {"enabled": False},
            "info_coords": {"enabled": False},
            "info_scripts": {"enabled": False},
            "info_transform": {"enabled": False},
        }

    def get(self, feature):
        with self._lock:
            settings = self._features.get(feature)
            return None if settings is None else dict(settings)

    def update(self, feature, **values):
        with self._lock:
            if feature not in self._features:
                return False
            self._features[feature].update(values)
            return True

    def snapshot(self):
        with self._lock:
            return {name: dict(values) for name, values in self._features.items()}


_OVERLAY_COLORS = {
    "cyan": (0, 255, 255, 255), "green": (0, 255, 0, 255),
    "red": (255, 0, 0, 255), "yellow": (255, 255, 0, 255),
    "white": (255, 255, 255, 255), "orange": (255, 165, 0, 255),
}

_CLEAR_SELECTORS = {
    "c": "class", "class": "class", "класс": "class",
    "n": "name", "name": "name", "имя": "name",
    "l": "layer", "layer": "layer", "уровень": "layer",
    "t": "type", "type": "type", "тип": "type",
}


def _clear_usage():
    print("Использование: clear <class|layer|name|type> <value>")
    print("Сокращения: c, l, n, t")


def _stream_request(
    main_in_stream, out_stream, command, payload=None, timeout=2.0
):
    """Отправляет команду Stream и ждёт ответ в очереди консоли."""

    request_id = f"console:{command}:{time.monotonic_ns()}"
    message = {
        "command": command,
        "request_id": request_id,
        "reply": "console",
    }
    if payload:
        message.update(payload)
    main_in_stream.put(message)

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"Stream не ответил на команду {command}")
            return None
        try:
            response = out_stream.get(timeout=min(0.1, remaining))
        except queue.Empty:
            continue
        except (EOFError, BrokenPipeError, OSError) as error:
            print(f"Очередь консоли закрылась: {error}")
            return None
        if isinstance(response, dict) and request_id in response:
            return response[request_id]


def _clear_command(
    args, cache, vcache, main_in_stream, console_out_stream=None
):
    """Удаляет все объекты, совпадающие по одному полю идентичности или визуальному слою."""

    if not args or args[0].lower() in {"help", "?"}:
        _clear_usage()
        return

    field = _CLEAR_SELECTORS.get(args[0].lower())
    if field is None:
        _clear_usage()
        return
    if len(args) < 2:
        _clear_usage()
        return

    raw_value = " ".join(args[1:])
    if field == "layer":
        try:
            value = float(raw_value)
        except ValueError:
            print(f"Слой должен быть числом: {raw_value!r}")
            return
    else:
        value = raw_value

    if main_in_stream is None:
        print("Очередь команд движка недоступна")
        return

    if console_out_stream is not None:
        payload = _stream_request(
            main_in_stream,
            console_out_stream,
            "clear",
            {"field": field, "value": value},
        )
        if payload is None:
            return
        if "error" in payload:
            print(f"Ошибка clear: {payload['error']}")
            return
        removed = payload.get("removed", 0)
        print(f"clear {field} {raw_value}: удалено объектов — {removed}")
        return

    # Локальный путь для тестов и встраиваний без процесса Stream.
    removed = 0
    for object_id, record in list(cache.items()):
        if not isinstance(record, dict):
            continue

        dict_data = record.get("dict_data")
        if not isinstance(dict_data, dict):
            continue

        if field == "layer":
            if "visual" not in dict_data.get("_components", ()):
                continue
            render_id = record.get("_render_id")
            if render_id is None:
                continue
            current = vcache.get(render_id, "layer")
            if not math.isclose(current, value, rel_tol=1e-6, abs_tol=1e-6):
                continue
        else:
            identity = dict_data.get("_identity")
            if not isinstance(identity, dict):
                continue
            current = (
                (identity.get("type") or dict_data.get("object_type"))
                if field == "type" else identity.get(field)
            )
            if current != value:
                continue

        main_in_stream.put({"command": "remove", "id": object_id})
        removed += 1

    print(f"clear {field} {raw_value}: удалено объектов — {removed}")


def _parse_overlay_color(value):
    color = _OVERLAY_COLORS.get(value.lower())
    if color is not None:
        return color
    if len(value) == 7 and value.startswith("#"):
        try:
            return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5)) + (255,)
        except ValueError:
            pass
    raise ValueError("Цвет: cyan, green, red, yellow, white, orange или #RRGGBB")


def _overlay_command(args, overlay_settings):
    if overlay_settings is None:
        print("Настройки overlay недоступны")
        return
    if not args or args[0] in {"help", "?"}:
        print("overlay [collision] [on|off|toggle|status]")
        print("overlay collision color <name|#RRGGBB>")
        print("overlay collision width <1..8>")
        print("overlay info [on|off|toggle|status]")
        print("overlay info <id|name|coords|scripts|transform> [on|off|toggle]")
        return
    if args[0] == "info" and len(args) > 1 and args[1] in {"on", "off", "toggle"}:
        current = overlay_settings.get("info")["enabled"]
        enabled = not current if args[1] == "toggle" else args[1] == "on"
        overlay_settings.update("info", enabled=enabled)
        for category in ("info_id", "info_name", "info_coords", "info_scripts", "info_transform"):
            overlay_settings.update(category, enabled=enabled)
        print(f"overlay info: {'on' if enabled else 'off'}")
        return
    if args[0] == "info" and len(args) > 1:
        category = {"id": "info_id", "name": "info_name", "coords": "info_coords",
                    "scripts": "info_scripts", "transform": "info_transform"}.get(args[1])
        if category is not None:
            args = [category, *args[2:]]
    if args[0] == "info" and len(args) == 1:
        enabled = not overlay_settings.get("info")["enabled"]
        overlay_settings.update("info", enabled=enabled)
        for category in ("info_id", "info_name", "info_coords", "info_scripts", "info_transform"):
            overlay_settings.update(category, enabled=enabled)
        print(f"overlay info: {'on' if enabled else 'off'}")
        return
    if args[0] in {"id", "name", "coords", "scripts", "transform"}:
        args[0] = f"info_{args[0]}"
    if args[0] in {"on", "off", "toggle", "color", "width"}:
        args.insert(0, "collision")
    if args[0] == "status":
        if len(args) == 1:
            for name in overlay_settings.snapshot():
                state = overlay_settings.get(name)
                print(f"{name}: {'on' if state['enabled'] else 'off'}")
            return
        args = ["collision", "status"]
    feature = {"colliders": "collision", "collisions": "collision", "objects": "info"}.get(args[0], args[0])
    if feature not in overlay_settings.snapshot():
        print(f"Неизвестный overlay: {args[0]}")
        return
    settings = overlay_settings.get(feature)
    option = args[1] if len(args) > 1 else "toggle"
    if option in {"on", "off", "toggle"}:
        enabled = not settings["enabled"] if option == "toggle" else option == "on"
        overlay_settings.update(feature, enabled=enabled)
        print(f"overlay {feature}: {'on' if enabled else 'off'}")
    elif option == "status":
        extra = f", color={settings['color']}, width={settings['width']}" if feature == "collision" else ""
        print(f"{feature}: {'on' if settings['enabled'] else 'off'}{extra}")
    elif option == "color" and len(args) == 3:
        try:
            color = _parse_overlay_color(args[2])
        except ValueError as error:
            print(error)
            return
        overlay_settings.update(feature, color=color)
        print(f"Цвет overlay {feature}: {args[2]}")
    elif option == "width" and len(args) == 3:
        try:
            width = float(args[2])
            if not 1 <= width <= 8:
                raise ValueError
        except ValueError:
            print("Толщина должна быть числом от 1 до 8")
            return
        overlay_settings.update(feature, width=width)
        print(f"Толщина overlay {feature}: {width:g}")
    else:
        print("Неверные параметры overlay. Введите 'overlay help'.")


def console(
    shared_queue,
    cache,
    proccess_status_info,
    vcache_data,
    icache_data,
    overlay_settings=None,
    main_in_stream=None,
    console_out_stream=None,
):
    vcache = ObjectCache(*vcache_data)
    icache = ObjectCache(*icache_data)
    try:
        while True:
            try:
                raw_parts = input('').strip().split()
            except EOFError:
                return
            except KeyboardInterrupt:
                return

            if not raw_parts:
                continue

            # Существующие команды нечувствительны к регистру.  Исходные токены
            # сохраняются для clear, поскольку имена и классы чувствительны к регистру.
            parts = [part.lower() for part in raw_parts]
            command, arg = parts[0], parts[1:]
            try:
                if command == 'show':
                    print('-' * 10)
                    if main_in_stream is not None and console_out_stream is not None:
                        payload = _stream_request(
                            main_in_stream, console_out_stream, "stats"
                        )
                        if payload is not None:
                            if "error" in payload:
                                print(f"Ошибка stats: {payload['error']}")
                            else:
                                print(
                                    f"Объектов: {payload.get('objects', 0)}, "
                                    f"отрисовано: {payload.get('rendered', 0)}"
                                )
                    else:
                        for key, value in list(cache.items()):
                            print(key, value)
                    print('-' * 10)
                elif command == 'pshow':
                    print(proccess_status_info)
                elif command == 'vshow':
                    if len(arg) < 2:
                        print("Использование: vshow <segment_id> <field>")
                        continue
                    print(vcache.get(int(arg[0]), arg[1]))
                elif command == 'ishow':
                    if len(arg) < 2:
                        print("Использование: ishow <segment_id> <field>")
                        continue
                    print(icache.get(int(arg[0]), arg[1]))
                elif command == 'overlay':
                    _overlay_command(arg, overlay_settings)
                elif command == 'clear':
                    _clear_command(
                        raw_parts[1:], cache, vcache, main_in_stream,
                        console_out_stream,
                    )
                else:
                    shared_queue.put(command)
            except Exception as error:
                print(error)
    finally:
        # Это присоединения только для чтения к shared memory, которой владеет
        # процесс рендерера/главный процесс, поэтому их нужно отсоединять (detach), а не удалять (unlink).
        vcache.detach()
        icache.detach()
