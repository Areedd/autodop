import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from autodop.data import build_metadata, build_pairs, load_dataset, write_jsonl
from autodop.training import (dataset_fingerprint, label_lineage, make_training_contract,
                              select_partition, validate_labels)


def dataset():
    samples = []
    for plan in range(4):
        for dop in (1, 2):
            samples.append({"plan_key": str(plan), "config_key": str(dop),
                            "fragments": [{"fragment_index": 0, "root_node": f"OP{plan}",
                                           "candidate_dops": [1, 2], "worker_ids": [1],
                                           "fragment_cost": 10 ** plan}],
                            "edges": [], "dop_config": [dop], "latency_ms": 100 / dop})
    return samples, build_pairs(samples)


class TrainingContractTest(unittest.TestCase):
    def test_identity_and_partition(self):
        samples, pairs = dataset()
        contract = make_training_contract(samples, pairs, .25, 0)
        train, train_pairs = select_partition(samples, pairs, contract, "train")
        valid, _ = select_partition(samples, pairs, contract, "valid")
        self.assertFalse({x['plan_key'] for x in train} & {x['plan_key'] for x in valid})
        self.assertEqual(3, len(train_pairs))
        self.assertEqual(dataset_fingerprint(samples, pairs),
                         dataset_fingerprint(samples[::-1], pairs[::-1]))
        changed = copy.deepcopy(samples)
        changed[0]['latency_ms'] += 1
        with self.assertRaisesRegex(ValueError, 'dataset fingerprint'):
            select_partition(changed, pairs, contract, 'train')
        changed_contract = copy.deepcopy(contract)
        changed_contract['seed'] += 1
        with self.assertRaisesRegex(ValueError, 'split fingerprint'):
            select_partition(samples, pairs, changed_contract, 'train')

    def test_label_lineage_and_coverage(self):
        samples, pairs = dataset()
        contract = make_training_contract(samples, pairs, .25, 0)
        train, _ = select_partition(samples, pairs, contract, 'train')
        with tempfile.TemporaryDirectory() as directory:
            teacher = Path(directory) / 'teacher.pt'
            teacher.write_bytes(b'teacher')
            rows = [{'plan_key': key, 'best_dop_config': [1],
                     'lineage': label_lineage(contract, teacher)}
                    for key in contract['train_plan_keys']]
            self.assertEqual(3, len(validate_labels(rows, train, contract, teacher, [1, 2])))
            with self.assertRaisesRegex(ValueError, 'cover every'):
                validate_labels(rows[:-1], train, contract, teacher, [1, 2])
            bad = copy.deepcopy(rows)
            bad[0]['plan_key'] = contract['valid_plan_keys'][0]
            with self.assertRaisesRegex(ValueError, 'outside'):
                validate_labels(bad, train, contract, teacher, [1, 2])
            bad = copy.deepcopy(rows)
            bad[0].pop('lineage')
            with self.assertRaisesRegex(ValueError, 'lineage'):
                validate_labels(bad, train, contract, teacher, [1, 2])
            for mutate in (
                lambda row: row['lineage'].update(split_fingerprint='old split'),
                lambda row: row['lineage'].update(method='measured_best'),
                lambda row: row.update(best_dop_config=[32]),
            ):
                bad = copy.deepcopy(rows)
                mutate(bad[0])
                with self.assertRaises(ValueError):
                    validate_labels(bad, train, contract, teacher, [1, 2])
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                validate_labels(rows + rows[:1], train, contract, teacher, [1, 2])
            teacher.write_bytes(b'new teacher')
            with self.assertRaisesRegex(ValueError, 'stale'):
                validate_labels(rows, train, contract, teacher, [1, 2])

    def test_cli_training_loop_uses_train_metadata_and_teacher(self):
        samples, pairs = dataset()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_jsonl(folder / 'samples.jsonl', samples)
            write_jsonl(folder / 'pairs.jsonl', pairs)
            (folder / 'metadata.json').write_text(json.dumps(build_metadata(samples, [1, 2])))
            cost, labels, policy = (folder / name for name in ('cost.pt', 'labels.jsonl', 'policy.pt'))
            def run(script, *args):
                result = subprocess.run([sys.executable, str(ROOT / 'scripts' / script),
                                         '--dataset', str(folder), *map(str, args)],
                                        capture_output=True, text=True, timeout=90)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            run('train_cost.py', '--output', cost, '--epochs', 1, '--hidden-dim', 8,
                '--heads', 2, '--valid-fraction', .25)
            samples, pairs, _ = load_dataset(folder)
            checkpoint = torch.load(cost, weights_only=False)
            training = checkpoint['training']
            self.assertEqual(training, json.loads(cost.with_suffix('.split.json').read_text()))
            train, _ = select_partition(samples, pairs, training, 'train')
            self.assertEqual(build_metadata(train, [1, 2]), checkpoint['metadata'])
            heldout = 'OP' + training['valid_plan_keys'][0]
            self.assertNotIn(heldout, checkpoint['metadata']['category_vocab']['root_node'])
            run('search_mcts.py', '--cost-model', cost, '--output', labels, '--iterations', 2)
            run('train_policy.py', '--cost-model', cost, '--labels', labels,
                '--output', policy, '--epochs', 1)
            distilled = torch.load(policy, weights_only=False)
            self.assertEqual(checkpoint['metadata'], distilled['metadata'])
            self.assertEqual(training['split_fingerprint'], distilled['training']['split_fingerprint'])
            self.assertEqual(label_lineage(training, cost), distilled['training']['teacher'])


if __name__ == '__main__':
    unittest.main()
