# RADiant reference miner

DenseNet-121 from torchxrayvision (`densenet121-res224-all`), wrapped in the
v1 contract. It is a working starting point, not a competitive entry: labels
DenseNet has no output for, such as tuberculosis and silicosis, always get
0.0.

```bash
cp -r "$(python -c 'import radiant_sdk, pathlib; print(pathlib.Path(radiant_sdk.__file__).parent / "reference_miner")')" my-miner
cd my-miner
cp .env.template .env
radiant-sdk prepare . --out build
radiant-sdk validate build/solution.zip --campaign testnet-cxr-01
```

To improve it, fine-tune on your own data, push the weights to a private
Hugging Face repo, point `.env` at that repo's commit with a fine-grained
read-only token, and change `map_to_labels`.
