"""Клиент мультиплеера.

Запускает обычное окно движка, но подключается к авторитетному серверу::

    python client_main.py --host 127.0.0.1 --port 7777 --name player

Это тонкая обёртка над ``render.main``: рендер, локальное предсказание и
сетевой клиент живут в одном процессе, поэтому отдельного сетевого процесса
нет.
"""

from __future__ import annotations

import sys

from render import main


if __name__ == "__main__":
    main(["--mode", "join", *sys.argv[1:]])
