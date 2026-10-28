# Python components

For the complete scripting API—including lifecycle methods, input, camera controls, object lookup, and collisions—see [SCRIPTING_API.md](SCRIPTING_API.md).

Reusable behavior is written as a class derived from `Component`. The class name must match the Python file name.

```python
from pyengine import Component


class rotate_on_click(Component):
    degrees = 45

    def on_mouse_click(self, event):
        self.GameObject.Visual.rotation += float(self.degrees)
```

Attach it to any rendered scene object and set per-object properties:

```json
"components": [
  {
    "path": "access\\components\\rotate_on_click.py",
    "properties": {"degrees": 45}
  }
]
```

Components and ordinary scripts receive `self.GameObject`, `self.Mouse`, and `self.Keyboard`. Values in `properties` are available directly as instance attributes (`self.degrees`) and override class defaults. Mouse callbacks include `on_mouse_click`, `on_mouse_press`, `on_mouse_release`, `on_mouse_move`, `on_mouse_enter`, `on_mouse_exit`, and `on_mouse_scroll`. `self.Mouse.position` gives world coordinates and `self.Mouse.screen_position` gives screen coordinates; `self.Mouse.is_over()`, `self.Mouse.is_pressed("left")`, and `self.Mouse.was_clicked("left")` provide common checks. Mouse event positions are available as `event.position` and `event.screen_position`.

Keyboard callbacks `on_key_press(event)` and `on_key_release(event)` run for each key event. Use `self.Keyboard.is_pressed("A")` for held keys, `self.Keyboard.was_pressed("SPACE")` / `was_released("SPACE")` for one-shot checks, and `self.Keyboard.get_pressed()` to read the pressed-key set. The event exposes `event.key` and `event.modifiers`. The older `self.is_pressed("A")` helper remains available. Existing `scripting` entries continue to work alongside components.

## Visual layers

Set a numeric `layer` inside an object's `visual` component to control draw order; it defaults to `0`. Higher values are drawn in front of lower values. If two objects have the same layer, the one spawned earlier is drawn in front.

```json
"visual": {
  "sprite": "access/resources/img.png",
  "position": [0, 0],
  "scale": 1,
  "rotation": 0,
  "alpha": 1,
  "layer": 2
}
```
