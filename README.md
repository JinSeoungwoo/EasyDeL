# EasyDeL 🔮

EasyDeL, an open-source library, is specifically designed to enhance and streamline the training process of machine
learning models. It focuses primarily on Jax/Flax and aims to provide convenient and effective solutions for training
Flax/Jax Models on TPU/GPU for both Serving and Training purposes. Additionally, EasyDeL will support mojo and will be
rewritten for mojo as well.

Some of the key features provided by EasyDeL include:

- Support for 8, 6, and 4 BIT inference and training in JAX
- Wide Range of models in Jax are supported which have never been implemented before such as _falcon_ 
- Integration of flashAttention in JAX for GPUs and TPUs
- Automatic serving of LLMs with mid and high-level APIs in both JAX and PyTorch
- LLM Trainer and fine-tuner in JAX
- RLHF (presumably Reinforcement Learning with Hybrid Functions) in Jax
- And various other features to enhance the training process and optimize performance.

> [!NOTE]
> These features collectively aim to simplify and accelerate the training of machine learning models, making it more
> efficient and accessible for developers working with Jax/Flax.

## Documentation 💫

> [!IMPORTANT]
> Documents and Examples are ready at [Here](https://erfanzar.github.io/EasyDeL)
> Please have that in mind that EasyDel is in the loop of fast-development
> so we might have API changes

## Serving

you can read docs or examples to see how `JAXServer` works but let me show you how you can simply host and serve a
LLama2
chat model (70B model is supported too)

```shell
python -m examples.serving.causal-lm.llama-2-chat \
  --repo_id='meta-llama/Llama-2-7b-chat-hf' --max_length=4096 \
  --max_new_tokens=2048 --max_stream_tokens=32 --temperature=0.6 \
  --top_p=0.95 --top_k=50 \
  --dtype='fp16' --use_prefix_tokenizer

```

> [!NOTE]
> you can use all the llama models not just 'meta-llama/Llama-2-7b-chat-hf'
> float16 or float32 , bfloat16 are supported dtype and make sure to use --use_prefix_tokenizer,
> and you will get links or api to use model from gradio app chat/instruct or FastAPI apis

## RLHF(Reinforcement Learning From Human Feedback)

> RLHF or Reinforcement Learning From Human Feedback is Available At the moment, but it's still
> under heavy development , because I don't have enough experience with Reinforcement Learning at the moment so its
> still
> in beta version but it's works and ill soon release a Tutorial For that

## FineTuning

with using EasyDel FineTuning LLM (CausalLanguageModels) are easy as much as possible with using Jax and Flax
and having the benefit of TPUs for the best speed here's a simple code to use in order to finetune your
own Model

Days Has Been Passed and now using easydel in Jax is way more similar to HF/PyTorch Style
now it's time to finetune our model

```python
import jax.numpy
from EasyDel import TrainArguments, CausalLMTrainer, AutoEasyDelModelForCausalLM, FlaxLlamaForCausalLM
from datasets import load_dataset
import flax
from jax import numpy as jnp

model, params = AutoEasyDelModelForCausalLM.from_pretrained("", )

max_length = 4096

configs_to_init_model_class = {
    'config': model.config,
    'dtype': jnp.bfloat16,
    'param_dtype': jnp.bfloat16,
    'input_shape': (1, 1)
}

train_args = TrainArguments(
    model_class=type(model),
    model_name='my_first_model_to_train_using_easydel',
    num_train_epochs=3,
    learning_rate=5e-5,
    learning_rate_end=1e-6,
    optimizer='adamw',  # 'adamw', 'lion', 'adafactor' are supported
    scheduler='linear',  # 'linear','cosine', 'none' ,'warm_up_cosine' and 'warm_up_linear'  are supported
    weight_decay=0.01,
    total_batch_size=64,
    max_steps=None,  # None to let trainer Decide
    do_train=True,
    do_eval=False,  # it's optional but supported 
    backend='tpu',  # default backed is set to cpu, so you must define you want to use tpu cpu or gpu
    max_length=max_length,  # Note that you have to change this in the model config too
    gradient_checkpointing='nothing_saveable',
    sharding_array=(1, -1, 1, 1),  # the way to shard model across gpu,cpu or TPUs using sharding array (1, -1, 1, 1)
    # everything training will be in fully FSDP automatic and share data between devices
    use_pjit_attention_force=False,
    remove_ckpt_after_load=True,
    gradient_accumulation_steps=8,
    loss_remat='',
    dtype=jnp.bfloat16
)
dataset = load_dataset('TRAIN_DATASET')
dataset_train = dataset['train']
dataset_eval = dataset['eval']

trainer = CausalLMTrainer(
    train_args,
    dataset_train,
    ckpt_path=None
)

output = trainer.train(flax.core.FrozenDict({'params': params}))
print(f'Hey ! , here\'s where your model saved {output.last_save_file_name}')


```

> [!TIP]
> you can then convert it to pytorch for better use I don't recommend jax/flax for hosting models since
> pytorch is better option for gpus

## LLMServe

To use EasyDeL in your project, you will need to import the library in your Python script and use its various functions
and classes. Here is an example of how to import EasyDeL and use its Model class:

```python
from EasyDel.modules import AutoEasyDelModelForCausalLM
from EasyDel.serve import JAXServer
from transformers import AutoTokenizer
import jax

model_id = 'meta-llama/Llama.md-2-7b-chat-hf'

tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
model, params = AutoEasyDelModelForCausalLM.from_pretrained(
    model_id,
    jax.devices('cpu')[0],
    jax.numpy.float16,
    jax.numpy.float16,
    jax.lax.Precision('fastest'),
    (1, -1, 1, 1),
    device_map='auto'
)

server = JAXServer.load_from_params(
    model=model,
    config_model=model.config,
    tokenizer=tokenizer,
    params=model.params,
    add_params_field=True
)

response_printed = 0
for response, tokens_used in server.process(
        'String To The Model', stream=True
):
    print(response[response_printed:], end='')
    response_printed = len(response)
``` 

## Contributing

EasyDeL is an open-source project, and contributions are welcome. If you would like to contribute to EasyDeL, please
fork the repository, make your changes, and submit a pull request. The team behind EasyDeL will review your changes and
merge them if they are suitable.

## License 📜

EasyDeL is released under the Apache v2 license. Please see the LICENSE file in the root directory of this project for
more information.

## Contact

If you have any questions or comments about EasyDeL, you can reach out to me

## Citing EasyDeL 🥶

To cite this repository:

```misc
@misc{Zare Chavoshi_2023,
    title={EasyDeL, an open-source library, is specifically designed to enhance and streamline the training process of machine learning models. It focuses primarily on Jax/Flax and aims to provide convenient and effective solutions for training Flax/Jax Models on TPU/GPU for both Serving and Training purposes.},
    url={https://github.com/erfanzar/EasyDel},
    journal={EasyDeL Easy and Fast DeepLearning with JAX},
    publisher={Erfan Zare Chavoshi},
    author={Zare Chavoshi, Erfan},
    year={2023}
} 
```
