import multiprocessing
from multiprocessing import shared_memory
import struct

from config import MAX_CACHE_STRING_BYTES


class ValueCache:
    MEMORY_TYPES_SIZE = {
        float: 4,
        int: 4,
        str: MAX_CACHE_STRING_BYTES,
    }

    def __init__(self, data=None, name=None, lock=None):
        self.__memory = None
        self._owner = False
        self._fmt = None
        self._size = None
        self._struct = None
        self.fmt_char = None

        if isinstance(data, list):
            name = data[1]
            lock = data[2]
            data = data[0]

        if data is not None:
            try:
                self.size = self.MEMORY_TYPES_SIZE[type(data)] + 1
            except KeyError as error:
                raise TypeError(f"Неподдерживаемый тип ValueCache: {type(data)!r}") from error
            self._set_format(data)

        if name is not None:
            if lock is None:
                raise ValueError("При подключении ValueCache требуется общая блокировка")
            self.__memory = shared_memory.SharedMemory(name=name)
            self.lock = lock
            self.name = name
            self._owner = False
            # Тип полезной нагрузки никогда не меняется после создания.
            # Кэшируем формат struct вместо повторного чтения заголовка
            # и пересборки строки формата при каждом get()/set().
            self._apply_raw_format(self.__memory.buf[0])
        elif name is None:
            if data is None:
                raise ValueError("ValueCache требует начального значения или имени shared memory")
            self.__memory = shared_memory.SharedMemory(create=True, size=self.size)
            self.lock = multiprocessing.Lock()
            self.name = [None, self.__memory.name, self.lock]
            self._owner = True

            for index in range(self.size):
                self.__memory.buf[index] = 0

            if type(data) is str:
                self.__memory.buf[0] = 2
            elif type(data) is float:
                self.__memory.buf[0] = 0
            else:
                self.__memory.buf[0] = 1

            self.set(data)

    def _set_format(self, data):
        if type(data) is str:
            self.fmt_char = "s"
            self._fmt = f"{MAX_CACHE_STRING_BYTES}s"
        elif type(data) is float:
            self.fmt_char = "f"
            self._fmt = "f"
        else:
            self.fmt_char = "i"
            self._fmt = "i"
        self._size = MAX_CACHE_STRING_BYTES if self.fmt_char == "s" else 4
        self._struct = struct.Struct(self._fmt)

    def _apply_raw_format(self, raw_fml):
        if raw_fml == 0:
            self.fmt_char = "f"
            self._fmt = "f"
        elif raw_fml == 1:
            self.fmt_char = "i"
            self._fmt = "i"
        elif raw_fml == 2:
            self.fmt_char = "s"
            self._fmt = f"{MAX_CACHE_STRING_BYTES}s"
        else:
            raise ValueError(f"Неизвестный тип полезной нагрузки ValueCache: {raw_fml}")
        self._size = MAX_CACHE_STRING_BYTES if self.fmt_char == "s" else 4
        self._struct = struct.Struct(self._fmt)

    def get_fml(self):
        raw_fml = self.__memory.buf[0]
        if raw_fml == 0:
            return "f"
        if raw_fml == 1:
            return "i"
        if raw_fml == 2:
            return f"{MAX_CACHE_STRING_BYTES}s"
        raise ValueError(f"Неизвестный тип полезной нагрузки ValueCache: {raw_fml}")

    def get_size(self):
        return MAX_CACHE_STRING_BYTES if self.get_fml().endswith("s") else 4

    def get(self):
        with self.lock:
            raw_val = self._struct.unpack_from(self.__memory.buf, 1)[0]

            if isinstance(raw_val, bytes):
                return raw_val.rstrip(b"\x00").decode("utf-8", errors="ignore")
            return raw_val

    def set(self, data):
        if isinstance(data, str):
            encoded = data.encode("utf-8")
            if len(encoded) > MAX_CACHE_STRING_BYTES:
                raise ValueError(
                    f"Строковое значение слишком длинное: {len(encoded)} байт "
                    f"(максимум {MAX_CACHE_STRING_BYTES})"
                )
            data = encoded

        packed_data = self._struct.pack(data)
        with self.lock:
            self.__memory.buf[1:self._size + 1] = packed_data

    def close(self):
        memory = self.__memory
        if memory is None:
            return
        self.__memory = None
        memory.close()
        if self._owner:
            try:
                memory.unlink()
            except FileNotFoundError:
                pass

    def detach(self):
        """Закрыть это подключение, не удаляя shared memory для других пользователей."""
        memory = self.__memory
        if memory is None:
            return
        self.__memory = None
        memory.close()


class ObjectCache:
    MEMORY_TYPES_SIZE = {
        float: 4,
        int: 4,
        str: MAX_CACHE_STRING_BYTES,
    }

    def __init__(self, id_or_name, data, locks=None, dirty_counts=None,
                 capacity=None, dirty_queue=None, dirty_ring_size=0):
        self.__memory = None
        self._fields = {}
        self._many_plans = {}
        self._lockless_plans = {}
        self.segment_len = 4
        self._field_len = 0
        self.field_bit_mapping = {}
        self._bit_to_field = {}
        self._owner = False
        self._masks = None
        self._dirty_queue = None
        self._dirty_ring_size = 0
        # Быстрый путь для самой частой записи движка: визуальной позиции.
        # Он включается, только когда x, y и tick занимают один непрерывный
        # участок и имеют ожидаемые типы, так что раскладка кэша не влияет.
        self._position_struct = None
        self._position_offset = 0
        self._position_length = 0
        self._position_mask = 0

        if len(data) > 31:
            raise ValueError("ObjectCache поддерживает не более 31 поля (32-битная dirty-маска)")

        for bit_index, field_name in enumerate(data):
            if field_name in self._fields:
                raise ValueError(f"Повторяющееся поле ObjectCache: {field_name!r}")
            size = self.MEMORY_TYPES_SIZE[data[field_name]]
            if data[field_name] is str:
                fmt_char = f"{size}s"
            elif data[field_name] is float:
                fmt_char = "f"
            else:
                fmt_char = "i"

            self._fields[field_name] = (
                [self._field_len, self._field_len + size],
                # Заранее собираем struct, чтобы get/set не разбирали строку
                # формата при каждом вызове. Горячий визуальный путь читает
                # несколько полей на спрайт за кадр.
                struct.Struct(fmt_char),
            )
            bit_value = 1 << bit_index
            self.field_bit_mapping[field_name] = bit_value
            self._bit_to_field[bit_value] = field_name
            self._field_len += size

        x_field = self._fields.get("x")
        y_field = self._fields.get("y")
        tick_field = self._fields.get("tick")
        if (
            x_field is not None
            and y_field is not None
            and tick_field is not None
            and data.get("x") is float
            and data.get("y") is float
            and data.get("tick") is int
            and x_field[0][1] == y_field[0][0]
            and y_field[0][1] == tick_field[0][0]
        ):
            self._position_struct = struct.Struct("ffi")
            self._position_offset = x_field[0][0]
            self._position_length = tick_field[0][1] - x_field[0][0]
            self._position_mask = (
                self.field_bit_mapping["x"]
                | self.field_bit_mapping["y"]
                | self.field_bit_mapping["tick"]
            )

        self.segment_len = 4 + self._field_len

        if isinstance(id_or_name, str):
            if locks is None:
                raise ValueError("При подключении ObjectCache требуется список общих блокировок")
            if capacity is None:
                raise ValueError(
                    "capacity обязателен при подключении ObjectCache"
                )
            self.__memory = shared_memory.SharedMemory(name=id_or_name)
            self.key = id_or_name
            self._owner = False
        elif isinstance(id_or_name, int):
            self.__memory = shared_memory.SharedMemory(create=True, size=id_or_name)
            self.key = self.__memory.name
            self._owner = True
            self.__memory.buf[:] = bytes(self.__memory.size)
        else:
            raise TypeError("ObjectCache ожидает имя shared memory или размер в байтах")

        if capacity is None:
            capacity = id_or_name // self.segment_len
        capacity = int(capacity)
        if capacity < 0 or capacity * self.segment_len > self.__memory.size:
            self._abort_init()
            raise ValueError(
                "Ёмкость ObjectCache не помещается в shared memory"
            )
        self.segments_count = capacity
        self._mask_bytes = self.segments_count * 4
        # Все dirty-маски живут одним непрерывным блоком в начале shared
        # memory. Типизированное представление удешевляет доступ к маске
        # сегмента и позволяет пакетным читателям пропускать чистые сегменты,
        # не распаковывая маску из сегмента с шагом в 100 байт.
        self._masks = memoryview(
            self.__memory.buf[:self._mask_bytes]
        ).cast("i")
        if locks is None:
            lock_count = min(64, max(1, self.segments_count))
            self.locks = [multiprocessing.Lock() for _ in range(lock_count)]
        elif isinstance(locks, (list, tuple)):
            self.locks = locks
        else:
            self.locks = [locks]

        # Один счётчик на stripe блокировки. Он отслеживает, сколько сегментов
        # в stripe сейчас имеют ненулевую dirty-маску, чтобы рендерер мог
        # пропустить целый чистый stripe, не трогая каждый сегмент. Блок
        # создаёт явно владелец и передаёт каждому подключению; кэши без него
        # сохраняют старое поведение с полным сканированием. Если блок есть у
        # писателя, но не у читателя (или наоборот), инвариант счётчика
        # нарушится, поэтому блок должен передаваться вместе с дескриптором
        # кэша.
        if dirty_counts is not None and len(dirty_counts) != len(self.locks):
            self._abort_init()
            raise ValueError(
                "dirty_counts должен содержать по одному счётчику "
                "на каждую блокировку ObjectCache"
            )
        self._dirty_counts = dirty_counts

        # Необязательное dirty-кольцо id на stripe. В отличие от масок, это
        # отдельный общий массив, поэтому раскладка и ёмкость кэша остаются
        # точно такими же с ним и без него. Писатели добавляют запись только
        # при переходе маски 0 -> dirty, удерживая блокировку stripe, так что
        # у сегмента не больше одной ожидающей записи в кольце. Заполненное
        # кольцо выставляет флаг переполнения, и читатель сканирует весь stripe.
        if dirty_queue is not None:
            if dirty_ring_size <= 0:
                self._abort_init()
                raise ValueError(
                    "dirty_ring_size должен быть положительным при dirty_queue"
                )
            expected = len(self.locks) * (3 + int(dirty_ring_size))
            if len(dirty_queue) != expected:
                self._abort_init()
                raise ValueError(
                    "размер dirty_queue должен быть locks * (3 + dirty_ring_size)"
                )
        elif dirty_ring_size:
            self._abort_init()
            raise ValueError(
                "dirty_ring_size требует dirty_queue"
            )
        self._dirty_queue = dirty_queue
        self._dirty_ring_size = int(dirty_ring_size)

    def _abort_init(self):
        """Освободить всё, что было захвачено до ошибки валидации в конструкторе."""

        masks = self._masks
        self._masks = None
        if masks is not None:
            try:
                masks.release()
            except ValueError:
                pass
        memory = self.__memory
        self.__memory = None
        if memory is None:
            return
        memory.close()
        if self._owner:
            try:
                memory.unlink()
            except FileNotFoundError:
                pass

    @property
    def dirty_queue(self):
        return self._dirty_queue

    @property
    def dirty_ring_size(self):
        return self._dirty_ring_size

    @property
    def capacity(self):
        return self.segments_count

    @property
    def dirty_counts(self):
        return self._dirty_counts

    def has_pending_changes(self):
        """Возвращает, есть ли в каком-либо stripe непрочитанные изменения.

        Без счётчиков каждый stripe приходится сканировать, поэтому ответ
        всегда ``True``. Со счётчиками это чтение одного небольшого массива
        без блокировки, что позволяет простаивающему рендереру не строить
        группировку по сегментам. Писатель, гоняющийся с проверкой, может
        лишь задержать своё обновление на один опрос, но не потерять его:
        счётчик увеличивается под той же блокировкой stripe, которую читатель
        берёт перед сканированием.
        """

        counts = self._dirty_counts
        return counts is None or any(counts)

    def _lock_for(self, segment_id):
        return self.locks[segment_id % len(self.locks)]

    def _mark_dirty_locked(self, lock_index):
        """Увеличить счётчик одного stripe. Вызывающий удерживает его блокировку."""

        if self._dirty_counts is not None:
            self._dirty_counts[lock_index] += 1

    def _enqueue_dirty_locked(self, lock_index, segment_id):
        """Добавить новый dirty-сегмент в кольцо его stripe.

        Вызывающий удерживает блокировку stripe и только что увидел переход
        маски из нуля в ненулевое значение, поэтому дубликаты невозможны.
        Заполненное кольцо не сдвигает хвост: читатель увидит флаг
        переполнения и вместо этого просканирует весь stripe.
        """

        queue = self._dirty_queue
        if queue is None:
            return
        ring_size = self._dirty_ring_size
        base = lock_index * (3 + ring_size)
        if queue[base + 2]:
            return
        head = queue[base]
        tail = queue[base + 1]
        if tail - head >= ring_size:
            queue[base + 2] = 1
            return
        queue[base + 3 + (tail % ring_size)] = segment_id
        queue[base + 1] = tail + 1

    def _mark_clean_locked(self, segment_id):
        """Уменьшить счётчик одного stripe после очистки его маски."""

        counts = self._dirty_counts
        if counts is None:
            return
        lock_index = segment_id % len(self.locks)
        current = counts[lock_index]
        if current > 0:
            counts[lock_index] = current - 1

    def _field(self, object_name):
        try:
            return self._fields[object_name]
        except KeyError as error:
            raise TypeError(f"Запрошен незаданый обьект: {object_name!r}") from error

    def get_frame(self, segment_id, object_name):
        start = self._mask_bytes + segment_id * self._field_len
        bounds = self._field(object_name)[0]
        return [bounds[0] + start, bounds[1] + start]

    def get(self, segment_id: int, object_name: str):
        try:
            bounds, struct_type = self._fields[object_name]
        except KeyError as error:
            raise TypeError(f"Запрошен незаданый обьект: {object_name!r}") from error
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("Индекс сегмента выходит за границы")

        offset = self._mask_bytes + segment_id * self._field_len + bounds[0]
        with self._lock_for(segment_id):
            raw_val = struct_type.unpack_from(
                self.__memory.buf, offset
            )[0]

            if isinstance(raw_val, bytes):
                return raw_val.rstrip(b"\x00").decode("utf-8", errors="ignore")
            return raw_val

    def set(self, segment_id: int, object_name: str, data):
        try:
            bounds, struct_type = self._fields[object_name]
        except KeyError as error:
            raise TypeError(f"Запрошен незаданый обьект: {object_name!r}") from error
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("Индекс сегмента выходит за границы")

        if isinstance(data, str):
            data = data.encode("utf-8")
            if len(data) > MAX_CACHE_STRING_BYTES:
                raise ValueError(
                    f"Строка слишком длинная! Максимум {MAX_CACHE_STRING_BYTES} байт, "
                    f"получено {len(data)}"
                )

        packed_data = struct_type.pack(data)

        base = self._mask_bytes + segment_id * self._field_len
        frame_start = base + bounds[0]
        frame_end = base + bounds[1]

        lock_index = segment_id % len(self.locks)
        with self.locks[lock_index]:
            if self.__memory.buf[frame_start:frame_end] == packed_data:
                return
            self.__memory.buf[frame_start:frame_end] = packed_data

            current_mask = self._masks[segment_id]
            if current_mask == 0:
                self._mark_dirty_locked(lock_index)
                self._enqueue_dirty_locked(lock_index, segment_id)
            self._masks[segment_id] = (
                current_mask | self.field_bit_mapping[object_name]
            )

    def get_many(self, segment_id: int, object_names):
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("Индекс сегмента выходит за границы")
        object_names = tuple(object_names)
        plan = self._many_plans.get(object_names)
        if plan is None:
            plan = tuple(
                (name, *self._field(name)) for name in object_names
            )
            self._many_plans[object_names] = plan

        base = self._mask_bytes + segment_id * self._field_len
        buf = self.__memory.buf
        with self._lock_for(segment_id):
            values = {}
            for object_name, (start, _end), struct_type in plan:
                raw_val = struct_type.unpack_from(buf, base + start)[0]
                if isinstance(raw_val, bytes):
                    raw_val = raw_val.rstrip(b"\x00").decode("utf-8", errors="ignore")
                values[object_name] = raw_val
            return values

    def get_many_lockless(self, segment_id: int, object_names):
        """Читать числовые поля без захвата блокировки сегмента.

        Цикл каждого скрипта читает часы движка и несколько монотонных
        счётчиков на каждой итерации. Захват одной общей для процессов
        блокировки там сериализовал все скрипты во всех воркерах на одном
        семафоре: при 1000 скриптах весь конвейер останавливался на ~25
        обновлениях/с и добавление воркеров почти не помогало.

        Принимаются только поля int/float фиксированного размера, и каждое
        поле записывается как одно 4-байтовое значение, поэтому параллельный
        читатель видит либо предыдущее, либо новое значение. Вызывающие
        поэтому должны допускать значение, отстающее на одну запись
        писателя; они не должны ожидать, что несколько полей образуют
        единый атомарный снимок.
        """

        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("Индекс сегмента выходит за границы")
        object_names = tuple(object_names)
        plan = self._lockless_plans.get(object_names)
        if plan is None:
            prepared = []
            for object_name in object_names:
                bounds, struct_type = self._field(object_name)
                if struct_type.format not in ("i", "f"):
                    raise TypeError(
                        "Чтение без блокировки поддерживает только поля int/float: "
                        f"{object_name!r}"
                    )
                prepared.append((object_name, bounds[0], struct_type))
            plan = tuple(prepared)
            self._lockless_plans[object_names] = plan

        base = self._mask_bytes + segment_id * self._field_len
        buf = self.__memory.buf
        values = {}
        for object_name, start, struct_type in plan:
            values[object_name] = struct_type.unpack_from(buf, base + start)[0]
        return values

    def set_many(self, segment_id: int, values):
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("Индекс сегмента выходит за границы")
        if not values:
            return

        prepared = []
        for object_name, data in values.items():
            bounds, struct_type = self._field(object_name)
            if isinstance(data, str):
                data = data.encode("utf-8")
                if len(data) > MAX_CACHE_STRING_BYTES:
                    raise ValueError(
                        f"Строковое значение превышает {MAX_CACHE_STRING_BYTES} байт"
                    )
            packed_data = struct_type.pack(data)
            prepared.append((
                bounds[0],
                bounds[1],
                packed_data,
                self.field_bit_mapping[object_name],
            ))

        base = self._mask_bytes + segment_id * self._field_len
        lock_index = segment_id % len(self.locks)
        with self.locks[lock_index]:
            changed_mask = 0
            for start, end, packed_data, bit_value in prepared:
                frame_start = base + start
                frame_end = base + end
                if self.__memory.buf[frame_start:frame_end] == packed_data:
                    continue
                self.__memory.buf[frame_start:frame_end] = packed_data
                changed_mask |= bit_value
            if changed_mask == 0:
                return
            current_mask = self._masks[segment_id]
            if current_mask == 0:
                self._mark_dirty_locked(lock_index)
                self._enqueue_dirty_locked(lock_index, segment_id)
            self._masks[segment_id] = current_mask | changed_mask

    def get_position(self, segment_id: int):
        """Вернуть ``(x, y, tick)`` под одной блокировкой и без временного dict.

        Это читающая сторона быстрого визуального пути. Она откатывается к
        общему ``get_many``, когда раскладка кэша не держит эти три значения
        одним непрерывным участком.
        """

        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("Индекс сегмента выходит за границы")
        if self._position_struct is None:
            values = self.get_many(segment_id, ("x", "y", "tick"))
            return values["x"], values["y"], values["tick"]

        offset = (
            self._mask_bytes
            + segment_id * self._field_len
            + self._position_offset
        )
        with self._lock_for(segment_id):
            return self._position_struct.unpack_from(
                self.__memory.buf, offset
            )

    def set_position(self, segment_id: int, x, y, tick):
        """Записать визуальные ``x``, ``y`` и ``tick`` одним упакованным участком."""

        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("Индекс сегмента выходит за границы")
        if tick is None:
            self.set_many(segment_id, {"x": x, "y": y})
            return
        if self._position_struct is None:
            self.set_many(segment_id, {"x": x, "y": y, "tick": tick})
            return

        packed_data = self._position_struct.pack(
            float(x), float(y), int(tick)
        )
        base = (
            self._mask_bytes
            + segment_id * self._field_len
            + self._position_offset
        )
        frame_end = base + self._position_length
        buf = self.__memory.buf
        lock_index = segment_id % len(self.locks)
        with self.locks[lock_index]:
            if buf[base:frame_end] == packed_data:
                return
            buf[base:frame_end] = packed_data

            current_mask = self._masks[segment_id]
            if current_mask == 0:
                self._mark_dirty_locked(lock_index)
                self._enqueue_dirty_locked(lock_index, segment_id)
            self._masks[segment_id] = current_mask | self._position_mask

    def move_position(self, segment_id: int, dx, dy, tick=None):
        """Прибавить ``dx``/``dy`` и записать новую позицию под одной блокировкой.

        Скрипты часто делают ``position = visual.position; position.x += dx;
        visual.position = position``. Чтение и запись через отдельные вызовы
        кэша требуют двух проходов блокировки; этот метод выполняет всё
        чтение-изменение-запись, удерживая блокировку stripe один раз.
        """

        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("Индекс сегмента выходит за границы")
        if self._position_struct is None:
            x, y, current_tick = self.get_position(segment_id)
            new_x, new_y = x + dx, y + dy
            self.set_position(
                segment_id,
                new_x,
                new_y,
                current_tick if tick is None else tick,
            )
            return new_x, new_y

        base = (
            self._mask_bytes
            + segment_id * self._field_len
            + self._position_offset
        )
        frame_end = base + self._position_length
        buf = self.__memory.buf
        lock_index = segment_id % len(self.locks)
        with self.locks[lock_index]:
            current_x, current_y, current_tick = (
                self._position_struct.unpack_from(buf, base)
            )
            new_x = current_x + float(dx)
            new_y = current_y + float(dy)
            packed_data = self._position_struct.pack(
                new_x,
                new_y,
                current_tick if tick is None else int(tick),
            )
            if buf[base:frame_end] == packed_data:
                return new_x, new_y
            buf[base:frame_end] = packed_data

            current_mask = self._masks[segment_id]
            if current_mask == 0:
                self._mark_dirty_locked(lock_index)
                self._enqueue_dirty_locked(lock_index, segment_id)
            self._masks[segment_id] = current_mask | self._position_mask
        return new_x, new_y

    def _changed_values_locked(self, segment_id: int, current_mask=None,
                               lock_index=None):
        """Прочитать и очистить один сегмент. Вызывающий должен держать блокировку stripe."""

        if current_mask is None:
            current_mask = self._masks[segment_id]
        if current_mask == 0:
            return None

        buf = self.__memory.buf
        base = self._mask_bytes + segment_id * self._field_len
        # Самое частое визуальное обновление — ровно x/y/tick, записываемое
        # set_position()/move_position(). Распаковываем тот же непрерывный
        # участок одним вызовом вместо обхода трёх dirty-битов и трёх поисков
        # полей. Для любой другой маски остаётся общий обход битов ниже.
        if (
            self._position_struct is not None
            and current_mask == self._position_mask
        ):
            x, y, tick = self._position_struct.unpack_from(
                buf, base + self._position_offset
            )
            values = {"x": x, "y": y, "tick": tick}
        else:
            values = {}
            # Обходим установленные биты вместо всех известных полей. Большинство
            # обновлений затрагивают только x/y/tick/rotation, поэтому это
            # избавляет от 5-8 проверок битов на спрайт.
            mask = current_mask
            while mask:
                bit_value = mask & -mask
                mask ^= bit_value
                field_name = self._bit_to_field[bit_value]
                bounds, struct_type = self._fields[field_name]
                raw_val = struct_type.unpack_from(
                    buf, base + bounds[0]
                )[0]
                if isinstance(raw_val, bytes):
                    raw_val = raw_val.rstrip(b"\x00").decode("utf-8", errors="ignore")
                values[field_name] = raw_val

        self._masks[segment_id] = 0
        # Встраиваем декремент счётчика для пакетных опросчиков: он выполняется
        # для каждого dirty-спрайта, и вызов метода с поиском по модулю заметен.
        counts = self._dirty_counts
        if counts is not None:
            if lock_index is None:
                lock_index = segment_id % len(self.locks)
            current = counts[lock_index]
            if current > 0:
                counts[lock_index] = current - 1
        return values

    def get_changed_values(self, segment_id: int):
        """Вернуть и очистить изменённые поля, удерживая блокировку одного сегмента."""
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("Индекс сегмента выходит за границы")

        with self._lock_for(segment_id):
            return self._changed_values_locked(segment_id) or {}

    def get_changed_values_many(self, segment_ids):
        """Вернуть изменённые поля для нескольких сегментов с одной блокировкой на stripe.

        Опрос каждого спрайта по отдельности снова и снова берёт одни и те же
        полосатые блокировки. Группировка id сегментов по блокировке превращает
        тысячи операций с семафорами за кадр не более чем в ``len(self.locks)``.
        """

        grouped = {}
        for segment_id in segment_ids:
            if segment_id < 0 or segment_id >= self.segments_count:
                raise IndexError("Индекс сегмента выходит за границы")
            grouped.setdefault(segment_id % len(self.locks), []).append(
                segment_id
            )
        if not grouped:
            return {}

        result = {}
        counts = self._dirty_counts
        masks = self._masks
        read_changes = self._changed_values_locked
        for lock_index, ids in grouped.items():
            with self.locks[lock_index]:
                if counts is not None and counts[lock_index] == 0:
                    continue
                for segment_id in ids:
                    current_mask = masks[segment_id]
                    if current_mask == 0:
                        continue
                    values = read_changes(
                        segment_id, current_mask, lock_index
                    )
                    result[segment_id] = values
        return result

    def _scan_dirty_stripe_locked(self, lock_index, result):
        """Просканировать целый stripe и сбросить его dirty-очередь.

        Плотные кадры быстрее при последовательном сканировании, чем при
        выборке из кольца: итерация по множеству плюс чтение кольца на уровне
        Python стоят дороже обхода непрерывного блока масок. Сканирование
        всего stripe также очищает маски сегментов, которых уже нет в активном
        наборе вызывающего, поэтому кольцо можно сбросить без потери ожидающих
        id. Вызывающий удерживает блокировку stripe.
        """

        masks = self._masks
        read_changes = self._changed_values_locked
        lock_count = len(self.locks)
        for segment_id in range(
            lock_index, self.segments_count, lock_count
        ):
            current_mask = masks[segment_id]
            if current_mask == 0:
                continue
            result[segment_id] = read_changes(
                segment_id, current_mask, lock_index
            )
        queue = self._dirty_queue
        if queue is not None:
            base = lock_index * (3 + self._dirty_ring_size)
            queue[base] = 0
            queue[base + 1] = 0
            queue[base + 2] = 0

    def _drain_dirty_queue_locked(self, lock_index, result):
        """Перенести dirty-id из кольца одного stripe в ``result``.

        Вызывающий удерживает блокировку stripe. Без переполнения обходятся
        ровно те id, что стали dirty с прошлого опроса. При переполнении
        сканируется весь stripe, включая сегменты, которых уже нет в активном
        наборе вызывающего, после чего кольцо сбрасывается.
        """

        queue = self._dirty_queue
        ring_size = self._dirty_ring_size
        base = lock_index * (3 + ring_size)
        masks = self._masks
        read_changes = self._changed_values_locked

        if queue[base + 2]:
            self._scan_dirty_stripe_locked(lock_index, result)
            return

        head = queue[base]
        tail = queue[base + 1]
        if head == tail:
            return

        position = head
        while position < tail:
            segment_id = queue[base + 3 + (position % ring_size)]
            if 0 <= segment_id < self.segments_count:
                current_mask = masks[segment_id]
                if current_mask:
                    result[segment_id] = read_changes(
                        segment_id, current_mask, lock_index
                    )
            position += 1
        queue[base] = 0
        queue[base + 1] = 0

    def get_changed_values_by_stripe(self, groups):
        """Читать изменения из id сегментов, уже сгруппированных по stripe.

        ``groups[lock_index]`` — итерируемое по id сегментов. Рендерер хранит
        свои активные id сгруппированными так, поэтому опрос не перестраивает
        словарь сегмент-в-stripe на каждом кадре. Чистые stripe пропускаются
        без захвата их блокировки.

        Когда у кэша есть dirty-очередь, именно очередь решает, какие id
        посещать, а ``groups`` используется только для запасного пути при
        переполнении. Очередь может ненадолго сообщить id спрайта, который
        только что удалили; вызывающие должны игнорировать неизвестные id
        (рендерер уже это делает).
        """

        if len(groups) != len(self.locks):
            raise ValueError(
                "groups должен содержать по одной записи "
                "на каждую блокировку ObjectCache"
            )

        result = {}
        counts = self._dirty_counts
        masks = self._masks
        read_changes = self._changed_values_locked
        has_queue = self._dirty_queue is not None
        for lock_index, segment_ids in enumerate(groups):
            if counts is not None and counts[lock_index] == 0:
                continue
            try:
                active_count = len(segment_ids)
            except TypeError:
                # Сохраняем проверку плотности для одноразовых итерируемых.
                segment_ids = tuple(segment_ids)
                active_count = len(segment_ids)
            with self.locks[lock_index]:
                # Перепроверяем под блокировкой: писатель мог очистить
                # stripe между чтением без блокировки и её захватом.
                if counts is not None and counts[lock_index] == 0:
                    continue
                if has_queue:
                    # Плотный stripe дешевле просканировать подряд, чем
                    # вычерпывать кольцо по одному id. Разреженные stripe
                    # используют кольцо, что сохраняет типичный кадр "сдвинулись
                    # несколько скриптов" пропорциональным числу dirty.
                    if (counts is not None
                            and counts[lock_index] * 2 >= active_count):
                        self._scan_dirty_stripe_locked(lock_index, result)
                    else:
                        self._drain_dirty_queue_locked(lock_index, result)
                    continue
                for segment_id in segment_ids:
                    current_mask = masks[segment_id]
                    if current_mask == 0:
                        continue
                    values = read_changes(
                        segment_id, current_mask, lock_index
                    )
                    result[segment_id] = values
        return result

    def clear_changes(self, segment_id: int):
        """Сбросить ожидающие изменения для сегмента, который больше не наблюдается."""

        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("Индекс сегмента выходит за границы")
        lock_index = segment_id % len(self.locks)
        with self.locks[lock_index]:
            if self._masks[segment_id] == 0:
                return False
            self._masks[segment_id] = 0
            self._mark_clean_locked(segment_id)
            return True

    def get_changes(self, segment_id: int) -> list:
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("Индекс сегмента выходит за границы")

        changed_fields = []

        with self._lock_for(segment_id):
            current_mask = self._masks[segment_id]
            if current_mask == 0:
                return changed_fields

            for field_name, bit_value in self.field_bit_mapping.items():
                if current_mask & bit_value:
                    changed_fields.append(field_name)

            self._masks[segment_id] = 0
            self._mark_clean_locked(segment_id)

        return changed_fields

    def _release_views(self):
        masks = self._masks
        if masks is not None:
            self._masks = None
            try:
                masks.release()
            except ValueError:
                pass

    def close(self):
        memory = self.__memory
        if memory is None:
            return
        self.__memory = None
        self._release_views()
        memory.close()
        if self._owner:
            try:
                memory.unlink()
            except FileNotFoundError:
                pass

    def detach(self):
        """Закрыть это подключение, не удаляя shared memory для других пользователей."""
        memory = self.__memory
        if memory is None:
            return
        self.__memory = None
        self._release_views()
        memory.close()


class FieldView:
    """Предоставить доступ к одному полю ObjectCache через небольшой API ValueCache.

    Часы движка, tick и счётчики теперь живут вместе в одном сегменте.
    Компоненты, которые раньше ожидали отдельный ValueCache, могут продолжать
    вызывать ``get()``/``set()``; горячий цикл скрипта по-прежнему читает их
    все одним вызовом ``ObjectCache.get_many()``.
    """

    __slots__ = ("_cache", "_segment_id", "_field")

    def __init__(self, cache, field, segment_id=0):
        cache._field(field)
        self._cache = cache
        self._segment_id = segment_id
        self._field = field

    def get(self):
        return self._cache.get(self._segment_id, self._field)

    def set(self, value):
        self._cache.set(self._segment_id, self._field, value)

    @property
    def name(self):
        return (self._cache.key, self._segment_id, self._field)

    def detach(self):
        # Представление не владеет shared memory; его закрывает владелец.
        pass
