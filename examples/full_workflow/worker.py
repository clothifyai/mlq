import asyncio
import time

from mlq.queue import MLQ

mlq = MLQ('example_app', 'ec2-3-86-25-133.compute-1.amazonaws.com', 6379, 'rRSnTQE9q5UU', 'default', 0)

def listener_func(number_dict, *args):
    print(number_dict['number'])
    time.sleep(10)
    return number_dict['number'] ** 2

async def main():
    print("Running, waiting for messages.")
    mlq.create_listener(listener_func)

if __name__ == '__main__':
    asyncio.run(main())
