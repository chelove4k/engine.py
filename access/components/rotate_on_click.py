from pyengine import Component


class rotate_on_click(Component):
    """Rotate the attached object's visual when it is clicked."""

    degrees = 45

    def on_mouse_click(self, event):
        if self.GameObject.Visual is not None:
            self.GameObject.Visual.rotation += float(self.degrees)

        self.create_object(json_text=
            {
    "object_type": "dynamic_object",
    "id": {
      "name": "runner",
      "class": "test",
      "type": "dynamic_object"
    },
    "visual": {
      "sprite": "access/resources/img.png",
      "layer": 1,
      "position": [0, 0],
      "scale": 0.2,
      "rotation": 0,
      "alpha": 1
    },
    "scripting": {
      "scripts": ["access\\scripts\\test.py"],
      "network": []
    }
  }
        )
