import math
from memory_lib import ObjectCache


def console(shared_queue, cache, proccess_status_info, vcache_data, icache_data):   
    vcache = ObjectCache(*vcache_data)
    icache = ObjectCache(*icache_data)
    while True:
        try:
            raw_command = input('').strip().lower()
            command = raw_command.split(' ')[0]
            arg = raw_command.split(' ')[1:]
            
            if not command:
                continue
            elif command == 'show':
                print('-' * 10)
                for i in cache:
                    print(i, cache[i]) if cache[i] is not None else print(i, None)
                print('-' * 10)
            elif command == 'pshow':
                print(proccess_status_info)
            elif command == 'vshow':
                print(vcache.get(int(arg[0]), arg[1]))
            elif command == 'ishow':
                print(icache.get(int(arg[0]), arg[1]))
            else:
                shared_queue.put(command)
        except Exception as e:
            print(e)
