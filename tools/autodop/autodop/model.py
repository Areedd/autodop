import math

import torch
from torch import nn


DEFAULT_MODEL_CONFIG = {
    "hidden_dim": 64,
    "heads": 4,
    "layers": 2,
    "category_embedding_dim": 8,
    "dop_embedding_dim": 8,
    "competition_edges": True,
}
MODEL_FORMAT_VERSION = 2


class AttentionChannel(nn.Module):
    def __init__(self, hidden_dim, heads):
        super().__init__()
        if hidden_dim % heads != 0:
            raise ValueError("hidden_dim must be divisible by heads")
        self.heads = heads
        self.head_dim = hidden_dim // heads
        self.query = nn.Linear(hidden_dim, hidden_dim)
        self.key = nn.Linear(hidden_dim, hidden_dim)
        self.value = nn.Linear(hidden_dim, hidden_dim)
        self.output = nn.Linear(hidden_dim, hidden_dim)
        self.edge_scale = nn.Parameter(torch.tensor(1.0))

    def _heads(self, values):
        return values.view(values.shape[0], self.heads, self.head_dim).transpose(0, 1)

    def forward(self, nodes, edges):
        query = self._heads(self.query(nodes))
        key = self._heads(self.key(nodes))
        value = self._heads(self.value(nodes))
        logits = torch.matmul(query, key.transpose(-1, -2)) / math.sqrt(self.head_dim)
        logits = logits + edges.unsqueeze(0) * self.edge_scale
        attended = torch.matmul(torch.softmax(logits, dim=-1), value)
        attended = attended.transpose(0, 1).contiguous().view(nodes.shape)
        return self.output(attended)


class DualEdgeAttentionBlock(nn.Module):
    """Graph Transformer block with separate dependency and competition channels."""

    def __init__(self, hidden_dim, heads):
        super().__init__()
        self.dependency_attention = AttentionChannel(hidden_dim, heads)
        self.competition_attention = AttentionChannel(hidden_dim, heads)
        self.channel_mix = nn.Linear(hidden_dim * 2, hidden_dim)
        self.attention_norm = nn.LayerNorm(hidden_dim)
        self.feed_forward = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim * 2),
            nn.GELU(),
            nn.Linear(hidden_dim * 2, hidden_dim),
        )
        self.output_norm = nn.LayerNorm(hidden_dim)

    def forward(self, nodes, dependency, competition):
        dependency_nodes = self.dependency_attention(nodes, dependency)
        competition_nodes = self.competition_attention(nodes, competition)
        attended = self.channel_mix(torch.cat([dependency_nodes, competition_nodes], dim=-1))
        nodes = self.attention_norm(nodes + attended)
        return self.output_norm(nodes + self.feed_forward(nodes))


class GlobalAttentionReadout(nn.Module):
    """Learned global query that returns both a plan embedding and fragment importance."""

    def __init__(self, hidden_dim, heads):
        super().__init__()
        if hidden_dim % heads != 0:
            raise ValueError("hidden_dim must be divisible by heads")
        self.heads = heads
        self.head_dim = hidden_dim // heads
        self.query = nn.Parameter(torch.empty(heads, self.head_dim))
        self.key = nn.Linear(hidden_dim, hidden_dim)
        self.value = nn.Linear(hidden_dim, hidden_dim)
        self.output = nn.Linear(hidden_dim, hidden_dim)
        self.norm = nn.LayerNorm(hidden_dim)
        nn.init.normal_(self.query, std=0.02)

    def _heads(self, values):
        return values.view(values.shape[0], self.heads, self.head_dim).transpose(0, 1)

    def forward(self, nodes):
        keys = self._heads(self.key(nodes))
        values = self._heads(self.value(nodes))
        logits = torch.einsum("hd,hnd->hn", self.query, keys) / math.sqrt(self.head_dim)
        attention = torch.softmax(logits, dim=-1)
        pooled = torch.einsum("hn,hnd->hd", attention, values).reshape(-1)
        return self.norm(self.output(pooled)), attention


class GraphEncoder(nn.Module):
    def __init__(self, metadata, model_config=None):
        super().__init__()
        model_config = model_config or {}
        unknown = set(model_config) - set(DEFAULT_MODEL_CONFIG)
        if unknown:
            raise ValueError(f"unsupported model configuration fields: {sorted(unknown)}")
        self.config = {**DEFAULT_MODEL_CONFIG, **model_config}
        category_dim = self.config["category_embedding_dim"]
        dop_dim = self.config["dop_embedding_dim"]
        hidden_dim = self.config["hidden_dim"]
        self.category_embeddings = nn.ModuleList([
            nn.Embedding(len(metadata["category_vocab"][feature]), category_dim)
            for feature in metadata["category_features"]
        ])
        self.dop_embedding = nn.Embedding(len(metadata["dop_classes"]) + 1, dop_dim)
        input_dim = len(self.category_embeddings) * category_dim + len(metadata["numeric_features"]) + dop_dim
        self.input = nn.Linear(input_dim, hidden_dim)
        self.layers = nn.ModuleList([
            DualEdgeAttentionBlock(hidden_dim, self.config["heads"])
            for _ in range(self.config["layers"])
        ])

    def forward(self, graph):
        categories = [
            embedding(graph["categorical"][:, index])
            for index, embedding in enumerate(self.category_embeddings)
        ]
        features = torch.cat(categories + [graph["numerical"], self.dop_embedding(graph["dop_ids"])], dim=-1)
        nodes = self.input(features)
        for layer in self.layers:
            competition = (graph["competition"] if self.config["competition_edges"]
                           else torch.zeros_like(graph["competition"]))
            nodes = layer(nodes, graph["dependency"], competition)
        return nodes


class _GraphScoreModel(nn.Module):
    """Shared graph encoder and scalar readout; checkpoint parameter names stay stable."""

    def __init__(self, metadata, model_config=None):
        super().__init__()
        self.encoder = GraphEncoder(metadata, model_config)
        hidden_dim = self.encoder.config["hidden_dim"]
        self.global_readout = GlobalAttentionReadout(hidden_dim, self.encoder.config["heads"])
        self.scorer = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.GELU(),
            nn.Linear(hidden_dim, 1),
        )

    def forward(self, graph):
        nodes = self.encoder(graph)
        pooled, _ = self.global_readout(nodes)
        return self.scorer(pooled).squeeze(-1)


class CostModel(_GraphScoreModel):
    def fragment_importance(self, graph):
        """Paper variable importance: sum global-readout attention across heads."""
        nodes = self.encoder(graph)
        _, attention = self.global_readout(nodes)
        return attention.sum(dim=0)


def dynamic_rank_margin(fast_latency_ms, slow_latency_ms):
    fast_latency_ms = float(fast_latency_ms)
    slow_latency_ms = float(slow_latency_ms)
    if fast_latency_ms <= 0 or slow_latency_ms <= fast_latency_ms:
        raise ValueError("ranking pair requires 0 < fast latency < slow latency")
    return math.log1p((slow_latency_ms - fast_latency_ms) / fast_latency_ms)


def dynamic_margin_ranking_loss(fast_score, slow_score, fast_latency_ms, slow_latency_ms):
    """Paper Eq. (5)-(6): hinge loss with a latency-gap-dependent margin."""
    margin = fast_score.new_tensor(dynamic_rank_margin(fast_latency_ms, slow_latency_ms))
    return torch.relu(margin - fast_score + slow_score)


def latency_target(sample):
    """Training target of the regression baseline: log1p of the absolute latency in ms."""
    return math.log1p(sample["latency_ms"])


class LatencyRegressionModel(_GraphScoreModel):
    """Absolute-latency regression baseline (G3, DD02 §6.3).

    Shares the same graph encoder and readout shape as :class:`CostModel`, but is trained with
    an MSE loss on ``log1p(latency_ms)`` instead of the pairwise ranking loss. It is used only
    as an evaluation baseline (train_cost.py --with-baseline); it never replaces the CostModel
    artifact.
    """


def pair_rank_accuracy(model, graphs, pairs, baseline=False):
    """Fraction of pairs where the fast configuration wins.

    For a cost model (higher score = faster) a pair is correct when the fast config scores
    higher; for a latency regression baseline (lower prediction = faster) a pair is correct
    when the fast config predicts lower latency. Pure forward pass: deterministic and
    reproducible for the same checkpoint + dataset.
    """
    if not pairs:
        return 0.0
    correct = 0
    with torch.no_grad():
        for pair in pairs:
            plan_key = pair["plan_key"]
            fast = float(model(graphs[(plan_key, pair["fast_config_key"])]))
            slow = float(model(graphs[(plan_key, pair["slow_config_key"])]))
            if baseline:
                if fast < slow:
                    correct += 1
            else:
                if fast > slow:
                    correct += 1
    return correct / len(pairs)


class PolicyModel(nn.Module):
    def __init__(self, metadata, model_config=None):
        super().__init__()
        self.encoder = GraphEncoder(metadata, model_config)
        hidden_dim = self.encoder.config["hidden_dim"]
        self.classifier = nn.Linear(hidden_dim, len(metadata["dop_classes"]))

    def forward(self, graph):
        nodes = self.encoder(graph)
        logits = self.classifier(nodes)
        return logits.masked_fill(~graph["candidate_mask"], torch.finfo(logits.dtype).min)


def model_config(model):
    return dict(model.encoder.config)


def save_checkpoint(path, kind, model, metadata, training=None):
    torch.save({
        "format_version": MODEL_FORMAT_VERSION,
        "kind": kind,
        "model_state": model.state_dict(),
        "model_config": model_config(model),
        "metadata": metadata,
        **({"training": training} if training is not None else {}),
    }, path)


def _load_model(path, kind, model_class, device):
    checkpoint = torch.load(path, map_location=device, weights_only=False)
    if checkpoint.get("format_version") != MODEL_FORMAT_VERSION:
        raise ValueError("checkpoint uses an obsolete AutoDOP model format; retraining is required")
    if checkpoint.get("kind") != kind:
        raise ValueError(f"checkpoint is not a {model_class.__name__}")
    model = model_class(checkpoint["metadata"], checkpoint["model_config"])
    model.load_state_dict(checkpoint["model_state"])
    return model.to(device), checkpoint


def load_cost_model(path, device="cpu"):
    return _load_model(path, "cost", CostModel, device)


def load_policy_model(path, device="cpu"):
    return _load_model(path, "policy", PolicyModel, device)
