#!/usr/bin/env python3
"""Run an experiment entrypoint from an immutable, hashed Python source snapshot."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, record):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, indent=2) + '\n')
    temporary.replace(path)


def prepare_step(run_root, name, command, project_root=None, dependency_paths=()):
    if not name or name in ('.', '..') or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for char in name):
        raise ValueError('step name must be a safe single path component')
    if not command:
        raise ValueError('missing child command')
    project = Path(project_root).resolve() if project_root else Path(__file__).resolve().parents[3]
    source_root = project / 'tools' / 'autodop'
    step = Path(run_root).resolve() / 'steps' / name
    if step.exists() and any(step.iterdir()):
        raise ValueError('refusing nonempty step directory')
    step.mkdir(parents=True, exist_ok=True)
    snapshot = step / 'source'
    hashes = {}
    sources = sorted(set(source_root.glob('*.py')) | set((source_root / 'autodop').glob('*.py')) |
                     set((source_root / 'scripts').glob('*.py')))
    for path in sources:
        relative = path.relative_to(project)
        destination = snapshot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Hash the copied bytes, not a second read that could observe concurrent edits.
        contents = path.read_bytes()
        destination.write_bytes(contents)
        shutil.copymode(path, destination)
        destination.chmod(destination.stat().st_mode & ~0o222)
        hashes[str(relative)] = hashlib.sha256(contents).hexdigest()
    executed = list(command)
    entrypoints = []
    for index, value in enumerate(command):
        if not value.endswith('.py'):
            continue
        path = Path(value)
        resolved = (project / path).resolve() if not path.is_absolute() else path.resolve()
        try:
            relative = resolved.relative_to(project)
        except ValueError:
            continue
        if str(relative) in hashes:
            entrypoints.append((index, relative))
            # Only the first repository Python file is the executable entrypoint.
            # Later .py arguments may be data and must retain their original meaning.
            break
    if not entrypoints:
        raise ValueError('command must invoke a snapshotted tools/autodop Python entrypoint')
    index, relative = entrypoints[0]
    executed[index] = str(snapshot / relative)
    environment = os.environ.copy()
    # A controlled source-only PYTHONPATH prevents editable local modules from being
    # imported accidentally, and avoids recording arbitrary potentially secret values.
    source_pythonpath = [str(snapshot / 'tools' / 'autodop'), str(snapshot / 'tools' / 'autodop' / 'scripts')]
    dependencies = [str(Path(path).resolve()) for path in dependency_paths]
    if any(not Path(path).is_dir() for path in dependencies):
        raise ValueError('dependency paths must be existing directories')
    source_pythonpath.extend(dependencies)
    environment['PYTHONPATH'] = os.pathsep.join(source_pythonpath)
    record = {'status': 'prepared', 'requested_argv': list(command), 'executed_argv': executed,
              'cwd': str(project), 'started_at_utc': None, 'finished_at_utc': None,
              'returncode': None, 'python_version': platform.python_version(),
              'wrapper_python_executable': sys.executable,
              'selected_environment': {'PYTHONPATH': environment['PYTHONPATH']},
              'dependency_paths': dependencies,
              'dependency_metadata_sha256': {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                    for folder in dependencies for path in Path(folder).glob('*.dist-info/METADATA')},
              'source_sha256': hashes, 'entrypoint': str(relative)}
    write_json(step / 'step.json', record)
    return step, executed, environment, record


def run_step(run_root, name, command, project_root=None, dependency_paths=()):
    step, executed, environment, record = prepare_step(run_root, name, command, project_root, dependency_paths)
    record.update(status='running', started_at_utc=utc_now())
    write_json(step / 'step.json', record)
    returncode = 1
    try:
        with (step / 'log.txt').open('wb') as log:
            child = subprocess.Popen(executed, cwd=record['cwd'], env=environment,
                                     stdout=log, stderr=subprocess.STDOUT)
            record['pid'] = child.pid
            write_json(step / 'step.json', record)
            try:
                returncode = child.wait()
            except BaseException:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                raise
        record.update(status='finished' if returncode == 0 else 'failed', returncode=returncode)
    except BaseException as exc:
        record.update(status='failed', error=f'{type(exc).__name__}: {exc}', returncode=returncode)
        raise
    finally:
        record['finished_at_utc'] = utc_now()
        write_json(step / 'step.json', record)
    return returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--dependency-path', action='append', default=[])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    code = run_step(args.run_root, args.name, command, dependency_paths=args.dependency_path)
    # Shell exit status convention for a child killed by a signal.
    raise SystemExit(code if code >= 0 else 128 - code)


if __name__ == '__main__':
    main()
