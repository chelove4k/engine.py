import threading
import os
import importlib
import stream
import traceback
import pyengine
from radon.visitors import ComplexityVisitor


class scripter_runner:
    
    def print(self, *args, **kwargs):
        prefix = f"[CORE {self.id:02d} : {self.core_time.value:.4f}]"

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
        diff = self.get_script_diff(data['path'])
        self.print(f"Запущен {data['path']}")

        try:
            func(*arg)

        except Exception as e:
            self.print(f"Ошибка внутри запущенного потока {data['path']}: {e}")

        finally:
            with lock:
                proccess_status_info[id] -= diff
        
            self.print(f"Остановлен {data['path']}")

    def __init__(self, Time, id, render_in_stream, render_out_stream, main_in_stream, main_out_stream, objects_cache, procces_in_stream, managers_stream, lock, keyboard_input, proccess_status_info, camera_queue):

        self.core_time = Time
        self.id = id

        self._script_complexity_cache = {}

        proccess_status_info[id] = 0

        while True:
            if procces_in_stream.qsize() > 0:
                status_info = [*proccess_status_info]


                if min(status_info) != status_info[id]:
                    continue

                if status_info.count(min(status_info)) > 1:
                    for i in range(len(status_info)):
                        if status_info[i] == min(status_info):
                            if i != id:
                                continue

                try:
                    data = procces_in_stream.get()

                    with lock:
                        proccess_status_info[id] += self.get_script_diff(data['path'])

                    module = data['path'].replace('.py', '').replace('\\', '.').replace('/', '.')
                    name = module.split('.')[-1]
                    obj_data = objects_cache[list(objects_cache.keys())[int(data['obj_id'])]]['dict_data']

                    module_obj = importlib.import_module(module)

                    thread = threading.Thread(target=self.wrapper, args=(
                        getattr(module_obj, name), 
                        (render_in_stream, render_out_stream, main_in_stream, main_out_stream, obj_data, objects_cache, lock, keyboard_input, Time, camera_queue),
                        proccess_status_info,
                        id, data, lock
                        ))
                    thread.start()
                    
                except (ImportError, AttributeError) as e:
                    self.print(f"Ошибка создания script потока {e}")  
                    continue
                except Exception as e:
                    self.print(f'Ошибка потока {e}')
               