"""New-database safety and failed statistics prevent trust certification."""
import argparse
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('trusted_tpch_loader', ROOT / 'prepare_trusted_tpch.py')
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


def test_existing_database_rejected_before_create(tmp_path):
    schema = tmp_path / 'schema.sql'
    schema.write_text('canonical fixture')
    args = argparse.Namespace(database='new_tpch', source_database='old_tpch',
                              output=tmp_path/'evidence.json', schema=schema)
    calls = []
    class Helper:
        layouts = dict(loader.PRIMARY_KEYS)
        def __init__(self, unused): pass
        def mysql(self, query):
            calls.append(query)
            return [['new_tpch']]
    with patch.object(loader, 'Repair', Helper), pytest.raises(RuntimeError, match='already exists'):
        loader.run(args)
    assert len(calls) == 1 and 'CREATE' not in calls[0]
    evidence = json.loads(args.output.read_text())
    assert evidence['deployment_valid'] is False
    assert evidence['status'] == 'failed'


def test_existing_evidence_preserved(tmp_path):
    evidence = tmp_path/'evidence.json'
    evidence.write_text('immutable')
    with pytest.raises(FileExistsError):
        loader.run(argparse.Namespace(output=evidence))
    assert evidence.read_text() == 'immutable'
