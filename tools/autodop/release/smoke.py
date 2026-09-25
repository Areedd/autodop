"""Check the delivered service and an isolated single-node SQL cluster."""
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
        if not cursor.fetchall():
            cursor.execute('ALTER SYSTEM ADD BACKEND "be:9050"')
        while True:
            cursor.execute('SHOW BACKENDS')
            backends = cursor.fetchall()
            if any(str(b['Alive']).lower() == 'true' for b in backends):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('BE did not become alive')
            time.sleep(2)
        # A dedicated test database is reusable; the upsert keeps repeats deterministic.
        cursor.execute('CREATE DATABASE IF NOT EXISTS autodop_release_smoke')
        cursor.execute('''CREATE TABLE IF NOT EXISTS autodop_release_smoke.items (
            id INT, value BIGINT) PRIMARY KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1
            PROPERTIES ("replication_num"="1")''')
        cursor.execute('INSERT INTO autodop_release_smoke.items VALUES (1, 10), (2, 20), (3, 30)')
        cursor.execute('SELECT COUNT(*) AS n, SUM(value) AS total FROM autodop_release_smoke.items')
        result = cursor.fetchone()
        assert result == {'n': 3, 'total': 60}, result
        cursor.execute('SELECT VERSION() AS version')
        version = cursor.fetchone()['version']
    print(json.dumps({'status': 'passed', 'health': True,
        'http_decisions': len(decision['decisions']), 'confidence': decision['confidence'],
        'sql': result, 'starrocks_version': version,
        'scope': 'Real HTTP policy inference, FE/BE startup, SQL create/insert/aggregate. '
                 'Small SQL may bypass AutoDOP; this is not a performance benchmark.'}, indent=2))


if __name__ == '__main__':
    main()
