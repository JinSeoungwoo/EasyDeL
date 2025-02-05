import flax.traverse_util
import jax

from flax.traverse_util import flatten_dict
from flax.serialization import from_bytes, to_bytes, to_state_dict
import msgpack
import os


def get_float_dtype_by_name(dtype):
    """
    The get_float_dtype_by_name function is a helper function that returns the JAX float dtype
    corresponding to the string name of a floating point type.  This is useful for converting
    between strings and JAX float types, which are used in many places throughout this codebase.


    :param dtype: Specify the type of data that is being passed into the function
    :return: The float dtype of the input string
    
    """
    return {
        'bf16': jax.numpy.bfloat16,
        'bfloat16': jax.numpy.bfloat16,
        'fp16': jax.numpy.float16,
        'float16': jax.numpy.float16,
        'fp32': jax.numpy.float32,
        'float32': jax.numpy.float32,
        'fp64': jax.numpy.float64,
        'float64': jax.numpy.float64,
    }[dtype]


def float_tensor_to_dtype(tensor, dtype):
    """
    The float_tensor_to_dtype function is used to convert a tensor's dtype to the specified dtype.

    :param tensor: Convert the tensor to a float dtype
    :param dtype: Convert the tensor to a specific dtype
    :return: A tensor with the specified dtype
    
    """
    if dtype is None or dtype == '':
        return tensor
    if isinstance(dtype, str):
        dtype = get_float_dtype_by_name(dtype)
    float_dtypes = (jax.numpy.bfloat16, jax.numpy.float16, jax.numpy.float32, jax.numpy.float64)
    if getattr(tensor, 'dtype', None) in float_dtypes:
        tensor = tensor.astype(dtype)
    return tensor


def match_keywords(string, ts, ns):
    """
    The match_keywords function takes a string, and two lists of strings.
    The first list is the &quot;must-have&quot; keywords, and the second list is the &quot;not-allowed&quot; keywords.
    It returns True if all of the must-have keywords are in string, but none of not allowed are in it.

    :param string: Pass in the text that is being searched
    :param ts: Specify the required keywords and ns is used to specify the non-required keywords
    :param ns: Specify a list of negative keywords
    :return: True if all the keywords in ts are present and none of the
    
    """
    for t in ts:
        if t not in string:
            return False
    for n in ns:
        if n in string:
            return False
    return True


def huggingface_to_easydel(
        state_dict,
        embedding_layer_name: str,
        device,
        dtype: jax.numpy.dtype = jax.numpy.float16
):
    """
    The huggingface_to_easydel function takes a huggingface model's state_dict and converts it to an easydel
    model's flax_dict. The function is designed to be used in conjunction with the load_huggingface function, which
    loads a huggingface model from disk. The embedding layer name must be specified as well as the device on which
    the conversion will take place.

    :param state_dict: Load the weights from a huggingface model
    :param embedding_layer_name: str: Identify the embedding layer in the huggingface model
    :param device: Determine which device the model will be loaded on
    :param dtype: jax.numpy.dtype: Specify the data type of the tensors
    :return: A dictionary of the weights and biases in a format that can be used by flax (it's an UnFlattenDict)
    
    """
    _l = len('.weight')
    with jax.default_device(device):
        flax_dict = {}
        for key, tensor in state_dict.items():
            if embedding_layer_name in key:
                # tensor = tensor.transpose(0, 1)
                key = key[:-_l] + '.embedding'
            elif match_keywords(key, ['kernel'], ['none']):
                if len(tensor.shape) == 2:
                    tensor = tensor.transpose(0, 1)
                if key.endswith('.weight'):
                    key = key[:-_l] + '.kernel'
            key_tuple = key.split('.')
            key_names = ()
            tensor = tensor.detach().cpu().numpy()
            for k in key_tuple:
                key_names += k,
            flax_dict[key_names] = tensor.astype(dtype)
        return flax.traverse_util.unflatten_dict(flax_dict)


def read_ckpt(path: [str, os.PathLike], shard_fns=None, add_extra_past_fix: list = None):
    """
    The read_ckpt function reads a checkpoint file and returns the tensors in it.

    :param path: [str: Specify the path to the checkpoint file
    :param os.PathLike]: Specify the path to the checkpoint file
    :param shard_fns: Shard the tensors
    :param add_extra_past_fix: list: Add an extra past to the key
    :return: A dictionary of tensors
    
    """
    tensors = {}
    with open(path, 'rb') as stream:
        unpacker = msgpack.Unpacker(stream, read_size=83886080, max_buffer_size=0)
        for key, value in unpacker:
            if add_extra_past_fix is not None:
                key = add_extra_past_fix + key
            key = tuple(key)
            tensor = from_bytes(None, value)
            if shard_fns is not None:
                tensor = shard_fns[key](tensor)
            tensors[key] = tensor
    return tensors


def save_ckpt(train_state, path, gather_fns=None, float_dtype=None):
    """
    The save_ckpt function saves the state of a training run to disk.

    :param train_state: Store the current state of the training process
    :param path: Specify the location of the checkpoint file
    :param gather_fns: Specify a function that will be used to convert the tensor to bytes
    :param float_dtype: Convert the tensor to a specific dtype
    :return: Nothing
    
    """

    train_state = to_state_dict(train_state)
    packer = msgpack.Packer()
    flatten_train_state = flatten_dict(train_state)
    if gather_fns is not None:
        gather_fns = flatten_dict(to_state_dict(gather_fns))

    with open(path, "wb") as stream:
        for key, value in flatten_train_state.items():
            if gather_fns is not None:
                value = gather_fns[key](value)
            value = float_tensor_to_dtype(value, float_dtype)
            stream.write(packer.pack((key, to_bytes(value))))
