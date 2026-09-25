import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('verify_paper', ROOT/'scripts/verify_paper_dataset.py')
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)

class PaperDatasetVerificationTest(unittest.TestCase):
    def fixture(self, folder):
        from test_build_trusted_dataset import BuildTrustedDatasetTest
        helper = BuildTrustedDatasetTest()
        records = helper.records()
        for row in records:
            row['trusted_provenance']['latency_ms'] = row['latency_ms']
        helper.invoke(records, folder, lambda c: c.update(settings={
            'manifest': str(folder/'manifest.json'), 'repetitions': 3}))
        (folder/'measured').mkdir()
        (folder/'accepted.jsonl').rename(folder/'measured/accepted.jsonl')
        (folder/'raw').mkdir()
        raw = [{k: v for k, v in row.items() if k != 'trusted_provenance'} for row in records]
        (folder/'raw/autodop_trace_fixture.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in raw))
        (folder/'provenance').mkdir()
        events = [copy.deepcopy(row['trusted_provenance']) for row in records]
        natives = [{**events[0], 'id': tid, 'phase': 'native', 'query_id': 'native-'+tid} for tid in ('a', 'b')]
        (folder/'provenance/executions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in natives+events))
        return records

    def test_partition_features_pairs_and_split_mutations(self):
        for target in ('all/samples.jsonl', 'train/samples.jsonl', 'train/pairs.jsonl', 'split_manifest.json'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                self.fixture(folder)
                self.assertEqual('verified', verifier.verify(folder, folder/'out')['status'])
                path = folder/'out'/target
                if target.endswith('samples.jsonl'):
                    values = list(verifier.rows(path))
                    values[0]['dop_config'] = [8]
                    path.write_text(''.join(json.dumps(r)+'\n' for r in values))
                elif target.endswith('pairs.jsonl'):
                    path.write_text('')
                else:
                    value = json.loads(path.read_text())
                    value['train'], value['test'] = value['test'], value['train']
                    path.write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, 'reconstructed contract'):
                    verifier.verify(folder, folder/'out')

    def test_matching_provenance_journal_tamper_rejected_against_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            records = self.fixture(folder)
            records[0]['trusted_provenance']['latency_ms'] = 999
            (folder/'measured/accepted.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
            journal = folder/'provenance/executions.jsonl'
            events = list(verifier.rows(journal))
            for event in events:
                if event['query_id'] == records[0]['query_id']:
                    event['latency_ms'] = 999
            journal.write_text(''.join(json.dumps(r)+'\n' for r in events))
            with self.assertRaisesRegex(ValueError, 'raw FE latency'):
                verifier.verify(folder, folder/'out')
