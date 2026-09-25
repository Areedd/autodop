import io
import json
import threading
import time
import pytest
from report90_memory_budget import MemoryBudget


def test_exception_releases_reservation_and_journal_balances():
    journal=io.StringIO();gate=MemoryBudget(10,journal)
    with pytest.raises(RuntimeError):
        with gate.reserve(9,'failed'):
            raise RuntimeError('SQL failure')
    with gate.reserve(10,'next'):
        assert gate.used==10
    assert gate.used==0
    rows=list(map(json.loads,journal.getvalue().splitlines()))
    assert [r['used_bytes'] for r in rows]==[9,0,10,0]
    assert [r['sequence'] for r in rows]==list(range(4))


def test_waiting_request_cannot_overcommit_and_resumes():
    gate=MemoryBudget(10);entered=threading.Event();finished=threading.Event()
    def worker():
        with gate.reserve(6,'second'):
            entered.set();assert gate.used<=10
        finished.set()
    with gate.reserve(6,'first'):
        thread=threading.Thread(target=worker);thread.start()
        assert not entered.wait(.05)
        assert gate.used==6
    assert finished.wait(2)
    thread.join();assert gate.used==0


def test_timeout_removes_waiter_and_oversize_fails_fast():
    gate=MemoryBudget(10)
    with pytest.raises(ValueError):
        with gate.reserve(11,'oversize'):pass
    with gate.reserve(10,'holder'):
        with pytest.raises(TimeoutError):
            with gate.reserve(1,'timeout',timeout=.01):pass
    with gate.reserve(10,'after'):
        assert not gate.queue
    assert gate.used==0
