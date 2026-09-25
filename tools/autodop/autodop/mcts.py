import math
import random

import torch

from .graph import build_graph
from .data import supported_dops


DEFAULT_EXPLORATION = math.sqrt(2.0)


def _cardinality_bucket(value):
    return int(math.log2(max(1.0, float(value))))


def equivalence_groups(sample, candidates):
    """Paper equivalence classes for structurally symmetric fragments.

    The paper groups by operator type, exchange type, join type, and input
    cardinality range.  Candidate domains are also part of the key because a
    shared decision variable must be legal for every member of the class.
    """
    fragments = sample["fragments"]
    grouped = {}
    for index, fragment in enumerate(fragments):
        key = (
            str(fragment.get("operator_signature", "")),
            str(fragment.get("data_partition", "")),
            str(fragment.get("output_partition", "")),
            str(fragment.get("sink_type", "")),
            str(fragment.get("join_type", "")),
            _cardinality_bucket(fragment.get("estimated_cardinality", 0.0)),
            tuple(candidates[index]),
        )
        grouped.setdefault(key, []).append(index)
    return [tuple(indices) for indices in grouped.values()]


def attention_guided_groups(sample, model, metadata, candidates, device="cpu"):
    graph = build_graph(sample, metadata, device=device)
    with torch.no_grad():
        importance = model.fragment_importance(graph).detach().cpu().tolist()
    groups = equivalence_groups(sample, candidates)
    return sorted(
        groups,
        # The paper sorts fragments by descending I_i, then collapses equivalent
        # variables. Preserving the first fragment in that order is equivalent
        # to using the maximum member importance for each class.
        key=lambda group: (-max(importance[index] for index in group), group[0]),
    )


class Node:
    def __init__(self, config, depth, groups, actions, parent=None, allowed_configs=None):
        self.config = config
        self.depth = depth
        self.parent = parent
        self.children = []
        self.allowed_configs = allowed_configs
        if depth == len(groups):
            self.untried = []
        elif allowed_configs is None:
            self.untried = list(actions[depth])
        else:
            representative = groups[depth][0]
            self.untried = sorted({config[representative] for config in allowed_configs})
        self.visits = 0
        self.value = 0.0

    def uct(self, parent_visits, exploration):
        return self.value / self.visits + exploration * math.sqrt(math.log(parent_visits) / self.visits)


def search(sample, model, metadata, iterations=500, exploration=DEFAULT_EXPLORATION, seed=0, device="cpu",
           allowed_configs=None):
    if iterations < 1:
        raise ValueError("MCTS iterations must be positive")
    model.eval()
    random_generator = random.Random(seed)
    dop_classes = set(metadata["dop_classes"])
    candidates = [
        supported_dops(fragment, dop_classes)
        for fragment in sample["fragments"]
    ]
    if any(not values for values in candidates):
        raise ValueError("MCTS fragment has no supported candidate")
    if allowed_configs is not None:
        allowed_configs = sorted({tuple(int(dop) for dop in config) for config in allowed_configs})
        if not allowed_configs:
            raise ValueError("MCTS allowed config list is empty")
        for config in allowed_configs:
            if len(config) != len(candidates):
                raise ValueError("MCTS allowed config has the wrong fragment count")
            if any(dop not in candidates[index] for index, dop in enumerate(config)):
                raise ValueError("MCTS allowed config contains an unsupported DOP")
    groups = attention_guided_groups(sample, model, metadata, candidates, device)
    if allowed_configs is not None:
        compatible = [
            config for config in allowed_configs
            if all(len({config[index] for index in group}) == 1 for group in groups)
        ]
        if compatible:
            allowed_configs = compatible
        else:
            # Measured-only evaluation must not discard every observed configuration.
            groups = [(index,) for index in range(len(candidates))]
    actions = [
        sorted(set.intersection(*(set(candidates[index]) for index in group)))
        for group in groups
    ]
    if any(not values for values in actions):
        raise ValueError("MCTS equivalence class has no shared DOP candidate")
    root = Node(tuple(0 for _ in candidates), 0, groups, actions,
                allowed_configs=allowed_configs)
    best_config = None
    best_score = float("-inf")

    for _ in range(iterations):
        node = root
        while node.depth < len(groups) and not node.untried:
            node = max(node.children, key=lambda child: child.uct(node.visits, exploration))

        if node.depth < len(groups):
            action = node.untried.pop(random_generator.randrange(len(node.untried)))
            config = list(node.config)
            group = groups[node.depth]
            for index in group:
                config[index] = action
            child_configs = None
            if node.allowed_configs is not None:
                child_configs = [
                    candidate for candidate in node.allowed_configs
                    if candidate[group[0]] == action
                ]
            child = Node(tuple(config), node.depth + 1, groups, actions, node, child_configs)
            node.children.append(child)
            node = child

        if node.allowed_configs is None:
            config = list(node.config)
            for group, choices in zip(groups[node.depth:], actions[node.depth:]):
                action = random_generator.choice(choices)
                for index in group:
                    config[index] = action
        else:
            config = list(random_generator.choice(node.allowed_configs))
        evaluated = {**sample, "dop_config": config}
        with torch.no_grad():
            score = float(model(build_graph(evaluated, metadata, device=device)).cpu())
        if score > best_score:
            best_score = score
            best_config = list(config)

        while node is not None:
            node.visits += 1
            node.value += score
            node = node.parent

    return best_config, best_score
