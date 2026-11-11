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
                raise TypeError(f"Unsupported ValueCache type: {type(data)!r}") from error
            self._set_format(data)

        if name is not None:
            if lock is None:
                raise ValueError("A shared lock is required when attaching a ValueCache")
            self.__memory = shared_memory.SharedMemory(name=name)
            self.lock = lock
            self.name = name
            self._owner = False
            # The payload type never changes after creation.  Cache the
            # struct format instead of re-reading the header and rebuilding
            # the format string on every get()/set().
            self._apply_raw_format(self.__memory.buf[0])
        elif name is None:
            if data is None:
                raise ValueError("ValueCache requires an initial value or a shared memory name")
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
            raise ValueError(f"Unknown ValueCache payload type: {raw_fml}")
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
        raise ValueError(f"Unknown ValueCache payload type: {raw_fml}")

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
                    f"String value is too long: {len(encoded)} bytes "
                    f"(maximum {MAX_CACHE_STRING_BYTES})"
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
        """Close this attachment without removing shared memory for other users."""
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

    def __init__(self, id_or_name, data, locks=None):
        self.__memory = None
        self._fields = {}
        self._many_plans = {}
        self.segment_len = 4
        self.field_bit_mapping = {}
        self._owner = False

        if len(data) > 31:
            raise ValueError("ObjectCache supports at most 31 fields (32-bit dirty mask)")

        for bit_index, field_name in enumerate(data):
            if field_name in self._fields:
                raise ValueError(f"Duplicate ObjectCache field: {field_name!r}")
            size = self.MEMORY_TYPES_SIZE[data[field_name]]
            if data[field_name] is str:
                fmt_char = f"{size}s"
            elif data[field_name] is float:
                fmt_char = "f"
            else:
                fmt_char = "i"

            self._fields[field_name] = (
                [self.segment_len, self.segment_len + size],
                fmt_char,
            )
            self.field_bit_mapping[field_name] = 1 << bit_index
            self.segment_len += size

        if isinstance(id_or_name, str):
            if locks is None:
                raise ValueError("A shared lock list is required when attaching an ObjectCache")
            self.__memory = shared_memory.SharedMemory(name=id_or_name)
            self.key = id_or_name
            self._owner = False
        elif isinstance(id_or_name, int):
            self.__memory = shared_memory.SharedMemory(create=True, size=id_or_name)
            self.key = self.__memory.name
            self._owner = True
            self.__memory.buf[:] = bytes(self.__memory.size)
        else:
            raise TypeError("ObjectCache expects a shared memory name or a size in bytes")

        self.segments_count = self.__memory.size // self.segment_len
        if locks is None:
            lock_count = min(64, max(1, self.segments_count))
            self.locks = [multiprocessing.Lock() for _ in range(lock_count)]
        elif isinstance(locks, (list, tuple)):
            self.locks = locks
        else:
            self.locks = [locks]

    @property
    def capacity(self):
        return self.segments_count

    def _lock_for(self, segment_id):
        return self.locks[segment_id % len(self.locks)]

    def _field(self, object_name):
        try:
            return self._fields[object_name]
        except KeyError as error:
            raise TypeError(f"Запрошен незаданый обьект: {object_name!r}") from error

    def get_frame(self, segment_id, object_name):
        start = segment_id * self.segment_len
        bounds = self._field(object_name)[0]
        return [bounds[0] + start, bounds[1] + start]

    def get(self, segment_id: int, object_name: str):
        try:
            bounds, fmt_type = self._fields[object_name]
        except KeyError as error:
            raise TypeError(f"Запрошен незаданый обьект: {object_name!r}") from error
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("Индекс сегмента выходит за границы")

        offset = segment_id * self.segment_len + bounds[0]
        with self._lock_for(segment_id):
            raw_val = struct.unpack_from(
                fmt_type, self.__memory.buf, offset
            )[0]

            if isinstance(raw_val, bytes):
                return raw_val.rstrip(b"\x00").decode("utf-8", errors="ignore")
            return raw_val

    def set(self, segment_id: int, object_name: str, data):
        try:
            bounds, fmt_type = self._fields[object_name]
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

        packed_data = struct.pack(fmt_type, data)

        frame_start = segment_id * self.segment_len + bounds[0]
        frame_end = segment_id * self.segment_len + bounds[1]
        mask_start = segment_id * self.segment_len
        mask_end = mask_start + 4

        with self._lock_for(segment_id):
            if bytes(self.__memory.buf[frame_start:frame_end]) == packed_data:
                return
            self.__memory.buf[frame_start:frame_end] = packed_data

            current_mask = struct.unpack_from("i", self.__memory.buf, mask_start)[0]
            new_mask = current_mask | self.field_bit_mapping[object_name]
            self.__memory.buf[mask_start:mask_end] = struct.pack("i", new_mask)

    def get_many(self, segment_id: int, object_names):
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("segment index out of range")
        object_names = tuple(object_names)
        plan = self._many_plans.get(object_names)
        if plan is None:
            plan = tuple(
                (name, *self._field(name)) for name in object_names
            )
            self._many_plans[object_names] = plan

        base = segment_id * self.segment_len
        buf = self.__memory.buf
        with self._lock_for(segment_id):
            values = {}
            for object_name, (start, _end), fmt_type in plan:
                raw_val = struct.unpack_from(fmt_type, buf, base + start)[0]
                if isinstance(raw_val, bytes):
                    raw_val = raw_val.rstrip(b"\x00").decode("utf-8", errors="ignore")
                values[object_name] = raw_val
            return values

    def set_many(self, segment_id: int, values):
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("segment index out of range")
        if not values:
            return

        prepared = []
        for object_name, data in values.items():
            bounds, fmt_type = self._field(object_name)
            if isinstance(data, str):
                data = data.encode("utf-8")
                if len(data) > MAX_CACHE_STRING_BYTES:
                    raise ValueError(
                        f"String value exceeds {MAX_CACHE_STRING_BYTES} bytes"
                    )
            packed_data = struct.pack(fmt_type, data)
            prepared.append((
                bounds[0],
                bounds[1],
                packed_data,
                self.field_bit_mapping[object_name],
            ))

        mask_start = segment_id * self.segment_len
        mask_end = mask_start + 4
        base = segment_id * self.segment_len
        with self._lock_for(segment_id):
            changed_mask = 0
            for start, end, packed_data, bit_value in prepared:
                frame_start = base + start
                frame_end = base + end
                if bytes(self.__memory.buf[frame_start:frame_end]) == packed_data:
                    continue
                self.__memory.buf[frame_start:frame_end] = packed_data
                changed_mask |= bit_value
            if changed_mask == 0:
                return
            current_mask = struct.unpack_from("i", self.__memory.buf, mask_start)[0]
            self.__memory.buf[mask_start:mask_end] = struct.pack("i", current_mask | changed_mask)

    def _changed_values_locked(self, segment_id: int):
        """Read and clear one segment.  The caller must hold its stripe lock."""

        buf = self.__memory.buf
        mask_start = segment_id * self.segment_len
        current_mask = struct.unpack_from("i", buf, mask_start)[0]
        if current_mask == 0:
            return None

        values = {}
        for field_name, bit_value in self.field_bit_mapping.items():
            if not current_mask & bit_value:
                continue
            bounds, fmt_type = self._fields[field_name]
            raw_val = struct.unpack_from(
                fmt_type, buf, mask_start + bounds[0]
            )[0]
            if isinstance(raw_val, bytes):
                raw_val = raw_val.rstrip(b"\x00").decode("utf-8", errors="ignore")
            values[field_name] = raw_val

        struct.pack_into("i", buf, mask_start, 0)
        return values

    def get_changed_values(self, segment_id: int):
        """Return and clear the changed fields while holding one segment lock."""
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("segment index out of range")

        with self._lock_for(segment_id):
            return self._changed_values_locked(segment_id) or {}

    def get_changed_values_many(self, segment_ids):
        """Return changed fields for several segments with one lock per stripe.

        Polling every sprite separately acquires the same striped locks over
        and over.  Grouping segment ids by lock turns thousands of semaphore
        operations per frame into at most ``len(self.locks)``.
        """

        grouped = {}
        for segment_id in segment_ids:
            if segment_id < 0 or segment_id >= self.segments_count:
                raise IndexError("segment index out of range")
            grouped.setdefault(segment_id % len(self.locks), []).append(
                segment_id
            )
        if not grouped:
            return {}

        result = {}
        for lock_index, ids in grouped.items():
            with self.locks[lock_index]:
                for segment_id in ids:
                    values = self._changed_values_locked(segment_id)
                    if values:
                        result[segment_id] = values
        return result

    def get_changes(self, segment_id: int) -> list:
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("Индекс сегмента выходит за границы")

        mask_start = segment_id * self.segment_len
        mask_end = mask_start + 4
        changed_fields = []

        with self._lock_for(segment_id):
            current_mask = struct.unpack("i", self.__memory.buf[mask_start:mask_end])[0]
            if current_mask == 0:
                return changed_fields

            for field_name, bit_value in self.field_bit_mapping.items():
                if current_mask & bit_value:
                    changed_fields.append(field_name)

            self.__memory.buf[mask_start:mask_end] = struct.pack("i", 0)

        return changed_fields

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
        """Close this attachment without removing shared memory for other users."""
        memory = self.__memory
        if memory is None:
            return
        self.__memory = None
        memory.close()


class FieldView:
    """Expose one ObjectCache field through the small ValueCache API.

    The engine clock, tick and counters now live together in a single
    segment.  Components that previously expected a dedicated ValueCache can
    keep calling ``get()``/``set()``; the hot script loop still reads all of
    them with one ``ObjectCache.get_many()`` call.
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
        # The view does not own the shared memory; its owner closes it.
        pass
