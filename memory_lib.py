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
        # Fast path for the engine's hottest write: visual position.  It is
        # enabled only when x, y and tick occupy one contiguous span and have
        # the expected types, so no other cache layout changes behaviour.
        self._position_struct = None
        self._position_offset = 0
        self._position_length = 0
        self._position_mask = 0

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
                [self._field_len, self._field_len + size],
                # Pre-build the struct so get/set do not parse the format
                # string on every call.  The visual hot path reads several
                # fields per sprite per frame.
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
                raise ValueError("A shared lock list is required when attaching an ObjectCache")
            if capacity is None:
                raise ValueError(
                    "capacity is required when attaching an ObjectCache"
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
            raise TypeError("ObjectCache expects a shared memory name or a size in bytes")

        if capacity is None:
            capacity = id_or_name // self.segment_len
        capacity = int(capacity)
        if capacity < 0 or capacity * self.segment_len > self.__memory.size:
            self._abort_init()
            raise ValueError(
                "ObjectCache capacity does not fit in shared memory"
            )
        self.segments_count = capacity
        self._mask_bytes = self.segments_count * 4
        # All dirty masks live in one contiguous block at the start of the
        # shared memory.  A typed view makes per-segment mask access cheap and
        # lets batch readers skip clean segments without unpacking the mask
        # from a 100-byte-stride segment.
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

        # One counter per lock stripe.  It tracks how many segments in the
        # stripe currently have a non-zero dirty mask, so a renderer can skip
        # an entire clean stripe without touching every segment.  The block is
        # created explicitly by the owner and passed to every attachment;
        # caches without it keep the old full-scan behaviour.  A writer that
        # has the block and a reader that does not (or vice versa) would break
        # the counter invariant, so the block must travel with the cache
        # descriptor.
        if dirty_counts is not None and len(dirty_counts) != len(self.locks):
            self._abort_init()
            raise ValueError(
                "dirty_counts must have one counter per ObjectCache lock"
            )
        self._dirty_counts = dirty_counts

        # Optional per-stripe dirty-id ring.  Unlike the masks, it is a
        # separate shared array so the cache layout and capacity stay exactly
        # the same with or without it.  Writers append only on a 0 -> dirty
        # mask transition while holding the stripe lock, so one segment has at
        # most one pending ring entry.  A full ring sets the overflow flag and
        # the reader falls back to scanning the whole stripe.
        if dirty_queue is not None:
            if dirty_ring_size <= 0:
                self._abort_init()
                raise ValueError(
                    "dirty_ring_size must be positive with dirty_queue"
                )
            expected = len(self.locks) * (3 + int(dirty_ring_size))
            if len(dirty_queue) != expected:
                self._abort_init()
                raise ValueError(
                    "dirty_queue size must be locks * (3 + dirty_ring_size)"
                )
        elif dirty_ring_size:
            self._abort_init()
            raise ValueError(
                "dirty_ring_size requires a dirty_queue"
            )
        self._dirty_queue = dirty_queue
        self._dirty_ring_size = int(dirty_ring_size)

    def _abort_init(self):
        """Release everything acquired before a constructor validation error."""

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
        """Return whether any stripe has unread changes.

        Without counters every stripe has to be scanned, so the answer is
        always ``True``.  With counters this is a lock-free read of one small
        array, which lets an idle renderer skip building the per-segment
        grouping entirely.  A writer that races the check can only delay its
        update by one poll, never lose it: the counter is incremented while
        holding the same stripe lock that the reader takes before scanning.
        """

        counts = self._dirty_counts
        return counts is None or any(counts)

    def _lock_for(self, segment_id):
        return self.locks[segment_id % len(self.locks)]

    def _mark_dirty_locked(self, lock_index):
        """Increment one stripe counter.  The caller holds its lock."""

        if self._dirty_counts is not None:
            self._dirty_counts[lock_index] += 1

    def _enqueue_dirty_locked(self, lock_index, segment_id):
        """Append a newly dirty segment to its stripe ring.

        The caller holds the stripe lock and has just observed the mask
        transition from zero to non-zero, so duplicates are impossible.  A
        full ring does not advance its tail: the reader will see the overflow
        flag and scan the whole stripe instead.
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
        """Decrement one stripe counter after its mask is cleared."""

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
            raise IndexError("segment index out of range")
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
        """Read numeric fields without taking the segment lock.

        The per-script loop reads the engine clock and a few monotonic
        counters on every iteration.  Acquiring one process-shared lock there
        serialised every script in every worker on the same semaphore: with
        1000 scripts the whole pipeline stalled at ~25 updates/s and adding
        worker processes barely helped.

        Only fixed-size int/float fields are accepted, and every field is
        written as one 4-byte value, so a concurrent reader observes either
        the previous or the new value.  Callers must therefore tolerate a
        value that is one writer update stale; they must not expect several
        fields to form one atomic snapshot.
        """

        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("segment index out of range")
        object_names = tuple(object_names)
        plan = self._lockless_plans.get(object_names)
        if plan is None:
            prepared = []
            for object_name in object_names:
                bounds, struct_type = self._field(object_name)
                if struct_type.format not in ("i", "f"):
                    raise TypeError(
                        "Lock-free reads support only int/float fields: "
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
            raise IndexError("segment index out of range")
        if not values:
            return

        prepared = []
        for object_name, data in values.items():
            bounds, struct_type = self._field(object_name)
            if isinstance(data, str):
                data = data.encode("utf-8")
                if len(data) > MAX_CACHE_STRING_BYTES:
                    raise ValueError(
                        f"String value exceeds {MAX_CACHE_STRING_BYTES} bytes"
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
        """Return ``(x, y, tick)`` with one lock and no temporary dict.

        This is the read side of the visual fast path.  It falls back to the
        generic ``get_many`` when the cache layout does not keep the three
        values in one contiguous span.
        """

        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("segment index out of range")
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
        """Write visual ``x``, ``y`` and ``tick`` as one packed span."""

        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("segment index out of range")
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
        """Add ``dx``/``dy`` and write the new position under one lock.

        Scripts frequently do ``position = visual.position; position.x += dx;
        visual.position = position``.  Doing the read and the write through
        separate cache calls takes two lock round-trips; this method performs
        the whole read-modify-write while holding the stripe lock once.
        """

        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("segment index out of range")
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
        """Read and clear one segment.  The caller must hold its stripe lock."""

        if current_mask is None:
            current_mask = self._masks[segment_id]
        if current_mask == 0:
            return None

        buf = self.__memory.buf
        base = self._mask_bytes + segment_id * self._field_len
        # The hottest visual update is exactly x/y/tick, written by
        # set_position()/move_position().  Unpack the same contiguous span in
        # one call instead of walking three dirty bits and three field
        # lookups.  Any other mask keeps the generic bit walk below.
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
            # Walk the set bits instead of every known field.  Most updates
            # touch only x/y/tick/rotation, so this avoids 5-8 bit tests per
            # sprite.
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
        # Inline the counter decrement for the batch pollers: this runs for
        # every dirty sprite and a method call plus modulo lookup is visible.
        counts = self._dirty_counts
        if counts is not None:
            if lock_index is None:
                lock_index = segment_id % len(self.locks)
            current = counts[lock_index]
            if current > 0:
                counts[lock_index] = current - 1
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
        """Scan one full stripe and reset its dirty queue.

        Dense frames are faster with a sequential scan than with a ring drain:
        a set iteration plus a Python-level ring read costs more than walking
        the contiguous mask block.  Scanning the whole stripe also clears
        masks for segments that are no longer in the caller's active set, so
        the ring can be reset without losing pending ids.  The caller holds
        the stripe lock.
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
        """Move dirty ids from one stripe ring into ``result``.

        The caller holds the stripe lock.  Without an overflow this visits
        exactly the ids that became dirty since the previous poll.  On
        overflow the whole stripe is scanned, including segments that are no
        longer in the caller's active set, then the ring is reset.
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
        """Read changes from segment ids that are already grouped by stripe.

        ``groups[lock_index]`` is an iterable of segment ids.  The renderer
        keeps its active ids grouped this way, so polling does not rebuild a
        segment-id-to-stripe dictionary on every frame.  Clean stripes are
        skipped without taking their lock.

        When the cache has a dirty queue, the queue decides which ids are
        visited and ``groups`` is only used by the overflow fallback.  The
        queue can briefly report an id whose sprite was just removed; callers
        must ignore unknown ids (the renderer already does).
        """

        if len(groups) != len(self.locks):
            raise ValueError(
                "groups must have one entry per ObjectCache lock"
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
                # Keep the density check working for one-shot iterables.
                segment_ids = tuple(segment_ids)
                active_count = len(segment_ids)
            with self.locks[lock_index]:
                # Re-check under the lock: a writer may have cleared the
                # stripe between the lock-free peek and this acquisition.
                if counts is not None and counts[lock_index] == 0:
                    continue
                if has_queue:
                    # A dense stripe is cheaper to scan sequentially than to
                    # drain through the ring one id at a time.  Sparse stripes
                    # still use the ring, which keeps the common "a few
                    # scripts moved" frame proportional to the dirty count.
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
        """Drop pending changes for a segment that is no longer observed."""

        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("segment index out of range")
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
        """Close this attachment without removing shared memory for other users."""
        memory = self.__memory
        if memory is None:
            return
        self.__memory = None
        self._release_views()
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
