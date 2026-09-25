import copy

import pytest
import torch

from autodop.data import build_metadata, supported_dops
from autodop.graph import build_graph
from autodop.mcts import search
from autodop.model import CostModel, PolicyModel
from autodop.training import label_lineage, validate_labels


def sample():
    return {'plan_key': 'p', 'dop_config': [1, 4], 'edges': [],
            'fragments': [{'fragment_index': i, 'candidate_dops': [1, 2, 4, 8],
                           'is_gather_root': i == 0, 'worker_ids': [1],
                           'root_node': 'EXCHANGE' if i == 0 else 'AGGREGATE'}
                          for i in range(2)]}


def test_policy_mask_preserves_raw_features_and_gather_contract():
    torch.set_num_threads(1)
    row = sample()
    original = copy.deepcopy(row)
    metadata = build_metadata([row], [1, 2, 4, 8])
    graph = build_graph(row, metadata, unknown_dop=True)
    assert graph['candidate_mask'].tolist() == [[True, False, False, False], [True] * 4]
    model = PolicyModel(metadata, {'hidden_dim': 8, 'heads': 2})
    probabilities = model(graph).softmax(-1)
    assert probabilities[0].tolist() == [1, 0, 0, 0]
    assert row == original
    cost = CostModel(metadata, {'hidden_dim': 8, 'heads': 2}).eval()
    original_mask = {**graph, 'candidate_mask': torch.ones_like(graph['candidate_mask'])}
    # The repaired classification mask does not alter CostModel scores/features.
    assert torch.equal(cost(graph), cost(original_mask))
    row['fragments'][0]['candidate_dops'] = [2, 4]
    assert supported_dops(row['fragments'][0], metadata['dop_classes']) == []
    with pytest.raises(ValueError, match='no supported'):
        build_graph(row, metadata)


class PreferLargerDop:
    def eval(self):
        return self

    def fragment_importance(self, graph):
        return torch.ones(len(graph['dop_ids']))

    def __call__(self, graph):
        return graph['dop_ids'].float().sum()


def test_mcts_does_not_extrapolate_gather_dop():
    row = sample()
    metadata = build_metadata([row])
    config, _ = search(row, PreferLargerDop(), metadata, iterations=32)
    assert config == [1, 8]
    with pytest.raises(ValueError, match='unsupported DOP'):
        search(row, PreferLargerDop(), metadata, allowed_configs=[[8, 8]])


def test_distillation_rejects_old_gather_teacher_labels(tmp_path):
    teacher = tmp_path / 'teacher.pt'
    teacher.write_bytes(b'teacher')
    training = {'train_plan_keys': ['p'], 'dataset_fingerprint': 'data', 'split_fingerprint': 'split'}
    labels = [{'plan_key': 'p', 'best_dop_config': [8, 8],
               'lineage': label_lineage(training, teacher)}]
    with pytest.raises(ValueError, match='invalid DOP'):
        validate_labels(labels, [sample()], training, teacher, [1, 2, 4, 8])
    labels[0]['best_dop_config'] = [1, 8]
    assert validate_labels(labels, [sample()], training, teacher, [1, 2, 4, 8]) == {'p': [1, 8]}
