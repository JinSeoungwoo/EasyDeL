from setuptools import setup, find_packages

setup(
    name='EasyDeL',
    version='0.0.40',
    author='Erfan Zare Chavoshi',
    author_email='erfanzare82@eyahoo.com',
    description='An open-source library to make training faster and more optimized in Jax/Flax',
    url='https://github.com/erfanzar/EasyDeL',
    packages=find_packages('lib/python'),
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Developers',
        'Topic :: Scientific/Engineering :: Artificial Intelligence',
        'License :: OSI Approved :: Apache Software License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
    ],
    keywords='machine learning, deep learning, pytorch, jax, flax',
    install_requires=[
        "chex",
        "typing",
        "jax>=0.4.10",
        "jaxlib>=0.4.10",
        "flax",
        "fjformer>=0.0.10",
        "transformers>=4.33.0",
        "einops>=0.6.1",
        "optax",
        "msgpack",
        "ipython",
        "tqdm",
        "datasets",
        "pydantic==2.4.2",
        "gradio",
        "distrax",
        "rlax",
        "wandb>=0.15.9",
        "tensorboard",
        # add any other required dependencies here
    ],
    python_requires='>=3.8',
    package_dir={'': 'lib/python'},

)
