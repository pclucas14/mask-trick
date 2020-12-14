import math
import logging
import numpy as np

import torch
from torch import nn
import torch.utils.data
from torch.nn import functional as F
from torch.autograd import Variable
#from VAE.layers import GatedDense
from utils import Reshape


def normalize(x):
    x_norm = torch.norm(x, p=2, dim=1).unsqueeze(1).expand_as(x)
    x_normalized = x.div(x_norm + 0.00001)
    return x_normalized

class ContrastiveLoss(torch.nn.Module):
    """
    Contrastive loss function.
    Based on:
    """

    def __init__(self, margin=1.0):
        super(ContrastiveLoss, self).__init__()
        self.margin = margin

    def check_type_forward(self, in_types):
        assert len(in_types) == 3

        x0_type, x1_type, y_type = in_types
        assert x0_type.size() == x1_type.shape
        assert x1_type.size()[0] == y_type.shape[0]
        assert x1_type.size()[0] > 0
        assert x0_type.dim() == 2
        assert x1_type.dim() == 2
        assert y_type.dim() == 1

    def forward(self, x0, x1, y):
        self.check_type_forward((x0, x1, y))

        # euclidian distance
        diff = x0 - x1
        dist_sq = torch.sum(torch.pow(diff, 2), 1)
        dist = torch.sqrt(dist_sq)

        mdist = self.margin - dist
        dist = torch.clamp(mdist, min=0.0)
        loss = y * dist_sq + (1 - y) * torch.pow(dist, 2)
        loss = torch.sum(loss) / 2.0 / x0.size()[0]
        return loss

from torch.nn.utils.weight_norm import WeightNorm
class distLinear(nn.Module):
    def __init__(self, indim, outdim, weight=None):
        super(distLinear, self).__init__()
        self.L = nn.Linear( indim, outdim, bias = False)
        if weight is not None:
            self.L.weight.data = Variable(weight)
        self.class_wise_learnable_norm = False #See the issue#4&8 in the github
        if self.class_wise_learnable_norm:
            WeightNorm.apply(self.L, 'weight', dim=0) #split the weight update component to direction and norm


        self.scale_factor = 10; #in omniglot, a larger scale factor is required to handle >1000 output classes.
       # self.scale_factor = nn.Parameter(torch.tensor(0.5))

    def forward(self, x):
        x_norm = torch.norm(x, p=2, dim =1).unsqueeze(1).expand_as(x)
        x_normalized = x.div(x_norm+ 0.00001)
        if not self.class_wise_learnable_norm:
            L_norm = torch.norm(self.L.weight, p=2, dim =1).unsqueeze(1).expand_as(self.L.weight.data)
            #self.L.weight = self.L.weight.div(L_norm + 0.00001)#.data = self.L.weight.data.div(L_norm + 0.00001)
            cos_dist = torch.mm(x_normalized,self.L.weight.div(L_norm + 0.00001).transpose(0,1))
        else:
            cos_dist = self.L(x_normalized) #matrix product by forward function, but when using WeightNorm, this also multiply the cosine distance by a class-wise learnable norm, see the issue#4&8 in the github
        scores = self.scale_factor * (cos_dist)

        if torch.abs(cos_dist.max())>2.0:
            print('cos dist too big')
            import ipdb; ipdb.set_trace()

        return scores

class TopKReLU(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input):

        assert input.ndim == 4 # B, C, H, W

        B, C, H, W = input.size()

        n_active = C // 3

        top_k_val, top_k_idx = input.topk(n_active, dim=1)
        active = torch.zeros_like(input)
        active.scatter_(dim=1, index=top_k_idx, value=1)

        ctx.save_for_backward(active)

        out = input * active

        return out

    @staticmethod
    def backward(ctx, g):

        active, = ctx.saved_tensors

        return g * active

topkrelu = lambda : TopKReLU.apply


# Classifiers
# -----------------------------------------------------------------------------------

def conv3x3(in_planes, out_planes, stride=1):
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, bias=False)

class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, in_planes, planes, stride=1):
        super(BasicBlock, self).__init__()
        self.conv1 = conv3x3(in_planes, planes, stride)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = conv3x3(planes, planes)
        self.bn2 = nn.BatchNorm2d(planes)

        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != self.expansion * planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, self.expansion * planes, kernel_size=1,
                          stride=stride, bias=False),
                nn.BatchNorm2d(self.expansion * planes)
            )

        # self.nin = nn.Conv2d(planes, planes, 1)
        # self.activation = topkrelu()
        self.activation = nn.ReLU()

    def forward(self, x):
        out = self.activation(self.bn1(self.conv1(x)))
        # out = self.activation(self.nin(self.bn1(self.conv1(x))))
        out = self.bn2(self.conv2(out))
        out += self.shortcut(x)
        out = self.activation(out)
        # out = self.activation(self.nin(out))
        return out

class ResNet(nn.Module):
    def __init__(self, block, num_blocks, num_classes, nf, input_size):
        super(ResNet, self).__init__()
        self.in_planes = nf
        self.input_size = input_size

        self.conv1 = conv3x3(input_size[0], nf * 1)
        self.bn1 = nn.BatchNorm2d(nf * 1)
        #self.bn1  = CategoricalConditionalBatchNorm(nf, 2)
        self.layer1 = self._make_layer(block, nf * 1, num_blocks[0], stride=1)
        self.layer2 = self._make_layer(block, nf * 2, num_blocks[1], stride=2)
        self.layer3 = self._make_layer(block, nf * 4, num_blocks[2], stride=2)
        self.layer4 = self._make_layer(block, nf * 8, num_blocks[3], stride=2)

        # hardcoded for now
        last_hid = nf * 8 * block.expansion if input_size[1] in [8,16,21,32,42] else 640
        self.linear = distLinear(last_hid,num_classes)#nn.Linear(last_hid, num_classes)

        self.activation = nn.ReLU()

    def _make_layer(self, block, planes, num_blocks, stride):
        strides = [stride] + [1] * (num_blocks - 1)
        layers = []
        for stride in strides:
            layers.append(block(self.in_planes, planes, stride))
            self.in_planes = planes * block.expansion
        return nn.Sequential(*layers)

    def return_hidden(self, x):
        bsz = x.size(0)
        #pre_bn = self.conv1(x.view(bsz, 3, 32, 32))
        #post_bn = self.bn1(pre_bn, 1 if is_real else 0)
        #out = F.relu(post_bn)
        out = self.activation(self.bn1(self.conv1(x.view(bsz, *self.input_size))))
        out = self.layer1(out)
        out = self.layer2(out)
        out = self.layer3(out)
        out = self.layer4(out)
        out = F.avg_pool2d(out, 4)
        out = out.view(out.size(0), -1)
        return out

    def forward(self, x):
        out = self.return_hidden(x)
        out = self.linear(out)
        return out

def ResNet18(nclasses, nf=20, input_size=(3, 32, 32)):
    return ResNet(BasicBlock, [2, 2, 2, 2], nclasses, nf, input_size)

