"""Check policy inference and FE startup without provisioning a backend."""
import json
import os
import time
import urllib.request
from pathlib import Path

import pymysql


def main():
    endpoint = os.environ.get('AUTODOP_ENDPOINT', 'http://autodop:18080')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(endpoint + '/health', timeout=10) as response:
        assert json.load(response) == {'status': 'ok'}
    request = json.loads((Path(__file__).parent / 'models/request.json').read_text())
    with opener.open(urllib.request.Request(endpoint + '/v1/decide',
                     data=json.dumps(request).encode(),
                     headers={'Content-Type': 'application/json'}), timeout=30) as response:
        decision = json.load(response)
    assert decision['query_id'] == request['query_id']
    assert 0 <= decision['confidence'] <= 1
    fragments = {str(f['fragment_id']): f for f in request['fragments']}
    assert len(decision['decisions']) == len(fragments)
    assert {d['fragment_id'] for d in decision['decisions']} == set(fragments)
    for item in decision['decisions']:
        assert item['dop'] in fragments[item['fragment_id']]['candidate_dops']
    deadline = time.monotonic() + 240
    while True:
        try:
            connection = pymysql.connect(host=os.environ.get('MYSQL_HOST', 'fe'),
                port=int(os.environ.get('MYSQL_PORT', '9030')), user='root',
                connect_timeout=5, read_timeout=60, autocommit=True,
                cursorclass=pymysql.cursors.DictCursor)
            break
        except pymysql.MySQLError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)
    with connection, connection.cursor() as cursor:
        cursor.execute('SHOW BACKENDS')
        backends = cursor.fetchall()
        # A constant query verifies the FE SQL endpoint without requiring storage.
        cursor.execute('SELECT 1 AS ok')
        result = cursor.fetchone()
        assert result == {'ok': 1}, result
        cursor.execute('SELECT VERSION() AS version')
        compatibility_version = cursor.fetchone()['version']
        cursor.execute('SHOW FRONTENDS')
        frontend_versions = [row['Version'] for row in cursor.fetchall()]
    print(json.dumps({'status': 'passed', 'health': True,
        'http_decisions': len(decision['decisions']), 'confidence': decision['confidence'],
        'sql': result, 'mysql_compatibility_version': compatibility_version,
        'frontend_versions': frontend_versions,
        'registered_backends': len(backends),
        'scope': 'Real HTTP policy inference, FE startup and constant SQL. '
                 'Table storage and data queries require an external BE and are not tested here.'}, indent=2))


if __name__ == '__main__':
    main()
