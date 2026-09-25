"""Opt-in failed-SQL identity capture; preserves original error and SQL timing."""
import uuid
from collect_trusted_dataset import fetch_timed as original_fetch_timed


def fetch_timed(cursor, statement, event):
    try:
        return original_fetch_timed(cursor, statement, event)
    except Exception:
        # Query on the same connection before any following statement changes last_query_id.
        # Diagnostic lookup is outside the original SQL timer, like the successful path.
        try:
            cursor.execute('SELECT /*+ SET_VAR(enable_auto_dop=false) */ last_query_id()')
            value = str(cursor.fetchone()[0])
            parsed = uuid.UUID(value)
            if parsed.int == 0:
                raise ValueError('empty last_query_id')
            event['failed_query_id_candidate'] = str(parsed)
            event['failed_query_id_source'] = 'same_connection_last_query_id_immediately_after_sql_error'
        except Exception as lookup_error:
            event['failed_query_id_lookup_error'] = f'{type(lookup_error).__name__}: {lookup_error}'
        # Candidate must be corroborated by a trace/audit record before attribution.
        # Do not upgrade failure to success or replace an existing verified query_id.
        raise
