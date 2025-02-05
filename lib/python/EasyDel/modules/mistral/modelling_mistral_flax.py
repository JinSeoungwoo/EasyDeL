import functools
import typing
from typing import Sequence

import fjformer.attention
import flax.core
from jax import numpy as jnp, Array, lax
from jax.experimental.shard_map import shard_map
from jax.sharding import PartitionSpec as PS
import jax
from flax import linen as nn
from flax.traverse_util import unflatten_dict, flatten_dict
from flax.core import freeze, unfreeze
from typing import Union, Optional, Tuple
from transformers import PretrainedConfig, FlaxPreTrainedModel
from flax.linen import partitioning as nn_partitioning, combine_masks
from transformers.modeling_flax_outputs import FlaxBaseModelOutput, FlaxCausalLMOutput

from ..flax_modelling_utils import (
    ACT2FN,
    with_sharding_constraint,
    get_gradient_checkpoint_policy,
    repeat_kv_bnsh,
    apply_rotary_pos_emb,
    precompute_freq_cis,
    JaxBaseClassModel,
    get_flash_attention,
    smart_flash_attention
)
import chex
from fjformer.bits import config as q_config, q_flax


class MistralConfig(PretrainedConfig, JaxBaseClassModel):
    def __init__(
            self,
            vocab_size=32000,
            hidden_size=4096,
            intermediate_size=14336,
            num_hidden_layers=32,
            num_attention_heads=32,
            num_key_value_heads=8,
            hidden_act="silu",
            max_position_embeddings=4096 * 32,
            initializer_range=0.02,
            rms_norm_eps=1e-6,
            use_cache=True,
            pad_token_id=None,
            bos_token_id=1,
            eos_token_id=2,
            tie_word_embeddings=False,
            rope_theta=10000.0,
            sliding_window=4096,
            gradient_checkpointing: str = 'nothing_saveable',
            use_pjit_attention_force: bool = False,
            use_flash_attention: bool = False,
            use_sacn_mlp: bool = False,
            flash_attn_query_chunk_size: int = 1024,
            flash_attn_key_chunk_size: int = 1024,
            scan_mlp_chunk_size: int = 1024,
            number_rep_kv: int = 1,
            attn_pdrop: float = 0.0,
            c_max_position_embeddings: int = 4096,
            freq_max_position_embeddings: int = 4096,
            bits: Optional[int] = None,
            axis_dims: Sequence[int] = (1, -1, 1, 1),
            axis_names: Sequence[str] = ("dp", "fsdp", "tp", "mp"),
            **kwargs,
    ):
        """
        The __init__ function is called when the class is instantiated.
        It allows the class to initialize the attributes of a class.
        The self parameter is a reference to the current instance of the class, and is used to access variables that belong to the class.

        :param self: Represent the instance of the class
        :param vocab_size: Define the size of the vocabulary
        :param hidden_size: Determine the size of the embedding layers
        :param intermediate_size: Define the size of the intermediate layer in each transformer block
        :param num_hidden_layers: Determine the number of layers in the encoder and decoder
        :param num_attention_heads: Determine the number of attention heads in each layer
        :param num_key_value_heads: Specify the number of heads for key and value
        :param hidden_act: Specify the activation function used in the hidden layers
        :param max_position_embeddings: Set the maximum length of the sequence
        :param initializer_range: Initialize the weights of the model
        :param rms_norm_eps: Avoid division by zero in the rms normalization
        :param use_cache: Determine whether to use the cache in the decoder
        :param pad_token_id: Specify the token id of the padding token
        :param bos_token_id: Specify the beginning of sentence token id
        :param eos_token_id: Specify the end of sentence token
        :param tie_word_embeddings: Tie the word embeddings and the output layer
        :param rope_theta: Control the number of tokens in a rope
        :param sliding_window: Control the number of tokens that are processed in parallel
        :param gradient_checkpointing: str: Specify whether to use gradient checkpointing
        :param use_pjit_attention_force: bool: Force the use of pjit attention
        :param use_flash_attention: bool: Enable the flash attention mechanism
        :param use_sacn_mlp: bool: Determine whether or not to use the scan_mlp function
        :param flash_attn_query_chunk_size: int: Determine the number of rows in each chunk
        :param flash_attn_key_chunk_size: int: Control the size of chunks that are used for the key matrix in flash attention
        :param scan_mlp_chunk_size: int: Specify the chunk size of the scan mlp
        :param number_rep_kv: int: Specify the number of times to repeat the key and value vectors
        :param attn_pdrop: float: Set the dropout rate for the attention layer
        :param c_max_position_embeddings: int: Set the maximum number of tokens in a sequence
        :param freq_max_position_embeddings: int: Set the maximum number of frequency bins that can be used in the model
        :param bits: Optional[int]: Specify the number of bits used for quantization
        :param axis_dims: Sequence[int]: Specify the dimension of each axis
        :param axis_names: Sequence[str]: Specify the names of each axis in the tensor
        :param &quot;fsdp&quot;: Specify the frequency dimension of the input
        :param &quot;tp&quot;: Determine the number of time-steps in the input sequence
        :param &quot;mp&quot;): Define the maximum position embeddings
        :param **kwargs: Pass a variable number of keyword arguments to a function
        :param : Define the number of layers in the model
        :return: An instance of the class
        
        """
        self.vocab_size = vocab_size
        self.max_position_embeddings = max_position_embeddings
        self.hidden_size = hidden_size
        self.intermediate_size = intermediate_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.sliding_window = sliding_window
        self.bits = bits
        # for backward compatibility
        if num_key_value_heads is None:
            num_key_value_heads = num_attention_heads

        self.num_key_value_heads = num_key_value_heads
        self.hidden_act = hidden_act
        self.initializer_range = initializer_range
        self.rms_norm_eps = rms_norm_eps
        self.use_cache = use_cache
        self.rope_theta = rope_theta
        self.use_flash_attention = use_flash_attention
        self.number_rep_kv = number_rep_kv
        self.gradient_checkpointing = gradient_checkpointing
        self.use_pjit_attention_force = use_pjit_attention_force
        self.use_sacn_mlp = use_sacn_mlp
        self.flash_attn_query_chunk_size = flash_attn_query_chunk_size
        self.flash_attn_key_chunk_size = flash_attn_key_chunk_size
        self.scan_mlp_chunk_size = scan_mlp_chunk_size
        self.attn_pdrop = attn_pdrop
        self.c_max_position_embeddings = c_max_position_embeddings
        self.freq_max_position_embeddings = freq_max_position_embeddings

        super().__init__(
            axis_names=axis_names,
            axis_dims=axis_dims,
            pad_token_id=pad_token_id,
            bos_token_id=bos_token_id,
            eos_token_id=eos_token_id,
            tie_word_embeddings=tie_word_embeddings,
            **kwargs,
        )

    @staticmethod
    def get_partition_rules(fully_fsdp: bool = True):
        """
        The get_partition_rules function is used to define the partitioning scheme for a model.
        It returns a list of tuples, where each tuple contains two elements:
          1) A regex string that matches the name of one or more parameters in the model.
          2) A PartitionScheme object that defines how those parameters should be partitioned.

        :param fully_fsdp: bool: Determine whether to use the fully_fsdp partitioning scheme or not
        :return: A list of tuples
        
        """
        return (

            ("model/embed_tokens/embedding", PS("tp", ("fsdp", "mp"))),

            ("self_attn/(q_proj|k_proj|v_proj)/kernel", PS(("fsdp", "mp"), "dp")),
            ("self_attn/o_proj/kernel", PS("tp", ("fsdp", "mp"))),

            ("mlp/gate_proj/kernel", PS(("fsdp", "mp"), "dp")),
            ("mlp/down_proj/kernel", PS("tp", ("fsdp", "mp"))),
            ("mlp/up_proj/kernel", PS(("fsdp", "mp"), "dp")),

            ("input_layernorm/kernel", PS(None)),
            ("post_attention_layernorm/kernel", PS(None)),

            ("model/norm/kernel", PS(None)),
            ("lm_head/kernel", PS(("fsdp", "mp"), "dp")),
            ('.*', PS(None)),
        ) if not fully_fsdp else (

            ("model/embed_tokens/embedding", PS(("fsdp", "mp"))),

            ("self_attn/(q_proj|k_proj|v_proj)/kernel", PS(("fsdp", "mp"))),
            ("self_attn/o_proj/kernel", PS(("fsdp", "mp"))),

            ("mlp/gate_proj/kernel", PS(("fsdp", "mp"))),
            ("mlp/down_proj/kernel", PS(("fsdp", "mp"))),
            ("mlp/up_proj/kernel", PS(("fsdp", "mp"))),

            ("input_layernorm/kernel", PS(None)),
            ("post_attention_layernorm/kernel", PS(None)),

            ("model/norm/kernel", PS(None)),
            ("lm_head/kernel", PS(("fsdp", "mp"))),
            ('.*', PS('fsdp')),
        )

    def add_jax_args(self,
                     gradient_checkpointing: str = 'nothing_saveable',
                     use_pjit_attention_force: bool = False,
                     use_flash_attention: bool = False,
                     use_sacn_mlp: bool = False,
                     flash_attn_query_chunk_size: int = 1024,
                     flash_attn_key_chunk_size: int = 1024,
                     scan_mlp_chunk_size: int = 1024,
                     number_rep_kv: int = 1,
                     attn_pdrop: float = 0.0,
                     c_max_position_embeddings: int = 4096,
                     freq_max_position_embeddings: int = None,
                     bits: Optional[int] = None,
                     axis_dims: Sequence[int] = (1, -1, 1, 1),
                     axis_names: Sequence[str] = ("dp", "fsdp", "tp", "mp"),
                     q_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
                     k_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
                     v_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
                     b_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), None, "tp", None),
                     a_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
                     backend: Optional[str] = None,
                     **kwargs,
                     ):
        """
        The add_jax_args function adds the following arguments to the model:

        :param self: Bind the attributes and methods of a class to an instance of that class
        :param gradient_checkpointing: str: Determine whether or not to use gradient checkpointing
        :param use_pjit_attention_force: bool: Determine whether to use the pjit_attention_force function
        :param use_flash_attention: bool: Determine if the flash attention module is used or not
        :param use_sacn_mlp: bool: Determine whether to use the scan_mlp function or not
        :param flash_attn_query_chunk_size: int: Specify the number of tokens that will be processed at a time
        :param flash_attn_key_chunk_size: int: Chunk the keys for flash attention
        :param scan_mlp_chunk_size: int: Chunk the input to the mlp
        :param number_rep_kv: int: Control the number of times that the key and value vectors are repeated
        :param attn_pdrop: float: Set the dropout rate for the attention layer
        :param c_max_position_embeddings: int: Set the maximum number of positional embeddings for the causal axis
        :param freq_max_position_embeddings: int: Set the maximum length of the frequency axis
        :param bits: Optional[int]: Specify the number of bits to use for quantization
        :param axis_dims: Sequence[int]: Specify the dimensions of each axis in the tensor
        :param axis_names: Sequence[str]: Name the axes of the tensors
        :param axis_dims: Sequence[int]: Specify the dimension of each axis
        :param axis_names: Sequence[str]: Name the axes of the tensor
        :param q_ps: jax.sharding.PartitionSpec: Specify the partitioning of the query tensor
        :param k_ps: jax.sharding.PartitionSpec: Partition the key matrix
        :param v_ps: jax.sharding.PartitionSpec: Specify the partitioning of the value tensor
        :param b_ps: jax.sharding.PartitionSpec: Specify the Attention Bias partition spec
        :param a_ps: jax.sharding.PartitionSpec: Specify the partitioning of the attention weights
        :param backend: typing.Optional[str]: backend to use for model
        :param : Enable gradient checkpointing
        :return: A tuple of the following:
        
        """
        self.use_flash_attention = use_flash_attention
        self.number_rep_kv = number_rep_kv
        self.gradient_checkpointing = gradient_checkpointing
        self.use_pjit_attention_force = use_pjit_attention_force
        self.use_sacn_mlp = use_sacn_mlp
        self.flash_attn_query_chunk_size = flash_attn_query_chunk_size
        self.flash_attn_key_chunk_size = flash_attn_key_chunk_size
        self.scan_mlp_chunk_size = scan_mlp_chunk_size
        self.attn_pdrop = attn_pdrop
        self.c_max_position_embeddings = c_max_position_embeddings
        self.freq_max_position_embeddings = freq_max_position_embeddings
        self.bits = bits
        self.axis_names = axis_names
        self.axis_dims = axis_dims
        self.q_ps = q_ps
        self.k_ps = k_ps
        self.v_ps = v_ps
        self.b_ps = b_ps
        self.a_ps = a_ps
        self.backend = backend

    @staticmethod
    def get_weight_decay_exclusions():
        return tuple()

    @staticmethod
    def rng_keys():
        return 'params', 'dropout', 'fcm'


re_mat = nn_partitioning.remat


def matmul_4d_loop(x, y):
    """Computes the matrix product of two 4D arrays x and y using a loop."""
    result = jnp.zeros(*x.shape[:-2] + x.shape[-2] + y.shape[-1])
    for i in range(x.shape[0]):
        for j in range(y.shape[1]):
            for key in range(x.shape[2]):
                for l in range(y.shape[3]):
                    result[i, j, key, l] += x[i, j, key, :] * y[key, l, :, :]
    return result


def _make_sliding_window_causal_mask(
        input_ids_shape,
        dtype: jnp.dtype,
        past_key_values_length: int = 0,
        sliding_window: int = 4096,
):
    """
    Make causal mask used for sliding window attention
    """
    bsz, tgt_len = input_ids_shape

    tensor = jnp.full(
        (tgt_len, tgt_len),
        fill_value=1,
    )
    mask = jnp.tril(tensor, 0)
    mask = jnp.triu(mask, -sliding_window)
    mask = jnp.log(mask).astype(dtype)

    if past_key_values_length > 0:
        mask = jnp.concatenate([jnp.zeros(tgt_len, past_key_values_length, dtype=dtype), mask], dim=-1)
    return mask[None, None, :, :].repeat(bsz, 0)


class MistralRMSNorm(nn.Module):
    dim: int
    eps: float = 1e-6
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16

    def setup(self) -> None:
        self.weight = self.param(
            'kernel',
            nn.initializers.ones,
            (self.dim,),
            self.param_dtype,
        )

    def _norm(self, x: jnp.ndarray) -> jnp.ndarray:
        return x * jax.lax.rsqrt(jnp.square(x).mean(-1, keepdims=True) + self.eps)

    def __call__(self, x: jnp.ndarray) -> jnp.ndarray:
        x = x.astype(jnp.promote_types(self.dtype, jnp.float32))
        output = self._norm(x).astype(self.dtype)
        weight = jnp.asarray(self.weight, self.dtype)
        return output * weight


class FlaxMistralRotaryEmbedding(nn.Module):
    dtype: jnp.dtype = jnp.float32

    def __call__(self, key, query, freq_cis, position_ids):
        sin, cos = freq_cis

        sin = sin[position_ids][:, None, :, :]
        cos = cos[position_ids][:, None, :, :]

        key = apply_rotary_pos_emb(key, sin, cos)
        query = apply_rotary_pos_emb(query, sin, cos)

        return query.astype(self.dtype), key.astype(self.dtype)


class FlaxMistralMLP(nn.Module):
    config: MistralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[None, jax.lax.Precision]] = jax.lax.Precision('fastest')

    def setup(self) -> None:
        if self.config.bits is not None:
            _dot_general_cls = q_config.fully_quantized(
                fwd_bits=self.config.bits,
                bwd_bits=self.config.bits
            )
        else:
            _dot_general_cls = None

        dot_general_cls = q_flax.QDotGeneral(_dot_general_cls)
        dense = functools.partial(
            nn.Dense,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision,
            kernel_init=nn.initializers.normal(),
            dot_general=dot_general_cls
        )
        self.gate_proj = dense(self.config.intermediate_size)
        self.up_proj = dense(self.config.intermediate_size)
        self.down_proj = dense(self.config.hidden_size)
        self.act_fn = ACT2FN[self.config.hidden_act]

    def __call__(self, x: chex.Array):
        return self.down_proj(self.act_fn(self.gate_proj(x)) * self.up_proj(x))


class FlaxMistralAttention(nn.Module):
    config: MistralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[None, jax.lax.Precision]] = jax.lax.Precision('fastest')

    def setup(self) -> None:
        config = self.config
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.hidden_size // self.num_heads
        self.num_key_value_heads = config.num_key_value_heads
        self.num_key_value_groups = self.num_heads // self.num_key_value_heads
        self.max_position_embeddings = config.max_position_embeddings
        if self.config.bits is not None:
            _dot_general_cls = q_config.fully_quantized(
                fwd_bits=self.config.bits,
                bwd_bits=self.config.bits
            )
        else:
            _dot_general_cls = None

        dot_general_cls = q_flax.QDotGeneral(_dot_general_cls)
        dense = functools.partial(
            nn.Dense,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision,
            kernel_init=nn.initializers.normal(),
            dot_general=dot_general_cls
        )

        self.q_proj = dense(self.num_heads * self.head_dim)
        self.k_proj = dense(self.num_key_value_heads * self.head_dim)
        self.v_proj = dense(self.num_key_value_heads * self.head_dim)
        self.o_proj = dense(self.hidden_size)
        self.rotary = FlaxMistralRotaryEmbedding(self.dtype)

    @nn.compact
    def concatenate_to_cache_(self, query: chex.Array, key: chex.Array, value: chex.Array, attention_mask: chex.Array):
        is_cache_available = self.has_variable('cache', 'key')
        key_cache = self.variable('cache', 'key', jnp.zeros, key.shape, key.dtype)
        value_cache = self.variable('cache', 'value', jnp.zeros, key.shape, value.dtype)
        index_cache = self.variable('cache', 'index', lambda: jnp.array(0, dtype=jnp.int32))
        if is_cache_available:
            *bd, ml, nh, dph = key_cache.value.shape
            indices = (0,) * len(bd) + (index_cache.value, 0, 0)
            key = jax.lax.dynamic_update_slice(key_cache.value, key, indices)
            value = jax.lax.dynamic_update_slice(value_cache.value, value, indices)
            key_cache.value = key
            value_cache.value = value
            num_updated_cache_vector = query.shape[1]
            index_cache.value = index_cache.value + num_updated_cache_vector
            pad_mask = jnp.broadcast_to(
                jnp.arange(ml) < index_cache.value,
                tuple(bd) + (1, num_updated_cache_vector, ml)
            )
            attention_mask = nn.combine_masks(pad_mask, attention_mask)
        return query, key, value, attention_mask

    @staticmethod
    def _t(query, key, value):
        return jnp.transpose(query, (0, 2, 1, 3)), jnp.transpose(key, (0, 2, 1, 3)), jnp.transpose(value, (0, 2, 1, 3))

    def t_rotary(self, batch_size, sequence_length, query, key, value, freq_cis, position_ids):
        query = query.reshape(batch_size, sequence_length, self.config.num_attention_heads, self.head_dim)
        key = key.reshape(batch_size, sequence_length, self.config.num_key_value_heads, self.head_dim)
        value = value.reshape(batch_size, sequence_length, self.config.num_key_value_heads, self.head_dim)

        query, key, value = self._t(query, key, value)
        query, key = self.rotary(position_ids=position_ids, query=query, key=key, freq_cis=freq_cis)
        key = repeat_kv_bnsh(key, self.num_key_value_groups)
        value = repeat_kv_bnsh(value, self.num_key_value_groups)
        return self._t(query, key, value)

    def __call__(
            self,
            hidden_state: chex.Array,
            freq_cis: chex.Array,
            attention_mask: chex.Array,
            causal_mask: chex.Array,
            position_ids: chex.Array,
            deterministic: bool = True,
            init_cache: bool = False,
            output_attentions: bool = True
    ):
        """
        The __call__ function is the main function of a JAX module.
        It defines how the module behaves when called as a function, and it's what you'll use to call your model in practice.
        The __call__ method takes an input tensor (x) and returns an output tensor (y).
        In this case, we're defining our model to be a simple linear layer with no activation: y = x @ w + b.

        :param self: Refer to the object itself
        :param hidden_state: chex.Array: Pass in the hidden state of the model
        :param freq_cis: chex.Array: Create the t_rotary variable
        :param attention_mask: chex.Array: Mask the attention weights
        :param causal_mask: chex.Array: Mask the attention weights
        :param position_ids: chex.Array: Specify the position of each token in a sequence
        :param deterministic: bool: Determine whether to use dropout or not
        :param init_cache: bool: Initialize the cache
        :param output_attentions: bool: Determine whether to return the attention weights
        :return: A tuple of (out, attn_output)
        
        """
        batch_size, sequence_length = hidden_state.shape[:2]
        query, key, value = self.q_proj(hidden_state), self.k_proj(hidden_state), self.v_proj(hidden_state)

        if self.config.use_pjit_attention_force:
            query = with_sharding_constraint(query, PS('fsdp', 'mp', None))
            key = with_sharding_constraint(key, PS('fsdp', 'mp', None))
            value = with_sharding_constraint(value, PS('fsdp', 'mp', None))
        query, key, value = self.t_rotary(
            batch_size=batch_size,
            sequence_length=sequence_length,
            query=query,
            key=key,
            value=value,
            freq_cis=freq_cis,
            position_ids=position_ids
        )
        if self.has_variable('cache', 'key') or init_cache:
            query, key, value, attention_mask = self.concatenate_to_cache_(query, key, value, attention_mask)

        q_l, k_l = query.shape[1], key.shape[1]
        if self.has_variable('cache', 'key'):
            mask_shift: int = self.variables['cache']['index']
            dl = self.variables['cache']['key'].shape[1]
            causal_mask = jax.lax.dynamic_slice(
                causal_mask, (0, 0, mask_shift, 0), (1, 1, q_l, dl)
            )
        else:
            causal_mask = causal_mask[:, :, :q_l, :k_l]
        dropout_rng = None
        if not deterministic and self.config.attn_pdrop > 0.0:
            dropout_rng = self.make_rng("dropout")
        causal_mask = jnp.broadcast_to(causal_mask, (batch_size,) + causal_mask.shape[1:])
        if attention_mask.ndim == 2:
            attention_mask = jnp.broadcast_to(jnp.expand_dims(attention_mask, axis=(-3, -2)), causal_mask.shape)

        attention_mask = nn.combine_masks(attention_mask, causal_mask)

        if self.config.use_flash_attention and not (self.has_variable("cache", "cached_key") or init_cache):

            if attention_mask.ndim == 2:
                attention_mask = jnp.expand_dims(attention_mask, axis=(-3, -2))

            if attention_mask.shape[1] != self.config.num_attention_heads:
                attention_mask = attention_mask.repeat(self.config.num_attention_heads, 1, )
            attention_bias = lax.select(
                attention_mask > 0,
                jnp.full(attention_mask.shape, 0.0).astype(self.dtype),
                jnp.full(attention_mask.shape, jnp.finfo(self.dtype).min).astype(self.dtype),
            )
            attn_weights = None
            rtp_axis = (0, 2, 1, 3)
            attn_output = smart_flash_attention(
                q=jnp.transpose(query, rtp_axis),
                k=jnp.transpose(key, rtp_axis),
                v=jnp.transpose(value, rtp_axis),
                q_ps=self.config.q_ps,
                k_ps=self.config.k_ps,
                v_ps=self.config.v_ps,
                b_ps=self.config.b_ps,
                a_ps=self.config.a_ps,
                bias=attention_bias,
                block_q=self.config.flash_attn_query_chunk_size,
                block_k=self.config.flash_attn_key_chunk_size,
                block_b=1,
                num_attention_heads=self.config.num_attention_heads,
                precision=self.precision,
                dtype=self.dtype,
                causal=False,
                mesh=self.config.jax_mesh(),
                dropout_rng=dropout_rng,
                deterministic=deterministic,
                q_seq_len=q_l,
                kv_seq_len=k_l,
                attn_pdrop=self.config.attn_pdrop,
                head_dims=self.head_dim,
                force_float32_tpu=True
            )
            attn_output = jnp.transpose(attn_output, rtp_axis)
        else:
            query_length, key_length = query.shape[1], key.shape[1]

            if self.has_variable("cache", "cached_key"):
                mask_shift = self.variables["cache"]["cache_index"]
                max_decoder_length = self.variables["cache"]["cached_key"].shape[1]
                causal_mask = lax.dynamic_slice(
                    causal_mask, (0, 0, mask_shift, 0), (1, 1, query_length, max_decoder_length)
                )
            else:
                causal_mask = causal_mask[:, :, :query_length, :key_length]

            batch_size = hidden_state.shape[0]
            causal_mask = jnp.broadcast_to(causal_mask, (batch_size,) + causal_mask.shape[1:])
            if attention_mask.ndim == 2:
                attention_mask = jnp.broadcast_to(jnp.expand_dims(attention_mask, axis=(-3, -2)), causal_mask.shape)
            attention_mask = combine_masks(attention_mask, causal_mask)

            if self.has_variable("cache", "cached_key") or init_cache:
                key, value, attention_mask = self._concatenate_to_cache(key, value, query,
                                                                        attention_mask)

            attn_weights = None
            ring_attention_sharded = shard_map(
                functools.partial(fjformer.attention.ring_attention_standard, axis_name="mp"),
                mesh=self.config.jax_mesh(),
                in_specs=(
                    self.config.q_ps,
                    self.config.k_ps,
                    self.config.v_ps,
                    self.config.b_ps
                ),
                out_specs=self.config.a_ps,
                check_rep=False
            )

            attn_output = ring_attention_sharded(
                query, key, value, attention_mask
            )
            attn_output = with_sharding_constraint(attn_output, self.config.a_ps)

        out = self.o_proj(attn_output.reshape(batch_size, sequence_length, self.hidden_size))
        outputs = (out, attn_output) if output_attentions else (out,)
        return outputs


class FlaxMistralDecoderLayer(nn.Module):
    config: MistralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[None, jax.lax.Precision]] = jax.lax.Precision('fastest')

    def setup(self) -> None:
        self.self_attn = FlaxMistralAttention(
            config=self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.mlp = FlaxMistralMLP(
            config=self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.input_layernorm = MistralRMSNorm(
            dim=self.config.hidden_size,
            eps=self.config.rms_norm_eps,
            dtype=self.dtype,
            param_dtype=self.param_dtype
        )
        self.post_attention_layernorm = MistralRMSNorm(
            dim=self.config.hidden_size,
            eps=self.config.rms_norm_eps,
            dtype=self.dtype,
            param_dtype=self.param_dtype
        )

    def __call__(
            self,
            hidden_state: chex.Array,
            freq_cis: chex.Array,
            attention_mask: chex.Array,
            causal_mask: chex.Array,
            position_ids: chex.Array,
            deterministic: bool = True,
            init_cache: bool = False,
            output_attentions: bool = True
    ):
        """
        The __call__ function is the main function of a TransformerEncoderLayer.
        It takes in the following arguments:
            hidden_state (chex.Array): The input to the encoder layer, which is also its output after being processed by all sublayers.
            freq_cis (chex.Array): A tensor containing frequency-domain representations of each token's context vector, used for computing self-attention weights and biases in a more efficient manner than using position embeddings or sinusoidal positional encoding vectors would allow for [2]. This tensor has shape `(batch_size, num

        :param self: Represent the instance of the class
        :param hidden_state: chex.Array: Represent the input to the encoder layer
        :param freq_cis: chex.Array: Pass the frequency information to the attention layer
        :param attention_mask: chex.Array: Mask out the attention weights for certain positions
        :param causal_mask: chex.Array: Mask the future tokens
        :param position_ids: chex.Array: Indicate the position of each token in the sequence
        :param deterministic: bool: Determine whether to use dropout or not
        :param init_cache: bool: Initialize the cache for the self-attention layer
        :param output_attentions: bool: Determine whether to return the attention weights or not
        :return: A tuple of hidden_state and attention_output
        
        """
        residual = hidden_state
        attention_output = self.self_attn(
            hidden_state=self.input_layernorm(hidden_state),
            freq_cis=freq_cis,
            attention_mask=attention_mask,
            causal_mask=causal_mask,
            position_ids=position_ids,
            deterministic=deterministic,
            init_cache=init_cache,
            output_attentions=output_attentions
        )

        hidden_state = attention_output[0] + residual

        hidden_state = self.mlp(self.post_attention_layernorm(hidden_state)) + hidden_state
        outputs = (hidden_state,)
        if output_attentions:
            outputs += attention_output[1]
        return outputs


class FlaxMistralPretrainedModel(FlaxPreTrainedModel):
    config_class = MistralConfig
    base_model_prefix = 'mistral'
    module_class: nn.Module = None

    def __init__(self,
                 config: MistralConfig,
                 input_shape: Tuple = (1, 1),
                 seed: int = 0,
                 dtype: jnp.dtype = jnp.bfloat16,
                 _do_init: bool = True,
                 **kwargs
                 ):
        module = self.module_class(config=config, dtype=dtype, **kwargs)
        super().__init__(config, module, input_shape=input_shape, seed=seed, dtype=dtype, _do_init=_do_init)

    def init_weights(
            self,
            rng: jax.random.PRNGKey,
            input_shape: Tuple,
            params: flax.core.FrozenDict = None
    ) -> flax.core.FrozenDict:

        """
        The init_weights function is used to initialize the weights of a model.
        It takes in an rng, which is a random number generator key that can be used to generate random numbers.
        The input_shape parameter specifies the shape of the inputs that will be fed into this model.
        The params parameter allows you to pass in pre-trained weights for your model, if you have them available.

        :param self: Access variables that belong to the class
        :param rng: jax.random.PRNGKey: Initialize the weights of the model
        :param input_shape: Tuple: Initialize the input_ids, attention_mask and position_ids
        :param params: flax.core.FrozenDict: Pass in the parameters of a pre-trained model
        :return: A frozendict of parameters
        
        """
        input_ids = jnp.zeros(input_shape, dtype="i4")
        attention_mask = jnp.ones_like(input_ids)
        position_ids = jnp.broadcast_to(jnp.arange(jnp.atleast_2d(input_ids).shape[-1]), input_shape)
        params_rng, dropout_rng = jax.random.split(rng)
        rng_s = {"params": params_rng, "dropout": dropout_rng}

        if self.config.add_cross_attention:
            encoder_hidden_states = jnp.zeros(input_shape + (self.config.hidden_size,))
            encoder_attention_mask = attention_mask
            module_init_outputs = self.module.init(
                rng_s,
                input_ids,
                attention_mask,
                position_ids,
                encoder_hidden_states,
                encoder_attention_mask,
                return_dict=False,
            )
        else:
            module_init_outputs = self.module.init(rng_s, input_ids, attention_mask, position_ids, return_dict=False)

        random_params = module_init_outputs["params"]

        if params is not None:
            random_params = flatten_dict(unfreeze(random_params))
            params = flatten_dict(unfreeze(params))
            for missing_key in self._missing_keys:
                params[missing_key] = random_params[missing_key]
            self._missing_keys = set()
            return freeze(unflatten_dict(params))
        else:
            return random_params

    def init_cache(self, batch_size, max_length):

        input_ids = jnp.ones((batch_size, max_length))
        attention_mask = jnp.ones_like(input_ids)
        position_ids = jnp.broadcast_to(jnp.arange(jnp.atleast_2d(input_ids).shape[-1]), input_ids.shape)

        init_variables = self.module.init(
            jax.random.PRNGKey(0), input_ids, attention_mask, position_ids, return_dict=False, init_cache=True
        )
        return init_variables["cache"]

    def __call__(
            self,
            input_ids,
            attention_mask=None,
            position_ids=None,
            params: dict = None,
            past_key_values: dict = None,
            dropout_rng: jax.random.PRNGKey = None,
            train: bool = False,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
            add_params_field: bool = False
    ):
        """
        The __call__ function is the main function of a JAX module.
        It takes as input:
        - The parameters of the model (self.params)
        - The inputs to the model (input_ids, attention_mask, position_ids)
        - Whether we are training (train=True/False) and whether we want to return all hidden states and
        attentions weights at each layer in addition to just the last layer output (output_hidden_states=True/False).

        :param self: Represent the instance of the class
        :param input_ids: Pass the input sequence to the model
        :param attention_mask: Mask out the padding tokens
        :param position_ids: Specify the position of each token in the sequence
        :param params: dict: Pass in the parameters of the model
        :param past_key_values: dict: Pass the past key values to the model
        :param dropout_rng: jax.random.PRNGKey: Pass in a random number generator key to the model
        :param train: bool: Determine whether to use dropout or not
        :param output_attentions: Optional[bool]: Determine whether to return the attention weights
        :param output_hidden_states: Optional[bool]: Determine whether to return the hidden states of all layers
        :param return_dict: Optional[bool]: Return a dictionary of the outputs
        :param add_params_field: bool: Add a params field to the inputs dictionary
        :return: A tuple of (last_hidden_state, past_key_values)
        
        """
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.return_dict

        batch_size, sequence_length = input_ids.shape

        if position_ids is None:
            if past_key_values is not None:
                raise ValueError("Make sure to provide `position_ids` when passing `past_key_values`.")

            position_ids = jnp.broadcast_to(jnp.arange(sequence_length)[None, :], (batch_size, sequence_length))

        if attention_mask is None:
            attention_mask = jnp.ones((batch_size, sequence_length))

        rng_s = {}
        if dropout_rng is not None:
            rng_s["dropout"] = dropout_rng

        inputs = {"params": params or self.params} if add_params_field else params or self.params
        rng_s['params'] = jax.random.key(0)
        if past_key_values:
            inputs["cache"] = past_key_values
            mutable = ["cache"]
        else:
            mutable = False

        outputs = self.module.apply(
            inputs,
            jnp.array(input_ids, dtype="i4"),
            jnp.array(attention_mask, dtype="i4"),
            jnp.array(position_ids, dtype="i4"),
            not train,
            None,
            False,
            output_attentions,
            output_hidden_states,
            return_dict,
            rngs=rng_s,
            mutable=mutable,
        )

        if past_key_values is not None and return_dict:
            outputs, past_key_values = outputs
            outputs["past_key_values"] = unfreeze(past_key_values["cache"])
            return outputs
        elif past_key_values is not None and not return_dict:
            outputs, past_key_values = outputs
            outputs = outputs[:1] + (unfreeze(past_key_values["cache"]),) + outputs[1:]

        return outputs


class FlaxMistralDecoratorCollection(nn.Module):
    config: MistralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[None, jax.lax.Precision]] = jax.lax.Precision('fastest')

    def setup(self) -> None:
        block = FlaxMistralDecoderLayer
        if self.config.gradient_checkpointing != '':
            block = re_mat(
                block,
                static_argnums=(5, 6, 7),
                policy=get_gradient_checkpoint_policy(
                    self.config.gradient_checkpointing
                )
            )
        self.layers = [
            block(
                config=self.config,
                dtype=self.dtype,
                param_dtype=self.param_dtype,
                precision=self.precision,
                name=str(i)
            ) for i in range(self.config.num_hidden_layers)
        ]

    def __call__(
            self,
            hidden_state: chex.Array,
            freq_cis: chex.Array,
            attention_mask: chex.Array,
            causal_mask: chex.Array,
            position_ids: chex.Array,
            deterministic: bool = True,
            init_cache: bool = False,
            output_attentions: bool = False,
            output_hidden_states: bool = False
    ):
        all_attentions = () if output_attentions else None
        all_hidden_states = () if output_hidden_states else None
        for layer in self.layers:
            if output_hidden_states:
                all_hidden_states += (hidden_state,)
            output = layer(
                hidden_state,
                freq_cis,
                attention_mask,
                causal_mask,
                position_ids,
                deterministic,
                init_cache,
                output_attentions
            )
            hidden_state = output[0]

            if output_attentions:
                output_attentions += (output[1],)

        return hidden_state, all_hidden_states, all_attentions


class FlaxMistralModule(nn.Module):
    config: MistralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[jax.lax.Precision, str]] = None

    def setup(self):

        self.embed_tokens = nn.Embed(
            self.config.vocab_size,
            self.config.hidden_size,
            embedding_init=jax.nn.initializers.normal(stddev=self.config.initializer_range),
            dtype=self.dtype,
            param_dtype=self.param_dtype,
        )

        self.layers = FlaxMistralDecoratorCollection(
            self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.norm = MistralRMSNorm(
            self.config.hidden_size,
            eps=self.config.rms_norm_eps,
            dtype=self.dtype,
            param_dtype=self.param_dtype
        )

        self.freq_cis = precompute_freq_cis(
            max_position_embedding=self.config.freq_max_position_embeddings if self.config.freq_max_position_embeddings is not None else self.config.max_position_embeddings,
            head_dim=self.config.hidden_size // self.config.num_attention_heads
        )
        self.causal_mask = nn.make_causal_mask(jnp.ones((1, self.config.c_max_position_embeddings), dtype='i4'))

    def __call__(
            self,
            input_ids: chex.Array,
            attention_mask: chex.Array,
            position_ids: chex.Array,
            deterministic: bool = True,
            input_embeds: chex.Array = None,
            init_cache: bool = False,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
            return_dict: bool = True,

    ) -> typing.Union[Tuple[Array, ...], FlaxBaseModelOutput]:
        """
        The __call__ function is the main function of a Flax model.
        It takes in input_ids, attention_mask, and position_ids as inputs to the model.
        The output is a tuple containing: last hidden state (hidden states), all hidden states (if output_hidden_states=True), attentions (if output attentions=True).


        :param self: Represent the instance of the class
        :param input_ids: chex.Array: Pass in the input ids
        :param attention_mask: chex.Array: Mask out the attention weights for certain tokens
        :param position_ids: chex.Array: Determine the position of each token in a sequence
        :param deterministic: bool: Determine whether to use dropout or not
        :param input_embeds: chex.Array: Pass in the embedding of the input_ids
        :param init_cache: bool: Initialize the cache for the decoder
        :param output_attentions: bool: Determine whether to return the attention weights or not
        :param output_hidden_states: bool: Return all hidden states or just the last one
        :param return_dict: bool: Return a dictionary of the outputs or not
        :param : Determine whether the model is in training mode or not
        :return: A tuple of the hidden states, all hidden states, and attentions
        
        """
        if input_embeds is None:
            input_embeds = self.embed_tokens(input_ids.astype("i4"))
        if attention_mask.ndim == 2:
            b, s = attention_mask.shape
            attention_mask = attention_mask.reshape(b, 1, 1, s)

        outputs = self.layers(
            hidden_state=input_embeds,
            attention_mask=attention_mask,
            position_ids=position_ids,
            freq_cis=self.freq_cis,
            init_cache=init_cache,
            output_attentions=output_attentions,
            deterministic=deterministic,
            causal_mask=self.causal_mask
        )

        hidden_states = outputs[0]
        hidden_states = self.norm(hidden_states)

        if output_hidden_states:
            all_hidden_states = outputs[1] + (hidden_states,)
            outputs = (hidden_states, all_hidden_states) + outputs[2:]
        else:
            outputs = (hidden_states,) + outputs[1:]

        if not return_dict:
            return tuple(value for value in outputs if value is not None)

        return FlaxBaseModelOutput(
            last_hidden_state=hidden_states,
            hidden_states=outputs[1],
            attentions=outputs[-1],
        )


class FlaxMistralModel(FlaxMistralPretrainedModel):
    module_class = FlaxMistralModule


class FlaxMistralForCausalLMModule(nn.Module):
    config: MistralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[jax.lax.Precision, str]] = None

    def setup(self):
        self.model: FlaxMistralModule = FlaxMistralModule(
            self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision,
        )

        if self.config.bits is not None:
            _dot_general_cls = q_config.fully_quantized(
                fwd_bits=self.config.bits,
                bwd_bits=self.config.bits
            )
        else:
            _dot_general_cls = None

        dot_general_cls = q_flax.QDotGeneral(_dot_general_cls)
        self.lm_head = nn.Dense(
            self.config.vocab_size,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            use_bias=False,
            kernel_init=jax.nn.initializers.normal(stddev=self.config.initializer_range),
            precision=self.precision,
            dot_general=dot_general_cls
        )

    def __call__(
            self,
            input_ids: chex.Array,
            attention_mask: chex.Array,
            position_ids: chex.Array,
            deterministic: bool = True,
            input_embeds: chex.Array = None,
            init_cache: bool = False,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
            return_dict: bool = True,
    ):
        """
            The __call__ function is the main function of a Flax module. It defines how the model will be called,
            and what it returns. In this case, we are calling our Transformer model with input_ids and attention_mask
            as inputs (these are defined in __init__). We also have some optional arguments that can be passed to
            the call function: deterministic (whether to use dropout), input_embeds (if you want to pass your own embeddings),
            output_attentions and output_hidden states which return additional outputs from the transformer layers if set True. Finally,

            :param self: Refer to the object itself
            :param input_ids: chex.Array: Pass in the input tokens
            :param attention_mask: chex.Array: Mask out the padding tokens
            :param position_ids: chex.Array: Specify the position of each token in the sequence
            :param deterministic: bool: Determine whether to use dropout in the model
            :param input_embeds: chex.Array: Pass in the embeddings of the input tokens
            :param init_cache: bool: Initialize the cache for the decoder
            :param output_attentions: bool: Return the attention weights
            :param output_hidden_states: bool: Return the hidden states of all layers
            :param return_dict: bool: Return a dictionary of the outputs or just the logits
            :param : Determine whether to return the logits or not
            :return: A tuple of (lm_logits, hidden_states, attentions)
            
        """
        batch_size, seq_length = input_ids.shape

        if attention_mask is None: attention_mask = jnp.ones_like(input_ids)
        if position_ids is None:
            position_ids = jnp.broadcast_to(
                jnp.clip(jnp.cumsum(attention_mask, axis=-1) - 1, a_min=0),
                (batch_size, seq_length)
            )
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            deterministic=deterministic,
            input_embeds=input_embeds,
            init_cache=init_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict
        )

        hidden_states = outputs[0]

        if self.config.tie_word_embeddings:
            shared_kernel = self.transformer.variables["params"]["wte"]["embedding"].T
            lm_logits = self.lm_head.apply({"params": {"kernel": shared_kernel}}, hidden_states)
        else:
            lm_logits = self.lm_head(hidden_states)

        # lm_logits = lm_logits.astype(jnp.float32)

        if not return_dict:
            return (lm_logits,) + outputs[1:]

        return FlaxCausalLMOutput(logits=lm_logits, hidden_states=outputs.hidden_states, attentions=outputs.attentions)


class FlaxMistralForCausalLM(FlaxMistralPretrainedModel):
    module_class = FlaxMistralForCausalLMModule

    def prepare_inputs_for_generation(self, input_ids, max_length, attention_mask: Optional[chex.Array] = None):
        batch_size, seq_length = input_ids.shape

        past_key_values = self.init_cache(batch_size, max_length)
        extended_attention_mask = jnp.ones((batch_size, max_length), dtype="i4")
        if attention_mask is not None:
            position_ids = attention_mask.cumsum(axis=-1) - 1
            extended_attention_mask = jax.lax.dynamic_update_slice(extended_attention_mask, attention_mask, (0, 0))
        else:
            position_ids = jnp.broadcast_to(jnp.arange(seq_length, dtype="i4")[None, :], (batch_size, seq_length))

        return {
            "past_key_values": past_key_values,
            "attention_mask": extended_attention_mask,
            "position_ids": position_ids,
        }

    @staticmethod
    def update_inputs_for_generation(model_outputs, model_kwargs):
        model_kwargs["past_key_values"] = model_outputs.past_key_values
        model_kwargs["position_ids"] = model_kwargs["position_ids"][:, -1:] + 1
        return model_kwargs
