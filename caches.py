"""Общие подклассы ``ObjectCache`` для кэшей движка.

Раньше ``VisualCache`` и ``IdCache`` дублировались в ``pyengine.py``,
``render.py`` и ``scripter_runner.py``.  Теперь определения живут в одном
месте, а модули импортируют их отсюда.
"""

from memory_lib import ObjectCache


class VisualCache(ObjectCache):
    """Кэш визуальных полей объекта в разделяемой памяти."""


class IdCache(ObjectCache):
    """Кэш идентификаторов и индексов объекта в разделяемой памяти."""
