# Note
# the GPT-j implemented by Huggingface is not supporting Partition spec s and using
# fully with pjit and required creating
# parameters even if you want to load already trained model so this one is the same
# but include those bugs / non-features fixed


# coding=utf-8
# Copyright 2021 The EleutherAI and HuggingFace Teams. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" GPT-J model configuration"""
from collections import OrderedDict
from typing import Any, List, Mapping, Sequence

from functools import partial
from typing import Optional, Tuple

from einops import einops
from fjformer import with_sharding_constraint
from jax.sharding import PartitionSpec
import flax.linen as nn
import jax
import jax.numpy as jnp
import numpy as np
from flax.core.frozen_dict import FrozenDict, unfreeze
from flax.linen import combine_masks, make_causal_mask
from flax.linen.attention import dot_product_attention_weights
from jax import lax

from transformers.modeling_flax_outputs import FlaxBaseModelOutput, FlaxCausalLMOutput
from transformers.modeling_flax_utils import ACT2FN, FlaxPreTrainedModel
from transformers.utils import logging

from transformers import PreTrainedTokenizer, TensorType, is_torch_available
from transformers.configuration_utils import PretrainedConfig
from transformers.onnx import OnnxConfigWithPast, PatchingSpec

from fjformer.attention import efficient_attention
from ..flax_modelling_utils import with_sharding_constraint, JaxBaseClassModel
import chex
from fjformer.bits import config as q_config, q_flax

logger = logging.get_logger(__name__)


class GPTJConfig(PretrainedConfig, JaxBaseClassModel):
    model_type = "gptj"
    attribute_map = {
        "max_position_embeddings": "n_positions",
        "hidden_size": "n_embd",
        "num_attention_heads": "n_head",
        "num_hidden_layers": "n_layer",
    }

    def __init__(
            self,
            vocab_size: int = 50400,
            n_positions: int = 2048,
            n_embd: int = 4096,
            n_layer: int = 28,
            n_head: int = 16,
            rotary_dim: int = 64,
            n_inner: int = None,
            activation_function: str = "gelu_new",
            resid_pdrop: float = 0.0,
            embd_pdrop: float = 0.0,
            attn_pdrop: float = 0.0,
            layer_norm_epsilon: float = 1e-5,
            initializer_range: int = 0.02,
            use_cache: int = True,
            bos_token_id: int = 50256,
            eos_token_id: int = 50256,
            tie_word_embeddings: bool = False,
            use_pjit_attention_force: bool = False,
            use_flash_attention: bool = False,
            flash_attn_query_chunk_size: int = 1024,
            flash_attn_key_chunk_size: int = 2048,
            bits: Optional[int] = None,
            axis_dims: Sequence[int] = (1, -1, 1, 1),
            axis_names: Sequence[str] = ("dp", "fsdp", "tp", "mp"),
            **kwargs,
    ):
        self.bits = bits
        self.vocab_size = vocab_size
        self.n_positions = n_positions
        self.n_embd = n_embd
        self.n_layer = n_layer
        self.n_head = n_head
        self.n_inner = n_inner
        self.rotary_dim = rotary_dim
        self.activation_function = activation_function
        self.resid_pdrop = resid_pdrop
        self.embd_pdrop = embd_pdrop
        self.attn_pdrop = attn_pdrop
        self.layer_norm_epsilon = layer_norm_epsilon
        self.initializer_range = initializer_range
        self.use_cache = use_cache
        self.use_pjit_attention_force = use_pjit_attention_force
        self.bos_token_id = bos_token_id
        self.eos_token_id = eos_token_id
        self.flash_attn_query_chunk_size = flash_attn_query_chunk_size
        self.flash_attn_key_chunk_size = flash_attn_key_chunk_size
        self.use_flash_attention = use_flash_attention
        self.from_pt = False
        super().__init__(
            axis_names=axis_names,
            axis_dims=axis_dims,
            bos_token_id=bos_token_id,
            eos_token_id=eos_token_id,
            tie_word_embeddings=tie_word_embeddings,
            **kwargs
        )

    @staticmethod
    def set_custom_partition(embedding_partition: PartitionSpec,
                             kvq_partition: PartitionSpec,
                             o_proj_partition: PartitionSpec,
                             fc_out_partition: PartitionSpec,
                             fc_in_partition: PartitionSpec,
                             fc_lm_head_partition: PartitionSpec,
                             rest_partitions: PartitionSpec = PartitionSpec(None)
                             ):
        return (
            ("model/wte/embedding", embedding_partition),

            ("attn/(k_proj|v_proj|q_proj)/kernel", kvq_partition),
            ("attn/out_proj/kernel", o_proj_partition),

            ("mlp/fc_out/kernel", fc_out_partition),
            ("mlp/fc_out/bias", fc_out_partition),

            ("mlp/fc_in/kernel", fc_in_partition),
            ("mlp/fc_in/bias", fc_in_partition),

            ("lm_head/kernel", fc_lm_head_partition),
            ("lm_head/bias", fc_lm_head_partition),
            ('.*', rest_partitions),
        )

    @staticmethod
    def get_partition_rules(just_fsdp: bool = True):
        if just_fsdp:
            rules = (
                ("model/wte/embedding", PartitionSpec(("fsdp", "mp"), )),

                ("attn/(k_proj|v_proj|q_proj)/kernel", PartitionSpec(("fsdp", "mp"), )),
                ("attn/out_proj/kernel", PartitionSpec(("fsdp", "mp"), )),

                ("mlp/fc_out/kernel", PartitionSpec(("fsdp", "mp"), )),
                ("mlp/fc_out/bias", PartitionSpec(("fsdp", "mp"), )),

                ("mlp/fc_in/kernel", PartitionSpec(("fsdp", "mp"), )),
                ("mlp/fc_in/bias", PartitionSpec(("fsdp", "mp"), )),

                ("lm_head/kernel", PartitionSpec(("fsdp", "mp"), )),
                ("lm_head/bias", PartitionSpec(("fsdp", "mp"), )),
                ('.*', PartitionSpec(None)),
            )
        else:
            rules = (
                ("model/wte/embedding", PartitionSpec('tp', ("fsdp", "mp"))),

                ("attn/(k_proj|v_proj|q_proj)/kernel", PartitionSpec(("fsdp", "mp"), 'tp')),
                ("attn/out_proj/kernel", PartitionSpec('tp', ("fsdp", "mp"), )),

                ("mlp/fc_out/kernel", PartitionSpec(("fsdp", "mp"), 'tp')),
                ("mlp/fc_out/bias", PartitionSpec(("fsdp", "mp"), 'tp')),

                ("mlp/fc_in/kernel", PartitionSpec('tp', ("fsdp", "mp"), )),
                ("mlp/fc_in/bias", PartitionSpec('tp', ("fsdp", "mp"), )),

                ("lm_head/kernel", PartitionSpec('tp', ("fsdp", "mp"), )),
                ("lm_head/bias", PartitionSpec('tp', ("fsdp", "mp"), )),
                ('.*', PartitionSpec(None)),
            )
        return rules

    @staticmethod
    def get_mesh_names():
        return "dp", "fsdp", "tp", "mp"

    def add_jax_args(
            self,
            vocab_size: int = 50400,
            n_positions: int = 2048,
            n_embd: int = 4096,
            n_layer: int = 28,
            n_head: int = 16,
            rotary_dim: int = 64,
            n_inner: int = None,
            activation_function: str = "gelu_new",
            resid_pdrop: float = 0.0,
            embd_pdrop: float = 0.0,
            attn_pdrop: float = 0.0,
            layer_norm_epsilon: float = 1e-5,
            initializer_range: int = 0.02,
            use_cache: int = True,
            bos_token_id: int = 50256,
            eos_token_id: int = 50256,
            tie_word_embeddings: bool = False,
            use_pjit_attention_force: bool = False,
            use_flash_attention: bool = False,
            flash_attn_query_chunk_size: int = 1024,
            flash_attn_key_chunk_size: int = 2048,
            bits: Optional[int] = None,
            axis_dims: Sequence[int] = (1, -1, 1, 1),
            axis_names: Sequence[str] = ("dp", "fsdp", "tp", "mp"),
            q_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
            k_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
            v_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
            b_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec("dp", None, ("dp", "fsdp"), None),
            a_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
            backend: Optional[str] = None,
            **kwargs,
    ):
        self.axis_names = axis_names
        self.axis_dims = axis_dims
        self.q_ps = q_ps
        self.k_ps = k_ps
        self.v_ps = v_ps
        self.b_ps = b_ps
        self.a_ps = a_ps
        self.backend = backend
        self.axis_names = axis_names
        basics = dict(
            bits=bits,
            vocab_size=vocab_size,
            n_positions=n_positions,
            n_embd=n_embd,
            n_layer=n_layer,
            n_head=n_head,
            rotary_dim=rotary_dim,
            n_inner=n_inner,
            activation_function=activation_function,
            resid_pdrop=resid_pdrop,
            embd_pdrop=embd_pdrop,
            attn_pdrop=attn_pdrop,
            layer_norm_epsilon=layer_norm_epsilon,
            initializer_range=initializer_range,
            use_cache=use_cache,
            bos_token_id=bos_token_id,
            eos_token_id=eos_token_id,
            tie_word_embeddings=tie_word_embeddings,
            use_pjit_attention_force=use_pjit_attention_force,
            use_flash_attention=use_flash_attention,
            flash_attn_query_chunk_size=flash_attn_query_chunk_size,
            flash_attn_key_chunk_size=flash_attn_key_chunk_size,
        )

        for k, v in basics.items():
            if not hasattr(self, k):
                setattr(self, k, v)
        self.from_pt = False
        return self


class GPTJOnnxConfig(OnnxConfigWithPast):
    def __init__(
            self,
            config: PretrainedConfig,
            task: str = "default",
            patching_specs: List[PatchingSpec] = None,
            use_past: bool = False,
    ):
        super().__init__(config, task=task, patching_specs=patching_specs, use_past=use_past)
        if not getattr(self._config, "pad_token_id", None):
            self._config.pad_token_id = 0

    @property
    def inputs(self) -> Mapping[str, Mapping[int, str]]:
        common_inputs = OrderedDict({"input_ids": {0: "batch", 1: "sequence"}})
        if self.use_past:
            self.fill_with_past_key_values_(common_inputs, direction="inputs")
            common_inputs["attention_mask"] = {0: "batch", 1: "past_sequence + sequence"}
        else:
            common_inputs["attention_mask"] = {0: "batch", 1: "sequence"}

        return common_inputs

    @property
    def num_layers(self) -> int:
        return self._config.n_layer

    @property
    def num_attention_heads(self) -> int:
        return self._config.n_head

    def generate_dummy_inputs(
            self,
            tokenizer: PreTrainedTokenizer,
            batch_size: int = -1,
            seq_length: int = -1,
            is_pair: bool = False,
            framework: Optional[TensorType] = None,
    ) -> Mapping[str, Any]:
        common_inputs = super(OnnxConfigWithPast, self).generate_dummy_inputs(
            tokenizer, batch_size=batch_size, seq_length=seq_length, is_pair=is_pair, framework=framework
        )

        # We need to order the input in the way they appears in the forward()
        ordered_inputs = OrderedDict({"input_ids": common_inputs["input_ids"]})

        # Need to add the past_keys
        if self.use_past:
            if not is_torch_available():
                raise ValueError("Cannot generate dummy past_keys inputs without PyTorch installed.")
            else:
                import torch

                batch, seqlen = common_inputs["input_ids"].shape
                # Not using the same length for past_key_values
                past_key_values_length = seqlen + 2
                past_shape = (
                    batch,
                    self.num_attention_heads,
                    past_key_values_length,
                    self._config.hidden_size // self.num_attention_heads,
                )
                ordered_inputs["past_key_values"] = [
                    (torch.zeros(past_shape), torch.zeros(past_shape)) for _ in range(self.num_layers)
                ]

        ordered_inputs["attention_mask"] = common_inputs["attention_mask"]
        if self.use_past:
            mask_dtype = ordered_inputs["attention_mask"].dtype
            ordered_inputs["attention_mask"] = torch.cat(
                [ordered_inputs["attention_mask"], torch.ones(batch, past_key_values_length, dtype=mask_dtype)], dim=1
            )

        return ordered_inputs

    @property
    def default_onnx_opset(self) -> int:
        return 13


logger = logging.get_logger(__name__)


def create_sinusoidal_positions(num_pos, dim):
    inv_freq = 1.0 / (10000 ** (np.arange(0, dim, 2) / dim))
    sinusoid_inp = np.einsum("i , j -> i j", np.arange(num_pos), inv_freq).astype("float32")
    sin, cos = np.sin(sinusoid_inp), np.cos(sinusoid_inp)

    sentinel = dim // 2 + dim % 2
    out = np.zeros((num_pos, dim))
    out[:, 0:sentinel] = sin
    out[:, sentinel:] = cos

    return jnp.array(out)


def rotate_every_two(tensor):
    rotate_half_tensor = jnp.stack((-tensor[:, :, :, 1::2], tensor[:, :, :, ::2]), axis=-1)
    rotate_half_tensor = rotate_half_tensor.reshape(rotate_half_tensor.shape[:-2] + (-1,))
    return rotate_half_tensor


def apply_rotary_pos_emb(tensor, sincos):
    sin_pos, cos_pos = sincos
    sin_pos = sin_pos[:, :, None, :].repeat(2, 3)
    cos_pos = cos_pos[:, :, None, :].repeat(2, 3)
    return (tensor * cos_pos) + (rotate_every_two(tensor) * sin_pos)


class FlaxGPTJAttention(nn.Module):
    config: GPTJConfig
    dtype: jnp.dtype = jnp.float32
    causal: bool = True
    is_cross_attention: bool = False

    def setup(self):
        config = self.config
        self.embed_dim = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.embed_dim // self.num_heads

        self.rotary_dim = config.rotary_dim
        if self.config.bits is not None:
            _dot_general_cls = q_config.fully_quantized(
                fwd_bits=self.config.bits,
                bwd_bits=self.config.bits
            )
        else:
            _dot_general_cls = None

        dot_general_cls = q_flax.QDotGeneral(_dot_general_cls)
        dense = partial(
            nn.Dense,
            self.embed_dim,
            use_bias=False,
            dtype=self.dtype,
            kernel_init=jax.nn.initializers.normal(self.config.initializer_range),
            dot_general=dot_general_cls
        )

        self.q_proj, self.k_proj, self.v_proj = dense(), dense(), dense()
        self.out_proj = dense()

        self.resid_dropout = nn.Dropout(rate=config.resid_pdrop)

        self.causal_mask = make_causal_mask(jnp.ones((1, config.max_position_embeddings), dtype="bool"), dtype="bool")

        pos_embd_dim = self.rotary_dim or self.embed_dim
        self.embed_positions = create_sinusoidal_positions(config.max_position_embeddings, pos_embd_dim)

    def _split_heads(self, hidden_states):
        return hidden_states.reshape(hidden_states.shape[:2] + (self.num_heads, self.head_dim))

    def _merge_heads(self, hidden_states):
        return hidden_states.reshape(hidden_states.shape[:2] + (self.embed_dim,))

    @nn.compact
    def _concatenate_to_cache(self, key, value, query, attention_mask):

        is_initialized = self.has_variable("cache", "cached_key")
        cached_key = self.variable("cache", "cached_key", jnp.zeros, key.shape, key.dtype)
        cached_value = self.variable("cache", "cached_value", jnp.zeros, value.shape, value.dtype)
        cache_index = self.variable("cache", "cache_index", lambda: chex.Array(0, dtype=jnp.int32))

        if is_initialized:
            *batch_dims, max_length, num_heads, depth_per_head = cached_key.value.shape
            # update key, value caches with our new 1d spatial slices
            cur_index = cache_index.value
            indices = (0,) * len(batch_dims) + (cur_index, 0, 0)
            key = lax.dynamic_update_slice(cached_key.value, key, indices)
            value = lax.dynamic_update_slice(cached_value.value, value, indices)
            cached_key.value = key
            cached_value.value = value
            num_updated_cache_vectors = query.shape[1]
            cache_index.value = cache_index.value + num_updated_cache_vectors
            # causal mask for cached decoder self-attention: our single query position should only attend to those key positions that have already been generated and cached, not the remaining zero elements.
            pad_mask = jnp.broadcast_to(
                jnp.arange(max_length) < cur_index + num_updated_cache_vectors,
                tuple(batch_dims) + (1, num_updated_cache_vectors, max_length),
            )
            attention_mask = combine_masks(pad_mask, attention_mask)
        return key, value, attention_mask

    def __call__(
            self,
            hidden_states,
            attention_mask,
            position_ids,
            deterministic: bool = True,
            init_cache: bool = False,
            output_attentions: bool = False,
    ):
        query = self.q_proj(hidden_states)
        key = self.k_proj(hidden_states)
        value = self.v_proj(hidden_states)

        # Force A local Sharding
        if self.config.use_pjit_attention_force:
            query = with_sharding_constraint(query, PartitionSpec(('dp', 'fsdp'), None, 'mp'))
            key = with_sharding_constraint(key, PartitionSpec(('dp', 'fsdp'), None, 'mp'))
            value = with_sharding_constraint(value, PartitionSpec(('dp', 'fsdp'), None, 'mp'))

        query = self._split_heads(query)
        key = self._split_heads(key)
        value = self._split_heads(value)

        sincos = jnp.take(self.embed_positions, position_ids, axis=0)
        sincos = jnp.split(sincos, 2, axis=-1)
        if self.rotary_dim is not None:
            k_rot = key[:, :, :, : self.rotary_dim]
            k_pass = key[:, :, :, self.rotary_dim:]

            q_rot = query[:, :, :, : self.rotary_dim]
            q_pass = query[:, :, :, self.rotary_dim:]

            k_rot = apply_rotary_pos_emb(k_rot, sincos)
            q_rot = apply_rotary_pos_emb(q_rot, sincos)

            key = jnp.concatenate([k_rot, k_pass], axis=-1)
            query = jnp.concatenate([q_rot, q_pass], axis=-1)
        else:
            key = apply_rotary_pos_emb(key, sincos)
            query = apply_rotary_pos_emb(query, sincos)

        query_length, key_length = query.shape[1], key.shape[1]

        if self.has_variable("cache", "cached_key"):
            mask_shift = self.variables["cache"]["cache_index"]
            max_decoder_length = self.variables["cache"]["cached_key"].shape[1]
            causal_mask = lax.dynamic_slice(
                self.causal_mask, (0, 0, mask_shift, 0), (1, 1, query_length, max_decoder_length)
            )
        else:
            causal_mask = self.causal_mask[:, :, :query_length, :key_length]

        batch_size = hidden_states.shape[0]
        causal_mask = jnp.broadcast_to(causal_mask, (batch_size,) + causal_mask.shape[1:])

        attention_mask = jnp.broadcast_to(jnp.expand_dims(attention_mask, axis=(-3, -2)), causal_mask.shape)
        attention_mask = combine_masks(attention_mask, causal_mask)

        dropout_rng = None
        if not deterministic and self.config.attn_pdrop > 0.0:
            dropout_rng = self.make_rng("dropout")

        # During fast autoregressive decoding, we feed one position at a time,
        # and cache the keys and values step by step.
        if self.has_variable("cache", "cached_key") or init_cache:
            key, value, attention_mask = self._concatenate_to_cache(key, value, query, attention_mask)

        # transform boolean mask into float mask
        attention_bias = lax.select(
            attention_mask > 0,
            jnp.full(attention_mask.shape, 0.0).astype(self.dtype),
            jnp.full(attention_mask.shape, jnp.finfo(self.dtype).min).astype(self.dtype),
        )

        # usual dot product attention
        if self.config.use_flash_attention:
            attn_weights = None
            attention_mask = einops.rearrange(
                attention_bias,
                '... s q k -> ... s 1 q k'
            )
            attn_output = efficient_attention(
                query,
                key,
                value,
                bias=attention_mask,
                dropout_rng=dropout_rng,
                attention_drop_rate=self.config.attn_pdrop,
                deterministic=not deterministic and self.config.attn_pdrop > 0.0,
                float32_logits=True,
                causal=True,
                dtype=self.dtype,
                precision=self.precision,
                query_chunk_size=self.config.flash_attn_query_chunk_size,
                key_chunk_size=self.config.flash_attn_key_chunk_size,
            )
        else:
            attn_weights = dot_product_attention_weights(
                query,
                key,
                bias=attention_bias,
                dropout_rng=dropout_rng,
                dropout_rate=self.config.attn_pdrop,
                deterministic=deterministic,
                dtype=jnp.promote_types(self.dtype, jnp.bfloat16),
                precision=self.precision,
            )
            if self.config.use_pjit_attention_force:
                attn_weights = with_sharding_constraint(attn_weights, PartitionSpec(("dp", "fsdp"), "mp", None, None))

            attn_output = jnp.einsum("...hqk,...khd->...qhd", attn_weights, value, precision=self.precision)
        attn_output = self._merge_heads(attn_output)
        attn_output = self.out_proj(attn_output)
        attn_output = self.resid_dropout(attn_output, deterministic=deterministic)

        outputs = (attn_output, attn_weights) if output_attentions else (attn_output,)
        return outputs


class FlaxGPTJMLP(nn.Module):
    config: GPTJConfig
    intermediate_size: int
    dtype: jnp.dtype = jnp.float32

    def setup(self):
        embed_dim = self.config.hidden_size
        kernel_init = jax.nn.initializers.normal(self.config.initializer_range)
        if self.config.bits is not None:
            _dot_general_cls = q_config.fully_quantized(
                fwd_bits=self.config.bits,
                bwd_bits=self.config.bits
            )
        else:
            _dot_general_cls = None

        dot_general_cls = q_flax.QDotGeneral(_dot_general_cls)
        self.fc_in = nn.Dense(
            self.intermediate_size,
            dtype=self.dtype,
            kernel_init=kernel_init,
            dot_general=dot_general_cls
        )
        self.fc_out = nn.Dense(
            embed_dim,
            dtype=self.dtype,
            kernel_init=kernel_init,
            dot_general=dot_general_cls
        )

        self.act = ACT2FN[self.config.activation_function]
        self.dropout = nn.Dropout(rate=self.config.resid_pdrop)

    def __call__(self, hidden_states, deterministic: bool = True):
        hidden_states = self.fc_in(hidden_states)
        hidden_states = self.act(hidden_states)
        hidden_states = self.fc_out(hidden_states)
        hidden_states = self.dropout(hidden_states, deterministic=deterministic)
        return hidden_states


class FlaxGPTJBlock(nn.Module):
    config: GPTJConfig
    dtype: jnp.dtype = jnp.float32

    def setup(self):
        hidden_size = self.config.hidden_size
        inner_dim = self.config.n_inner if self.config.n_inner is not None else 4 * hidden_size

        self.ln_1 = nn.LayerNorm(epsilon=self.config.layer_norm_epsilon, dtype=self.dtype)
        self.attn = FlaxGPTJAttention(self.config, dtype=self.dtype)

        self.mlp = FlaxGPTJMLP(self.config, inner_dim, dtype=self.dtype)

    def __call__(
            self,
            hidden_states,
            attention_mask=None,
            position_ids=None,
            deterministic: bool = True,
            init_cache: bool = False,
            output_attentions: bool = False,
    ):
        residual = hidden_states
        hidden_states = self.ln_1(hidden_states)
        attn_outputs = self.attn(
            hidden_states,
            attention_mask=attention_mask,
            position_ids=position_ids,
            deterministic=deterministic,
            init_cache=init_cache,
            output_attentions=output_attentions,
        )
        attn_output = attn_outputs[0]

        feed_forward_hidden_states = self.mlp(hidden_states, deterministic=deterministic)
        # residual connection
        hidden_states = attn_output + feed_forward_hidden_states + residual

        return (hidden_states,) + attn_outputs[1:]


class FlaxGPTJPreTrainedModel(FlaxPreTrainedModel):
    config_class = GPTJConfig
    base_model_prefix = "transformer"
    module_class: nn.Module = None

    def __init__(
            self,
            config: GPTJConfig,
            input_shape: Tuple = (1, 1),
            seed: int = 0,
            dtype: jnp.dtype = jnp.float32,
            _do_init: bool = True,
            **kwargs,
    ):
        module = self.module_class(config=config, dtype=dtype, **kwargs)
        super().__init__(config, module, input_shape=input_shape, seed=seed, dtype=dtype, _do_init=_do_init)

    def init_weights(self, rng: jax.random.PRNGKey, input_shape: Tuple, params: FrozenDict = None) -> FrozenDict:
        # init input tensors
        input_ids = jnp.zeros(input_shape, dtype="i4")
        attention_mask = jnp.ones_like(input_ids)
        position_ids = jnp.broadcast_to(jnp.arange(jnp.atleast_2d(input_ids).shape[-1]), input_shape)
        params_rng, dropout_rng = jax.random.split(rng)
        rngs = {"params": params_rng, "dropout": dropout_rng}

        if params is None:
            if self.config.add_cross_attention:
                encoder_hidden_states = jnp.zeros(input_shape + (self.config.n_embd,))
                encoder_attention_mask = attention_mask
                module_init_outputs = self.module.init(
                    rngs,
                    input_ids,
                    attention_mask,
                    position_ids,
                    encoder_hidden_states,
                    encoder_attention_mask,
                    return_dict=False,
                )
            else:
                module_init_outputs = self.module.init(rngs, input_ids, attention_mask, position_ids, return_dict=False)
            return module_init_outputs
        else:
            return params

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

        # Handle any PRNG if needed
        rngs = {}
        if dropout_rng is not None:
            rngs["dropout"] = dropout_rng

        inputs = {"params": params or self.params} if add_params_field else params or self.params

        if past_key_values:
            inputs["cache"] = past_key_values
            mutable = ["cache"]
        else:
            mutable = False
        rngs['params'] = jax.random.key(0)
        outputs = self.module.apply(
            inputs,
            jnp.array(input_ids, dtype="i4"),
            jnp.array(attention_mask, dtype="i4"),
            jnp.array(position_ids, dtype="i4"),
            not train,
            False,
            output_attentions,
            output_hidden_states,
            return_dict,
            rngs=rngs,
            mutable=mutable,
        )

        # add updated cache to model output
        if past_key_values is not None and return_dict:
            outputs, past_key_values = outputs
            outputs["past_key_values"] = unfreeze(past_key_values["cache"])
            return outputs
        elif past_key_values is not None and not return_dict:
            outputs, past_key_values = outputs
            outputs = outputs[:1] + (unfreeze(past_key_values["cache"]),) + outputs[1:]

        return outputs


class FlaxGPTJBlockCollection(nn.Module):
    config: GPTJConfig
    dtype: jnp.dtype = jnp.float32

    def setup(self):
        self.blocks = [
            FlaxGPTJBlock(self.config, name=str(i), dtype=self.dtype) for i in range(self.config.num_hidden_layers)
        ]

    def __call__(
            self,
            hidden_states,
            attention_mask=None,
            position_ids=None,
            deterministic: bool = True,
            init_cache: bool = False,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
            return_dict: bool = True,
    ):
        all_attentions = () if output_attentions else None
        all_hidden_states = () if output_hidden_states else None

        for block in self.blocks:
            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            layer_outputs = block(
                hidden_states,
                attention_mask,
                position_ids=position_ids,
                deterministic=deterministic,
                init_cache=init_cache,
                output_attentions=output_attentions,
            )
            hidden_states = layer_outputs[0]

            if output_attentions:
                all_attentions += (layer_outputs[1],)

        outputs = (hidden_states, all_hidden_states, all_attentions)

        return outputs


class FlaxGPTJModule(nn.Module):
    config: GPTJConfig
    dtype: jnp.dtype = jnp.float32

    def setup(self):
        self.embed_dim = self.config.hidden_size

        self.wte = nn.Embed(
            self.config.vocab_size,
            self.config.hidden_size,
            embedding_init=jax.nn.initializers.normal(stddev=self.config.initializer_range),
        )
        self.dropout = nn.Dropout(rate=self.config.embd_pdrop)
        self.h = FlaxGPTJBlockCollection(self.config, dtype=self.dtype)
        self.ln_f = nn.LayerNorm(epsilon=self.config.layer_norm_epsilon, dtype=self.dtype)

    def __call__(
            self,
            input_ids,
            attention_mask,
            position_ids,
            deterministic=True,
            init_cache: bool = False,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
            return_dict: bool = True,
    ):
        input_embeds = self.wte(input_ids.astype("i4"))

        hidden_states = self.dropout(input_embeds, deterministic=deterministic)

        outputs = self.h(
            hidden_states,
            attention_mask,
            position_ids=position_ids,
            deterministic=deterministic,
            init_cache=init_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        hidden_states = outputs[0]
        hidden_states = self.ln_f(hidden_states)

        if output_hidden_states:
            all_hidden_states = outputs[1] + (hidden_states,)
            outputs = (hidden_states, all_hidden_states) + outputs[2:]
        else:
            outputs = (hidden_states,) + outputs[1:]

        if not return_dict:
            return tuple(v for v in outputs if v is not None)

        return FlaxBaseModelOutput(
            last_hidden_state=hidden_states,
            hidden_states=outputs[1],
            attentions=outputs[-1],
        )


class FlaxGPTJModel(FlaxGPTJPreTrainedModel):
    module_class = FlaxGPTJModule


class FlaxGPTJForCausalLMModule(nn.Module):
    config: GPTJConfig
    dtype: jnp.dtype = jnp.float32

    def setup(self):
        if self.config.bits is not None:
            _dot_general_cls = q_config.fully_quantized(
                fwd_bits=self.config.bits,
                bwd_bits=self.config.bits
            )
        else:
            _dot_general_cls = None

        dot_general_cls = q_flax.QDotGeneral(_dot_general_cls)
        self.transformer = FlaxGPTJModule(self.config, dtype=self.dtype)
        self.lm_head = nn.Dense(
            self.config.vocab_size,
            dtype=self.dtype,
            kernel_init=jax.nn.initializers.normal(stddev=self.config.initializer_range),
            dot_general=dot_general_cls
        )

    def __call__(
            self,
            input_ids,
            attention_mask,
            position_ids,
            deterministic: bool = True,
            init_cache: bool = False,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
            return_dict: bool = True,
    ):
        outputs = self.transformer(
            input_ids,
            attention_mask,
            position_ids,
            deterministic=deterministic,
            init_cache=init_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        hidden_states = outputs[0]

        if self.config.tie_word_embeddings:
            shared_kernel = self.transformer.variables["params"]["wte"]["embedding"].T
            lm_logits = self.lm_head.apply({"params": {"kernel": shared_kernel}}, hidden_states)
        else:
            lm_logits = self.lm_head(hidden_states)

        if not return_dict:
            return (lm_logits,) + outputs[1:]

        return FlaxCausalLMOutput(logits=lm_logits, hidden_states=outputs.hidden_states, attentions=outputs.attentions)


class FlaxGPTJForCausalLM(FlaxGPTJPreTrainedModel):
    module_class = FlaxGPTJForCausalLMModule

    def prepare_inputs_for_generation(self, input_ids, max_length, attention_mask: Optional[chex.Array] = None):

        batch_size, seq_length = input_ids.shape

        past_key_values = self.init_cache(batch_size, max_length)
        extended_attention_mask = jnp.ones((batch_size, max_length), dtype="i4")
        if attention_mask is not None:
            position_ids = attention_mask.cumsum(axis=-1) - 1
            extended_attention_mask = lax.dynamic_update_slice(extended_attention_mask, attention_mask, (0, 0))
        else:
            position_ids = jnp.broadcast_to(jnp.arange(seq_length, dtype="i4")[None, :], (batch_size, seq_length))

        return {
            "past_key_values": past_key_values,
            "attention_mask": extended_attention_mask,
            "position_ids": position_ids,
        }

    def update_inputs_for_generation(self, model_outputs, model_kwargs):
        model_kwargs["past_key_values"] = model_outputs.past_key_values
        model_kwargs["position_ids"] = model_kwargs["position_ids"][:, -1:] + 1
        return model_kwargs
