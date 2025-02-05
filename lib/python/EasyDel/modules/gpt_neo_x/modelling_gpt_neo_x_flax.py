import math

from flax import linen as nn
from flax.core import FrozenDict
from typing import Optional, Dict, Union, Tuple, Sequence
from transformers import FlaxPreTrainedModel, PretrainedConfig
from jax import numpy as jnp
import jax
from jax.sharding import PartitionSpec
from transformers.modeling_flax_outputs import FlaxBaseModelOutput
from einops import rearrange
from ..flax_modelling_utils import get_gradient_checkpoint_policy, \
    with_sharding_constraint, ACT2FN, JaxBaseClassModel
import chex


class GPTNeoXConfig(PretrainedConfig, JaxBaseClassModel):
    model_type = "gpt_neox"

    def __init__(
            self,
            vocab_size=50432,
            hidden_size=6144,
            num_hidden_layers=44,
            num_attention_heads=64,
            intermediate_size=24576,
            hidden_act="gelu",
            rotary_pct=0.25,
            rotary_emb_base=10000,
            classifier_dropout=0.1,
            max_position_embeddings=2048,
            initializer_range=0.02,
            layer_norm_eps=1e-5,
            use_cache=True,
            bos_token_id=0,
            eos_token_id=2,
            tie_word_embeddings=False,
            gradient_checkpointing='everything_saveable',
            use_parallel_residual=True,
            axis_dims: Sequence[int] = (1, -1, 1, 1),
            axis_names: Sequence[str] = ("dp", "fsdp", "tp", "mp"),
            **kwargs,
    ):
        super().__init__(
            axis_dims=axis_dims,
            axis_names=axis_names,
            bos_token_id=bos_token_id,
            eos_token_id=eos_token_id,
            **kwargs
        )
        self.vocab_size = vocab_size
        self.max_position_embeddings = max_position_embeddings
        self.hidden_size = hidden_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.intermediate_size = intermediate_size
        self.hidden_act = hidden_act
        self.rotary_pct = rotary_pct
        self.rotary_emb_base = rotary_emb_base
        self.classifier_dropout = classifier_dropout
        self.initializer_range = initializer_range
        self.layer_norm_eps = layer_norm_eps
        self.use_cache = use_cache
        self.tie_word_embeddings = tie_word_embeddings
        self.gradient_checkpointing = gradient_checkpointing

        self.use_parallel_residual = use_parallel_residual
        self.from_pt = False

    @staticmethod
    def get_partition_rules(fully_fsdp: bool = False):
        return (
            ('wte/embedding', PartitionSpec(('fsdp', 'mp'), 'tp')),
            ('attention/w_qkv/(kernel|bias)', PartitionSpec(('fsdp', 'mp'), 'tp')),
            ('attention/wo/(kernel|bias)', PartitionSpec(('fsdp', 'mp'), 'tp')),
            ('mlp/dense_h_to_4h/(kernel|bias)', PartitionSpec(('fsdp', 'mp'), 'tp')),
            ('mlp/dense_4h_to_h/(kernel|bias)', PartitionSpec('tp', ('fsdp', 'mp'))),

            ('post_attention_layernorm/(bias|scale)', PartitionSpec(('fsdp', 'mp'), 'tp')),
            ('input_layernorm/(bias|scale)', PartitionSpec(('fsdp', 'mp'), 'tp')),

            ('transformer/final_layer_norm/(scale|bias)', PartitionSpec('tp', ('fsdp', 'mp'))),
            ('lm_head/kernel', PartitionSpec('tp', ('fsdp', 'mp'))),
            ('.*', PartitionSpec(None))
        ) if not fully_fsdp else (

            ('embed_in/embedding', PartitionSpec(('fsdp', 'mp'))),

            ('attention/w_qkv/(kernel|bias)', PartitionSpec(('fsdp', 'mp'))),
            ('attention/wo/(kernel|bias)', PartitionSpec(('fsdp', 'mp'))),
            ('mlp/dense_h_to_4h/(kernel|bias)', PartitionSpec(('fsdp', 'mp'))),
            ('mlp/dense_4h_to_h/(kernel|bias)', PartitionSpec(('fsdp', 'mp'))),

            ('post_attention_layernorm/(bias|scale)', PartitionSpec(('fsdp', 'mp'))),
            ('input_layernorm/(bias|scale)', PartitionSpec(('fsdp', 'mp'))),

            ('transformer/final_layer_norm/(scale|bias)', PartitionSpec(('fsdp', 'mp'))),
            ('lm_head/kernel', PartitionSpec(('fsdp', 'mp'))),
            ('.*', PartitionSpec(None))
        )

    @staticmethod
    def get_mesh_names():
        return "dp", "fsdp", "tp", "mp"

    def add_jax_args(
            self,
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
        self.from_pt = False


def precompute_freqs_cis(dim: int, end: int, theta: float = 10000.0,
                         dtype: jnp.dtype = jnp.bfloat16) -> jnp.ndarray:
    freqs = 1.0 / (theta ** (jnp.arange(0, dim, 2)[: (dim // 2)].astype(dtype) / dim))
    t = jnp.arange(end)  # type: ignore
    freqs = jnp.outer(t, freqs).astype(dtype)
    sin, cos = jnp.sin(freqs), jnp.cos(freqs)
    freqs_cis = jnp.complex64(cos + 1j * sin)
    return jnp.asarray(freqs_cis)


def apply_rotary_emb(
        xq: jnp.ndarray,
        xk: jnp.ndarray,
        freqs_cis: jnp.ndarray,
        dtype: jnp.dtype = jnp.bfloat16,
) -> Tuple[jnp.ndarray, jnp.ndarray]:
    reshape_xq = xq.astype(jnp.float32).reshape(*xq.shape[:-1], -1, 2)
    reshape_xk = xk.astype(jnp.float32).reshape(*xk.shape[:-1], -1, 2)

    xq_ = jax.lax.complex(reshape_xq[..., 0], reshape_xq[..., 1])
    xk_ = jax.lax.complex(reshape_xk[..., 0], reshape_xk[..., 1])

    freqs_cis = jnp.reshape(freqs_cis, (*freqs_cis.shape[:2], 1, *freqs_cis.shape[2:]))

    xq_out = xq_ * freqs_cis
    xq_out = jnp.stack((jnp.real(xq_out), jnp.imag(xq_out)), axis=-1).reshape(*xq_out.shape[:-1], -1)

    xk_out = xk_ * freqs_cis
    xk_out = jnp.stack((jnp.real(xk_out), jnp.imag(xk_out)), axis=-1).reshape(*xk_out.shape[:-1], -1)

    return xq_out.astype(dtype), xk_out.astype(dtype)


class FlaxGPTNeoXAttention(nn.Module):
    config: GPTNeoXConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[Union[jax.lax.Precision, str]] = None

    def setup(self) -> None:
        self.head_size = self.config.hidden_size // self.config.num_attention_heads
        self.freq_cis = precompute_freqs_cis(
            dtype=self.dtype,
            dim=self.head_size,
            end=self.config.max_position_embeddings
        )
        self.w_qkv = nn.Dense(
            3 * self.config.hidden_size
        )
        self.w_o = nn.Dense(
            self.config.hidden_size
        )

        self.factor = jnp.sqrt(jnp.asarray(self.head_size, dtype=jnp.float32))
        self.bias = nn.make_causal_mask(jnp.ones((1, self.config.max_position_embeddings)))

    def __call__(self,
                 hidden_states: chex.Array,
                 attention_mask: chex.Array = None,
                 ):
        b, s, d = hidden_states.shape
        q, k, v = jnp.split(self.w_qkv(hidden_states), indices_or_sections=3, axis=-1)
        freq = self.freq_cis[:s].reshape(1, s, -1)
        q = with_sharding_constraint(q, PartitionSpec(('dp', 'fsdp'), None, 'mp'))
        k = with_sharding_constraint(k, PartitionSpec(('dp', 'fsdp'), None, 'mp'))
        v = with_sharding_constraint(v, PartitionSpec(('dp', 'fsdp'), None, 'mp'))

        q = rearrange(q, 'b s (h d) -> b s h d', h=self.config.num_attention_heads)
        k = rearrange(k, 'b s (h d) -> b s h d', h=self.config.num_attention_heads)
        v = rearrange(v, 'b s (h d) -> b s h d', h=self.config.num_attention_heads)
        bias = jnp.where(self.bias == 1, 0, jnp.finfo(
            hidden_states.dtype
        ).min
                         )
        q, k = apply_rotary_emb(q, k, freqs_cis=freq, dtype=self.dtype)

        attn = jnp.einsum(
            '...qhd,...khd->...hqk', q, k, precision=self.precision
        ) * self.factor
        attn = attn + bias[:, :, :s, :s]
        if attention_mask is not None:
            attn += attention_mask
        attn = jax.nn.softmax(attn, axis=-1)
        attn = with_sharding_constraint(attn, PartitionSpec(('dp', 'fsdp'), 'mp', None, None))
        attn = jnp.einsum('...hqk,..khd->qhd', attn, v, precision=self.precision)
        attn = self.w_o(attn.reshape(b, s, d))
        return attn


class FlaxGPTNeoXMlp(nn.Module):
    config: GPTNeoXConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[Union[jax.lax.Precision, str]] = None

    def setup(self) -> None:
        self.dense_h_to_4h = nn.Dense(self.config.intermediate_size)
        self.dense_4h_to_h = nn.Dense(self.config.hidden_size)
        self.act = ACT2FN[self.config.hidden_act]

    def __call__(self, x):
        return self.dense_4h_to_h(self.act(self.dense_h_to_4h(x)))


class FlaxGPTNeoXBlock(nn.Module):
    config: GPTNeoXConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[Union[jax.lax.Precision, str]] = None

    def setup(self) -> None:
        self.use_parallel_residual = self.config.use_parallel_residual
        self.input_layernorm = nn.LayerNorm(
            epsilon=self.config.layer_norm_eps,
            dtype=self.dtype
        )
        self.post_attention_layernorm = nn.LayerNorm(
            epsilon=self.config.layer_norm_eps,
            dtype=self.dtype
        )
        self.attention = FlaxGPTNeoXAttention(
            config=self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.mlp = FlaxGPTNeoXMlp(
            config=self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )

    def __call__(self,
                 hidden_states: chex.Array,
                 attention_mask: chex.Array,
                 ):
        attn = self.attention(
            self.input_layernorm(hidden_states),
            attention_mask=attention_mask
        )

        if self.use_parallel_residual:
            mlp = self.mlp(
                self.post_attention_layernorm(
                    hidden_states
                )
            )
            hidden_states = mlp + hidden_states + attn
        else:
            hidden_states = attn + hidden_states
            hidden_states = self.mlp(self.post_attention_layernorm(hidden_states)) + hidden_states
        return hidden_states


def get_gradient_checkpoint_policy(name):
    return {
        'everything_saveable': jax.checkpoint_policies.everything_saveable,
        'nothing_saveable': jax.checkpoint_policies.nothing_saveable,
        'checkpoint_dots': jax.checkpoint_policies.checkpoint_dots,
        'checkpoint_dots_with_no_batch_dims': jax.checkpoint_policies.checkpoint_dots_with_no_batch_dims,
    }[name]


class FlaxGPTNeoXCollection(nn.Module):
    config: GPTNeoXConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[Union[jax.lax.Precision, str]] = None

    def setup(self) -> None:
        block = FlaxGPTNeoXBlock
        if self.config.gradient_checkpointing != '':
            block = nn.remat(
                block, static_argnums=None,
                policy=get_gradient_checkpoint_policy(
                    self.config.gradient_checkpointing
                ),

            )
        self.blocks = [
            block(
                config=self.config,
                dtype=self.dtype,
                param_dtype=self.param_dtype,
                precision=self.precision,
                name=str(i)
            )
            for i in range(
                self.config.num_hidden_layers
            )
        ]

    def __call__(self,
                 hidden_states: chex.Array,
                 attention_mask: chex.Array,

                 ):
        for block in self.blocks:
            hidden_states = block(
                hidden_states,
                attention_mask=attention_mask
            )
        return hidden_states


class FlaxGPTNeoXModule(nn.Module):
    config: GPTNeoXConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[Union[jax.lax.Precision, str]] = None

    def setup(self) -> None:
        self.embed_in = nn.Embed(self.config.vocab_size, self.config.hidden_size)
        self.layers = FlaxGPTNeoXCollection(
            config=self.config,
            param_dtype=self.param_dtype,
            dtype=self.dtype,
            precision=self.precision
        )
        self.final_layer_norm = nn.LayerNorm(
            epsilon=self.config.layer_norm_eps,
            dtype=self.dtype
        )

    def __call__(self,
                 input_ids: jnp.int32 = None,
                 attention_mask: Optional[chex.Array] = None,
                 return_dict: Optional[bool] = None,
                 ):
        b, s = input_ids.shape
        hidden_state = self.embed_in(
            inputs=input_ids
        )
        hidden_state = self.final_layer_norm(self.layers(
            hidden_state=hidden_state,
            attention_mask=attention_mask
        ))
        if return_dict:
            return FlaxBaseModelOutput(
                last_hidden_state=hidden_state
            )
        else:
            return hidden_state,


class FlaxGPTNeoXPretrainedModel(FlaxPreTrainedModel):
    module_class: nn.Module = None
    config_class = GPTNeoXConfig

    def __init__(self, config, _do_init=False, dtype: jnp.dtype = jnp.float32, param_dtype: jnp.dtype = jnp.float32,
                 input_shape: Tuple = (1, 12)):
        module = self.module_class(config=config, dtype=dtype, param_dtype=param_dtype)
        super().__init__(_do_init=_do_init, module=module, config=config, dtype=dtype, input_shape=input_shape)

    def init_weights(self, rng: jax.random.PRNGKey, input_shape: Tuple, params: FrozenDict = None) -> Dict:
        if params is None:
            params = self.module.init(
                rngs=rng,
                input_ids=jnp.ones(input_shape),
                attention_mask=jnp.ones(input_shape)
            )
        return params['params']

    def __call__(self, input_ids,
                 attention_mask=None,
                 params: FrozenDict = None,
                 add_params_field: bool = False,
                 return_dict: bool = True):
        params = {'params': params or self.params} if add_params_field else params or self.params
        predict = self.module.apply(
            params,
            input_ids=jnp.asarray(input_ids, dtype=jnp.int32),
            attention_mask=jnp.asarray(attention_mask,
                                       dtype=jnp.int32) if attention_mask is not None else attention_mask,
            return_dict=return_dict
        )
        return predict

    def prepare_inputs_for_generation(self, input_ids, max_length, attention_mask: Optional[chex.Array] = None):
        return {
            "attention_mask": attention_mask,
        }

    def update_inputs_for_generation(self, model_outputs, model_kwargs):
        return model_kwargs


class FlaxGPTNeoXModel(FlaxGPTNeoXPretrainedModel):
    module_class = FlaxGPTNeoXModule


class FlaxGPTNeoXForCausalLMModule(nn.Module):
    config: GPTNeoXConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[Union[jax.lax.Precision, str]] = None

    def setup(self) -> None:
        self.transformer = FlaxGPTNeoXModule(
            config=self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.lm_head = nn.Dense(
            self.config.vocab_size,
            use_bias=False
        )

    def __call__(self, input_ids, attention_mask, return_dict: bool = False):
        pred = self.transformer(input_ids=input_ids, attention_mask=attention_mask, return_dict=True).last_hidden_state
        return self.lm_head(pred)


class FlaxGPTNeoXForCausalLM(FlaxGPTNeoXPretrainedModel):
    module_class = FlaxGPTNeoXForCausalLMModule
