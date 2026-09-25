"""Check failure retention and retry boundaries without a database."""
import pytest
import report90_retry_capture as retry


def test_memory_retry_retains_failure_and_separate_timers(monkeypatch):
    count = []
    sleeps = []
    def attempt(cursor, sql, event):
        count.append(sql)
        event.update(client_latency_ms=10, sql_finished_monotonic=len(count),
                     client_timing_contract='execute_and_fetch_v2_excludes_setup_and_digest')
        if len(count) == 1:
            event['failed_query_id_candidate'] = 'failed-id'
            raise RuntimeError('Memory of process exceed limit')
        return [('ok',)]
    monkeypatch.setattr(retry, 'attempt_fetch_timed', attempt)
    monkeypatch.setattr(retry.time, 'sleep', sleeps.append)
    event = {}
    assert retry.fetch_timed(None, 'select 1', event) == [('ok',)]
    assert count == ['select 1', 'select 1'] and sleeps == [1.0]
    assert event['sql_attempt_count'] == 2
    assert event['sql_attempts'][0]['failed_query_id_candidate'] == 'failed-id'
    assert 'failed_query_id_candidate' not in event
    assert event['client_latency_ms'] == 10 and 'logical_request_latency_ms' in event


def test_non_memory_error_never_retried(monkeypatch):
    error = ValueError('syntax error')
    def attempt(*args): raise error
    monkeypatch.setattr(retry, 'attempt_fetch_timed', attempt)
    monkeypatch.setattr(retry.time, 'sleep', lambda _: pytest.fail('unexpected retry'))
    event = {}
    with pytest.raises(ValueError) as caught: retry.fetch_timed(None, 'bad sql', event)
    assert caught.value is error and event['sql_attempt_count'] == 1


def test_retry_limit_retains_all_attempts(monkeypatch):
    errors = []
    sleeps = []
    def attempt(*args):
        error = RuntimeError('Memory of process exceed limit')
        errors.append(error)
        raise error
    monkeypatch.setattr(retry, 'attempt_fetch_timed', attempt)
    monkeypatch.setattr(retry.time, 'sleep', sleeps.append)
    event = {}
    with pytest.raises(RuntimeError) as caught: retry.fetch_timed(None, 'select 1', event)
    assert caught.value is errors[-1]
    assert len(errors) == event['sql_attempt_count'] == 4
    assert sleeps == [1., 2., 4.]
    assert all(row['status'] == 'failed' for row in event['sql_attempts'])
