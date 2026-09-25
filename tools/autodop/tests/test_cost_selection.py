import json
from pathlib import Path
import sys

import pytest
import torch

from autodop.data import build_metadata, write_jsonl
from test_training_contract import dataset

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import train_cost


def inputs(folder):
    samples, pairs = dataset()
    write_jsonl(folder / 'samples.jsonl', samples)
    write_jsonl(folder / 'pairs.jsonl', pairs)
    (folder / 'metadata.json').write_text(json.dumps(build_metadata(samples, [1, 2])))


@pytest.mark.parametrize('mode,selected,second_accuracy', [('final', 1, .5), ('best-valid', 0, .5), ('best-valid', 0, .9), ('best-valid-regret', 1, .5)])
def test_checkpoint_contains_selected_epoch_weights(tmp_path, monkeypatch, mode, selected, second_accuracy):
    inputs(tmp_path)
    snapshots = []

    def accuracy(model, graphs, pairs):
        snapshots.append({k: v.detach().clone() for k, v in model.state_dict().items()})
        return second_accuracy if len(snapshots) == 2 else .9

    monkeypatch.setattr(train_cost, 'pair_rank_accuracy', accuracy)
    if mode == 'best-valid-regret':
        def regret(model, graphs, samples):
            assert len({s['plan_key'] for s in samples}) == 1
            return .5 if len(snapshots) == 1 else .1
        monkeypatch.setattr(train_cost, 'validation_choice_regret', regret)
    output = tmp_path / 'cost.pt'
    monkeypatch.setattr(sys, 'argv', ['train_cost.py', '--dataset', str(tmp_path),
                                    '--output', str(output), '--epochs', '2', '--hidden-dim', '8',
                                    '--heads', '2', '--threads', '1', '--valid-fraction', '.25',
                                    '--checkpoint-selection', mode])
    train_cost.main()
    checkpoint = torch.load(output, weights_only=False)
    assert any(not torch.equal(snapshots[0][k], snapshots[1][k]) for k in snapshots[0])
    assert all(torch.equal(checkpoint['model_state'][k], value)
               for k, value in snapshots[selected].items())
    selection = json.loads(output.with_suffix('.selection.json').read_text())
    assert selection['selected_epoch'] == selected + 1
    assert selection['epochs_run'] == 2
    assert selection['validation_gradients_used'] is False


@pytest.mark.parametrize('mode', ['best-valid', 'best-valid-regret'])
def test_best_valid_requires_validation_pairs(tmp_path, monkeypatch, mode):
    inputs(tmp_path)
    output = tmp_path / 'cost.pt'
    monkeypatch.setattr(sys, 'argv', ['train_cost.py', '--dataset', str(tmp_path),
                                    '--output', str(output), '--epochs', '1', '--valid-fraction', '0',
                                    '--checkpoint-selection', mode])
    with pytest.raises(ValueError, match='requires validation pairs'):
        train_cost.main()
    assert not output.exists()


class ScoreModel(torch.nn.Module):
    def forward(self, graph):
        assert not torch.is_grad_enabled()
        return graph


def test_choice_regret_uses_model_scores_and_equal_plan_weight():
    model = ScoreModel()
    samples = [
        {'plan_key': 'a', 'config_key': '1', 'latency_ms': 20},
        {'plan_key': 'a', 'config_key': '2', 'latency_ms': 10},
        {'plan_key': 'b', 'config_key': '1', 'latency_ms': 1},
        {'plan_key': 'b', 'config_key': '2', 'latency_ms': 3},
        {'plan_key': 'b', 'config_key': '3', 'latency_ms': 5},
    ]
    # Tied scores resolve by stable config key, never by measured latency.
    graphs = {(s['plan_key'], s['config_key']): torch.tensor(1.) for s in samples}
    assert train_cost.validation_choice_regret(model, graphs, samples) == .5
    assert model.training
    model.eval()
    assert train_cost.validation_choice_regret(model, graphs, samples[::-1]) == .5
    assert not model.training


def test_choice_regret_rejects_nonfinite_and_restores_mode():
    model = ScoreModel()
    samples = [{'plan_key': 'a', 'config_key': '1', 'latency_ms': 1}]
    with pytest.raises(ValueError, match='finite'):
        train_cost.validation_choice_regret(model, {('a', '1'): torch.tensor(float('nan'))}, samples)
    assert model.training
    with pytest.raises(ValueError, match='validation samples'):
        train_cost.validation_choice_regret(model, {}, [])


def test_warm_start_keeps_metadata_and_epoch_zero_if_updates_regress(tmp_path, monkeypatch):
    inputs(tmp_path)
    initial = tmp_path / 'initial.pt'
    common = ['train_cost.py', '--dataset', str(tmp_path), '--epochs', '1',
              '--hidden-dim', '8', '--heads', '2', '--valid-fraction', '.25']
    monkeypatch.setattr(sys, 'argv', common + ['--output', str(initial)])
    train_cost.main()
    before = torch.load(initial, weights_only=False)
    samples = [json.loads(l) for l in (tmp_path / 'samples.jsonl').read_text().splitlines()]
    samples[0]['latency_ms'] += 1
    write_jsonl(tmp_path / 'samples.jsonl', samples)
    calls = []
    def regret(model, graphs, samples):
        calls.append(1)
        return 0. if len(calls) == 1 else 1.
    monkeypatch.setattr(train_cost, 'validation_choice_regret', regret)
    monkeypatch.setattr(train_cost, 'pair_rank_accuracy', lambda *args: .8)
    output = tmp_path / 'adapted.pt'
    monkeypatch.setattr(sys, 'argv', common + ['--output', str(output),
                        '--init-checkpoint', str(initial), '--checkpoint-selection', 'best-valid-regret'])
    train_cost.main()
    after = torch.load(output, weights_only=False)
    assert after['metadata'] == before['metadata']
    assert all(torch.equal(v, after['model_state'][k]) for k,v in before['model_state'].items())
    selection = json.loads(output.with_suffix('.selection.json').read_text())
    assert selection['selected_epoch'] == 0 and selection['epochs_run'] == 1
    assert selection['history'][0]['epoch'] == 0
    assert after['training']['dataset_fingerprint'] != before['training']['dataset_fingerprint']
    assert after['training']['initialization']['source_dataset_fingerprint'] == before['training']['dataset_fingerprint']
    assert after['training']['initialization']['optimizer_state_reused'] is False


def test_initial_cost_rejects_partition_architecture_and_domain_changes(tmp_path, monkeypatch):
    import copy
    inputs(tmp_path)
    path = tmp_path / 'initial.pt'
    monkeypatch.setattr(sys, 'argv', ['train_cost.py', '--dataset', str(tmp_path),
                        '--output', str(path), '--epochs', '1', '--hidden-dim', '8', '--heads', '2'])
    train_cost.main()
    ck = torch.load(path, weights_only=False)
    changed = copy.deepcopy(ck['training']);changed['train_plan_keys'] = []
    with pytest.raises(ValueError, match='frozen plan partition'):
        train_cost.load_initial_cost(path, changed, ck['metadata'], 8, 2)
    with pytest.raises(ValueError, match='architecture'):
        train_cost.load_initial_cost(path, ck['training'], ck['metadata'], 16, 2)
    changed = copy.deepcopy(ck['metadata']);changed['dop_classes'] = [1]
    with pytest.raises(ValueError, match='DOP domain'):
        train_cost.load_initial_cost(path, ck['training'], changed, 8, 2)


def test_plan_weights_equalize_mass_without_discarding_pairs():
    pairs = [{'plan_key':'a'}] * 9 + [{'plan_key':'b'}]
    weights = train_cost.plan_pair_weights(pairs)
    assert 9*weights['a'] == pytest.approx(weights['b'])
    assert sum(weights[p['plan_key']] for p in pairs) == pytest.approx(len(pairs))
    # Replicating observations within one plan leaves the mean weighted loss unchanged.
    def objective(rows):
        w = train_cost.plan_pair_weights(rows)
        return sum(w[p['plan_key']] * {'a':2.,'b':6.}[p['plan_key']] for p in rows)/len(rows)
    assert objective(pairs) == pytest.approx(4.)
    assert objective(pairs + [{'plan_key':'a'}] * 10) == pytest.approx(4.)
    assert train_cost.plan_pair_weights(pairs[::-1]) == weights
    with pytest.raises(ValueError, match='requires ranking pairs'):
        train_cost.plan_pair_weights([])


def test_balanced_training_records_only_training_plan_weights(tmp_path, monkeypatch):
    inputs(tmp_path)
    output = tmp_path / 'balanced.pt'
    monkeypatch.setattr(sys, 'argv', ['train_cost.py','--dataset',str(tmp_path),
                        '--output',str(output),'--epochs','1','--hidden-dim','8','--heads','2',
                        '--valid-fraction','.25','--pair-weighting','plan-balanced'])
    train_cost.main()
    ck = torch.load(output, weights_only=False)
    weighting = ck['training']['pair_weighting']
    assert weighting['mode'] == 'plan-balanced'
    assert set(weighting['weights_by_plan']) <= set(ck['training']['train_plan_keys'])
    assert not set(weighting['weights_by_plan']) & set(ck['training']['valid_plan_keys'])


def test_append_only_initialization_freezes_heldout_and_metadata(tmp_path, monkeypatch):
    import copy
    inputs(tmp_path)
    path = tmp_path / 'initial.pt'
    monkeypatch.setattr(sys, 'argv', ['train_cost.py', '--dataset', str(tmp_path),
                        '--output', str(path), '--epochs', '1', '--hidden-dim', '8', '--heads', '2'])
    train_cost.main()
    ck = torch.load(path, weights_only=False)
    expanded = copy.deepcopy(ck['training'])
    expanded['train_plan_keys'] = [*expanded['train_plan_keys'], 'new-development-plan']
    with pytest.raises(ValueError, match='frozen plan partition'):
        train_cost.load_initial_cost(path, expanded, ck['metadata'], 8, 2)
    model, metadata, provenance = train_cost.load_initial_cost(
        path, expanded, ck['metadata'], 8, 2, allow_appended_training_plans=True)
    original, _ = train_cost.load_cost_model(path)
    assert all(torch.equal(v, original.state_dict()[k]) for k,v in model.state_dict().items())
    assert metadata == ck['metadata']
    assert provenance['appended_train_plan_keys'] == ['new-development-plan']
    for mutation in ('remove_train', 'valid_change', 'test_change', 'heldout_to_train'):
        bad = copy.deepcopy(expanded)
        if mutation == 'remove_train': bad['train_plan_keys'] = ['new-development-plan']
        if mutation == 'valid_change': bad['valid_plan_keys'] = []
        if mutation == 'test_change': bad.setdefault('split_source', {})['test_plan_keys'] = ['new-test']
        if mutation == 'heldout_to_train': bad['train_plan_keys'] += ck['training']['valid_plan_keys']
        with pytest.raises(ValueError):
            train_cost.load_initial_cost(path, bad, ck['metadata'], 8, 2, allow_appended_training_plans=True)


def test_appended_training_cli_keeps_initial_metadata_and_new_lineage(tmp_path, monkeypatch):
    inputs(tmp_path)
    initial = tmp_path / 'initial.pt'
    common = ['train_cost.py', '--dataset', str(tmp_path), '--epochs', '1',
              '--hidden-dim', '8', '--heads', '2']
    monkeypatch.setattr(sys, 'argv', common + ['--output', str(initial)])
    train_cost.main()
    ck = torch.load(initial, weights_only=False)
    samples, pairs = dataset()
    source = ck['training']['train_plan_keys'][0]
    new_samples = [{**s, 'plan_key': 'appended'} for s in samples if s['plan_key'] == source]
    new_pairs = [{**p, 'plan_key': 'appended'} for p in pairs if p['plan_key'] == source]
    expanded_samples = [{**s, 'template_id': s['plan_key']} for s in samples + new_samples]
    write_jsonl(tmp_path / 'samples.jsonl', expanded_samples)
    write_jsonl(tmp_path / 'pairs.jsonl', pairs + new_pairs)
    split = tmp_path / 'split.json'
    split.write_text(json.dumps({'train': ck['training']['train_plan_keys'] + ['appended'],
                                'valid': ck['training']['valid_plan_keys'], 'test': []}))
    output = tmp_path / 'expanded.pt'
    monkeypatch.setattr(sys, 'argv', common + ['--output', str(output), '--split-manifest', str(split),
                        '--init-checkpoint', str(initial), '--allow-appended-training-plans'])
    train_cost.main()
    after = torch.load(output, weights_only=False)
    assert after['metadata'] == ck['metadata']
    assert after['training']['initialization']['appended_train_plan_keys'] == ['appended']
    assert after['training']['dataset_fingerprint'] != ck['training']['dataset_fingerprint']
    assert after['training']['valid_plan_keys'] == ck['training']['valid_plan_keys']
