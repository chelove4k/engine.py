import math


def console(shared_queue, cache, proccess_status_info):    
    while True:
        command = input('').strip().lower()
        
        if not command:
            continue
        elif command == 'show':
            print('-' * 10)
            for i in cache:
                print(i, cache[i]) if cache[i] is not None else print(i, None)
            print('-' * 10)
        elif command == 'pshow':
            print(proccess_status_info)
        else:
            shared_queue.put(command)


        # data = []
        # color = 255
        # if command == 'run':
        #     for y in range(-300, 300, 40):
        #         for x in range(-400, 400, 40):
        #             command = f'add C:\\Users\\root\\Desktop\\arcade_test\\image.png {x+400} {y+300} 0.5 90'
        #             
        # if command == 'new':
        #     command = f'add C:\\Users\\root\\Desktop\\arcade_test\\image.png 400 300 0.5 90'
        #     shared_queue.put(command)
        #     command = f'moveto id 0 -100 -200'
        #     shared_queue.put(command)
        # else:
        #     shared_queue.put(command)