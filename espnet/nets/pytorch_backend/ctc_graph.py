# Copyright (c)  2020  Xiaomi Corp.       (author: Fangjun Kuang)

from functools import lru_cache
from typing import (
    Iterable,
    List,
)

import torch
import k2
import math
from typing import Tuple

class K2CTCLoss(torch.nn.Module):
  def __init__(self, odim: int, reduction: str = 'sum') -> None:
    torch.nn.Module.__init__(self)
    self.graph_compiler = NaiveCtcTrainingGraphCompiler(odim)

  def forward(self, log_probs: torch.Tensor, targets: torch.Tensor, input_lengths: torch.Tensor, target_lengths: torch.Tensor) -> torch.Tensor:
    
    log_probs = log_probs.permute(1,0,2).cpu() # now log_probs is [N, T, C]  batchSize x seqLength x alphabet_size
    supervision_segments = torch.stack(
        (torch.tensor(range(input_lengths.shape[0])),
         torch.zeros(input_lengths.shape[0]),
         input_lengths), 1).to(torch.int32)
    indices = torch.argsort(supervision_segments[:, 2], descending=True)
    supervision_segments = supervision_segments[indices]

    dense_fsa_vec = k2.DenseFsaVec(log_probs, supervision_segments)
    decoding_graph = self.graph_compiler.compile(targets.cpu(), target_lengths)
    decoding_graph = k2.index(decoding_graph, indices.to(torch.int32)).to(log_probs.device)

    target_graph = k2.intersect_dense(decoding_graph, dense_fsa_vec, 10.0)
    tot_scores = k2.get_tot_scores(target_graph,
                                   log_semiring=True,
                                   use_double_scores=True)
    (tot_score, tot_frames,
     all_frames) = get_tot_objf_and_num_frames(tot_scores,
                                               supervision_segments[:, 2])
    return -tot_score

def build_ctc_topo(tokens: List[int]) -> k2.Fsa:
    '''Build CTC topology.
    The resulting topology converts repeated input
    symbols to a single output symbol.
    Caution:
      The resulting topo is an FST. Epsilons are on the left
      side (i.e., ilabels) and tokens are on the right side (i.e., olabels)
    Args:
      tokens:
        A list of tokens, e.g., phones, characters, etc.
    Returns:
      Returns an FST that converts repeated tokens to a single token.
    '''
    assert 0 in tokens, 'We assume 0 is ID of the blank symbol'

    num_states = len(tokens)
    final_state = num_states
    rules = ''
    for i in range(num_states):
        for j in range(num_states):
            if i == j:
                rules += f'{i} {i} 0 {tokens[i]} 0.0\n'
            else:
                rules += f'{i} {j} {tokens[j]} {tokens[j]} 0.0\n'
        rules += f'{i} {final_state} -1 -1 0.0\n'
    rules += f'{final_state}'
    ans = k2.Fsa.from_str(rules)
    ans = k2.arc_sort(ans)
    return ans


class NaiveCtcTrainingGraphCompiler(object):

    def __init__(self,
                 odim: int,
                 oov: str = '<unk>'):
        '''
        Args:
        odim:
          Output dimension of CTC linear layer, len(symbol_list) + 2 (<blank> and <eos>).
        oov:
          Out of vocabulary word.
        '''

        self.dim = odim
        self.oov = oov
        self.ctc_topo = build_ctc_topo(list(range(self.dim)))


    def compile(self, texts: torch.Tensor, texts_lengths: torch.Tensor) -> k2.Fsa:
        texts_lengths = torch.cat([torch.tensor([0]), texts_lengths])
        texts_end_index = torch.cumsum(texts_lengths, 0)

        decoding_graphs = k2.create_fsa_vec(
            [self.compile_one_and_cache(texts[texts_end_index[i]: texts_end_index[i + 1]]) for i in range(texts_lengths.shape[0] - 1)])

        # make sure the gradient is not accumulated
        decoding_graphs.requires_grad_(False)
        return decoding_graphs

    @lru_cache(maxsize=100000)
    def compile_one_and_cache(self, text: torch.Tensor) -> k2.Fsa:
        word_ids = text.tolist()
        linear_fsa = k2.linear_fsa(word_ids)
        decoding_graph = k2.intersect(self.ctc_topo, linear_fsa)
        decoding_graph = k2.connect(decoding_graph).invert_()
        return decoding_graph

def get_tot_objf_and_num_frames(tot_scores: torch.Tensor,
                                frames_per_seq: torch.Tensor
                                ) -> Tuple[float, int, int]:
    ''' Figures out the total score(log-prob) over all successful supervision segments
    (i.e. those for which the total score wasn't -infinity), and the corresponding
    number of frames of neural net output
         Args:
            tot_scores: a Torch tensor of shape (num_segments,) containing total scores
                       from forward-backward
        frames_per_seq: a Torch tensor of shape (num_segments,) containing the number of
                       frames for each segment
        Returns:
             Returns a tuple of 3 scalar tensors:  (tot_score, ok_frames, all_frames)
        where ok_frames is the frames for successful (finite) segments, and
       all_frames is the frames for all segments (finite or not).
    '''
    mask = torch.ne(tot_scores, -math.inf)
    # finite_indexes is a tensor containing successful segment indexes, e.g.
    # [ 0 1 3 4 5 ]
    finite_indexes = torch.nonzero(mask).squeeze(1)
    if False:
        bad_indexes = torch.nonzero(~mask).squeeze(1)
        if bad_indexes.shape[0] > 0:
            print("Bad indexes: ", bad_indexes, ", bad lengths: ",
                  frames_per_seq[bad_indexes], " vs. max length ",
                  torch.max(frames_per_seq), ", avg ",
                  (torch.sum(frames_per_seq) / frames_per_seq.numel()))
    # print("finite_indexes = ", finite_indexes, ", tot_scores = ", tot_scores)
    ok_frames = frames_per_seq[finite_indexes].sum()
    all_frames = frames_per_seq.sum()
    return (tot_scores[finite_indexes].sum(), ok_frames, all_frames)
