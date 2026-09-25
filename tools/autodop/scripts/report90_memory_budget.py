"""Experimental client admission, explicitly separate from deployed AutoDOP."""
from collections import deque
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time


class MemoryBudget:
    def __init__(self, capacity, journal=None):
        if type(capacity) is not int or capacity<=0:
            raise ValueError('capacity must be positive integer bytes')
        self.capacity=capacity
        self.used=0
        self.condition=threading.Condition()
        self.queue=deque()
        self.journal=journal
        self.sequence=0

    def record(self,action,token,amount):
        row=dict(sequence=self.sequence,action=action,token=token,bytes=amount,
                 used_bytes=self.used,capacity_bytes=self.capacity,monotonic=time.monotonic())
        if self.journal is not None:
            self.journal.write(json.dumps(row)+'\n');self.journal.flush()
        self.sequence+=1

    @contextmanager
    def reserve(self,amount,token,timeout=240):
        if type(amount) is not int or not 0<amount<=self.capacity:
            raise ValueError('request must fit positive memory budget')
        ticket=object();start=time.monotonic()
        with self.condition:
            self.queue.append(ticket)
            try:
                while self.queue[0] is not ticket or self.used+amount>self.capacity:
                    remaining=timeout-(time.monotonic()-start)
                    if remaining<=0:
                        raise TimeoutError('experimental memory admission timed out')
                    self.condition.wait(remaining)
                self.queue.popleft();self.used+=amount
                try:
                    self.record('reserve',token,amount)
                except BaseException:
                    self.used-=amount;self.condition.notify_all();raise
                self.condition.notify_all()
            except BaseException:
                if ticket in self.queue:self.queue.remove(ticket)
                self.condition.notify_all();raise
        try:
            yield (time.monotonic()-start)*1000
        finally:
            with self.condition:
                self.used-=amount
                assert 0<=self.used<=self.capacity
                try:self.record('release',token,amount)
                finally:self.condition.notify_all()


_lock=threading.Lock()
_gate=None
_spec=None


def fetch_timed(cursor,statement,event):
    from report90_retry_capture import fetch_timed as retry_fetch
    global _gate,_spec
    if event['mode'] not in ('native_budget','policy_budget'):
        return retry_fetch(cursor,statement,event)
    with _lock:
        if _gate is None:
            _spec=json.loads(Path(os.environ['AUTODOP_EXPERIMENT_MEMORY_BUDGET']).read_text())
            journal=Path(os.environ['AUTODOP_EXPERIMENT_ADMISSION_JOURNAL']).open('x')
            _gate=MemoryBudget(_spec['capacity_bytes'],journal)
    row=_spec['queries'][event['id']]
    assert row['sql_sha256']==event['sql_sha256']
    token='|'.join(str(event[k]) for k in ('mode','clients','phase','round','client','sequence'))
    start=time.monotonic()
    with _gate.reserve(row['reservation_bytes'],token) as wait_ms:
        event['admission']=dict(token=token,reservation_bytes=row['reservation_bytes'],
                                wait_ms=wait_ms,capacity_bytes=_gate.capacity,
                                protocol='historical_profile_fifo_reservation_v1')
        try:
            return retry_fetch(cursor,statement,event)
        finally:
            event['admission_request_latency_ms']=(time.monotonic()-start)*1000
            event['admission_timing_contract']='wait_plus_all_attempts_and_retry_backoff_v1'
