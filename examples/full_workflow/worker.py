import asyncio
import time

from mlq.queue import MLQ

mlq = MLQ('example_app', 'redis-14870.c277.us-east-1-3.ec2.cloud.redislabs.com', 14870, 'BI7KiI3MntIlW5lRaDW20oGG7Y1bA2kv', 0)

def listener_func(number_dict, *args):
    print(number_dict['number'])
    time.sleep(10)
    return number_dict['number'] ** 2

async def main():
    print("Running, waiting for messages.")
    mlq.create_listener(listener_func)

if __name__ == '__main__':
    asyncio.run(main())
