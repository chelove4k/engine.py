import threading
import os
import importlib
import stream
import traceback
import pyengine
from radon.visitors import ComplexityVisitor
import queue
import time
from memory_lib import ObjectCache, ValueCache


class VisualCache(ObjectCache):
    pass

class IdCache(ObjectCache):
    pass


class scripter_runner:
    
    def print(self, *args, **kwargs):
        prefix = f"[CORE {self.id:02d} : {self.core_time.get():.4f}]"

        print(prefix, *args, **kwargs)

    def get_script_diff(self, path):
    
        if path in self._script_complexity_cache:
            return self._script_complexity_cache[path]

        source_code = open(path).read()

        visitor = ComplexityVisitor.from_code(
            source_code
        )

        sum_index = 0

        for block in visitor.blocks:
            sum_index += block.complexity

        self._script_complexity_cache[path] = sum_index

        return sum_index

    def wrapper(self, func, arg, proccess_status_info, id, data, lock):
        self.print(f"Запущен {data['path']}")

        try:
            func(*arg)

        except Exception as e:
            # self.print(f"Ошибка внутри запущенного потока {data['path']}: {e}")
            traceback.print_exc()

        finally:
            with lock:
                proccess_status_info[id] -= self.get_script_diff(data['path'])
        
            self.print(f"Остановлен {data['path']}")

    def __init__(self, 
                 time_key, id, 
                 render_in_stream, 
                 change_in_stream, 
                 main_in_stream, 
                 main_out_stream, 
                 objects_cache, 
                 procces_in_stream, 
                 lock, 
                 keyboard_input, 
                 proccess_status_info, 
                 visual_cache,
                 id_cache,
                 object_lock,
                 camera_state
        ):
        self.core_time = ValueCache(time_key)
        self.id = id
        self.change_in_stream = change_in_stream
        self.main_in_stream = main_in_stream
        self.main_out_stream = main_out_stream
        self.objects_cache = objects_cache
        self.proccess_status_info = proccess_status_info
        self.procces_in_stream = procces_in_stream
        self.lock = lock
        self.keyboard_input = keyboard_input
        self.object_lock = object_lock
        self.camera_state = camera_state
        self._script_complexity_cache = {}

        proccess_status_info[id] = 0
        while True:
                try:
                    data = procces_in_stream.get()
                except (EOFError, BrokenPipeError, OSError):
                    return
                if data['proc_id'] != id:
                    procces_in_stream.put(data)
                    time.sleep(1/100)
                    continue

                try:
                    if data == None:
                        continue
                    module = data['path'].replace('.py', '').replace('\\', '.').replace('/', '.')
                    name = module.split('.')[-1]
                    obj_data = data['script_data']

                    module_obj = importlib.import_module(module)

                    thread = threading.Thread(target=self.wrapper, args=(
                        getattr(module_obj, name), 
                        (render_in_stream, 
                         change_in_stream, 
                         main_in_stream, 
                         main_out_stream, 
                         obj_data, 
                         objects_cache, 
                         lock, keyboard_input, 
                         time_key, 
                         VisualCache(*visual_cache), 
                         visual_cache,
                         IdCache(*id_cache), 
                         id_cache,
                         self.object_lock,
                         self.camera_state),
                        proccess_status_info,
                        id, data, lock
                        ))
                    thread.start()
                    
                except (ImportError, AttributeError) as e:
                    self.print(f"Ошибка создания script потока {e}")  
                    continue
                except Exception as e:
                    self.print(f'Ошибка потока {e}')
               
