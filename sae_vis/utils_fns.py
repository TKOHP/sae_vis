# %%

from jaxtyping import Float, Int, Bool
from typing import Tuple, Optional, Union, Dict, List
from dataclasses import dataclass
import re
import torch
from torch import Tensor
import numpy as np
from transformers.tokenization_utils_fast import PreTrainedTokenizerFast
import einops
from datasets.arrow_dataset import Dataset
from sae_vis.model_fns import DemoTransformer
from transformers import AutoTokenizer

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


Arr = np.ndarray

MAIN = __name__ == "__main__"

def k_largest_indices(
    x: Float[Tensor, "rows cols"],
    k: int,
    largest: bool = True,
    buffer: Tuple[int, int] = (5, 5),
) -> Int[Tensor, "k 2"]:
    '''
    Args:
        x:
            2D array of floats (these will be the values of feature activations or losses for each
            token in our batch)
        k:
            Number of indices to return
        largest:
            Whether to return the indices for the largest or smallest values
        buffer:
            How many positions to avoid at the start and end of the sequence

    Returns:
        The indices of the top or bottom `k` elements in `x`. In other words, output[i, :] is the
        (row, column) index of the i-th largest/smallest element in `x`. Note that we restrict
        `column` to be in the range `buffer[0] : -buffer[1]`. This is so that we make sure each
        token we're choosing has some surrounding context in that sequence.
    '''
    x = x[:, buffer[0]:-buffer[1]]
    indices = x.flatten().topk(k=k, largest=largest).indices
    rows = indices // x.size(1)
    cols = indices % x.size(1) + buffer[0]
    return torch.stack((rows, cols), dim=1)



def sample_unique_indices(large_number: int, small_number: int) -> Int[Tensor, "small_number"]:
    '''
    Samples a small number of unique indices from a large number of indices.

    This is more efficient than using `torch.permutation`, because we don't need to shuffle everything.
    '''
    weights = torch.ones(large_number)  # Equal weights for all indices
    sampled_indices = torch.multinomial(weights, small_number, replacement=False)
    return sampled_indices



def random_range_indices(
    x: Float[Tensor, "batch seq"],
    k: int,
    bounds: Tuple[float, float],
    buffer: Tuple[int, int] = (5, 5),
) -> Int[Tensor, "k 2"]:
    '''
    Args:
        x:
            2D array of floats (these will be the values of feature activations or losses for each
            token in our batch)
        k:
            Number of indices to return
        bounds:
            The range of values to consider (so we can get quantiles)
        buffer:
            How many positions to avoid at the start and end of the sequence

    Returns:
        Same thing as `k_largest_indices`, but the difference is that we're using quantiles rather than
        the top/bottom k.
    '''
    # Limit x, because our indices (bolded words) shouldn't be too close to the left/right of sequence
    x = x[:, buffer[0]:-buffer[1]]

    # Creat a mask for where x is in range, and get the indices as a tensor of shape (k, 2)
    mask = (bounds[0] <= x) & (x <= bounds[1])
    indices = torch.stack(torch.where(mask), dim=-1)

    # If we have more indices than we need, randomly select k of them
    if len(indices) > k:
        indices = indices[sample_unique_indices(len(indices), k)]

    # Adjust indices to account for the buffer
    return indices + torch.tensor([0, buffer[0]]).to(indices.device)



def create_vocab_dict(tokenizer: PreTrainedTokenizerFast) -> Dict[int, str]:
    '''
    Creates a vocab dict by replacing all the annoying special tokens with their HTML representations.
    '''
    vocab_dict: Dict[str, int] = tokenizer.vocab
    vocab_dict = {v: process_str_tok(k) for k, v in vocab_dict.items()}
    return vocab_dict



def process_str_tok(str_tok: str) -> str:
    '''
    Takes a string token, and does the necessary formatting to produce the right HTML output.
    This involves dealing with spaces, newlines (and other backslashes), and angle brackets.
    '''
    # Get rid of the quotes and apostrophes, and replace them with their HTML representations
    str_tok = str_tok.replace("'", "&apos;").replace('"', "&quot;")
    str_tok = repr(str_tok)[1:-1]  # repr turns \n into \\n, while slicing removes the quotes from the repr

    # Deal with other HTML or special characters
    html_replacements = {"Ġ": " ", " ": "&nbsp;", "\\": "&bsol;", "<": "&lt;", ">": "&gt;"}
    for k, v in html_replacements.items():
        str_tok = str_tok.replace(k, v)

    return str_tok



def to_str_tokens(vocab_dict: Dict[int, str], tokens: Union[int, torch.Tensor]):
    '''
    Helper function which converts tokens to their string representations, but (if tokens is a tensor) keeps
    them in the same shape as the original tensor (i.e. nested lists).
    '''
    if isinstance(tokens, int):
        return vocab_dict[tokens]

    # Get flattened list of tokens
    str_tokens = [vocab_dict[t] for t in tokens.flatten().tolist()]

    # Reshape
    return np.reshape(str_tokens, tokens.shape).tolist()



class TopK:
    '''
    Wrapper around the object returned by torch.topk. This is useful because:

        (1) Nicer type signatures (the default obj returned by torck.topk isn't well typed)
        (2) We have helper functions for indexing & other standard tensor operations like .ndim, .shape, etc.
    '''
    values: Arr
    indices: Arr

    def __init__(self, obj: Optional[Tuple[Arr, Arr]] = None):
        '''
        We either initialize with the actual object returned by torch.topk, or with `None` if we want to
        add to this TopK object incrementally via the `concat` method.
        '''
        self.values: Arr = obj[0] if isinstance(obj[0], Arr) else obj[0].detach().cpu().numpy()
        self.indices: Arr = obj[1] if isinstance(obj[1], Arr) else obj[1].detach().cpu().numpy()
    
    def __getitem__(self, item) -> "TopK":
        return TopK((self.values[item], self.indices[item]))
        
    def clone(self) -> "TopK":
        return TopK((self.values.copy(), self.indices.copy()))
    
    def __len__(self) -> int:
        return len(self.values)
    
    @property
    def ndim(self) -> int:
        return self.values.ndim

    @property
    def shape(self) -> Tuple[int]:
        return self.values.shape
    
    def numel(self) -> int:
        return self.values.size



def efficient_topk(
    tensor: Float[Tensor, "... d"],
    tensor_mask: Bool[Tensor, "..."],
    k: int,
    largest: bool = True,
) -> TopK:
    '''
    This is an efficient version of `torch.topk(..., dim=-1)`. It saves time by only doing the topk calculation over
    the bits of `tensor` where `tensor_mask=True`. This is useful when `tensor` is very sparse, e.g. it has shape
    (batch, seq, d_vocab) and its elements are zero if the corresponding token has feature activation zero. In this
    case, we don't want to waste time taking topk over a tensor of zeros.
    '''
    # Get the topk of the tensor, but only computed over the values of the tensor which are nontrivial
    tensor_nontrivial_values = tensor[tensor_mask] # shape [rows d]
    topk = tensor_nontrivial_values.topk(k=k, largest=largest) # shape [rows k]

    # Get an array of indices and values (with unimportant elements) which we'll index into using the topk object
    topk_shape = (*tensor_mask.shape, k)
    topk_indices = torch.zeros(topk_shape).to(device).long() # shape [... k]
    topk_indices[tensor_mask] = topk.indices
    topk_values = torch.zeros(topk_shape).to(device) # shape [... k]
    topk_values[tensor_mask] = topk.values

    return TopK((topk_values, topk_indices))




def merge_lists(*lists):
    '''
    Merges a bunch of lists into a single list.
    '''
    return [item for sublist in lists for item in sublist]



def extract_and_remove_scripts(html_content: str) -> Tuple[str, str]:
    '''
    Extracts JavaScript from script tags in the HTML content, and returns it as a single string,
    along with the original content with the script tags removed.
    '''
    # Pattern to find <script>...</script> tags and capture content inside
    pattern = r'<script[^>]*>(.*?)</script>'

    # Find all script tags and extract content
    scripts = re.findall(pattern, html_content, re.DOTALL)

    # Remove script tags from the original content
    html_without_scripts = re.sub(pattern, '', html_content, flags=re.DOTALL)

    # Join extracted JavaScript code
    javascript = "\n".join(scripts)

    return javascript, html_without_scripts




# %%

SYMMETRIC_RANGES_AND_PRECISIONS = [
    ((0.0, 0.01), 5),
    ((0.01, 0.05), 4),
    ((0.05, 0.95), 3),
    ((0.95, 0.99), 4),
    ((0.99, 1.0), 5),
]
ASYMMETRIC_RANGES_AND_PRECISIONS = [
    ((0.0, 0.95), 3),
    ((0.95, 0.99), 4),
    ((0.99, 1.0), 5),
]


class QuantileCalculator:
    '''
    This class is initialized with some float-type data, as well as a list of ranges and precisions. It will only keep
    the data which is necessary to calculate the quantile of additional data to the required precision, but no more.

    This was created because (for example) when looking at the top-activating features, we care way more about precision
    if the feature's activation is in the top 1% of its activations over all other data it's been analyzed on.

    Note, this class works in parallel, i.e. it can handle multiple different sets of data at once. The data is expected
    in 2D tensor format, with the first dimension being the batch dim, i.e. each row is a different dataset which we
    want to be able to compute quantiles from.
    '''
    def __init__(
        self,
        data: Optional[Float[Tensor, "batch data"]] = None,
        ranges_and_precisions: list = ASYMMETRIC_RANGES_AND_PRECISIONS,
    ):
        # Get the data for the actual ranges_and_precisions list (so we can more easily return the precision)
        rp = ranges_and_precisions
        self.rp = rp
        self.ranges = torch.tensor([r[0] for (r, p) in rp] + [1.0]).to(device)
        self.precisions = torch.tensor([rp[0][1]] + [p for (r, p) in rp] + [rp[-1][1]]).to(device)

        # Generate quantiles from the ranges_and_precisions list
        quantiles = []
        for r, p in ranges_and_precisions:
            start, end = r
            step = 10 ** -p
            quantiles.extend(np.arange(start, end - 0.5 * step, step))

        # If data is None, then set the quantiles and quantile_data to None, and return
        if data is None:
            self.quantiles = None
            self.quantile_data = None
        # Else, get the actual quantile values (which we'll use to calculate the quantiles of any new data)
        else:
            self.quantiles = torch.tensor(quantiles + [1.0]).to(device)
            self.quantile_data = torch.quantile(data, torch.tensor(quantiles).to(device, dtype=data.dtype), dim=-1).T


    def update(self, other: "QuantileCalculator"):
        '''
        Merges two QuantileCalculator objects together (changing self inplace). This is useful when we're batching our
        calculations over different groups of features, and we want to merge them together at the end.

        Note, we also deal with the special case where self has no data.
        '''
        assert self.rp == other.rp, "Error: can't merge two QuantileCalculator objects with different ranges."
        # assert isinstance(other, QuantileCalculator), "Error: can only merge QuantileCalculator objects."

        if self.quantiles is None:
            self.quantiles = other.quantiles
            self.quantile_data = other.quantile_data
        else:
            self.quantiles = torch.cat([self.quantiles, other.quantiles])
            self.quantile_data = torch.cat([self.quantile_data, other.quantile_data])


    def get_quantile(
        self,
        values: Float[Tensor, "batch *data_dim"],
        batch_indices: Optional[List[int]] = None,
    ) -> Tuple[Float[Tensor, "batch *data_dim"], Int[Tensor, "batch *data_dim"]]:
        '''
        Args:
            values:
                Tensor of values for which we want to compute the quantiles. If this is 1D then it is interpreted as a
                single value for each dataset (i.e. for each row of the reference data), if it's 2D then it's a row of
                values for each dataset.
            batch_indices:
                If not None, then this should be a list of batch indices we're actually using, in other words we should
                index `self.quantiles` down to only these indices. This is useful because often we're only doing this
                calculation on a small set of features (the ones which are non-zero).

        Returns:
            quantiles:
                The quantiles of `values` within the respective rows of the reference data.
            precisions:
                The precision of the quantiles (i.e. how many decimal places we're accurate to).
        '''
        values_is_1d = (values.ndim == 1)
        if values_is_1d:
            values = values.unsqueeze(1)
        if batch_indices is None:
            batch_indices = slice(None)

        # Find the quantiles of these values (i.e. the values between 0 and 1)
        quantile_indices = torch.searchsorted(self.quantile_data[batch_indices], values) # shape [batch data_dim]
        quantiles = self.quantiles[quantile_indices]

        # Also get the precisions (which we do using a separate searchsorted, only over the range dividers)
        precision_indices = torch.searchsorted(self.ranges, quantiles) # shape [batch data_dim]
        precisions = self.precisions[precision_indices]

        # If values was 1D, we want to return the result as 1D also (for convenience)
        if values_is_1d:
            quantiles = quantiles.squeeze(1)
            precisions = precisions.squeeze(1)

        return quantiles, precisions


# Example usage
if MAIN:
    # 2D data: each row represents the activations data of a different feature
    data = torch.stack([torch.rand(100_000), torch.rand(100_000)]).to(device)
    qc = QuantileCalculator(data)
    print(f"Total datapoints stored = {qc.quantile_data.numel():_}\n(less than full data size = {data.numel():_})\n")

    # 2D values tensor: each row applies to a different dataset
    values = torch.tensor([[0.0, 0.005, 0.02, 0.25], [0.75, 0.98, 0.995, 1.0]]).to(device)
    quantiles, precisions = qc.get_quantile(values)

    for v, q, p in zip(values.flatten(), quantiles.flatten(), precisions.flatten()):
        print(f"Value: {v:.3f}, Precision: {p}, Quantile: {q:.{p-2}%}")

# %%


def split_string(input_string, str1, str2):
    assert str1 in input_string and str2 in input_string, "Error: str1 and str2 must be in input_string"
    pattern = f'({re.escape(str1)}.*?){re.escape(str2)}'
    match = re.search(pattern, input_string, flags=re.DOTALL)
    if match:
        between_str1_str2 = match.group(1)
        remaining_string = input_string.replace(between_str1_str2, '')
        return between_str1_str2, remaining_string
    else:
        return None, input_string

# Example usage
if MAIN:
    input_string = "The quick brown fox jumps over the lazy dog"
    str1 = "quick"
    str2 = "jumps"
    print(split_string(input_string, str1, str2))

    input_string = "Before table <!-- Logits table --> Table <!-- Logits histogram --> After table"
    str1 = r"<!-- Logits table -->"
    str2 = r"<!-- Logits histogram -->"
    print(split_string(input_string, str1, str2))

# %%



def tokenize_and_concatenate(
    dataset: Dataset,
    tokenizer: AutoTokenizer,
    streaming: bool = False,
    max_length: int = 1024,
    column_name: str = "text",
    add_bos_token: bool = True,
    num_proc: int = 10,
) -> Dataset:
    for key in dataset.features:
        if key != column_name:
            dataset = dataset.remove_columns(key)

    if tokenizer.pad_token is None:
        tokenizer.add_special_tokens({"pad_token": "<PAD>"})

    seq_len = (max_length - 1) if add_bos_token else max_length

    def tokenize_function(examples: Dict[str, List[str]]) -> Dict[str, np.ndarray]:
        text = examples[column_name]
        # Concatenate it all into an enormous string, separated by eos_tokens
        full_text = tokenizer.eos_token.join(text)
        # Divide into 20 chunks of ~ equal length
        num_chunks = 20
        chunk_length = (len(full_text) - 1) // num_chunks + 1
        chunks = [
            full_text[i * chunk_length : (i + 1) * chunk_length]
            for i in range(num_chunks)
        ]
        # Tokenize the chunks in parallel. Uses NumPy because HuggingFace map doesn't want tensors returned
        tokens = tokenizer(chunks, return_tensors="np", padding=True)["input_ids"].flatten()
        # Drop padding tokens
        tokens = tokens[tokens != tokenizer.pad_token_id]
        num_tokens = len(tokens)
        num_batches = num_tokens // (seq_len)
        # Drop the final tokens if not enough to make a full sequence
        tokens = tokens[: seq_len * num_batches]
        tokens = einops.rearrange(tokens, "(batch seq) -> batch seq", batch=num_batches, seq=seq_len)
        if add_bos_token:
            prefix = np.full((num_batches, 1), tokenizer.bos_token_id)
            tokens = np.concatenate([prefix, tokens], axis=1)
        return {"tokens": tokens}

    tokenized_dataset = dataset.map(
        tokenize_function,
        batched=True,
        num_proc=(num_proc if not streaming else None),
        remove_columns=[column_name],
    )
    tokenized_dataset.set_format(type="torch", columns=["tokens"])
    return tokenized_dataset

def to_resid_dir(dir: Tensor, model: DemoTransformer):
    """
        Takes a direction (eg. in the post-ReLU MLP activations) and returns
        the corresponding direction in the residual stream.

        Args:
            dir: 
    """
    
    if dir.shape[1] == model.cfg.d_mlp:
        return dir @ model.W_out[0] # (feats, d_model)
    elif dir.shape[1] == model.cfg.d_model:
        return dir
    else:
        raise NotImplementedError("The hook your SAE was trained on isn't yet supported")

