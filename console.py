import math
import threading
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


def console(shared_queue, cache, proccess_status_info, vcache_data, icache_data, overlay_settings=None):
    vcache = ObjectCache(*vcache_data)
    icache = ObjectCache(*icache_data)
    while True:
        try:
            parts = input('').strip().lower().split()
            if not parts:
                continue
            command, arg = parts[0], parts[1:]
            
            if not command:
                continue
            elif command == 'show':
                print('-' * 10)
                for i in cache:
                    print(i, cache[i]) if cache[i] is not None else print(i, None)
                print('-' * 10)
            elif command == 'pshow':
                print(proccess_status_info)
            elif command == 'vshow':
                print(vcache.get(int(arg[0]), arg[1]))
            elif command == 'ishow':
                print(icache.get(int(arg[0]), arg[1]))
            elif command == 'overlay':
                _overlay_command(arg, overlay_settings)
            else:
                shared_queue.put(command)
        except Exception as e:
            print(e)
