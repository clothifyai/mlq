import asyncio
import concurrent.futures
from datetime import datetime as dt
import functools
import logging
import json
import pickle
import urllib3
import time
from uuid import uuid1 as uuid

import msgpack
import redis
import jsonpickle
import cloudpickle

class MLQ():
    """Create a queue object"""
    def __init__(self, q_name, redis_host, redis_port, redis_db):
        self.q_name = q_name
        self.processing_q = self.q_name + '_processing'
        self.progress_q = self.q_name + '_progress'
        self.jobs_refs_q = self.q_name + '_jobsrefs'
        self.dead_letter_q = self.q_name + '_deadletter'
        # msgs have a 64 bit id starting at 0
        self.id_key = self.q_name + '_max_id'
        self.id = str(uuid())
        self.loop = asyncio.get_running_loop()
        self.redis = redis.StrictRedis(host=redis_host, port=redis_port, db=redis_db, decode_responses=True)
        logging.info('Connected to Redis at {}:{}'.format(redis_host, redis_port))
        self.pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        self.pool = concurrent.futures.ThreadPoolExecutor()
        self.funcs_to_execute = []
        self.listener = None
        self.http = urllib3.PoolManager()

    def _utility_functions(self):
        """These utilities are passed to listener functions. They allow those
        functions to create new messages (e.g. so it can pipe the output of a listener
        function into another listener function which is potentially running on a different
        listener) and to update progress in case someone queries it midway through."""

        def update_progress(job_id, progress):
            progress_str = self.redis.get(self.progress_q + '_' + job_id)
            progress_dict = msgpack.unpackb(progress_str, raw=False)
            progress_dict['progress'] = progress
            new_record = msgpack.packb(progress_dict, use_bin_type=False)
            self.redis.set(self.progress_q + '_' + job_id, new_record)
            return True

        def store_data(data, key=None, ex=None):
            uid = key or uuid()
            self.redis.set(uid, data, ex=ex)
            return uid

        def fetch_data(data_key):
            return self.redis.get(data_key)

        def post(msg, callback=None, functions=None):
            return self.post(msg, callback, functions)

        def block_until_result(job_id):
            wait_key = 'pub_' + str(job_id)
            self.pubsub.unsubscribe()
            self.pubsub.subscribe(wait_key)
            while True:
                msg = self.pubsub.get_message()
                if msg:
                    return msg['data']
                time.sleep(0.001)

        return {
            'post': post,
            'update_progress': update_progress,
            'store_data': store_data,
            'fetch_data': fetch_data,
            'block_until_result': block_until_result,
        }

    def remove_listener(self, function):
        """Remove a function from the execution schedule of a worker upon msg.
        Workers' functions must be unique by name."""
        if isinstance(function, dict):
            fun_bytes = jsonpickle.decode(json.dumps(function))
            function = cloudpickle.loads(fun_bytes)
        for func in self.funcs_to_execute:
            if func.__name__ == function.__name__:
                self.funcs_to_execute.remove(func)
                return True
        return False

    def create_listener(self, function):
        """Create a MLQ consumer that executes `function` on message received.
        :param func function: A function with the signature my_func(msg, *args).
        Msg is the posted message, optional args[0] inside the function gives access
        to the entire message object (worker id, timestamp, retries etc)
        If there are multiple consumers, they get messages round-robin

        function can return a single item or a tuple of (short_result, long_result)
        short_result must be str, but long_result can be binary.
        """
        # TODO: Probably should be able to specify which worker will do what
        # functions. So also need an endpoint to get worker name.
        if isinstance(function, dict):
            fun_bytes = jsonpickle.decode(json.dumps(function))
            function = cloudpickle.loads(fun_bytes)
        self.funcs_to_execute.append(function)
        if self.listener:
            return True
        def listener():
            while True:
                msg_str = self.redis.brpoplpush(self.q_name, self.processing_q, timeout=0)
                msg_dict = msgpack.unpackb(msg_str, raw=False)
                # update progress_q with worker id + time started
                msg_dict['worker'] = self.id
                msg_dict['processing_started'] = dt.timestamp(dt.utcnow())
                msg_dict['progress'] = 0
                new_record = msgpack.packb(msg_dict, use_bin_type=False)
                self.redis.set(self.progress_q + '_' + str(msg_dict['id']), new_record)
                # process msg ...
                all_ok = True
                short_result = None
                utils = self._utility_functions()
                utils['full_message'] = msg_dict
                utils['update_progress'] = functools.partial(utils['update_progress'], str(msg_dict['id']))
                result = None
                for func in self.funcs_to_execute:
                    if msg_dict['functions'] is None or func.__name__ in msg_dict['functions']:
                        try:
                            result = func(msg_dict['msg'], utils)
                            # end processing message
                            # remove from progress_q ?? or keep along with result????
                        except Exception as e:
                            all_ok = False
                            logging.error(e)
                            logging.info("Moving message {} to dead letter queue".format(msg_dict['id']))
                            # TODO: requeue (write a requeue function) that will attempt
                            # to retry callback if it hangs or response != 200
                            if msg_dict['callback']:
                                self.http.request('GET', msg_dict['callback'], fields={
                                    'success': 0,
                                    'job_id': str(msg_dict['id']),
                                    'short_result': None
                                })
                            msg_dict['progress'] = -1
                            msg_dict['result'] = str(e)
                            new_record = msgpack.packb(msg_dict, use_bin_type=False)
                            self.redis.set(self.progress_q + '_' + str(msg_dict['id']), new_record)
                            self.redis.rpush(self.dead_letter_q, msg_str)
                if all_ok:
                    logging.info('Completed job {}'.format(str(msg_dict['id'])))
                    msg_dict['worker'] = None
                    if result and type(result) in [tuple, list] and len(result) > 1:
                        short_result = result[0]
                        result = result[1]
                    else:
                        short_result = result
                    msg_dict['result'] = result
                    msg_dict['short_result'] = short_result
                    self.redis.publish('pub_' + str(msg_dict['id']), short_result)
                    msg_dict['progress'] = 100
                    msg_dict['processing_finished'] = dt.timestamp(dt.utcnow())
                    new_record = msgpack.packb(msg_dict, use_bin_type=False)
                    self.redis.set(self.progress_q + '_' + str(msg_dict['id']), new_record)
                    # TODO: requeue (write a requeue function) that will attempt
                    # to retry callback if it hangs or response != 20
                    if msg_dict['callback']:
                        self.http.request('GET', msg_dict['callback'], fields={
                            'success': 1,
                            'job_id': str(msg_dict['id']),
                            'short_result': short_result
                        })
                # TODO: rename progress_q to job_status
                self.redis.lrem(self.processing_q, -1, msg_str)
                self.redis.lrem(self.jobs_refs_q, 1, str(msg_dict['id']))
        logging.info('Created listener')
        self.listener = self.loop.run_in_executor(self.pool, listener)
        return True

    def job_count(self):
        return self.redis.llen(self.q_name)

    def create_reaper(self, call_how_often=1, job_timeout=30, max_retries=5):
        """A thread to reap jobs that were too slow
        :param int call_how_often: How often reaper should be called, every [this] seconds
        :param int job_timeout: Jobs processing for longer than this will be requeued"""
        def reaper():
            while True:
                time.sleep(call_how_often)
                time_now = dt.timestamp(dt.utcnow())
                queued_jobs_length = self.redis.llen(self.jobs_refs_q)
                # check first 5 msgs in queue, if any exceed timeout, keep checking
                for i in range(0, (queued_jobs_length // 5) + 1, 5):
                    job_keys = self.redis.lrange(self.jobs_refs_q, i, i + 5)
                    all_ok = True
                    print(job_keys)
                    for job_key in job_keys:
                        progress_key = self.progress_q + '_' + job_key
                        job_str = self.redis.get(progress_key)
                        if not job_str:
                            logging.warning('Found orphan job {}'.format(job_key))
                            self.redis.lrem(self.jobs_refs_q, 1, job_key)
                            all_ok = False
                            continue
                        job = msgpack.unpackb(job_str, raw=False)
                        if job['progress'] != 100 and job['worker'] and time_now - job['processing_started'] > job_timeout:
                            logging.warning('Moved job id {} on worker {} back to queue after timeout {}'.format(job['id'], job['worker'], job_timeout))
                            pipeline = self.redis.pipeline()
                            job['processing_started'] = None
                            job['progress'] = None
                            job['worker'] = None
                            pipeline.lrem(self.processing_q, -1, msgpack.packb(job, use_bin_type=False))
                            job['timestamp'] = dt.timestamp(dt.utcnow())
                            job['retries'] += 1
                            job_id = job['id']
                            if job['retries'] >= max_retries:
                                pipeline.rpush(self.dead_letter_q, job['msg'])
                            else:
                                job = msgpack.packb(job, use_bin_type=False)
                                # update the progress queue
                                pipeline.set(self.progress_q + '_' + job_id, job)
                                pipeline.lpush(self.q_name, job)
                            pipeline.lrem(self.jobs_refs_q, 1, job_id)
                            pipeline.rpush(self.jobs_refs_q, job_id)
                            pipeline.execute()
                            all_ok = False
                            # call callback with failed + requeued status
                            # increment retries and requeue to q_name
                            # delete from processing queue
                    if all_ok:
                        break
        self.loop.run_in_executor(self.pool, reaper)

    def post(self, msg, callback=None, functions=None):
        msg_id = str(self.redis.incr(self.id_key))
        timestamp = dt.timestamp(dt.utcnow())
        logging.info('Posting message with id {} to {} at {}'.format(msg_id, self.q_name, timestamp))
        pipeline = self.redis.pipeline()
        pipeline.rpush(self.jobs_refs_q, msg_id)
        job = {
            'id': msg_id,
            'timestamp': timestamp,
            'worker': None,
            'processing_started': None,
            'processing_finished': None,
            'progress': None,
            'short_result': None,
            'result': None,
            'callback': callback,
            'retries': 0,
            'functions': functions, # Which function names should be called
            'msg': msg
        }
        job = msgpack.packb(job, use_bin_type=False)
        pipeline.lpush(self.q_name, job)
        pipeline.set(self.progress_q + '_' + msg_id, job)
        pipeline.execute()
        return msg_id