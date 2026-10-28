from pyengine import Component


class rotate_on_click(Component):
    """Rotate the attached object's visual when it is clicked."""

    degrees = 45

    def on_mouse_click(self, event):
        if self.GameObject.Visual is not None:
            self.GameObject.Visual.rotation += float(self.degrees)
