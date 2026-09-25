import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from run_paper_step import prepare_step, run_step


def fixture(tmp_path):
    project = tmp_path / 'project'
    source = project / 'tools' / 'autodop'
    (source / 'scripts').mkdir(parents=True)
    (source / 'autodop').mkdir()
    (source / 'autodop' / '__init__.py').write_text('')
    (source / 'autodop' / 'sibling.py').write_text('VALUE = "original"\n')
    (source / 'scripts' / 'entry.py').write_text('from autodop.sibling import VALUE\nprint(VALUE)\n')
    return project, source


def test_snapshot_survives_original_mutation_and_preserves_cwd(tmp_path):
    project, source = fixture(tmp_path)
    command = [sys.executable, 'tools/autodop/scripts/entry.py']
    step, executed, environment, record = prepare_step(tmp_path / 'runs', 'test', command, project)
    (source / 'autodop' / 'sibling.py').write_text('VALUE = "mutated"\n')
    (source / 'scripts' / 'entry.py').write_text('raise RuntimeError("mutated")\n')
    result = subprocess.run(executed, cwd=record['cwd'], env=environment, capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == 'original'
    assert record['cwd'] == str(project)
    assert len(record['source_sha256']) == 3
    assert executed[1].startswith(str(step / 'source'))


def test_nonzero_child_persists_failed_status_and_refuses_overwrite(tmp_path):
    import pytest
    project, source = fixture(tmp_path)
    (source / 'scripts' / 'entry.py').write_text('import sys\nprint("failure evidence")\nsys.exit(7)\n')
    command = [sys.executable, 'tools/autodop/scripts/entry.py']
    assert run_step(tmp_path / 'runs', 'failed', command, project) == 7
    step = tmp_path / 'runs' / 'steps' / 'failed'
    record = json.loads((step / 'step.json').read_text())
    assert record['status'] == 'failed'
    assert record['returncode'] == 7
    assert record['started_at_utc'] and record['finished_at_utc']
    assert 'failure evidence' in (step / 'log.txt').read_text()
    with pytest.raises(ValueError, match='nonempty'):
        run_step(tmp_path / 'runs', 'failed', command, project)


def test_taskset_prefix_and_relative_data_arguments_preserved(tmp_path):
    project, _ = fixture(tmp_path)
    command = ['taskset', '-c', '24', sys.executable, 'tools/autodop/scripts/entry.py',
               '--dataset', 'tools/autodop/runs/data']
    _, executed, _, _ = prepare_step(tmp_path / 'runs', 'prefixed', command, project)
    assert executed[:4] == command[:4]
    assert executed[5:] == command[5:]
    assert Path(executed[4]).is_absolute()
