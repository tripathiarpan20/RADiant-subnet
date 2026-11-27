# Mining on RADiant

You compete in a campaign by submitting a chest X-ray classifier: a small zip
of Python code, with weights on Hugging Face. The owner validator gates and
builds it, runs it offline on fresh DataHive studies against the current
leader, and pays the crown holder. This page covers preparing a solution,
checking it exactly as the validator will, and submitting it on testnet.

## What you need

| Need | Why | Set up |
| --- | --- | --- |
| Python 3.12 or newer | The SDK targets the sandbox's Python | `uv python install 3.12` |
| [uv](https://docs.astral.sh/uv/) | `radiant-sdk lock` uses it to build the hash-locked lockfile | see uv's install page |
| A Modal account | `validate` builds your image and runs a short offline qualification there, on your account (usually a few cents) | `modal setup`, or set `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` |
| A Hugging Face account | Your weights live in a repo there, pinned by commit | huggingface.co |
| A Bittensor wallet on testnet (R3) | Signing, the fee (testTAO on testnet, netuid 292) and the commitment | `btcli wallet create`, then register a hotkey with `btcli subnet register --netuid 292 --network test` |

## Install

```bash
pip install "radiant-subnet[sdk] @ git+https://github.com/tripathiarpan20/RADiant-subnet"
# or, from a clone:  uv sync --extra sdk
modal setup
```

## Start from the reference miner

```bash
cp -r "$(python -c 'import radiant_sdk, pathlib; print(pathlib.Path(radiant_sdk.__file__).parent / "reference_miner")')" my-miner
cd my-miner
cp .env.template .env
cp miner.env.template miner.env      # API URL, network, wallet; never goes in the zip
radiant-sdk prepare . --out build
radiant-sdk validate build/solution.zip --campaign testnet-cxr-01
```

`validate` prints every gate as it runs and ends with a receipt next to the
zip. `submit` will refuse anything without a passing receipt under 24 hours
old, for that exact zip and campaign.

## Submit (testnet)

`miner.env` holds `RADIANT_API_URL`, `RADIANT_NETWORK=test`,
`RADIANT_NETUID=292`, `WALLET_NAME` and `WALLET_HOTKEY`.

```bash
radiant-sdk submit build/solution.zip --campaign testnet-cxr-01 --dry-run   # checks only
radiant-sdk submit build/solution.zip --campaign testnet-cxr-01             # upload, pay, commit
radiant-sdk status sha256:<zip hash>
```

`submit` refuses to pay if the validator reports a different network or
netuid. It also refuses if the campaign's fee recipient is neither one of the
owner addresses pinned in the SDK nor listed in your
`RADIANT_TRUSTED_FEE_RECIPIENTS`.
It asks before paying, and your coldkey password unlocks the transfer. If the
commitment fails after paying, rerun with the `--payment-block` and
`--payment-tx` it prints, rather than paying again.

## What a solution is

```text
radiant_solution.json   base_class, sdk_version, gpu, cpus, batch_size
.env                    HF_REPO_ID, HF_REVISION (full 40-char commit), HF_TOKEN (optional)
requirements.lock       hash-locked extras beyond the base image (radiant-sdk lock)
model.py                exactly one subclass of radiant_sdk.models.v1:ChestXRayClassifier
src/                    optional extra .py / .json / .yaml / .txt / .md files
```

- **Weights must be safetensors.** A repo holding `.pt`, `.bin`, `.pkl` or
  any other pickle-based file is refused (`HF_WEIGHTS_FORMAT`). Convert with
  `safetensors.torch.save_file(model.state_dict(), "model.safetensors")`.
- **`HF_TOKEN` travels in your zip.** The validator uses it once to download
  your private weights and strips it before any reveal. Because it leaves
  your machine, make it a fine-grained token with read access to that one
  repo only (huggingface.co/settings/tokens → Fine-grained → Repositories →
  read). `validate` refuses a token that can write anything
  (`HF_TOKEN_WRITE_SCOPE`). Public weights need no token at all.
- **The base image** for SDK 1.0.0 has Python 3.12, torch 2.5.1,
  torchvision 0.20.1, numpy 2.2.6, pillow 11.3.0 and safetensors 0.8.0. List
  anything else in `requirements.in` and run `radiant-sdk lock`.
- **Your code runs offline.** No network or secrets; network and process
  modules (`socket`, `requests`, `subprocess`, ...) can't be imported.
- **Inputs are raw.** Each image is a 2-D `uint16` array (raw pixels,
  MONOCHROME2). Preprocessing is yours, and its time counts toward speed.
- **Outputs** from `map_to_labels` must be a probability in [0, 1] for exactly
  the campaign's labels; they are rounded to 3 decimals.
- **The load clock** runs from importing your `model.py` until `load_model`
  returns (default limit 120 s). GPU queueing and container start never count.

## When validate says no

Every rejection has a stable code, and the validator gives the same one
(`radiant_sdk/reasons.py`). `GPU_UNAVAILABLE` (exit status 4) is not your
solution: Modal had no free GPU of the type you chose. Retry, wait longer
with `--gpu-wait`, or pick another GPU the campaign allows.
