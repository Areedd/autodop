import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from autodop.data import build_metadata, write_jsonl
from autodop.training import file_sha256, make_training_contract, select_partition
from test_training_contract import dataset
import run_pipeline
import train_cost


class ExplicitSplitTest(unittest.TestCase):
    def setUp(self):
        self.samples, self.pairs = dataset()
        self.manifest = {'train': ['0', '2'], 'valid': ['1', '3'], 'test': ['test-plan']}
        for sample in self.samples:
            sample['template_id'] = 'q' + sample['plan_key']
            sample['split'] = 'train' if sample['plan_key'] in self.manifest['train'] else 'valid'

    def write_dataset(self, folder):
        write_jsonl(folder / 'samples.jsonl', self.samples)
        write_jsonl(folder / 'pairs.jsonl', self.pairs)
        (folder / 'metadata.json').write_text(json.dumps(build_metadata(self.samples, [1, 2])))
        manifest_path = folder / 'split_manifest.json'
        manifest_path.write_text(json.dumps(self.manifest))
        return manifest_path

    def test_manifest_overrides_random_split_and_is_fingerprinted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_dataset(Path(directory))
            contracts = [make_training_contract(self.samples, self.pairs, .9, seed, path)
                         for seed in (0, 100)]
            for contract in contracts:
                self.assertEqual(self.manifest['train'], contract['train_plan_keys'])
                self.assertEqual(self.manifest['valid'], contract['valid_plan_keys'])
                self.assertIsNone(contract['valid_fraction'])
                self.assertEqual(file_sha256(path), contract['split_source']['manifest_sha256'])
                selected, _ = select_partition(self.samples, self.pairs, contract, 'train')
                self.assertEqual({'0', '2'}, {sample['plan_key'] for sample in selected})
            bad = copy.deepcopy(contracts[0])
            bad['split_source']['manifest_sha256'] = 'changed'
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                select_partition(self.samples, self.pairs, bad, 'train')

    def test_rejects_leakage_missing_plans_and_template_inconsistency(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_dataset(Path(directory))
            for change in (
                {'valid': ['0', '1', '3']}, {'test': ['0']}, {'valid': []},
                {'train': ['0']}, {'train': ['0', '2', 'extra']}, {'train': ['0', '0', '2']},
            ):
                path.write_text(json.dumps({**self.manifest, **change}))
                with self.subTest(change=change), self.assertRaises(ValueError):
                    make_training_contract(self.samples, self.pairs, .2, 0, path)
            path.write_text(json.dumps(self.manifest))
            for field, value in (('template_id', 'q1'), ('template_id', ''), ('split', 'valid')):
                samples = copy.deepcopy(self.samples)
                for sample in samples:
                    if sample['plan_key'] == '0':
                        sample[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    make_training_contract(samples, self.pairs, .2, 0, path)

    def test_cost_cli_reads_fixed_split_before_model_training(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = self.write_dataset(folder)
            argv = ['train_cost.py', '--dataset', str(folder), '--output', str(folder / 'cost.pt'),
                    '--split-manifest', str(path), '--valid-fraction', '.99']
            with patch.object(sys, 'argv', argv), patch.object(
                    train_cost, 'CostModel', side_effect=RuntimeError('stop before training')) as model:
                with self.assertRaisesRegex(RuntimeError, 'stop before training'):
                    train_cost.main()
            metadata = model.call_args.args[0]
            self.assertEqual({'<UNK>', 'OP0', 'OP2'}, set(metadata['category_vocab']['root_node']))

    def test_runner_snapshots_manifest_and_passes_it_only_to_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = self.write_dataset(folder)
            output = folder / 'run'
            with patch.object(run_pipeline.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
                result = run_pipeline.main(['--dataset', str(folder), '--output-dir', str(output),
                                            '--split-manifest', str(path), '--valid-fraction', '0'])
            self.assertEqual('complete', result['status'])
            snapshot = output / 'input_split_manifest.json'
            self.assertEqual(path.read_bytes(), snapshot.read_bytes())
            self.assertEqual(file_sha256(snapshot), result['split_manifest_sha256'])
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn('--split-manifest', commands[0])
            self.assertIn(str(snapshot), commands[0])
            self.assertTrue(all('--split-manifest' not in command for command in commands[1:]))
            path.write_text('{}')
            self.assertEqual(self.manifest, json.loads(snapshot.read_text()))


if __name__ == '__main__':
    unittest.main()
