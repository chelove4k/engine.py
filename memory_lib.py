import multiprocessing
from multiprocessing import shared_memory
import struct


class ValueCache:
    MEMORY_TYPES_SIZE = {
            float: 4,
            int: 4,
            str: 64
        }

    def __init__(self, data = None, name=None, lock=None):
        self.__memory = None
        if isinstance(data, list):
            name = data[1]
            lock = data[2]
            data = data[0]

        if data is not None:
            self.size = self.MEMORY_TYPES_SIZE[type(data)] + 1
            if type(data) is str:
                self.fmt_char = "s"
            elif type(data) is float:
                self.fmt_char = 'f'
            else:
                self.fmt_char = 'i'

        if name is not None:
            self.__memory = shared_memory.SharedMemory(name=name)
            self.lock = lock
            self.name = name

        elif name is None:
            self.__memory = shared_memory.SharedMemory(create=True, size=self.size)
            self.lock = multiprocessing.Lock()
            self.name = [None, self.__memory.name, self.lock]

            
            for i in range(self.size):
                self.__memory.buf[i] = 0

            if type(data) is str:
                self.__memory.buf[0] = 2
            elif type(data) is float:
                self.__memory.buf[0] = 0
            else:
                self.__memory.buf[0] = 1

            self.set(data)
        

    def get_fml(self):
        fmt = None
        raw_fml = self.__memory.buf[0]
        if raw_fml == 0:
            fmt = 'f'
        if raw_fml == 1:
            fmt = 'i'
        if raw_fml == 2:
            fmt = '64s'
        return fmt
    
    def get_size(self):
        fml = self.get_fml()
        size = 4
        if fml == '64s':
            size = 64
        return size
    
    def get(self):
        with self.lock:
            memory_segment = bytes(self.__memory.buf[1:self.get_size()+1])
            raw_val = struct.unpack(self.get_fml(), memory_segment)[0]
            
            if isinstance(raw_val, bytes):
                return raw_val.rstrip(b'\x00').decode('utf-8', errors='ignore')
            return raw_val

    def set(self, data):
        if isinstance(data, str):
            data = data.encode('utf-8')[:64].ljust(64, b'\x00')

        packed_data = struct.pack(self.get_fml(), data) 

        with self.lock:
            self.__memory.buf[1:self.get_size()+1] = packed_data
    def close(self):
            memory = self.__memory
            if memory is None:
                return
            self.__memory = None
            memory.close()
            try:
                memory.unlink()
            except FileNotFoundError:
                pass

class ObjectCache:
    MEMORY_TYPES_SIZE = {
        float: 4,
        int: 4,
        str: 64
    }
    
    def __init__(self, id_or_name, data, locks=None):
        self.__memory = None
        self._fields = {} 

        self.segment_len = 4 
        self.field_bit_mapping = {} 
        
        for bit_index, field_name in enumerate(data):
            size = self.MEMORY_TYPES_SIZE[data[field_name]]
            if data[field_name] is str:
                fmt_char = f"{size}s"
            elif data[field_name] is float:
                fmt_char = 'f'
            else:
                fmt_char = 'i'

            self.__dict__[field_name] = [[self.segment_len, self.segment_len + size], fmt_char]
            self.field_bit_mapping[field_name] = 1 << bit_index
            self.segment_len += size

        if isinstance(id_or_name, str):
            self.__memory = shared_memory.SharedMemory(name=id_or_name)
            self.key = id_or_name

        elif isinstance(id_or_name, int):
            self.__memory = shared_memory.SharedMemory(create=True, size=id_or_name)
            self.key = self.__memory.name
            
            self.__memory.buf[:] = bytes(self.__memory.size)
        

        self.segments_count = self.__memory.size // self.segment_len
        if locks is None:
            lock_count = min(64, max(1, self.segments_count))
            self.locks = [multiprocessing.Lock() for _ in range(lock_count)]
        elif isinstance(locks, (list, tuple)):
            self.locks = locks
        else:
            self.locks = [locks]

    def _lock_for(self, segment_id):
        return self.locks[segment_id % len(self.locks)]

    def get_frame(self, segment_id, object_name):
        start = segment_id * self.segment_len
        bounds = self.__dict__[object_name][0]
        
        return [bounds[0] + start, bounds[1] + start]

    def get(self, segment_id: int, object_name: str):
        if object_name not in self.__dict__:
            raise TypeError('Запрошен незаданый обьект')
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError('Индекс сегмента выходит за границы')
        
        frame = self.get_frame(segment_id, object_name)
        fmt_type = self.__dict__[object_name][1]

        with self._lock_for(segment_id):
            memory_segment = bytes(self.__memory.buf[frame[0]:frame[1]])
            raw_val = struct.unpack(fmt_type, memory_segment)[0]
            
            if isinstance(raw_val, bytes):
                return raw_val.rstrip(b'\x00').decode('utf-8', errors='ignore')
            return raw_val

    def set(self, segment_id: int, object_name: str, data):
        if object_name not in self.__dict__:
            raise TypeError('Запрошен незаданый обьект')
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError('Индекс сегмента выходит за границы')

        frame = self.get_frame(segment_id, object_name)
        fmt_type = self.__dict__[object_name][1]

        if isinstance(data, str):
            data = data.encode('utf-8')
            if len(data) > 64:
                raise ValueError(f"Строка слишком длинная! Максимум 64 байта, получено {len(data)}")

        packed_data = struct.pack(fmt_type, data) 
        
        mask_start = segment_id * self.segment_len
        mask_end = mask_start + 4

        with self._lock_for(segment_id):
            if bytes(self.__memory.buf[frame[0]:frame[1]]) == packed_data:
                return
            self.__memory.buf[frame[0]:frame[1]] = packed_data

            current_mask = struct.unpack('i', self.__memory.buf[mask_start:mask_end])[0]
            new_mask = current_mask | self.field_bit_mapping[object_name]
            self.__memory.buf[mask_start:mask_end] = struct.pack('i', new_mask)

    def get_many(self, segment_id: int, object_names):
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("segment index out of range")
        object_names = tuple(object_names)
        for object_name in object_names:
            if object_name not in self.field_bit_mapping:
                raise TypeError(f"unknown cache field: {object_name}")

        with self._lock_for(segment_id):
            values = {}
            for object_name in object_names:
                frame = self.get_frame(segment_id, object_name)
                fmt_type = self.__dict__[object_name][1]
                raw_val = struct.unpack(fmt_type, bytes(self.__memory.buf[frame[0]:frame[1]]))[0]
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
            if object_name not in self.field_bit_mapping:
                raise TypeError(f"unknown cache field: {object_name}")
            fmt_type = self.__dict__[object_name][1]
            if isinstance(data, str):
                data = data.encode("utf-8")
                if len(data) > 64:
                    raise ValueError("string value exceeds 64 bytes")
            packed_data = struct.pack(fmt_type, data)
            prepared.append((
                self.get_frame(segment_id, object_name),
                packed_data,
                self.field_bit_mapping[object_name],
            ))

        mask_start = segment_id * self.segment_len
        mask_end = mask_start + 4
        with self._lock_for(segment_id):
            changed_mask = 0
            for frame, packed_data, bit_value in prepared:
                if bytes(self.__memory.buf[frame[0]:frame[1]]) == packed_data:
                    continue
                self.__memory.buf[frame[0]:frame[1]] = packed_data
                changed_mask |= bit_value
            if changed_mask == 0:
                return
            current_mask = struct.unpack("i", self.__memory.buf[mask_start:mask_end])[0]
            self.__memory.buf[mask_start:mask_end] = struct.pack("i", current_mask | changed_mask)

    def get_changed_values(self, segment_id: int):
        """Return and clear the changed fields while holding one segment lock."""
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError("segment index out of range")

        mask_start = segment_id * self.segment_len
        mask_end = mask_start + 4
        with self._lock_for(segment_id):
            current_mask = struct.unpack("i", self.__memory.buf[mask_start:mask_end])[0]
            if current_mask == 0:
                return {}

            values = {}
            for field_name, bit_value in self.field_bit_mapping.items():
                if not current_mask & bit_value:
                    continue
                frame = self.get_frame(segment_id, field_name)
                fmt_type = self.__dict__[field_name][1]
                raw_val = struct.unpack(
                    fmt_type,
                    bytes(self.__memory.buf[frame[0]:frame[1]]),
                )[0]
                if isinstance(raw_val, bytes):
                    raw_val = raw_val.rstrip(b"\x00").decode("utf-8", errors="ignore")
                values[field_name] = raw_val

            self.__memory.buf[mask_start:mask_end] = struct.pack("i", 0)
            return values

    def get_changes(self, segment_id: int) -> list:
        if segment_id < 0 or segment_id >= self.segments_count:
            raise IndexError('Индекс сегмента выходит за границы')

        mask_start = segment_id * self.segment_len
        mask_end = mask_start + 4
        changed_fields = []

        with self._lock_for(segment_id):
            current_mask = struct.unpack('i', self.__memory.buf[mask_start:mask_end])[0]
            if current_mask == 0:
                return changed_fields
                
            for field_name, bit_value in self.field_bit_mapping.items():
                if current_mask & bit_value:
                    changed_fields.append(field_name)

            self.__memory.buf[mask_start:mask_end] = struct.pack('i', 0)
            
        return changed_fields

    def close(self):
        memory = self.__memory
        if memory is None:
            return
        self.__memory = None
        memory.close()
        try:
            memory.unlink()
        except FileNotFoundError:
            pass

    def detach(self):
        memory = self.__memory
        if memory is None:
            return
        self.__memory = None
        memory.close()
