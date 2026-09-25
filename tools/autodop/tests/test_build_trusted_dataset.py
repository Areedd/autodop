import copy
import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from build_trusted_dataset import build
from autodop.data import sample_from_trace
from autodop.training import file_sha256
from test_trusted_dataset import trace


class BuildTrustedDatasetTest(unittest.TestCase):
    def records(self):
        rows=[]
        for tid, split in [('a','train'),('b','test')]:
            for dop in (1,2):
                for repetition in range(3):
                    qid=f'{tid}-{dop}-{repetition}'
                    row=trace({'0':dop},qid)
                    row['latency_ms']=100/dop+repetition
                    row['trusted_provenance']={
                        'id':tid,'split':split,'sql_sha256':hashlib.sha256(('select '+tid).encode()).hexdigest(),
                        'database':'fixture','config_id':dop-1,'phase':'measured','round':repetition,
                        'query_id':qid,'expected_repetitions':3,'status':'verified',
                        'requested_dop':{'0':dop},'actual_dop':{'0':dop},
                        'result_digest':{'sha256':'same-result','rows':1},
                    }
                    rows.append(row)
        return rows

    def invoke(self, rows, folder, mutate_collection=None):
        manifest=folder/'manifest.json'
        templates = [{'id':tid,'split':split,'database':'fixture',
                      'sql_sha256':hashlib.sha256(('select '+tid).encode()).hexdigest()}
                     for tid,split in [('a','train'),('b','test')]]
        manifest.write_text(json.dumps({'queries':templates}))
        (folder/'sql').mkdir()
        for row in templates:
            (folder/'sql'/(row['id']+'.sql')).write_text('select '+row['id'])
        collection={'status':'finished','manifest_sha256':file_sha256(manifest), 'queries':[
            {**row,'status':'accepted','configs':[{'0':1},{'0':2}],'measured_executions':6,
             'native_result_digest':{'sha256':'same-result','rows':1},
             'plan_key':sample_from_trace(trace({'0':1}))['plan_key']} for row in templates]}
        if mutate_collection:
            mutate_collection(collection)
        collection_path=folder/'collection.json'
        collection_path.write_text(json.dumps(collection))
        source=folder/'accepted.jsonl'
        source.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        return build(source,manifest,folder/'out',collection_path=collection_path)

    def test_measured_medians_and_template_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            result=self.invoke(self.records(),folder)
            self.assertEqual(12,result['measured_executions'])
            self.assertEqual(2,result['partitions']['train']['samples'])
            self.assertEqual(1,result['partitions']['test']['pairs'])
            splits=json.loads((folder/'out/split_manifest.json').read_text())
            self.assertFalse(set(splits['train']) & set(splits['test']))
            samples=[json.loads(line) for line in (folder/'out/train/samples.jsonl').read_text().splitlines()]
            self.assertEqual([51,101],sorted(s['latency_ms'] for s in samples))

    def test_warmup_incomplete_and_corrupt_provenance_rejected(self):
        changes=[
            lambda rows: rows[0]['trusted_provenance'].update(phase='warmup'),
            lambda rows: rows.pop(),
            lambda rows: rows.__delitem__(slice(0,3)),
            lambda rows: rows[0]['trusted_provenance'].update(config_id=1),
            lambda rows: rows[0]['trusted_provenance'].update(split='test'),
            lambda rows: rows[0]['trusted_provenance'].update(result_digest={'sha256':'different','rows':1}),
            lambda rows: rows[0]['trusted_provenance'].update(actual_dop={'0':8}),
            lambda rows: rows.append(copy.deepcopy(rows[0])),
        ]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                rows=self.records();change(rows)
                with self.assertRaises(ValueError):self.invoke(rows,Path(tmp))

    def test_collection_and_native_baseline_are_authoritative(self):
        changes=[
            lambda c: c.update(status='running'),
            lambda c: c.update(manifest_sha256='changed'),
            lambda c: c['queries'][0].update(status='excluded'),
            lambda c: c['queries'][0].update(native_result_digest={'sha256':'other','rows':1}),
            lambda c: c['queries'][0].update(plan_key='changed'),
            lambda c: c['queries'][0].update(sql_sha256='changed'),
        ]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    self.invoke(self.records(),Path(tmp),change)

    def test_overlapping_and_touching_ranges_drop_pairs_but_preserve_samples(self):
        for fast_last in (100, 101):
            with self.subTest(fast_last=fast_last), tempfile.TemporaryDirectory() as tmp:
                rows = self.records()
                for row in rows:
                    p = row['trusted_provenance']
                    if p['id'] == 'a' and p['requested_dop']['0'] == 2 and p['round'] == 2:
                        row['latency_ms'] = fast_last
                folder = Path(tmp)
                summary = self.invoke(rows,folder)
                train = summary['partitions']['train']
                self.assertEqual(1, train['candidate_pairs'])
                self.assertEqual(1, train['dropped_unstable_pairs'])
                self.assertEqual(0, train['pairs'])
                self.assertEqual(2, train['samples'])
                self.assertEqual(1, summary['partitions']['test']['pairs'])
                self.assertEqual(0, summary['partitions']['test']['dropped_unstable_pairs'])
                samples = [json.loads(line) for line in (folder/'out/train/samples.jsonl').read_text().splitlines()]
                self.assertEqual([51,101],sorted(row['latency_ms'] for row in samples))
                self.assertEqual(0,json.loads((folder/'out/train/metadata.json').read_text())['pair_count'])
                self.assertEqual(1,json.loads((folder/'out/test/metadata.json').read_text())['pair_count'])
