# -*- coding: utf-8 -*-
"""Neural Turing Machine

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1onkl-srlRWHik2licCdzls5GezXBlr7R

# ***Library import***
"""

!pip install argcomplete
!pip install attrs
!pip install numpy
!pip install pytest

from google.colab import files
files.upload()

import yaml
import json
import logging
from matplotlib.lines import Line2D      
import time
import random
import matplotlib.pyplot as plt
import re
import sys
from attr import attrs, attrib, Factory
from torch import optim
import attr 
import torch 
import numpy as np 
from torch.nn import Parameter 
from torch import nn 
import torch.nn.functional as F

# Default values for program arguments
RANDOM_SEED = 1000
REPORT_INTERVAL = 50
CHECKPOINT_INTERVAL = 1000

"""# ***Neural Turing Machine***

Model
"""

class EncapsulatedNTM(nn.Module):

    def __init__(self, num_inputs, num_outputs,
                 controller_size, controller_layers, num_heads, N, M):
        """Initialize an EncapsulatedNTM.
        :param num_inputs: External number of inputs. \8 + 1
        :param num_outputs: External number of outputs. \8
        :param controller_size: The size of the internal representation. \100
        :param controller_layers: Controller number of layers. \1
        :param num_heads: Number of heads. \1
        :param N: Number of rows in the memory bank. \128
        :param M: Number of cols/features in the memory bank. \20
        """
        super(EncapsulatedNTM, self).__init__()

        # Save args
        self.num_inputs = num_inputs # dimension of xt + 1, one for delimiter
        self.num_outputs = num_outputs # dimension of xt
        self.controller_size = controller_size
        self.controller_layers = controller_layers
        self.num_heads = num_heads
        self.N = N
        self.M = M

        # Create the NTM components
        memory = NTMMemory(N, M)
        # each batch has it own memory
        # we learn paramters of read, write heads and controller 
        # Controller takes in the current input xt and prev read
        controller = LSTMController(num_inputs + M*num_heads, controller_size, controller_layers)
        heads = nn.ModuleList([])
        for i in range(num_heads):
            heads += [
                NTMReadHead(memory, controller_size),
                NTMWriteHead(memory, controller_size)
            ]

        self.ntm = NTM(num_inputs, num_outputs, controller, memory, heads)
        self.memory = memory

    def init_sequence(self, batch_size):
        """Initializing the state."""
        self.batch_size = batch_size
        self.memory.reset(batch_size) 
        self.previous_state = self.ntm.create_new_state(batch_size)
        # output : init_r, controller_state, heads_state

    def forward(self, x=None):
        if x is None:
            x = torch.zeros(self.batch_size, self.num_inputs)

        o, self.previous_state = self.ntm(x, self.previous_state)
        return o, self.previous_state

    def calculate_num_params(self):
        """Returns the total number of parameters."""
        num_params = 0
        for p in self.parameters():
            num_params += p.data.view(-1).size(0)
        return num_params

"""controller"""

class LSTMController(nn.Module):
    """An NTM controller based on LSTM."""
    def __init__(self, num_inputs, num_outputs, num_layers):
        super(LSTMController, self).__init__()

        self.num_inputs = num_inputs
        self.num_outputs = num_outputs
        self.num_layers = num_layers

        self.lstm = nn.LSTM(input_size=num_inputs,
                            hidden_size=num_outputs,
                            num_layers=num_layers)

        # The hidden state is a learned parameter
        self.lstm_h_bias = Parameter(torch.zeros(self.num_layers, 1, self.num_outputs) * 0.05)
        self.lstm_c_bias = Parameter(torch.zeros(self.num_layers, 1, self.num_outputs) * 0.05)

        self.reset_parameters()

    def create_new_state(self, batch_size):
        # Dimension: (num_layers * num_directions, batch, hidden_size)
        lstm_h = self.lstm_h_bias.clone().repeat(1, batch_size, 1)
        lstm_c = self.lstm_c_bias.clone().repeat(1, batch_size, 1)
        return lstm_h, lstm_c

    def reset_parameters(self):
        for p in self.lstm.parameters():
            if p.dim() == 1:
                nn.init.constant_(p, 0)
            else:
                stdev = 5 / (np.sqrt(self.num_inputs +  self.num_outputs))
                nn.init.uniform_(p, -stdev, stdev)

    def size(self):
        return self.num_inputs, self.num_outputs

    def forward(self, x, prev_state):
        x = x.unsqueeze(0) # x = [1, bs, dim + M*num_heads]
        outp, state = self.lstm(x, prev_state)
        return outp.squeeze(0), state

"""head"""

def _split_cols(mat, lengths):
    """Split a 2D matrix to variable length columns."""
    assert mat.size()[1] == sum(lengths), "Lengths must be summed to num columns"
    l = np.cumsum([0] + lengths)
    results = []
    for s, e in zip(l[:-1], l[1:]):
        results += [mat[:, s:e]]
    return results


class NTMHeadBase(nn.Module):
    """An NTM Read/Write Head."""

    def __init__(self, memory, controller_size):
        """Initilize the read/write head.
        :param memory: The :class:`NTMMemory` to be addressed by the head.
        :param controller_size: The size of the internal representation.
        """
        super(NTMHeadBase, self).__init__()

        self.memory = memory
        self.N, self.M = memory.size()
        self.controller_size = controller_size

    def create_new_state(self, batch_size):
        raise NotImplementedError

    def register_parameters(self):
        raise NotImplementedError

    def is_read_head(self):
        return NotImplementedError

    def _address_memory(self, k, β, g, s, γ, w_prev):
        # Handle Activations
        k = k.clone()
        β = F.softplus(β)
        g = torch.sigmoid(g)
        s = F.softmax(s, dim=1)
        γ = 1 + F.softplus(γ)

        w = self.memory.address(k, β, g, s, γ, w_prev)

        return w


class NTMReadHead(NTMHeadBase):
    def __init__(self, memory, controller_size):
        super(NTMReadHead, self).__init__(memory, controller_size)

        # Corresponding to k, β, g, s, γ sizes from the paper
        self.read_lengths = [self.M, 1, 1, 3, 1]
        self.fc_read = nn.Linear(controller_size, sum(self.read_lengths))
        self.reset_parameters()

    def create_new_state(self, batch_size):
        # The state holds the previous time step address weightings
        return torch.zeros(batch_size, self.N)

    def reset_parameters(self):
        # Initialize the linear layers
        nn.init.xavier_uniform_(self.fc_read.weight, gain=1.4)
        nn.init.normal_(self.fc_read.bias, std=0.01)

    def is_read_head(self):
        return True

    def forward(self, embeddings, w_prev):
        """NTMReadHead forward function.
        :param embeddings: input representation of the controller.
        :param w_prev: previous step state
        """
        o = self.fc_read(embeddings)
        k, β, g, s, γ = _split_cols(o, self.read_lengths)
        # print("read prev", w_prev)
        # Read from memory
        w = self._address_memory(k, β, g, s, γ, w_prev)
        r = self.memory.read(w)

        return r, w


class NTMWriteHead(NTMHeadBase):
    def __init__(self, memory, controller_size):
        super(NTMWriteHead, self).__init__(memory, controller_size)

        # Corresponding to k, β, g, s, γ, e, a sizes from the paper
        self.write_lengths = [self.M, 1, 1, 3, 1, self.M, self.M]
        self.fc_write = nn.Linear(controller_size, sum(self.write_lengths))
        self.reset_parameters()

    def create_new_state(self, batch_size):
        return torch.zeros(batch_size, self.N)

    def reset_parameters(self):
        # Initialize the linear layers
        nn.init.xavier_uniform_(self.fc_write.weight, gain=1.4)
        nn.init.normal_(self.fc_write.bias, std=0.01)

    def is_read_head(self):
        return False

    def forward(self, embeddings, w_prev):
        """NTMWriteHead forward function.
        :param embeddings: input representation of the controller.
        :param w_prev: previous step state
        """
        
        o = self.fc_write(embeddings)
        k, β, g, s, γ, e, a = _split_cols(o, self.write_lengths)

        # e should be in [0, 1]
        e = torch.sigmoid(e)
        # print("write prev", w_prev)
        # Write to memory
        w = self._address_memory(k, β, g, s, γ, w_prev)
        self.memory.write(w, e, a)

        return w

"""memory"""

def _convolve(w, s):
    """Circular convolution implementation."""
    assert s.size(0) == 3
    t = torch.cat([w[-1:], w, w[:1]])
    c = F.conv1d(t.view(1, 1, -1), s.view(1, 1, -1)).view(-1)
    return c


class NTMMemory(nn.Module):
    """Memory bank for NTM."""
    def __init__(self, N, M):
        """Initialize the NTM Memory matrix.
        The memory's dimensions are (batch_size x N x M).
        Each batch has it's own memory matrix.
        :param N: Number of rows in the memory.
        :param M: Number of columns/features in the memory.
        """
        super(NTMMemory, self).__init__()

        self.N = N
        self.M = M

        # The memory bias allows the heads to learn how to initially address
        # memory locations by content
        self.register_buffer('mem_bias', torch.Tensor(N, M))

        # Initialize memory bias
        # stdev = 1 / (np.sqrt(N + M))
        nn.init.constant_(self.mem_bias, 1.0)

    def reset(self, batch_size):
        """Initialize memory from bias, for start-of-sequence."""
        self.batch_size = batch_size
        self.memory = self.mem_bias.clone().repeat(batch_size, 1, 1)

    def size(self):
        return self.N, self.M

    def read(self, w):
        """Read from memory (according to section 3.1)."""
        """
          = W[bs, 1, m] * Mem[bs, m, n] \\check 

        """
        # print("read", w)
        return torch.matmul(w.unsqueeze(1), self.memory).squeeze(1)

    def write(self, w, e, a):
        """write to memory (according to section 3.2)."""
        # print("write", w)
        self.prev_mem = self.memory
        self.memory = torch.Tensor(self.batch_size, self.N, self.M)
        erase = torch.matmul(w.unsqueeze(-1), e.unsqueeze(1))
        add = torch.matmul(w.unsqueeze(-1), a.unsqueeze(1))
        self.memory = self.prev_mem * (1 - erase) + add

    def address(self, k, β, g, s, γ, w_prev):
        """NTM Addressing (according to section 3.3).
        Returns a softmax weighting over the rows of the memory matrix.
        :param k: The key vector.
        :param β: The key strength (focus).
        :param g: Scalar interpolation gate (with previous weighting).
        :param s: Shift weighting.
        :param γ: Sharpen weighting scalar.
        :param w_prev: The weighting produced in the previous time step.
        """
        # Content focus
        wc = self._similarity(k, β) # wc = [bs, N]

        # Location focus
        wg = self._interpolate(w_prev, wc, g) # wg = [bs, N]
        ŵ = self._shift(wg, s)
        w = self._sharpen(ŵ, γ)

        return w

    def _similarity(self, k, β):
        k = k.view(self.batch_size, 1, -1) # k = [bs, 1, M]
        w = F.softmax(β * F.cosine_similarity(self.memory + 1e-16, k + 1e-16, dim=-1), dim=1) # sim(Mem[bs, N, M], K[bs, 1, M]) 
        return w

    def _interpolate(self, w_prev, wc, g):
        return g * wc + (1 - g) * w_prev

    def _shift(self, wg, s):
        result = torch.zeros(wg.size())
        for b in range(self.batch_size):
            result[b] = _convolve(wg[b], s[b])
        return result

    def _sharpen(self, ŵ, γ):
        w = ŵ ** γ
        print("sharpen", w)
        w = torch.div(w, torch.sum(w, dim=1).view(-1, 1) + 1e-16)
        return w

"""ntm"""

class NTM(nn.Module):
    """A Neural Turing Machine."""
    def __init__(self, num_inputs, num_outputs, controller, memory, heads):
        """Initialize the NTM.
        :param num_inputs: External input size. \8 + 1
        :param num_outputs: External output size. \8
        :param controller: :class:`LSTMController` 
        :param memory: :class:`NTMMemory`
        :param heads: list of :class:`NTMReadHead` or :class:`NTMWriteHead`
        : This design allows the flexibility of using any number of read and
              write heads independently, also, the order by which the heads are
              called in controlled by the user (order in list)
        """
        super(NTM, self).__init__()

        # Save arguments
        self.num_inputs = num_inputs
        self.num_outputs = num_outputs
        self.controller = controller
        self.memory = memory
        self.heads = heads

        self.N, self.M = memory.size()
        _, self.controller_size = controller.size()

        # Initialize the initial previous read values to random biases
        self.num_read_heads = 0
        self.init_r = []
        for head in heads:
            if head.is_read_head():
                # r = sum_i w_t(i) M_t(i)
                # dim(r) = M
                # init_r to feed it into controller
                init_r_bias = torch.zeros(1, self.M) 
                self.register_buffer("read{}_bias".format(self.num_read_heads), init_r_bias.data)
                self.init_r += [init_r_bias]
                self.num_read_heads += 1

        assert self.num_read_heads > 0, "heads list must contain at least a single read head"

        # Initialize a fully connected layer to produce the actual output:
        #   [controller_output; previous_reads ] -> output
        self.fc = nn.Linear(self.controller_size + self.num_read_heads * self.M, num_outputs)
        self.reset_parameters()

    def create_new_state(self, batch_size):
        init_r = [r.clone().repeat(batch_size, 1) for r in self.init_r] 
        # read heads reset 
        # dim = [n, bs, M]
        controller_state = self.controller.create_new_state(batch_size) 
        # LSTM controller reset, output : lstm_h, lstm_c
        heads_state = [head.create_new_state(batch_size) for head in self.heads] 
        # reset read & write head values
        # dim = [n, bs, N]

        return init_r, controller_state, heads_state

    def reset_parameters(self):
        # Initialize the linear layer
        nn.init.xavier_uniform_(self.fc.weight, gain=1)
        nn.init.normal_(self.fc.bias, std=0.01)

    def forward(self, x, prev_state):
        """NTM forward function.
        :param x: input vector (batch_size x num_inputs)
        :param prev_state: The previous state of the NTM
        """
        # Unpack the previous state
        prev_reads, prev_controller_state, prev_heads_states = prev_state

        # Use the controller to get an embeddings
        inp = torch.cat([x] + prev_reads, dim=1)
        controller_outp, controller_state = self.controller(inp, prev_controller_state)

        # Read/Write from the list of heads
        reads = []
        heads_states = []
        for head, prev_head_state in zip(self.heads, prev_heads_states):
            if head.is_read_head():
                r, head_state = head(controller_outp, prev_head_state)
                reads += [r]
            else:
                head_state = head(controller_outp, prev_head_state)
            heads_states += [head_state]

        # Generate Output
        inp2 = torch.cat([controller_outp] + reads, dim=1)
        o = (self.fc(inp2))

        # Pack the current state
        state = (reads, controller_state, heads_states)

        return o, state

"""# ***Task***

Copy
"""

import pandas as pd 
df = pd.read_csv("train10min.csv")
date = np.unique(df.date.values)
# date = []
print(date)
# timestamp = df.Date
# for t in timestamp:
#   date.append(t.split(",")[0])
# df['date'] = date
p = df["Avg3(E,25,E,13,E,8)-Avg3"].values
df['Avg3(E,25,E,13,E,8)-Avg3'] = (p - np.min(p))/(np.max(p) - np.min(p))
df_gp = df.groupby('date')
date = np.unique(np.array(date))

"""Copy Task NTM model."""

# Generator of randomized test sequences
def dataloader(num_batches,
               batch_size,
               seq_width,
               min_len,
               max_len):
    """Generator of random sequences for the copy task.
    Creates random batches of "bits" sequences.
    All the sequences within each batch have the same length.
    The length is [`min_len`, `max_len`]
    :param num_batches: Total number of batches to generate.
    :param seq_width: The width of each item in the sequence.
    :param batch_size: Batch size.
    :param min_len: Sequence minimum length.
    :param max_len: Sequence maximum length.
    NOTE: The input width is `seq_width + 1`, the additional input
    contain the delimiter.
    """
    
    # for batch_num in range(num_batches):

    #     # All batches have the same sequence length
    #     seq_len = random.randint(min_len, max_len)
    #     seq = np.random.binomial(1, 0.5, (seq_len, batch_size, seq_width))
    #     seq = torch.from_numpy(seq)

    #     # The input includes an additional channel used for the delimiter
    #     inp = torch.zeros(seq_len + 1, batch_size, seq_width + 1)
    #     inp[:seq_len, :, :seq_width] = seq
    #     inp[seq_len, :, seq_width] = 1.0 # delimiter in our control channel
    #     outp = seq.clone()

    #     yield batch_num+1, inp.float(), outp.float()
    
    for batch_num in range(num_batches):
        batch_day = date[batch_num%len(date)]

        # All batches have the same sequence length

        seq = df_gp.get_group(batch_day)["Avg3(E,25,E,13,E,8)-Avg3"].values[::-1]

        seq_len = len(seq)
        seq = seq.reshape(seq_len, batch_size, seq_width)

        seq = torch.from_numpy(seq.copy())

        # The input includes an additional channel used for the delimiter
        inp = torch.zeros(seq_len + 1, batch_size, seq_width + 1)
        inp[:seq_len, :, :seq_width] = seq
        inp[seq_len, :, seq_width] = 1.0 # delimiter in our control channel
        outp = seq.clone()

        yield batch_num+1, inp.float(), outp.float()


@attrs
class CopyTaskParams(object):
    name = attrib(default="copy-task")
    # controller_size = attrib(default=100, convert=int)
    # controller_layers = attrib(default=1,convert=int)
    # num_heads = attrib(default=1, convert=int)
    # sequence_width = attrib(default=8, convert=int)
    # sequence_min_len = attrib(default=1,convert=int)
    # sequence_max_len = attrib(default=20, convert=int)
    # memory_n = attrib(default=128, convert=int)
    # memory_m = attrib(default=20, convert=int)
    # num_batches = attrib(default=50000, convert=int)
    # batch_size = attrib(default=1, convert=int)
    # rmsprop_lr = attrib(default=1e-4, convert=float)
    # rmsprop_momentum = attrib(default=0.9, convert=float)
    # rmsprop_alpha = attrib(default=0.95, convert=float)

    controller_size = attrib(default=100)
    controller_layers = attrib(default=1)
    num_heads = attrib(default=1)
    sequence_width = attrib(default=1)
    sequence_min_len = attrib(default=1)
    sequence_max_len = attrib(default=20)
    memory_n = attrib(default=40)
    memory_m = attrib(default=20)
    num_batches = attrib(default=50000)
    batch_size = attrib(default=1)
    rmsprop_lr = attrib(default=1e-3)
    rmsprop_momentum = attrib(default=0.9)
    rmsprop_alpha = attrib(default=0.95)


#
# To create a network simply instantiate the `:class:CopyTaskModelTraining`,
# all the components will be wired with the default values.
# In case you'd like to change any of defaults, do the following:
#
# > params = CopyTaskParams(batch_size=4)
# > model = CopyTaskModelTraining(params=params)
#
# Then use `model.net`, `model.optimizer` and `model.criterion` to train the
# network. Call `model.train_batch` for training and `model.evaluate`
# for evaluating.
#
# You may skip this alltogether, and use `:class:CopyTaskNTM` directly.
#

@attrs
class CopyTaskModelTraining(object):
    params = attrib(default=Factory(CopyTaskParams))
    net = attrib()
    dataloader = attrib()
    criterion = attrib()
    optimizer = attrib()

    @net.default
    def default_net(self):
        # We have 1 additional input for the delimiter which is passed on a
        # separate "control" channel
        net = EncapsulatedNTM(self.params.sequence_width + 1, self.params.sequence_width,
                              self.params.controller_size, self.params.controller_layers,
                              self.params.num_heads,
                              self.params.memory_n, self.params.memory_m)
        return net

    @dataloader.default
    def default_dataloader(self):
        return dataloader(self.params.num_batches, self.params.batch_size,
                          self.params.sequence_width,
                          self.params.sequence_min_len, self.params.sequence_max_len)

    @criterion.default
    def default_criterion(self):
        # return nn.BCELoss()
        return nn.MSELoss()

    @optimizer.default
    def default_optimizer(self):
        return optim.RMSprop(self.net.parameters(),
                             momentum=self.params.rmsprop_momentum,
                             alpha=self.params.rmsprop_alpha,
                             lr=self.params.rmsprop_lr)

"""# ***Main***

**Utilis**
"""

# --- Utils ---
import yaml

def save_yaml(filepath, content, width=120):
    with open(filepath, 'w') as f:
        yaml.dump(content, f, width=width)


def load_yaml(filepath):
    with open(filepath, 'r') as f:
        content = yaml.safe_load(f)
    return content


class DotDict(dict):
    """dot.notation access to dictionary attributes

    Refer: https://stackoverflow.com/questions/2352181/how-to-use-a-dot-to-access-members-of-dictionary/23689767#23689767
    """  # NOQA

    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__

LOGGER = logging.getLogger(__name__)

device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
flags_dict = {
    # --- Seed value for RNGs ---
    "seed": RANDOM_SEED,
    # --- task ---
    "task": "copy",
    # --- Override model params. Example: "-pbatch_size=4 -pnum_heads=2" ---
    "param": [],
    # --- Checkpoint interval ---
    "checkpoint_interval": CHECKPOINT_INTERVAL,
    "checkpoint_path": "./",
    "report_interval": REPORT_INTERVAL,
}
save_yaml('./flags.yaml', flags_dict)

# TASKS = {
#     'copy': (CopyTaskModelTraining, CopyTaskParams),
#     'repeat-copy': (RepeatCopyTaskModelTraining, RepeatCopyTaskParams)
# }

TASKS = {
    'copy': (CopyTaskModelTraining, CopyTaskParams),
    # 'repeat-copy': (RepeatCopyTaskModelTraining, RepeatCopyTaskParams)
}

def plot_grad_flow(named_parameters):
    '''Plots the gradients flowing through different layers in the net during training.
    Can be used for checking for possible gradient vanishing / exploding problems.
    
    Usage: Plug this function in Trainer class after loss.backwards() as 
    "plot_grad_flow(self.model.named_parameters())" to visualize the gradient flow'''
    ave_grads = []
    max_grads= []
    layers = []
    for n, p in named_parameters:
        if(p.requires_grad) and ("bias" not in n):
            layers.append(n)
            ave_grads.append(p.grad.abs().mean())
            max_grads.append(p.grad.abs().max())
    # plt.bar(np.arange(len(max_grads)), max_grads, alpha=0.1, lw=1, color="c")
    # plt.bar(np.arange(len(max_grads)), ave_grads, alpha=0.1, lw=1, color="b")
    # plt.hlines(0, 0, len(ave_grads)+1, lw=2, color="k" )
    plt.plot(ave_grads, color="g", label="avg")
    plt.plot(max_grads, color="r", label="max")
    plt.xticks(range(0,len(ave_grads), 1), layers, rotation="vertical")
    plt.xlim(left=0, right=len(ave_grads))
    # plt.ylim(bottom = -0.001, top= 0.2) # zoom in on the lower gradient regions
    plt.xlabel("Layers")
    plt.ylabel("average gradient")
    plt.title("Gradient flow")
    plt.grid(True)
    plt.legend()
    # plt.legend([Line2D([0], [0], color="r", lw=4),
    #             Line2D([0], [0], color="g", lw=4),
    #             Line2D([0], [0], color="b", lw=4)], ['max-gradient', 'mean-gradient', 'zero-gradient'])
    plt.show()
def get_ms():
    """Returns the current time in miliseconds."""
    return time.time() * 1000

def init_seed(seed=None):
    """Seed the RNGs for predicatability/reproduction purposes."""
    if seed is None:
          seed = int(get_ms() // 1000)

    LOGGER.info("Using seed=%d", seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    random.seed(seed)


def progress_clean():
    """Clean the progress bar."""
    print("\r{}".format(" " * 80), end='\r')


def progress_bar(batch_num, report_interval, last_loss):
    """Prints the progress until the next report."""
    progress = (((batch_num-1) % report_interval) + 1) / report_interval
    fill = int(progress * 40)
    print("\r[{}{}]: {} (Loss: {:.4f})".format(
        "=" * fill, " " * (40 - fill), batch_num, last_loss), end='')


def save_checkpoint(net, name, args, batch_num, losses, costs, seq_lengths):
    progress_clean()

    basename = "{}/{}-{}-batch-{}".format(args.checkpoint_path, name, args.seed, batch_num)
    model_fname = basename + ".model"
    LOGGER.info("Saving model checkpoint to: '%s'", model_fname)
    torch.save(net.state_dict(), model_fname)

    # Save the training history
    train_fname = basename + ".json"
    LOGGER.info("Saving model training history to '%s'", train_fname)
    content = {
        "loss": losses,
        "cost": costs,
        "seq_lengths": seq_lengths
    }
    open(train_fname, 'wt').write(json.dumps(content))


def clip_grads(net):
    """Gradient clipping to the range [10, 10]."""
    parameters = list(filter(lambda p: p.grad is not None, net.parameters()))
    for p in parameters:
        p.grad.data.clamp_(-5, 5)


def train_batch(net, criterion, optimizer, X, Y):
    """Trains a single batch."""
    optimizer.zero_grad()
    inp_seq_len = X.size(0) # inp_seq_len, batch_size, inp_seq_dim
    outp_seq_len, batch_size, _ = Y.size()
    Y_label = Y.permute(1, 0, 2).clone()

    # New sequence
    net.init_sequence(batch_size) # initialize memory, LSTM controller, read_heads

    # Feed the sequence + delimiter
    for i in range(inp_seq_len):
        net(X[i])

    # Read the output (no input given)
    y_out = torch.zeros(Y.size())
    for i in range(outp_seq_len):
        y_out[i], _ = net()
    y_pred = y_out.permute(1, 0, 2).clone()
    plt.plot(X.cpu().detach().numpy()[:-1, 0, 0], label = "True")
    plt.plot(y_out.cpu().detach().numpy()[:, 0, 0], label = "Pred")
    plt.legend()
    plt.show()

    loss = criterion(y_pred, Y_label)
    lambda1 = 0.2
    all_linear1_params = torch.cat([x.view(-1) for x in net.parameters()])
    l1_regularization = lambda1 * torch.norm(all_linear1_params, 1)
    #loss += l1_regularization
    
    loss.backward()
    clip_grads(net)
    plot_grad_flow(net.named_parameters())
    for n, p in net.named_parameters():
      print(n, p.grad.norm())
    
    optimizer.step()

    y_out_binarized = y_out.clone().data
    y_out_binarized.apply_(lambda x: 0 if x < 0.5 else 1)

    # The cost is the number of error bits per sequence
    cost = torch.sum(torch.abs(y_out_binarized - Y.data))

    return loss.item(), cost.item() / batch_size


def evaluate(net, criterion, X):

    """Evaluate a single batch (without training)."""
    inp_seq_len = X.size(0)
    outp_seq_len, batch_size, _ = X.size()

    # New sequence
    net.init_sequence(batch_size)

    # Feed the sequence + delimiter
    states = []
    for i in range(inp_seq_len):

        o, state = net(X[i])
        states += [state]

    # Read the output (no input given)
    y_out = torch.zeros(X.size())
    for i in range(outp_seq_len):
        y_out[i], state = net()
        states += [state]

    plt.plot(X.cpu().detach().numpy()[:-1, 0, 0], label = "True")
    plt.plot(y_out.cpu().detach().numpy()[:, 0, 0], label = "Pred")
    plt.legend()
    plt.show()
    loss = criterion(y_out, X)

    # y_out_binarized = y_out.clone().data
    # y_out_binarized.apply_(lambda x: 0 if x < 0.5 else 1)

    # The cost is the number of error bits per sequence
    # cost = torch.sum(torch.abs(y_out_binarized - Y.data))

    # result = {
    #     'loss': loss.data[0],
    #     # 'cost': cost / batch_size,
    #     'y_out': y_out,
    #     # 'y_out_binarized': y_out_binarized,
    #     'states': states
    # }

    # return result


def train_model(model, args):
    num_batches = model.params.num_batches
    batch_size = model.params.batch_size

    LOGGER.info("Training model for %d batches (batch_size=%d)...",
                num_batches, batch_size)

    losses = []
    costs = []
    seq_lengths = []
    start_ms = get_ms()

    for batch_num, x, y in model.dataloader:
        
        loss, cost = train_batch(model.net, model.criterion, model.optimizer, x, y)
        losses += [loss]
        costs += [cost]
        seq_lengths += [y.size(0)]

        # Update the progress bar
        # progress_bar(batch_num, args.report_interval, loss)

        # Report
        if batch_num % args.report_interval == 0:
            seq = df_gp.get_group("2014/01/02")["Avg3(E,25,E,13,E,8)-Avg3"].values[::-1]
            seq_len = len(seq)
            seq = seq.reshape(seq_len, batch_size, 1)
            seq = torch.from_numpy(seq.copy())

            # The input includes an additional channel used for the delimiter
            inp = torch.zeros(seq_len + 1, batch_size, 2)
            inp[:seq_len, :, :1] = seq
            inp[seq_len, :, 1] = 1.0 # delimiter in our control channel
   
            evaluate(model.net, model.criterion, inp)
            mean_loss = np.array(losses[-args.report_interval:]).mean()
            mean_cost = np.array(costs[-args.report_interval:]).mean()
            mean_time = int(((get_ms() - start_ms) / args.report_interval) / batch_size)
            progress_clean()
            LOGGER.info("Batch %d Loss: %.6f Cost: %.2f Time: %d ms/sequence",
                        batch_num, mean_loss, mean_cost, mean_time)
            start_ms = get_ms()

        # Checkpoint
        if (args.checkpoint_interval != 0) and (batch_num % args.checkpoint_interval == 0):
            save_checkpoint(model.net, model.params.name, args,
                            batch_num, losses, costs, seq_lengths)

    LOGGER.info("Done training.")


def init_arguments():
    args = DotDict(flags_dict)
    return args


def update_model_params(params, update):
    """Updates the default parameters using supplied user arguments."""
    update_dict = {}
    for p in update:
        m = re.match("(.*)=(.*)", p)
        if not m:
            LOGGER.error("Unable to parse param update '%s'", p)
            sys.exit(1)

        k, v = m.groups()
        update_dict[k] = v

    try:
        params = attr.evolve(params, **update_dict)
    except TypeError as e:
        LOGGER.error(e)
        LOGGER.error("Valid parameters: %s", list(attr.asdict(params).keys()))
        sys.exit(1)

    return params

def init_model(args):
    LOGGER.info("Training for the **%s** task", args.task)

    model_cls, params_cls = TASKS[args.task]
    params = params_cls()
    params = update_model_params(params, args.param)

    LOGGER.info(params)

    model = model_cls(params=params)
    return model


def init_logging():
    logging.basicConfig(format='[%(asctime)s] [%(levelname)s] [%(name)s]  %(message)s',
                        level=logging.DEBUG)
  

def main():
    init_logging()

    # Initialize arguments
    args = init_arguments()

    # Initialize random
    init_seed(args.seed)

    # Initialize the model
    model = init_model(args)                                                             

    LOGGER.info("Total number of parameters: %d", model.net.calculate_num_params())
    train_model(model, args)


if __name__ == '__main__':
    main()

ls