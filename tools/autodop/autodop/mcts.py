import math
import random

import torch

from .graph import build_graph, fragment_order


class Node:
    def __init__(self, config, depth, candidates, order, parent=None, allowed_configs=None):
        self.config = config
        self.depth = depth
        self.parent = parent
        self.children = []
        self.allowed_configs = allowed_configs
        if depth == len(order):
            self.untried = []
        elif allowed_configs is None:
            self.untried = list(candidates[order[depth]])
        else:
            self.untried = sorted({config[order[depth]] for config in allowed_configs})
        self.visits = 0
        self.value = 0.0

    def uct(self, parent_visits, exploration):
        return self.value / self.visits + exploration * math.sqrt(math.log(parent_visits) / self.visits)


def search(sample, model, metadata, iterations=500, exploration=1.414, seed=0, device="cpu",
           allowed_configs=None):
    random_generator = random.Random(seed)
    dop_classes = set(metadata["dop_classes"])
    candidates = [
        [int(dop) for dop in fragment.get("candidate_dops", metadata["dop_classes"]) if int(dop) in dop_classes]
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
    order = fragment_order(sample)
    root = Node(tuple(0 for _ in candidates), 0, candidates, order,
                allowed_configs=allowed_configs)
    best_config = None
    best_score = float("-inf")

    model.eval()
    for _ in range(iterations):
        node = root
        while node.depth < len(order) and not node.untried:
            node = max(node.children, key=lambda child: child.uct(node.visits, exploration))

        if node.depth < len(order):
            action = node.untried.pop(random_generator.randrange(len(node.untried)))
            config = list(node.config)
            config[order[node.depth]] = action
            child_configs = None
            if node.allowed_configs is not None:
                child_configs = [
                    candidate for candidate in node.allowed_configs
                    if candidate[order[node.depth]] == action
                ]
            child = Node(tuple(config), node.depth + 1, candidates, order, node, child_configs)
            node.children.append(child)
            node = child

        if node.allowed_configs is None:
            config = list(node.config)
            for index in order[node.depth:]:
                config[index] = random_generator.choice(candidates[index])
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
