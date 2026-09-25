"""Opt-in bounded client retries; retain every SQL attempt and both timing scopes."""
import time
from report90_failure_capture import fetch_timed as attempt_fetch_timed

BACKOFF_SECONDS = (1.0, 2.0, 4.0)


def fetch_timed(cursor, statement, event):
    started = time.monotonic()
    attempts = []
    event['sql_attempts'] = attempts
    event['retry_backoff_seconds'] = []
    try:
        for index in range(len(BACKOFF_SECONDS) + 1):
            attempt = {'attempt_index': index}
            try:
                rows = attempt_fetch_timed(cursor, statement, attempt)
            except Exception as error:
                attempt.update(status='failed', error=f'{type(error).__name__}: {error}')
                attempts.append(attempt)
                event.update({k:v for k,v in attempt.items() if k not in ['status','error','attempt_index']})
                # Retry only the process-limit failure actually observed in our BE logs.
                memory_limit = 'Memory of process exceed limit' in str(error)
                if not memory_limit or index == len(BACKOFF_SECONDS):
                    raise
                delay = BACKOFF_SECONDS[index]
                event['retry_backoff_seconds'].append(delay)
                time.sleep(delay)
            else:
                attempt['status'] = 'sql_succeeded_pending_digest_and_trace_verification'
                attempts.append(attempt)
                # Preserve the original last-SQL timer contract. Logical time is separate.
                for key in ['failed_query_id_candidate','failed_query_id_source','failed_query_id_lookup_error']:
                    event.pop(key, None)
                event.update({k:v for k,v in attempt.items() if k not in ['status','attempt_index']})
                return rows
    finally:
        event['logical_request_latency_ms'] = (time.monotonic() - started) * 1000
        event['logical_timing_contract'] = 'all_sql_attempts_failure_identity_lookups_and_backoff_v1'
        event['sql_attempt_count'] = len(attempts)
