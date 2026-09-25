"""Ensure independent slot verification rejects broken execution evidence."""
import json
from pathlib import Path
import shutil

import pytest
import report90_native_slot_live_audit as audit


@pytest.mark.parametrize('corruption', [None, 'allocation', 'release', 'identity', 'prediction'])
def test_native_slot_evidence(tmp_path, monkeypatch, corruption):
    original = audit.RUN / 'native_slot_live17'
    proof = json.loads((original / 'ALLOCATION_AUDIT.json').read_text())
    repo = tmp_path / 'repo'
    root = repo / 'tools/autodop'
    run = root / 'runs/test'
    out = run / 'native_slot_live17'
    out.mkdir(parents=True)
    for name in ['RESULT.json', 'SLOT_HISTORY.json', 'PROTOCOL.json',
                 'frontend_settings.json', 'QUEUE_SETTINGS.json']:
        shutil.copy2(original / name, out / name)
    history = json.loads((out / 'SLOT_HISTORY.json').read_text())
    qid = proof['records'][0]['query_id']
    slot = next(h for h in history['rows'] if h['query_id'] == qid)
    if corruption == 'allocation':
        slot['allocate_slots'] = str(int(slot['allocate_slots']) + 1)
    elif corruption == 'release':
        slot['state'] = 'ALLOCATED'
    elif corruption == 'identity':
        slot['query_id'] = '00000000-0000-0000-0000-000000000000'
    (out / 'SLOT_HISTORY.json').write_text(json.dumps(history))
    lines = [r['audit_line'] for r in proof['records']]
    if corruption == 'prediction':
        lines[0] = lines[0].replace('|PredictMemBytes=', '|UntrustedPredictMemBytes=')
    log = repo / 'output/fe/log/fe.audit.log'
    log.parent.mkdir(parents=True)
    log.write_text('\n'.join(lines) + '\n')
    monkeypatch.setattr(audit, 'ROOT', root)
    monkeypatch.setattr(audit, 'RUN', run)
    if corruption:
        with pytest.raises(AssertionError):
            audit.main()
        assert not (out / 'ALLOCATION_AUDIT.json').exists()
    else:
        audit.main()
        replay = json.loads((out / 'ALLOCATION_AUDIT.json').read_text())
        assert replay['status'] == 'verified'
        assert replay['historical_extra_mismatches'] == proof['historical_extra_mismatches']
