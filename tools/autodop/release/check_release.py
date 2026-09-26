#!/usr/bin/env python3
"""Load and test an offline release in a fresh Compose project, then remove test data."""
import argparse
import json
import os
import subprocess
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    root = args.directory.resolve()
    if not (root / 'images.tar').is_file() or not (root / 'compose.yaml').is_file():
        parser.error('directory must contain images.tar and compose.yaml')
    # Never re-use a user project: cleanup below deletes only these new test volumes.
    project = 'autodop-check-' + uuid.uuid4().hex[:12]
    env = {**os.environ, 'MYSQL_PORT': '0', 'FE_HTTP_PORT': '0', 'AUTODOP_PORT': '0'}
    compose = ['docker', 'compose', '-p', project, '-f', str(root / 'compose.yaml')]
    output = root / 'validation' / project
    output.mkdir(parents=True, exist_ok=False)

    def run(command, name, timeout=300, check=True):
        with (output / (name + '.log')).open('w') as log:
            return subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                  timeout=timeout, check=check)

    result = {'status': 'running', 'project': project, 'scope': 'fe_and_autodop_without_be'}
    try:
        run(['docker', 'load', '-i', str(root / 'images.tar')], 'load', timeout=600)
        run([*compose, 'up', '-d', 'autodop', 'fe'], 'start')
        run([*compose, '--profile', 'check', 'run', '--rm', '--no-deps', 'check'], 'smoke')
        run([*compose, 'ps', '--format', 'json'], 'containers')
        result['status'] = 'passed'
    except Exception as error:
        result.update(status='failed', error=str(error))
        raise
    finally:
        try:
            run([*compose, 'logs', '--no-color', '--tail', '150'], 'container-logs', check=False)
        finally:
            cleanup = run([*compose, 'down', '-v', '--remove-orphans'], 'cleanup', check=False)
            result['cleanup_returncode'] = cleanup.returncode
            if cleanup.returncode:
                result['status'] = 'cleanup_failed'
            (output / 'RESULT.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps({**result, 'logs': str(output)}, indent=2))
    if result['status'] != 'passed':
        raise RuntimeError('Release check did not complete successfully')


if __name__ == '__main__':
    main()
