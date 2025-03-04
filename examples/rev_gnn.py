# Model Paramters: 206,607
# Peak GPU memory usage: 1.57 G
# RevGNN with 7 layers and 160 channels reaches around 0.8200 test accuracy.
# Final Train: 0.9373, Highest Val: 0.9230, Final Test: 0.8200.
# Training longer should produces better results.

import os.path as osp

import torch
import torch.nn.functional as F
from torch.nn import LayerNorm, Linear
from torch_sparse import SparseTensor
from tqdm import tqdm

import torch_geometric.transforms as T
from torch_geometric.loader import RandomNodeSampler
from torch_geometric.nn import GroupAddRev, SAGEConv
from torch_geometric.utils import index_to_mask


class GNNBlock(torch.nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__(in_channels)
        self.norm = LayerNorm(in_channels, elementwise_affine=True)
        self.conv = SAGEConv(in_channels, out_channels)

    def reset_parameters(self):
        self.norm.reset_parameters()
        self.conv.reset_parameters()

    def forward(self, x, edge_index, dropout_mask=None):
        x = self.norm(x).relu()
        if self.training and dropout_mask is not None:
            x = x * dropout_mask
        return self.conv(x, edge_index)


class RevGNN(torch.nn.Module):
    def __init__(self, in_channels, hidden_channels, out_channels, num_layers,
                 dropout, num_groups=2):
        super().__init__()

        self.dropout = dropout

        self.lin1 = Linear(in_channels, hidden_channels)
        self.lin2 = Linear(hidden_channels, out_channels)
        self.norm = LayerNorm(hidden_channels, elementwise_affine=True)

        assert hidden_channels % num_groups == 0
        self.convs = torch.nn.ModuleList()
        for _ in range(self.num_layers):
            conv = GNNBlock(
                hidden_channels // num_groups,
                hidden_channels // num_groups,
            )
            self.convs.append(GroupAddRev(conv, num_groups=num_groups))

    def reset_parameters(self):
        self.lin1.reset_parameters()
        self.lin2.reset_parameters()
        self.norm.reset_parameters()
        for conv in self.convs:
            conv.reset_parameters()

    def forward(self, x, edge_index):
        # Generate a dropout mask which will be shared across GNN blocks:
        mask = None
        if self.training and self.dropout > 0:
            mask = torch.zeros_like(x).bernoulli_(1 - self.dropout)
            mask = mask.requires_grad_(False)
            mask = mask / (1 - self.dropout)

        x = self.lin1(x)
        for conv in self.convs:
            x = conv(x, edge_index, mask)
        x = self.norm(x).relu()
        x = F.dropout(x, p=self.dropout, training=self.training)
        return self.lin2(x)


from ogb.nodeproppred import Evaluator, PygNodePropPredDataset  # noqa

transform = T.AddSelfLoops()
root = osp.join(osp.dirname(osp.realpath(__file__)), '..', 'data', 'products')
dataset = PygNodePropPredDataset('ogbn-products', root, transform=transform)
evaluator = Evaluator(name='ogbn-products')

data = dataset[0]
split_idx = dataset.get_idx_split()
for split in ['train', 'valid', 'test']:
    data[f'{split}_mask'] = index_to_mask(split_idx[split], data.y.shape[0])

train_loader = RandomNodeSampler(data, num_parts=10, shuffle=True,
                                 num_workers=5)
# Increase the num_parts of the test loader if you cannot have fix
# the full batch graph into your GPU:
test_loader = RandomNodeSampler(data, num_parts=1, num_workers=5)

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
model = RevGNN(
    in_channels=dataset.num_features,
    hidden_channels=160,
    out_channels=dataset.num_classes,
    num_layers=7,  # You can try 1000 layers for fun
    dropout=0.5,
    num_groups=2,
).to(device)
optimizer = torch.optim.Adam(model.parameters(), lr=0.003)


def train(epoch):
    model.train()

    pbar = tqdm(total=len(train_loader))
    pbar.set_description(f'Training epoch: {epoch:03d}')

    total_loss = total_examples = 0
    for data in train_loader:
        data = data.to(device)
        optimizer.zero_grad()

        # Memory-efficient aggregations:
        adj_t = SparseTensor.from_edge_index(data.edge_index).t()
        out = model(data.x, adj_t)[data.train_mask]
        loss = F.cross_entropy(out, data.y[data.train_mask].view(-1))
        loss.backward()
        optimizer.step()

        total_loss += float(loss) * int(data.train_mask.sum())
        total_examples += int(data.train_mask.sum())
        pbar.update(1)

    pbar.close()

    return total_loss / total_examples


@torch.no_grad()
def test(epoch):
    model.eval()

    y_true = {"train": [], "valid": [], "test": []}
    y_pred = {"train": [], "valid": [], "test": []}

    pbar = tqdm(total=len(test_loader))
    pbar.set_description(f'Evaluating epoch: {epoch:03d}')

    for data in test_loader:
        data = data.to(device)

        # Memory-efficient aggregations
        adj_t = SparseTensor.from_edge_index(data.edge_index).t()
        out = model(data.x, adj_t).argmax(dim=-1, keepdim=True)

        for split in ['train', 'valid', 'test']:
            mask = data[f'{split}_mask']
            y_true[split].append(data.y[mask].cpu())
            y_pred[split].append(out[mask].cpu())

        pbar.update(1)

    pbar.close()

    train_acc = evaluator.eval({
        'y_true': torch.cat(y_true['train'], dim=0),
        'y_pred': torch.cat(y_pred['train'], dim=0),
    })['acc']

    valid_acc = evaluator.eval({
        'y_true': torch.cat(y_true['valid'], dim=0),
        'y_pred': torch.cat(y_pred['valid'], dim=0),
    })['acc']

    test_acc = evaluator.eval({
        'y_true': torch.cat(y_true['test'], dim=0),
        'y_pred': torch.cat(y_pred['test'], dim=0),
    })['acc']

    return train_acc, valid_acc, test_acc


for epoch in range(1, 501):
    loss = train(epoch)
    train_acc, val_acc, test_acc = test(epoch)
    print(f'Loss: {loss:.4f}, Train: {train_acc:.4f}, Val: {val_acc:.4f}, '
          f'Test: {test_acc:.4f}')
