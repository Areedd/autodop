"""Independently join predictor, FE statement audit, and released global slots."""
import hashlib
import json
from pathlib import Path
import re
import time

from report90_execute import ROOT, RUN


def main():
    out = RUN / 'native_slot_live17'
    result = json.loads((out / 'RESULT.json').read_text())
    records = result['records']
    assert len(records) == 8 and not result['errors']
    rows = {r['audit_token']: r for r in records}
    audit = ROOT.parents[1] / 'output/fe/log/fe.audit.log'
    matches = {}
    for _ in range(15):
        matches = {}
        with audit.open() as f:
            for line in f:
                if ' [query] ' not in line:
                    continue
                token = re.search(r'autodop_memory_features_[0-9a-f]{32}', line)
                if not token or token[0] not in rows:
                    continue
                assert token[0] not in matches
                matches[token[0]] = line.rstrip('\n')
        if matches.keys() == rows.keys():
            break
        time.sleep(1)
    assert matches.keys() == rows.keys(), 'Missing Explain statement audit'
    history = json.loads((out / 'SLOT_HISTORY.json').read_text())['rows']
    normalize = lambda s: ' '.join(s.split()).rstrip(';').rstrip()
    normalize_id = lambda s: s.lower().replace('-', '')
    verified = []
    for token, row in rows.items():
        line = matches[token]
        assert '|State=EOF|' in line and '|ErrorCode=|' in line
        assert '|ScanBytes=0|' in line and '|ScanRows=0|' in line
        assert '|Db=' + row['database'] + '|' in line
        assert normalize(line.split('|Stmt=', 1)[1].split('|Digest=', 1)[0]) == normalize(row['explain_sql'])
        prediction = row['prediction']
        expected = prediction['expected_physical_slots']
        assert '|PredictMemBytes=' + str(prediction['encoded_cost_bytes']) + '|' in line
        assert '|Slots=' + str(expected) + '|' in line, (token, expected, line)
        qid = re.search(r'\|QueryId=([0-9a-f-]+)\|', line)[1]
        slots = [h for h in history if normalize_id(h['query_id']) == normalize_id(qid)]
        assert len(slots) == 1, ('No unique global slot history', qid, slots)
        slot = slots[0]
        assert slot['state'] == 'RELEASED', slot
        assert int(slot['est_costs_slots']) == expected and int(slot['allocate_slots']) == expected, slot
        # QUERY is truncated to 16 characters in native ExtraMessage. Its other
        # fields are overwritten by later statements through connection listeners.
        # Identity and prediction are therefore verified against the immutable FE
        # statement audit; the slot itself has the exact query ID and allocation.
        extra = json.loads(slot['extra_message'])
        extra_matches = int(extra['PredictMemBytes']) == prediction['encoded_cost_bytes']
        verified.append(dict(id=row['id'], mode=row['predictor_mode'], dop=row['pipeline_dop'],
                             query_id=qid, expected_slots=expected, slot=slot, audit_line=line,
                             historical_extra_prediction_matches=extra_matches))
    assert len({r['query_id'] for r in verified}) == 8
    for file in ['frontend_settings.json', 'QUEUE_SETTINGS.json']:
        assert json.loads((out / file).read_text())['restored']
    files = [out / n for n in ['RESULT.json', 'SLOT_HISTORY.json', 'PROTOCOL.json', 'frontend_settings.json', 'QUEUE_SETTINGS.json']]
    files.append(Path(__file__))
    proof = dict(status='verified', records=verified,
                 scope='Eight Explain queries: prediction and Slots from unique immutable FE statement audit, joined by exact QueryId to globally allocated and RELEASED slots. No data execution or concurrency performance claim.',
                 metadata_note='Native history QUERY is truncated; ExtraMessage is overwritten by subsequent queries on a reused connection. It is retained as diagnostic data, not identity or prediction evidence.',
                 historical_extra_mismatches=sum(not r['historical_extra_prediction_matches'] for r in verified),
                 source_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
    (out / 'ALLOCATION_AUDIT.json').write_text(json.dumps(proof, indent=2) + '\n')
    (out / 'STATUS.json').write_text(json.dumps(dict(status='verified', records=8)) + '\n')
    print('Verified native allocations and releases:', len(verified), flush=True)


if __name__ == '__main__':
    main()
