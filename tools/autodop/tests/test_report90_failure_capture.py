import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from report90_failure_capture import fetch_timed


class Cursor:
    def __init__(self, diagnostic_failure=False):
        self.original_error = RuntimeError('original SQL error')
        self.diagnostic_failure = diagnostic_failure
        self.calls = []

    def execute(self, sql):
        self.calls.append(sql)
        if sql == 'broken SQL':
            raise self.original_error
        if self.diagnostic_failure:
            raise ConnectionError('connection lost')

    def fetchone(self):
        return ('12345678-1234-1234-1234-123456789abc',)

    def fetchall(self):
        return ((1,),)


def test_failed_sql_preserves_original_error_and_timing():
    cursor = Cursor(); event = {}
    with pytest.raises(RuntimeError) as captured:
        fetch_timed(cursor, 'broken SQL', event)
    assert captured.value is cursor.original_error
    assert event['failed_query_id_candidate'] == '12345678-1234-1234-1234-123456789abc'
    assert 'query_id' not in event and 'status' not in event
    assert event['client_latency_ms'] >= 0 and 'sql_finished_monotonic' in event
    assert len(cursor.calls) == 2


def test_failed_lookup_does_not_mask_original_error():
    cursor = Cursor(diagnostic_failure=True); event = {}
    with pytest.raises(RuntimeError) as captured:
        fetch_timed(cursor, 'broken SQL', event)
    assert captured.value is cursor.original_error
    assert 'failed_query_id_candidate' not in event
    assert 'ConnectionError' in event['failed_query_id_lookup_error']


def test_success_never_adds_diagnostic_query():
    cursor = Cursor(); event = {}
    assert fetch_timed(cursor, 'SELECT 1', event) == ((1,),)
    assert cursor.calls == ['SELECT 1']
    assert not any(k.startswith('failed_') for k in event)
