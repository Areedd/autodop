import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from autodop.data import build_metadata, build_pairs, load_dataset, write_jsonl
from autodop.model import CostModel, save_checkpoint
from autodop.training import make_training_contract
import auto_retrain
import evaluate_cost
from test_training_contract import dataset


class PipelineTest(unittest.TestCase):
    def test_external_evaluation_ignores_foreign_vocab_and_scaling(self):
        raw, _ = dataset()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_jsonl(folder / 'samples.jsonl', raw)
            write_jsonl(folder / 'pairs.jsonl', build_pairs(raw))
            (folder / 'metadata.json').write_text(json.dumps(build_metadata(raw, [1, 2])))
            samples, pairs, metadata = load_dataset(folder)
            training = make_training_contract(samples, pairs, .25, 0)
            model = CostModel(metadata, {'hidden_dim': 8, 'heads': 2})
            cost = folder / 'cost.pt'
            save_checkpoint(cost, 'cost', model, metadata, training=training)
            # A truly external corpus has unknown categories and independently assigned ids.
            for sample in raw:
                sample['plan_key'] = 'external-' + sample['plan_key']
                sample['fragments'][0]['root_node'] = 'UNSEEN_OPERATOR'
            write_jsonl(folder / 'samples.jsonl', raw)
            write_jsonl(folder / 'pairs.jsonl', build_pairs(raw))
            first = evaluate_cost.evaluate(cost, folder, 'external')
            foreign = copy.deepcopy(metadata)
            foreign['category_vocab']['root_node'] = {'<UNK>': 0, 'UNSEEN_OPERATOR': 999999}
            foreign['feature_mean'] = [1e20] * len(foreign['feature_mean'])
            foreign['feature_std'] = [0] * len(foreign['feature_std'])
            (folder / 'metadata.json').write_text(json.dumps(foreign))
            self.assertEqual(first, evaluate_cost.evaluate(cost, folder, 'external'))
            with self.assertRaisesRegex(ValueError, 'different evaluation corpus'):
                evaluate_cost.evaluate(cost, folder)
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                evaluate_cost.evaluate(cost, folder, 'valid')

    def test_runner_produces_verified_bundle_and_does_not_overwrite(self):
        raw, pairs = dataset()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_jsonl(folder / 'samples.jsonl', raw)
            write_jsonl(folder / 'pairs.jsonl', pairs)
            (folder / 'metadata.json').write_text(json.dumps(build_metadata(raw, [1, 2])))
            out = folder / 'run'
            command = [sys.executable, str(ROOT / 'scripts/run_pipeline.py'),
                       '--dataset', str(folder), '--output-dir', str(out), '--cost-epochs', '1',
                       '--policy-epochs', '1', '--iterations', '2', '--hidden-dim', '8', '--valid-fraction', '.25']
            result = subprocess.run(command, text=True, capture_output=True, timeout=120)
            logs = '\n'.join(p.read_text() for p in out.glob('*.log'))
            self.assertEqual(0, result.returncode, result.stdout + result.stderr + logs)
            manifest = json.loads((out / 'manifest.json').read_text())
            serving = json.loads((out / 'serving.json').read_text())
            valid = json.loads((out / 'evaluation_valid.json').read_text())
            self.assertEqual('complete', manifest['status'])
            self.assertFalse(manifest['database_executed'])
            self.assertEqual(4, serving['http_verified_plans'])
            self.assertTrue(serving['feature_parity'])
            self.assertEqual('valid', valid['partition'])
            self.assertEqual(1, len(valid['plans']))
            self.assertEqual(3, sum(1 for _ in (out / 'labels.jsonl').open()))
            # A subsequent auto_retrain may rebuild the original directory. The
            # completed bundle must still evaluate against its own frozen corpus.
            snapshot = out / 'dataset'
            self.assertTrue((snapshot / 'samples.jsonl').exists())
            original_evaluation = evaluate_cost.evaluate(out / 'cost.pt', snapshot)
            (folder / 'samples.jsonl').write_text('')
            self.assertEqual(original_evaluation, evaluate_cost.evaluate(out / 'cost.pt', snapshot))
            before = (out / 'manifest.json').read_bytes()
            again = subprocess.run(command, text=True, capture_output=True, timeout=20)
            self.assertNotEqual(0, again.returncode)
            self.assertEqual(before, (out / 'manifest.json').read_bytes())

    def test_loaded_dataset_order_is_canonical(self):
        raw, pairs = dataset()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'metadata.json').write_text(json.dumps(build_metadata(raw, [1, 2])))
            write_jsonl(folder / 'samples.jsonl', raw)
            write_jsonl(folder / 'pairs.jsonl', pairs)
            first = load_dataset(folder)
            write_jsonl(folder / 'samples.jsonl', raw[::-1])
            write_jsonl(folder / 'pairs.jsonl', pairs[::-1])
            self.assertEqual(first, load_dataset(folder))

    def test_retrain_accumulates_appends_and_keeps_failed_records_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            traces = folder / 'autodop_trace_test.jsonl'
            write_jsonl(traces, [{'record': index} for index in range(2)])
            args = ['--trace-dir', str(traces), '--dataset-dir', str(folder / 'data'),
                    '--output-dir', str(folder / 'output'), '--min-new-traces', '3', '--with-policy']
            auto_retrain.main(args)
            state_path = folder / 'output/retrain_state.json'
            before = state_path.read_bytes()
            with traces.open('a') as handle:
                handle.write('{"record": 2}\n')
            with patch.object(auto_retrain, 'run_script', side_effect=RuntimeError('training failed')):
                with self.assertRaisesRegex(RuntimeError, 'training failed'):
                    auto_retrain.main(args)
            self.assertEqual(before, state_path.read_bytes())
            with patch.object(auto_retrain, 'run_script') as run:
                auto_retrain.main(args)
            self.assertEqual(['build_dataset.py', 'run_pipeline.py'], [call.args[0] for call in run.call_args_list])
            state = json.loads(state_path.read_text())
            self.assertEqual(3, state['traces_seen'])
            with traces.open('a') as handle:
                handle.write('{"record": 3}\n')
            self.assertEqual(1, auto_retrain.count_new_traces(traces, state['consumed_counts'])[0])


if __name__ == '__main__':
    unittest.main()
