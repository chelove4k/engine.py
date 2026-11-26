# Как устроен код pyengine

Практическая карта исходников: кто за что отвечает, как модули связаны и в
каком порядке всё выполняется. Документ дополняет [README.MD](../README.MD)
(пользовательская часть) и [NETWORK_ARCHITECTURE.md](../NETWORK_ARCHITECTURE.md)
(детали протокола).

## Содержание

- [Четыре слоя проекта](#четыре-слоя-проекта)
- [Потоки выполнения](#потоки-выполнения)
- [Корневые модули](#корневые-модули)
- [Пакет `net`](#пакет-net)
- [Контент `access`](#контент-access)
- [Тесты](#тесты)
- [Быстрые ориентиры](#быстрые-ориентиры)
- [Карта зависимостей](#карта-зависимостей)

---

## Четыре слоя проекта

| Слой | Файлы | Роль |
| --- | --- | --- |
| Публичный API | `pyengine.py` | То, с чем работает автор скрипта: `GameObject`, `script`, `Component`, ввод, камера |
| Ядро/процессы | `render.py`, `stream.py`, `scripter_runner.py`, `server_main.py`, `client_main.py` | Окно, реестр объектов, исполнение скриптов, запуск сервера и клиента |
| Инфраструктура | `memory_lib.py`, `config.py`, `scene.py`, `collision.py`, `caches.py`, `console.py` | Разделяемая память, валидация сцен, коллизии, консоль |
| Сеть | `net/*` | Протокол, транспорт, сервер, клиент, предсказание и снапшоты |

Главная мысль: **одна и та же симуляция** исполняется в процессе
`scripter_runner`, а `render`, `Stream`, `GameServer` и `NetClient` только
готовят данные и читают результат.

---

## Потоки выполнения

### Одиночная игра (`solo`)

```text
Arcade Window (render.py)
  ├─ ввод → main_in_stream / input_cache
  ├─ tact_counter → engine_state.tick
  └─ отрисовка ← VisualCache

Stream (отдельный процесс)
  ├─ реестр объектов и scene1.json
  └─ команды → worker_queues

scripter_runner (процесс)
  └─ шагает скрипты на каждом тике → VisualCache
```

`render` публикует тик и ввод, `scripter_runner` считает мир, `Stream`
хранит реестр и регистрирует объекты. Рендер никогда не исполняет игровые
скрипты.

### Выделенный сервер

```text
GameServer (главный процесс)
  ├─ accept-loop, рукопожатие, INPUT
  └─ EngineWorld.step(tick)
         │ net_tick + ввод игроков
         ▼
scripter_runner (процесс)
  ├─ ServerSimulation: ввод по игрокам
  ├─ скрипты ровно один раз за тик
  ├─ VisualCache
  └─ executed_tick = tick
         │
         ▼
GameServer проверяет заявки и рассылает дельты снапшотов
```

### Клиент мультиплеера

```text
Arcade Window + Stream (после WELCOME)
  ├─ локальные скрипты и предсказание своего owner-объекта
  ├─ NetClient.pump() → SnapshotState / corrections / events
  ├─ NetClient.send_input() → INPUT с заявкой
  └─ рендер: интерполяция удалённых + поправки CORRECTION
```

Клиентский `Stream` стартует только после рукопожатия: роль процесса и
`owner_render_id` должны быть известны до запуска скриптов.

---

## Корневые модули

### `pyengine.py` — публичный API и объектная модель

**Зачем:** всё, что импортирует автор скрипта: `from pyengine import *`.

**Что внутри:**

- `Vector2` — простая пара координат с `+`, `-`, `*`.
- `Keyboard` и `Mouse` — чтение ввода из разделяемой памяти. Умеют
  переключаться на `InputSource`, поэтому один и тот же скрипт работает и в
  одиночной игре, и на сервере с сетевым вводом.
- `_Camera` — выбор цели камеры, `move_camera` / `moveto_camera` /
  `switch` / `scale_camera`; пишет позицию прямо в `VisualCache`.
- `GameObject` — объект сцены. Собирает `Visual`, `Transform`, `Identity`,
  `Trigger`, `Scripting`, умеет `register()`, `register_many()`, `remove()`,
  `render()`. Регистрация — это команда в `Stream` с ожиданием ответа: сервер
  выдаёт `object_id`, `render_id` и поколение слота.
- `create_object()` — создаёт объект из JSON-строки, `dict` или списка.
- `TriggerEvent` — событие `enter`/`exit` триггер-зоны с плоскими полями
  `trigger_*` и `object_*`.
- `script` — базовый класс поведения: `start`, `update`, `fixed_update`,
  `stop`, обработчики мыши/клавиатуры, `on_trigger_enter`/`on_trigger_exit`,
  `dispatch_trigger_event`, `find_object`, `create_object`,
  `get_pressed_keys`, `bind_input`, `dispatch_network_click`, `step`.
- `Component` — подкласс `script` для переиспользуемого поведения из
  `access/components`.
- `push_dependencies` / `pop_dependencies` / `current_dependencies` —
  контекст с кэшами и очередями, который наследуют создаваемые объекты.

**Важно:** `script.step()` вызывает не рендер, а `scripter_runner`. Один
экземпляр скрипта живёт в процессе симуляции и видит общие кэши.

### `render.py` — окно Arcade и клиентская сторона сети

**Зачем:** показать мир и связать ввод, камеру, спрайты и сетевой клиент.

**Что внутри:**

- `Game(arcade.Window)` — главное окно:
  - `_render_queue()` разбирает команды `Stream` (добавить/удалить/обновить);
  - `_read_camera_state()`, `_track_camera_target()` — камера;
  - `_interpolate_sprite_positions()` и `_position_from_history()` — плавное
    движение по истории тиков, с ограниченной экстраполяцией;
  - `_hit_test()` и hit-grid — попадание мыши по спрайтам;
  - `_net_update()` — `pump()`, снапшоты, коррекции, отправка ввода;
  - `on_draw()` — отрисовка спрайтов, оверлеев и коллизий.
- `ManagedQueue` и наследники (`RenderInQueue`, `MainInQueue`,
  `ProccesInQueue` и т.д.) — тонкие обёртки над `multiprocessing.Queue`.
- `tact_counter()` — часы одиночной игры: публикует `tick` и `time`.
- `_connect_net_client()` — подключение с повторами и проверкой хэшей.
- `_start_host_server()` — запуск `server_main.py` подпроцессом для `--mode host`.
- `main()` — разбор аргументов и сборка процессов для `solo` / `join` / `host`.

**Ключевая деталь:** клиентский `Stream` создаётся **после** `WELCOME`, чтобы
не запустить чужие `replicate`-скрипты и знать свой `owner_render_id`.

### `stream.py` — реестр объектов и диспетчер команд

**Зачем:** единственный владелец реестра объектов и `render_id`.

**Что внутри:**

- Очередь команд: регистрация, удаление, `find_object`, `clear`, `stats`,
  `camera_targets`, `net_objects`, `trigger_tick`.
- Выдача `render_id` из free-list с поколениями (`generation`) — защита от
  команд старого объекта после переиспользования слота.
- Индексы идентичности (`class`/`name`/`type`) для быстрого поиска.
- Загрузка сцены (`__init_scene_objects`) и регистрация пачкой.
- `__add_script` и `__should_run_script` — фильтрация скриптов по роли
  процесса: `solo`, `server`, `client` (см. `net_role`).
- Реестр триггер-зон (`_triggers`, `_trigger_contacts`): после `trigger_tick`
  `__evaluate_triggers()` сравнивает зоны с визуальными объектами и рассылает
  `enter`/`exit` воркерам командой `trigger_events`.
- Публикация ввода: нажатые клавиши и события мыши кладутся в общую память.
- Уведомление `net_object_events` о новых сетевых объектах, чтобы сервер
  добавил их в снапшоты без опроса всего реестра.
- Запуск процессов `scripter_runner` (`__start_scripter_runners`).

### `scripter_runner.py` — кооперативный исполнитель скриптов

**Зачем:** один процесс и один поток на всю симуляцию.

**Что внутри:**

- `_scripts`: `token -> (instance, complexity, path)`.
- `__loop()` — главный цикл; в сети крутится `__run_net_until_stopped`.
- `__run_once()` — один проход: разобрать команды, один раз прочитать
  `engine_state`, шагнуть все скрипты.
- `__step_scripts()` — у каждого скрипта вызвать `step(state)`; ошибка одного
  скрипта не роняет остальные.
- `__step_net()` — применить ввод игроков, выставить `time`/`tick`, сделать
  ровно один тик, раздать клики и записать `executed_tick`.
- `__request_trigger_evaluation()` — после прохода просит `Stream`
  пересчитать триггеры командой `trigger_tick`.
- `__dispatch_trigger_events()` — раздаёт `enter`/`exit` скриптам объекта,
  чей `render_id` указан в событии.
- `__dispatch_network_clicks()` — клик доставляется всем подходящим объектам,
  а не только владельцу ввода.
- `__handle_worker_message()` — `add_script`, `remove`, `shutdown`,
  `net_join`, `net_leave`; загрузка модуля и активация `start()`.

### `server_main.py` — headless-сервер

**Зачем:** поднять авторитетный мир без окна Arcade.

**Что внутри:**

- `Runtime` — создаёт разделяемую память (`engine_state`, `VisualCache`,
  `IdCache`, кольца ввода), очереди, `executed_tick`, `net_object_events`;
  запускает `Stream` с `net_role="server"` и `scripter_runner`.
- `GameServer` + `EngineWorld` — сетевой цикл и мост к симуляции.
- `ServerConsole` — команды `players`, `netstat`, `kick`, `anticheat`,
  добавление/остановка отладочных скриптов.
- `_wait_scene()` — ждёт, пока сцена зарегистрируется, и возвращает объекты
  с сетевыми ролями.
- `main()` — аргументы `scene`, `--host`, `--port`, `--max-players`,
  `--token`, `--anticheat`, `--no-console`.

### `client_main.py` — точка входа клиента

20 строк: вызывает `render.main(["--mode", "join", *sys.argv[1:]])`.
Вся логика клиента — в `render.py` и `net/client.py`.

### `config.py` — общие константы и раскладки памяти

**Зачем:** одно место для чисел, от которых зависят все процессы.

**Что внутри:**

- `SIMULATION_HZ = 100`, `FIXED_DT`, `SCRIPT_UPDATE_PERIOD`,
  `POSITION_INTERPOLATION_DELAY = 0.04`, `POSITION_HISTORY_SIZE`,
  `SNAPSHOT_ACTIVE_WINDOW`, `OBJECT_LIVENESS_PERIOD`.
- `resolve_project_path()` — пути сцен/скриптов/ресурсов относительно проекта.
- Описания структур: `MOUSE_MOTION_STRUCT`, `KEYBOARD_EVENT_STRUCT`,
  `ENGINE_STATE_STRUCT`; размеры сегментов и ёмкости колец.
- `pack_pressed_keys` / `unpack_pressed_keys` / `unpack_input_state` — ввод
  в фиксированных слотах shared memory.

### `scene.py` — схема и валидация сцен

**Зачем:** не пускать в движок некорректный JSON.

**Что внутри:**

- `OBJECT_TYPE_RULES` — допустимые и обязательные компоненты для
  `static_object`, `dynamic_object`, `background_object`, `camera`, `manager`,
  `trigger_object`.
- `custom_component_names()` — верхнеуровневые ключи, которые являются
  компонентами.
- `resolve_component_path()` — `access/components/<name>.py`.
- `validate_component_properties()` и `validate_object_data()` — проверки
  полей, чисел, строк и лишних компонентов.
- `validate_cache_string()` — ограничение длины строки под слот shared memory.

### `triggers.py` — схема и геометрия триггер-зон

**Зачем:** отделить проверку `trigger` от сцены и сетевого слоя.

**Что внутри:** `validate_trigger()` (полигон, позиция, поворот, масштаб,
фильтр `activates`), `trigger_world_polygon()`, `filter_matches()` (AND между
полями, OR внутри списков), `convex_polygon_contains_point()` и
`TriggerTarget` — снимок объекта, попавшего под фильтр.

### `collision.py` — геометрия коллизий

**Зачем:** проверять пересечения `Transform`-полигонов.

**Что внутри:** `validate_convex_polygon()`, `polygon_bounds()`,
`transform_polygon()` (локальные координаты → мировые с учётом позиции,
поворота и масштаба), `convex_polygons_overlap()` — SAT для выпуклых
многоугольников.

### `caches.py` — общие подклассы кэшей

`VisualCache` и `IdCache` — наследники `ObjectCache` с фиксированными
раскладками полей. Вынесены сюда, чтобы `pyengine`, `render`, `stream` и
`scripter_runner` не дублировали определения.

### `memory_lib.py` — разделяемая память

**Зачем:** быстрый обмен состоянием между процессами без `Manager` в горячем
пути.

**Что внутри:**

- `ValueCache` — одно значение в именованном сегменте `shared_memory`
  (число или строка), с блокировкой и кэшем формата.
- `ObjectCache` — массив записей фиксированной ёмкости:
  - раскладка полей через `struct`, сегменты и stripe-блокировки;
  - `get` / `set` / `get_many` / `set_many`;
  - быстрый путь `get_position` / `set_position` / `move_position`;
  - dirty-маска, очередь и кольцо изменений для инкрементального рендера;
  - `clear_changes`, `close`, `detach`.
- `FieldView` и `PositionView` — лёгкие представления одного поля/блока
  `x, y, tick`, чтобы не собирать словарь на каждый кадр.

### `console.py` — консольные команды движка

**Зачем:** отладка без перезапуска.

**Что внутри:** `OverlaySettings` (какие оверлеи включены), парсер команд
`show`, `pshow`, `vshow`, `ishow`, `overlay`, `clear`; `_stream_request()`
для запросов к `Stream`; `_clear_command()` — удаление объектов по
`class`/`name`/`layer`/`type`; `console()` — цикл чтения команд.

---

## Пакет `net`

### `net/__init__.py`

Маркер пакета. Намеренно не импортирует тяжёлые модули: тесты протокола не
должны тянуть Arcade.

### `net/config.py`

Сетевые константы: `PROTOCOL_VERSION`, порт/хост по умолчанию,
`TICK_RATE`, `SNAPSHOT_HZ = 20`, задержки ввода, `INTERP_DELAY_TICKS = 10`,
допуски сверки заявок, таймауты, `AnticheatPolicy` (`log` / `kick`).

### `net/protocol.py`

Бинарный протокол. Заголовок `!BBII` (тип, флаги, seq, tick), тело через
`struct`; JSON только в `EVENT`.

- `MessageType`: `HELLO`, `WELCOME`, `REJECT`, `INPUT`, `SNAPSHOT`,
  `CORRECTION`, `EVENT`, `RESYNC`, `PING`, `PONG`, `ACK`, `BYE`.
- `FrameFlags`, `RejectCode`, `CorrectionReason`, `ProtocolError`.
- `Hello` / `Welcome` / `Reject` — рукопожатие.
- `InputFrame` + `state_crc()` — ввод, клики и заявки на сверку.
- `SpawnRecord` / `StateRecord` / `Snapshot` — дельта-снапшоты.
- `Correction`, `EVENT`, `RESYNC`, `ACK`, `PING/PONG`.
- Пары `encode_*` / `decode_*` для каждого сообщения.

### `net/transport.py`

TCP с префиксом длины, неблокирующие сокеты.

- `TcpConnection` — буферы ввода/вывода, `send_payload`, `receive`, `flush`,
  лимиты `MAX_FRAME_BYTES` и `MAX_PENDING_BYTES`.
- `TcpListener` — слушающий сокет; `port=0` выбирает свободный порт.
- `ConnectionSet` — регистрация соединений в `selectors` с автоматическим
  `EVENT_WRITE`, когда есть что отправлять.
- Исключения `TransportError`, `ConnectionClosed`, `FrameTooLarge`.

### `net/loopback.py`

Транспорт в памяти для тестов: задержка, джиттер, потери, лимиты. Позволяет
проверять предсказание и коррекции без сокетов.

### `net/clock.py`

- `ServerClock` — монотонный тик сервера из `perf_counter`; умеет переводить
  тик в секунды и обратно.
- `ClientClockSync` — оценка смещения часов по `PING/PONG` (t0…t3), сглаживание
  выборки и оценка серверного тика.

### `net/hashes.py`

CRC32 сцены и скриптов. При входе клиент и сервер сравнивают хэши, чтобы не
запустить разные миры.

### `net/keys.py`

`normalize_key()` (верхний регистр, без подчёркиваний), `pack_keys()` в
256-битную маску + список редких имён, `unpack_keys()` обратно. Горячий путь
ввода не передаёт строки на каждый тик.

### `net/directives.py`

Разбор `scripting.network` как объекта осей: `NetworkRole` (`authority`,
`replication`, `prediction`, `input`) с производными свойствами
`networked`, `replicated`, `owner`, `predict`, `takes_input`, `server_only`;
`parse_network_directives()`, `role_from_object()`. Один и тот же разбор на
клиенте и сервере; списки старого формата и `room` не поддерживаются.

### `net/input_queue.py`

Очередь кадров одного игрока. Кадры упорядочены по `seq`; опоздавшие и
повторные отбрасываются, при пропуске повторяется последний кадр (не дольше
`repeat_ticks`), клики не повторяются, переполнение вытесняет старое.

### `net/input_source.py`

`InputSnapshot` — состояние ввода на тике; `FrameInputSource` — источник для
одного игрока на сервере (`push`/`advance`); `snapshot_from_frame()` —
сборка снимка из кадра. Это точка подмены локального ввода сетевым.

### `net/simulation.py`

`ServerSimulation` живёт рядом со скриптами: хранит `FrameInputSource` по
`client_id`, связывает `client_id ↔ render_id`, применяет пачку ввода на
тике (`begin_tick`) и отдаёт клики (`take_clicks`).

### `net/state.py`

`ObjectState` — поля одного объекта; `SnapshotState` — плоский словарь
`render_id -> ObjectState`. `apply()` накладывает дельту, `build()` строит
дельту относительно базы; отсюда одинаковое состояние у сервера и клиента.

### `net/reconcile.py`

- `PredictionHistory` — кольцо предсказанных состояний владельца.
- `check_claim()` — сверка заявки клиента с серверной истиной с допусками по
  позиции и повороту.
- `CorrectionBuffer` — визуальные поправки: рендер плавно гасит смещение,
  симуляция его не видит.

### `net/engine_world.py`

Мост `GameServer` → процесс симуляции:

- `on_join` / `on_leave` — выдача и освобождение owner-слота;
- `apply_input` — отправка кадра в `worker_queue`;
- `step(tick)` — команда `net_tick` и ожидание `executed_tick`;
- `snapshot_state` / `object_state` — чтение авторитетного `VisualCache`;
- `poll_network_events` — динамически созданные объекты;
- `set_replicated` — обновление набора реплицируемых `render_id`.

### `net/server.py`

Сам сервер.

- `ClientSession` — состояние подключения, очередь ввода, заявки, RTT,
  счётчики расхождений.
- `GameServer` — `start`, `poll`, `run_forever`, `_accept`, `_on_hello`,
  `_on_input`, `_advance`, `_validate_claims`, `_send_correction`,
  `_broadcast_snapshot`, таймауты, `kick`, `broadcast_event`, `stats`.
- `NullWorld` — заглушка мира для тестов и пустого сервера.

Сервер принимает только ввод, сам считает тик, сверяет заявки и рассылает
дельты снапшотов относительно подтверждённого клиентом тика.

### `net/client.py`

`NetClient` — транспортный слой без Arcade:

- `connect()` — TCP + `HELLO`, ожидание `WELCOME` или `REJECT`;
- `pump()` — неблокирующее чтение и разбор сообщений;
- `send_input()`, `send_ack()`, `send_resync()`, `ping()`;
- `state` (`SnapshotState`), `corrections`, `events`, `clock_sync`;
- `drain_corrections()` / `drain_events()` для рендера;
- `close()`.

---

## Контент `access`

### `access/components/rotate_on_click.py`

Компонент: по `on_mouse_click` увеличивает `Visual.rotation` на `degrees`.
В демо-сцене висит на `tilt_trigger` и создаёт реплицируемых участников.

### `access/components/stress_mover.py`

Компонент стресс-сцены: двигает один спрайт по круговой орбите. Нужен для
режима «скрипт на каждый объект».

### `access/components/stress_scene.py`

Контроллер визуального бенчмарка: создаёт сетку спрайтов и либо двигает их
одним `fixed_update` (режим режиссёра), либо вешает `stress_mover` на каждый
объект (`per_object_scripts`).

### `access/scripts/camera.py`

Демо-управление камерой: WASD двигает цель, Q/E меняет масштаб, 1/2
переключает `alpha`/`beta`. Использует `get_pressed_keys()` — один снимок
ввода на тик.

### `access/scripts/net_player.py`

Детерминированный игрок для сетевого демо: одинаковый код на сервере и
клиенте, поэтому предсказание совпадает с авторитетной симуляцией.

### `access/scripts/test.py`

Демо-«бегунок»: меняет `y` при старте, ходит влево-вправо, меняет `alpha` по
синусу и реагирует на `tilt_trigger`. В `scene1.json` помечен как
`replicate`.

---

## Тесты

### `tests/test_engine.py`

Большой unit-набор ядра: кэши, `Vector2`, пути, ввод, камера, коллизии,
валидация сцен и триггеров, регистрация и жизненный цикл `render_id`,
команды `Stream`, интерполяция, роли скриптов, `enter`/`exit`, сетевой путь
рендера.

### `tests/test_net.py`

Тесты сети: клавиши, протокол, `SnapshotState`, TCP-транспорт, loopback,
часы, очередь ввода, reconcile, оси `scripting.network`, `FrameInputSource`,
`ServerSimulation`, `EngineWorld`, интеграция с `GameServer`, лимит игроков.

### `tests/stress_test.py`

Отдельный раннер (не `unittest`): сценарии `cache`, `contention`,
`collision`, `render`, `registration`, `batch`, `scripts`, `motion`.
Пишет JSON-отчёт и проверяет пороги.

### `tests/fixtures/`

- `motion_component.py` — объект для живого сценария движения;
- `stress_component.py` — нагрузка на CPU и кэш;
- `stress_scene.py`-подобные сцены лежат в `access/scenes`.

---

## Быстрые ориентиры

| Задача | Куда смотреть |
| --- | --- |
| Добавить метод в API скрипта | `pyengine.py`, класс `script` |
| Изменить движение демо-объекта | `access/scripts/test.py`, `net_player.py` |
| Добавить компонент | `access/components/`, затем ключ в JSON-сцене |
| Поменять тик/задержки | `config.py`, `net/config.py` |
| Разобраться с регистрацией и `render_id` | `stream.py` + `GameObject.register()` |
| Изменить формат сцены | `scene.py` (`OBJECT_TYPE_RULES`) |
| Ускорить обмен между процессами | `memory_lib.py` |
| Изменить сетевой протокол | `net/protocol.py`, затем `net/server.py` / `net/client.py` |
| Починить «дёргание» удалённых объектов | `render.py` (`_position_from_history`, `_interpolate_sprite_positions`), `net/config.py` |
| Понять предсказание и коррекции | `net/reconcile.py`, `render.py` (`_apply_net_corrections`) |
| Добавить серверную команду | `server_main.py` (`ServerConsole`) |

---

## Карта зависимостей

```text
render.py ───────► pyengine.py ───────► memory_lib.py
    │                  │                     ▲
    │                  └──► scene.py          │
    ├──► stream.py ─────────► caches.py ──────┘
    │       │
    │       └──► scripter_runner.py ───► net/simulation.py
    │                                      │
    └──► net/client.py ───► net/state.py   │
             │             net/reconcile.py│
             └──► net/protocol.py ◄────────┘
                        ▲
server_main.py ───► net/server.py ───► net/engine_world.py
       │                  │
       └──► stream.py     └──► net/transport.py

config.py и scene.py — общие листья: их импортируют почти все модули.
```

Стрелки означают «использует». Циклов между слоями нет: сеть не импортирует
Arcade, а `pyengine.py` не знает про `GameServer`.
