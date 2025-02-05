import fjformer.attention
from jax.interpreters import pxla
from jax.experimental.pjit import with_sharding_constraint as wsc
import jax
from flax import linen as nn
from functools import partial
import chex
from typing import Sequence, Optional
from jax.experimental.mesh_utils import create_device_mesh
from jax.sharding import PartitionSpec as PS
from jax.experimental.shard_map import shard_map

ACT2FN = {
    "gelu": partial(nn.gelu, approximate=False),
    "relu": nn.relu,
    "silu": nn.swish,
    "swish": nn.swish,
    "gelu_new": partial(nn.gelu, approximate=True),

}


def canonicalize_dtype(
        *args, dtype: Optional[chex.ArrayDType] = None, inexact: bool = True
) -> chex.ArrayDType:
    """Canonicalize an optional dtype to the definitive dtype.

    If the ``dtype`` is None this function will infer the dtype. If it is not
    None it will be returned unmodified or an exceptions is raised if the dtype
    is invalid.
    from the input arguments using ``jnp.result_type``.

    Args:
      *args: JAX array compatible values. None values
        are ignored.
      dtype: Optional dtype override. If specified the arguments are cast to
        the specified dtype instead and dtype inference is disabled.
      inexact: When True, the output dtype must be a subdtype
      of `jnp.inexact`. Inexact dtypes are real or complex floating points. This
      is useful when you want to apply operations that don't work directly on
      integers like taking a mean for example.
    Returns:
      The dtype that *args should be cast to.
    """
    if dtype is None:
        args_filtered = [jax.numpy.asarray(x) for x in args if x is not None]
        dtype = jax.numpy.result_type(*args_filtered)
        if inexact and not jax.numpy.issubdtype(dtype, jax.numpy.inexact):
            dtype = jax.numpy.promote_types(jax.numpy.float32, dtype)
    if inexact and not jax.numpy.issubdtype(dtype, jax.numpy.inexact):
        raise ValueError(f'Dtype must be inexact: {dtype}')
    return dtype


def get_names_from_partition_spec(partition_specs):
    """
    The get_names_from_partition_spec function takes a partition_specs argument, which is either a dictionary or list.
    If it's a dictionary, the function converts it to a list of values. Then for each item in the partition_specs list:
        If the item is None, continue (do nothing) and move on to next iteration of loop.
        If the item is an instance of str (i.e., if it's just one string), add that string to names set and move
        on to next iteration of loop.
        Otherwise, (if not None or str), call get_names_from_partition_spec recurs

    :param partition_specs: Define the partitioning of a table
    :return: A list of the names of all partitions
    
    """
    names = set()
    if isinstance(partition_specs, dict):
        partition_specs = partition_specs.values()
    for item in partition_specs:
        if item is None:
            continue
        elif isinstance(item, str):
            names.add(item)
        else:
            names.update(get_names_from_partition_spec(item))

    return list(names)


def names_in_mesh(*names):
    """
    The names_in_mesh function is a decorator that can be used to check whether
    the names of the axes passed into a function are valid.  It will raise an
    exception if any of the axis names are not in the physical mesh.  For example,
    if you have a function that takes two axes as arguments, and you want to make sure they're both in your mesh:

    :param *names: Collect all the names passed to the function into a tuple
    :return: A boolean indicating whether all the given
    
    """
    return set(names) <= set(pxla.thread_resources.env.physical_mesh.axis_names)


def with_sharding_constraint(x, partition_specs):
    """
    The with_sharding_constraint function is used to ensure that the sharding of a tensor
    is consistent with the sharding of its inputs.  This function should be called on any
    tensor which has been created by an operation which does not automatically handle this,
    such as tf.concat or tf.split.

    :param x: Define the tensor that will be sharded
    :param partition_specs: Specify the partitioning of the data
    :return: The same tensor with the
    
    """
    axis_names = get_names_from_partition_spec(partition_specs)
    if names_in_mesh(*axis_names):
        x = wsc(x, partition_specs)
    return x


def get_gradient_checkpoint_policy(name):
    """
    The get_gradient_checkpoint_policy function is a helper function that returns the gradient checkpoint policy
        specified by the name parameter.

    :param name: Select the checkpoint policy from the dictionary
    :return: A function that is used in the jax
    
    """
    gradients = dict(
        everything_saveable=jax.checkpoint_policies.everything_saveable,
        nothing_saveable=jax.checkpoint_policies.nothing_saveable,
        dots_saveable=jax.checkpoint_policies.dots_saveable,
        checkpoint_dots=jax.checkpoint_policies.checkpoint_dots,
        dots_with_no_batch_dims_saveable=jax.checkpoint_policies.dots_with_no_batch_dims_saveable,
        checkpoint_dots_with_no_batch_dims=jax.checkpoint_policies.checkpoint_dots_with_no_batch_dims,
        save_anything_except_these_names=jax.checkpoint_policies.save_anything_except_these_names,
        save_any_names_but_these=jax.checkpoint_policies.save_any_names_but_these,
        save_only_these_names=jax.checkpoint_policies.save_only_these_names,
        save_from_both_policies=jax.checkpoint_policies.save_from_both_policies
    )
    return gradients[name]


def repeat_kv_bnsh(x: chex.Array, n_rep: int) -> chex.Array:
    """
    The repeat_kv_bnsh function is used to repeat the key and value vectors for each head in a multi-head attention
    module. This function takes as input an array of shape (batch_size, n_heads, sequence_length, head_dim) and returns
    an array of shape (batch_size, n_heads * nrep, sequence length, head dim). The reason this is necessary is because the
    attention module expects keys/values/queries to be repeated across heads but not across batches. However we want our
    keys/values/queries to be repeated both across heads AND batches so that we can use them

    :param x: chex.Array: Pass in the input to the function
    :param n_rep: int: Repeat the key and value heads
    :return: A new array with the same shape as x, except for the second dimension which is n_kv_heads * n_rep
    
    """
    bs, n_kv_heads, s, head_dim = x.shape
    if n_rep == 1:
        return x
    x = x[:, :, jax.numpy.newaxis, :, :]
    x = jax.numpy.repeat(x, n_rep, axis=2)

    return x.reshape(bs, n_kv_heads * n_rep, s, head_dim)


def repeat_kv_bsnh(x: chex.Array, n_rep: int) -> chex.Array:
    """
    The repeat_kv_bsnh function is used to repeat the key and value vectors for each head.

    :param x: chex.Array: Specify the input array
    :param n_rep: int: Repeat the key-value attention heads n_rep times
    :return: A new array with the same batch size, sequence length, and head dimension as the input array
    
    """
    bs, s, n_kv_heads, head_dim = x.shape
    x = x.transpose(0, 2, 1, 3)
    if n_rep == 1:
        return x
    x = x[:, :, jax.numpy.newaxis, :, :]
    x = jax.numpy.repeat(x, n_rep, axis=2)

    x = x.transpose(0, 2, 1, 3)

    return x.reshape(bs, s, n_kv_heads * n_rep, head_dim)


def precompute_freq_cis(max_position_embedding, head_dim):
    """
    The precompute_freq_cis function is used to precompute the sinusoidal embeddings for positional encoding.

    :param max_position_embedding: Define the maximum length of the sequence
    :param head_dim: Determine the number of heads in the attention layer
    :return: Two arrays:
    
    """
    inv_freq = 1.0 / (10000 ** (jax.numpy.arange(0, head_dim, 2, dtype=jax.numpy.float32) / head_dim))
    freq = jax.numpy.einsum("i , j -> i j", jax.numpy.arange(max_position_embedding), inv_freq).astype("float32")

    embed = jax.numpy.concatenate((freq, freq), axis=-1)
    return jax.numpy.sin(embed)[:, :], jax.numpy.cos(embed)[:, :]


def rotate_half(x):
    """
    The rotate_half function takes a complex-valued array and rotates the
    phase of its second half by 180 degrees. This is equivalent to multiplying
    the second half by -i, or equivalently rotating it 90 degrees counterclockwise.


    :param x: Specify the input array
    :return: A new array that is the same as the input
    
    """
    x1 = x[..., : x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2:]
    return jax.numpy.concatenate((-x2, x1), axis=-1)


def apply_rotary_pos_emb(tensor, sin_, cos_):
    """
    The apply_rotary_pos_emb function applies a rotary positional embedding to the input tensor.

    :param tensor: Store the tensor that is passed into the function
    :param sin_: Rotate the tensor by pi/2
    :param cos_: Apply the cosine function to the tensor
    :return: A tensor with the same shape as the input tensor
    
    """
    return (tensor * cos_) + (rotate_half(tensor) * sin_)


def get_ranks_and_size(mesh):
    """
    The get_ranks_and_size function is used to determine the number of MPI processes
    (``mp_node_size``) and the number of devices per process (``dp_node_size``).
    The ``mesh.shape[&quot;tp&quot;] * mesh.shape[&quot;mp&quot;]`` determines how many MPI processes are needed,
    and then we divide that by the local device count to get ``mp_node_size = max( 1, mp / jax.local )`.
    This means that if there are more than enough devices for all MPI ranks on a node, each rank will only use one device; otherwise it will use

    :param mesh: Get the shape of the mesh
    :return: A dictionary with the following keys:
    
    """
    out = dict(mesh=mesh)
    mp_size = mesh.shape["tp"] * mesh.shape["mp"]
    mp_node_size = max(1, mp_size // jax.local_device_count())
    dp_node_size = jax.process_count() // mp_node_size
    out.update(mp_node_size=mp_node_size,
               dp_node_size=dp_node_size)

    dp_node_rank = jax.process_index() // mp_node_size
    mp_node_rank = jax.process_index() % mp_node_size
    out.update(dp_node_rank=dp_node_rank,
               mp_node_rank=mp_node_rank)
    return out


def get_flash_attention():
    """
    return: FlashAttention FN, Upcast Needed to float32,do_shard_map
    """
    platform = jax.lib.xla_bridge.get_backend().platform
    if platform == "gpu":
        float32_logits = False
        ring_attention_fn = fjformer.attention.ring_flash_attention_gpu
        do_shard_map = True
    elif platform == "tpu":
        float32_logits = True
        ring_attention_fn = fjformer.attention.tpu_flash_attention
        do_shard_map = False
    else:
        raise ValueError(f"Unsupported platform {platform}")

    return ring_attention_fn, float32_logits, do_shard_map


def smart_flash_attention(
        q: chex.Array,
        k: chex.Array,
        v: chex.Array,
        bias: chex.Array,
        q_ps: jax.sharding.PartitionSpec,
        k_ps: jax.sharding.PartitionSpec,
        v_ps: jax.sharding.PartitionSpec,
        b_ps: jax.sharding.PartitionSpec,
        a_ps: jax.sharding.PartitionSpec,
        block_k: int,
        block_q: int,
        block_b: int,
        q_seq_len: int,
        kv_seq_len: int,
        num_attention_heads: int,
        head_dims: int,
        causal: bool,
        attn_pdrop: float,
        mesh: jax.sharding.Mesh = None,
        dtype: jax.numpy.dtype = jax.numpy.float32,
        precision: jax.lax.Precision = jax.lax.Precision('fastest'),
        dropout_rng: jax.random.PRNGKey = None,
        force_float32_tpu: bool = True,
        deterministic: bool = False
) -> chex.Array:
    """
    Smart Flash Attention mechanism for efficient attention computation.

    :param q: Query tensor with shape [batch_size, num_attention_heads, q_seq_len, head_dims].
    :type q: tensor

    :param k: Key tensor with shape [batch_size, num_attention_heads, kv_seq_len, head_dims].
    :type k: tensor

    :param v: Value tensor with shape [batch_size, num_attention_heads, kv_seq_len, head_dims].
    :type v: tensor

    :param bias: Bias tensor with shape [batch_size, num_attention_heads, q_seq_len, kv_seq_len].
    :type bias: tensor

    :param q_ps: jax.sharding.PartitionSpec: Specify the partitioning of the query tensor

    :param k_ps: jax.sharding.PartitionSpec: Partition the key matrix

    :param v_ps: jax.sharding.PartitionSpec: Specify the partitioning of the value tensor

    :param b_ps: jax.sharding.PartitionSpec: Specify the Attention Bias partition spec

    :param a_ps: jax.sharding.PartitionSpec: Specify the partitioning of the attention weights

    :param block_k: Block size for key tensor reshaping.
    :type block_k: int

    :param block_q: Block size for query tensor reshaping.
    :type block_q: int

    :param block_b: Block size for bias tensor reshaping.
    :type block_b: int

    :param q_seq_len: Length of the query sequence.
    :type q_seq_len: int

    :param kv_seq_len: Length of the key-value sequence.
    :type kv_seq_len: int

    :param num_attention_heads: Number of attention heads.
    :type num_attention_heads: int

    :param head_dims: Dimensionality of each attention head.
    :type head_dims: int

    :param causal: If True, applies causal masking to the attention scores.
    :type causal: bool

    :param attn_pdrop: Dropout probability for attention weights.
    :type attn_pdrop: float

    :param mesh: Mesh specifying the data distribution for parallel computation.
    :type mesh: mesh_type

    :param dtype: Data type of the tensors.
    :type dtype: data_type

    :param precision: Precision mode for computation (default is 'fastest').
    :type precision: str

    :param dropout_rng: Random number generator key for dropout.
    :type dropout_rng: rng_key

    :param force_float32_tpu: If True, forces computation to use float32 on TPU.
    :type force_float32_tpu: bool

    :param deterministic: If True, ensures deterministic computation.
    :type deterministic: bool

    :return: chex.Array: Output tensor with the same shape as the input value tensor v.
    :rtype: tensor

    :raises ValueError: If the shapes of input tensors are not compatible for attention computation.
    """
    assertion_mkv_err = """
    Q,K,V and bias shapes must be like
    Q Shape : [batch_size, num_attention_heads, q_seq_len, head_dims]
    K Shape : [batch_size, num_attention_heads, kv_seq_len, head_dims]
    V Shape : [batch_size, num_attention_heads, kv_seq_len, head_dims]
    bias Shape : [batch_size, num_attention_heads, q_seq_len, kv_seq_len]
    """
    batch_size = q.shape[0]
    assert batch_size == k.shape[0] == v.shape[0], 'Batch Size for q,k,v wont match'

    assert q.shape == (batch_size, num_attention_heads, q_seq_len, head_dims), assertion_mkv_err
    assert k.shape == (batch_size, num_attention_heads, kv_seq_len, head_dims), assertion_mkv_err
    assert v.shape == (batch_size, num_attention_heads, kv_seq_len, head_dims), assertion_mkv_err
    assert bias.shape == (batch_size, num_attention_heads, q_seq_len, kv_seq_len), assertion_mkv_err

    flash_attn_fn, f32_upcast, do_shard_map = get_flash_attention()

    if do_shard_map:
        q, k, v = map(lambda x: jax.numpy.transpose(x, (0, 2, 1, 3)), [q, k, v])
        assert mesh is not None, 'For Using Shard Map on GPUs you have to pass Mesh'
        ring_attention_sharded = shard_map(
            partial(
                flash_attn_fn,
                axis_name="mp",
                float32_logits=f32_upcast,
                blockwise_kwargs=dict(
                    deterministic=deterministic,
                    dropout_rng=dropout_rng,
                    attn_pdrop=attn_pdrop,
                    causal=causal,
                    query_chunk_size=block_q,
                    key_chunk_size=block_k,
                    dtype=dtype,
                    policy=jax.checkpoint_policies.nothing_saveable,
                    precision=precision,
                    prevent_cse=False,
                )
            ),
            mesh=mesh,
            in_specs=(
                q_ps,
                k_ps,
                v_ps,
                b_ps
            ),
            out_specs=a_ps,
            check_rep=False
        )
        attn_output = ring_attention_sharded(q, k, v, bias)
        attn_output = with_sharding_constraint(attn_output, a_ps)
    else:
        if force_float32_tpu or f32_upcast:
            q, k, v = map(lambda x: x.astype(jax.numpy.float32), [q, k, v])
        attn_output = fjformer.attention.jax_flash_attn_tpu.flash_attention(
            q,
            k,
            v,
            bias,
            None,
            causal=False,
            sm_scale=1.0,
            block_sizes=fjformer.attention.jax_flash_attn_tpu.BlockSizes(
                block_b=block_b,
                block_k=block_k,
                block_q=block_q,
                block_k_major=block_k
            ),
            debug=False,
        )

    attn_output = attn_output.astype(dtype)
    return attn_output


def create_mesh(
        axis_dims: Sequence[int] = (1, -1, 1, 1), axis_names: Sequence[str] = ("dp", "fsdp", "tp", "mp"), backend=""
):
    """
    The create_mesh function creates a mesh object that can be used to shard arrays.

    :param axis_dims: Sequence[int]: Specify the dimensions of the mesh
    :param axis_names: Sequence[str]: Name the axes of the mesh
    :param backend: Specify the backend to use
    :return: A mesh object
    
    """
    array_devices = jax.numpy.ones((len(jax.devices() if backend == "" else jax.devices(backend)), 1))
    resh = array_devices.reshape(axis_dims).shape

    return jax.sharding.Mesh(
        create_device_mesh(resh), axis_names
    )


class JaxBaseClassModel:
    """
    It initializes all the attributes of an object, and it's called when you create a new instance of that class.
    :param self: Refer to the instance of the class
    :param axis_dims: Sequence[int]: Specify the number of dimensions for each axis
    :param axis_names: Sequence[str]: Set the names of the axes
    :param q_ps: jax.sharding.PartitionSpec: Specify the partitioning of the query tensor
    :param k_ps: jax.sharding.PartitionSpec: Partition the key matrix
    :param v_ps: jax.sharding.PartitionSpec: Specify the partitioning of the value tensor
    :param b_ps: jax.sharding.PartitionSpec: Specify the Attention Bias partition spec
    :param a_ps: jax.sharding.PartitionSpec: Specify the partitioning of the attention weights
    :param backend: Optional[None]: Specify the backend to use
    """

    def __init__(
            self,
            axis_dims: Sequence[int] = (1, -1, 1, 1),
            axis_names: Sequence[str] = ("dp", "fsdp", "tp", "mp"),
            q_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
            k_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
            v_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
            b_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec("dp", None, ("dp", "fsdp"), None),
            a_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
            backend: Optional[None] = None
    ):
        """
        The __init__ function is the constructor for a class.
        It initializes all the attributes of an object, and it's called when you create a new instance of that class.


        :param self: Refer to the instance of the class
        :param axis_dims: Sequence[int]: Specify the number of dimensions for each axis
        :param axis_names: Sequence[str]: Set the names of the axes
        :param q_ps: jax.sharding.PartitionSpec: Specify the partitioning of the query tensor
        :param k_ps: jax.sharding.PartitionSpec: Partition the key matrix
        :param v_ps: jax.sharding.PartitionSpec: Specify the partitioning of the value tensor
        :param b_ps: jax.sharding.PartitionSpec: Specify the Attention Bias partition spec
        :param a_ps: jax.sharding.PartitionSpec: Specify the partitioning of the attention weights
        :param backend: Optional[None]: Specify the backend to use
        :return: A new instance of the class

        """
        self.q_ps = q_ps
        self.k_ps = k_ps
        self.v_ps = v_ps
        self.b_ps = b_ps
        self.a_ps = a_ps
        self.axis_dims = axis_dims
        self.axis_names = axis_names
        self.backend = backend if backend is not None else ""

    def jax_mesh(self) -> jax.sharding.Mesh:
        """
        The jax_mesh function is a helper function that creates a jax.sharding.Mesh object from the
        axis_dims and axis_names attributes of an object, which are assumed to be lists of integers and strings, respectively.
        The backend attribute is also used if it exists.

        :param self: Refer to the object itself
        :return: A jaxMesh

        """
        return create_mesh(
            axis_dims=self.axis_dims,
            axis_names=self.axis_names,
            backend=(self.backend if self.backend is not None else "") if hasattr(self, 'backend') else ""
        )

    def get_axis_dims(self) -> Sequence[int]:
        """
        The get_axis_dims function returns a sequence of integers representing the dimensions of each axis.

        :param self: Represent the instance of the class
        :return: The dimensions of the axes

        """
        return self.axis_dims

    def get_axis_names(self) -> Sequence[str]:
        """
        The get_axis_names function returns a list of the names of the axes.

        :param self: Represent the instance of the class
        :return: A list of the names of all axes

        """
        return self.axis_names

    def get_backend(self) -> str:
        """
        The get_backend function returns the backend that is currently being used.
        If no backend has been set, it will return the default JAX backend.

        :param self: Bind the method to an object
        :return: The backend platform

        """
        return self.backend if not self.backend == "" else jax.lib.xla_bridge.get_backend().platform

    @staticmethod
    def get_flash_attention():
        """
        The get_flash_attention function is used to get the flash attention value from the database.
            :returns: The flash attention value from the database.

        :return: A function

        """
        return get_flash_attention()

    def add_pss(
            self,
            axis_dims: Sequence[int] = (1, -1, 1, 1),
            axis_names: Sequence[str] = ("dp", "fsdp", "tp", "mp"),
            q_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
            k_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
            v_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
            b_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), None, "tp", None),
            a_ps: jax.sharding.PartitionSpec = jax.sharding.PartitionSpec(("dp", "fsdp"), "mp", "tp", None),
            backend: Optional[str] = None,
    ):
        self.axis_dims = axis_dims
        self.axis_names = axis_names
        self.q_ps = q_ps
        self.k_ps = k_ps
        self.v_ps = v_ps
        self.b_ps = b_ps
        self.a_ps = a_ps
        self.backend = backend


def add_start_docstrings(*docstr):
    """
    The add_start_docstrings function is a decorator that adds the docstrings to the beginning of a function.
    The add_start_docstrings function takes in an arbitrary number of strings and returns a decorator.
    The returned decorator takes in one argument, fn, which is assumed to be a function. The docstring for fn is set equal to
    the concatenation of all the strings passed into add_start_docstrings plus (if it exists) the original docstring for fn.
    
    :param *docstr: Pass in a variable number of arguments to the function
    :return: A decorator that adds the docstrings to the function
    
    """

    def docstring_decorator(fn):
        fn.__doc__ = "".join(docstr) + (fn.__doc__ if fn.__doc__ is not None else "")
        return fn

    return docstring_decorator
