import math

import torch
from torch import nn


DEFAULT_MODEL_CONFIG = {
    "hidden_dim": 64,
    "heads": 4,
    "layers": 2,
    "category_embedding_dim": 8,
    "dop_embedding_dim": 8,
}


class DenseAttentionBlock(nn.Module):
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
        self.dependency_scale = nn.Parameter(torch.tensor(1.0))
        self.competition_scale = nn.Parameter(torch.tensor(1.0))
        self.attention_norm = nn.LayerNorm(hidden_dim)
        self.feed_forward = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim * 2),
            nn.GELU(),
            nn.Linear(hidden_dim * 2, hidden_dim),
        )
        self.output_norm = nn.LayerNorm(hidden_dim)

    def _heads(self, values):
        return values.view(values.shape[0], self.heads, self.head_dim).transpose(0, 1)

    def forward(self, nodes, dependency, competition):
        query = self._heads(self.query(nodes))
        key = self._heads(self.key(nodes))
        value = self._heads(self.value(nodes))
        logits = torch.matmul(query, key.transpose(-1, -2)) / math.sqrt(self.head_dim)
        logits = logits + dependency.unsqueeze(0) * self.dependency_scale
        logits = logits + competition.unsqueeze(0) * self.competition_scale
        attended = torch.matmul(torch.softmax(logits, dim=-1), value)
        attended = attended.transpose(0, 1).contiguous().view(nodes.shape)
        nodes = self.attention_norm(nodes + self.output(attended))
        return self.output_norm(nodes + self.feed_forward(nodes))


class GraphEncoder(nn.Module):
    def __init__(self, metadata, model_config=None):
        super().__init__()
        self.config = {**DEFAULT_MODEL_CONFIG, **(model_config or {})}
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
            DenseAttentionBlock(hidden_dim, self.config["heads"])
            for _ in range(self.config["layers"])
        ])

    def _position_encoding(self, nodes):
        positions = torch.arange(nodes.shape[0], device=nodes.device, dtype=nodes.dtype).unsqueeze(1)
        dimensions = torch.arange(0, nodes.shape[1], 2, device=nodes.device, dtype=nodes.dtype)
        angles = positions * torch.exp(-math.log(10000.0) * dimensions / nodes.shape[1])
        encoding = torch.zeros_like(nodes)
        encoding[:, 0::2] = torch.sin(angles)
        encoding[:, 1::2] = torch.cos(angles[:, :encoding[:, 1::2].shape[1]])
        return encoding

    def forward(self, graph):
        categories = [
            embedding(graph["categorical"][:, index])
            for index, embedding in enumerate(self.category_embeddings)
        ]
        features = torch.cat(categories + [graph["numerical"], self.dop_embedding(graph["dop_ids"])], dim=-1)
        nodes = self.input(features)
        if self.config.get("fragment_position_encoding", False):
            nodes = nodes + self._position_encoding(nodes)
        for layer in self.layers:
            nodes = layer(nodes, graph["dependency"], graph["competition"])
        return nodes


class CostModel(nn.Module):
    def __init__(self, metadata, model_config=None):
        super().__init__()
        self.encoder = GraphEncoder(metadata, model_config)
        hidden_dim = self.encoder.config["hidden_dim"]
        self.readout = nn.Sequential(
            nn.Linear(hidden_dim * 3, hidden_dim),
            nn.GELU(),
            nn.Linear(hidden_dim, 1),
        )

    def forward(self, graph):
        nodes = self.encoder(graph)
        pooled = torch.cat([
            nodes.mean(dim=0),
            nodes.max(dim=0).values,
            nodes[graph["root_index"]],
        ])
        return self.readout(pooled).squeeze(-1)


class PolicyModel(nn.Module):
    def __init__(self, metadata, model_config=None):
        super().__init__()
        self.encoder = GraphEncoder(metadata, model_config)
        hidden_dim = self.encoder.config["hidden_dim"]
        self.global_context = bool(self.encoder.config.get("policy_global_context", False))
        if self.global_context:
            self.classifier = nn.Sequential(
                nn.Linear(hidden_dim * 4, hidden_dim),
                nn.GELU(),
                nn.Linear(hidden_dim, len(metadata["dop_classes"])),
            )
        else:
            self.classifier = nn.Linear(hidden_dim, len(metadata["dop_classes"]))

    def forward(self, graph):
        nodes = self.encoder(graph)
        if self.global_context:
            context = torch.cat([
                nodes.mean(dim=0),
                nodes.max(dim=0).values,
                nodes[graph["root_index"]],
            ])
            context = context.unsqueeze(0).expand(nodes.shape[0], -1)
            nodes = torch.cat([nodes, context], dim=-1)
        logits = self.classifier(nodes)
        return logits.masked_fill(~graph["candidate_mask"], torch.finfo(logits.dtype).min)


def model_config(model):
    return dict(model.encoder.config)


def save_checkpoint(path, kind, model, metadata):
    torch.save({
        "kind": kind,
        "model_state": model.state_dict(),
        "model_config": model_config(model),
        "metadata": metadata,
    }, path)


def load_cost_model(path, device="cpu"):
    checkpoint = torch.load(path, map_location=device, weights_only=False)
    if checkpoint.get("kind") != "cost":
        raise ValueError("checkpoint is not a CostModel")
    model = CostModel(checkpoint["metadata"], checkpoint["model_config"])
    model.load_state_dict(checkpoint["model_state"])
    return model.to(device), checkpoint


def load_policy_model(path, device="cpu"):
    checkpoint = torch.load(path, map_location=device, weights_only=False)
    if checkpoint.get("kind") != "policy":
        raise ValueError("checkpoint is not a PolicyModel")
    model = PolicyModel(checkpoint["metadata"], checkpoint["model_config"])
    model.load_state_dict(checkpoint["model_state"])
    return model.to(device), checkpoint
