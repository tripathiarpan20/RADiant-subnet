"""Convert the reference DenseNet to safetensors and publish it on Hugging Face.

    uv run --extra sdk --extra r0-models python scripts/publish_reference_weights.py \
        --out /tmp/ref-weights [--repo RADIANT_ORG/densenet121-res224-all-safetensors]

1. Downloads torchxrayvision/densenet121-res224-all at a pinned commit
   (a pickled module: it is unpickled here, once, on the operator's machine,
   from torchxrayvision's own repo).
2. Rebuilds the network as torchxrayvision's loader does, and saves its
   state_dict as model.safetensors plus config.json (labels, thresholds,
   provenance with the source commit and sha256).
3. Loads the safetensors copy back with the reference miner's own loader and
   checks its outputs equal torchxrayvision's on the warmup set.
4. With --repo, uploads the folder as a public model repo (needs HF_TOKEN
   with write access to that org) and prints the commit to pin in
   radiant_sdk/reference_miner/.env.template.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

SOURCE_REPO = "torchxrayvision/densenet121-res224-all"
SOURCE_REVISION = "588eadcc6a744da71f8dce768bc974ec629443f5"

CARD = """---
license: apache-2.0
library_name: torchxrayvision
tags: [chest-x-ray, densenet, safetensors, radiant]
---

# DenseNet-121 (torchxrayvision densenet121-res224-all), safetensors

The RADiant reference miner's weights. A conversion of
[{src}](https://huggingface.co/{src}) at commit `{rev}` from a pickled
torchxrayvision module to safetensors, so loading it cannot run code.
Nothing was retrained: outputs match torchxrayvision's own loader exactly.

`config.json` holds the 18 output labels in order, the operating-point
thresholds torchxrayvision applies, and the source file's sha256.

Original weights: Cohen et al., TorchXRayVision (Apache-2.0). They were
trained on public datasets (NIH, CheXpert, MIMIC-CXR, PadChest and others),
some of which are licensed for research use only.
"""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--repo", help="org/name to publish to (public); omit to only convert and verify")
    args = p.parse_args()

    import torch
    import torchxrayvision as xrv
    from huggingface_hub import HfApi, hf_hub_download
    from safetensors.torch import save_file

    args.out.mkdir(parents=True, exist_ok=True)
    src_pt = Path(hf_hub_download(SOURCE_REPO, "model.pt", revision=SOURCE_REVISION))
    src_cfg = json.loads(Path(hf_hub_download(SOURCE_REPO, "config.json", revision=SOURCE_REVISION)).read_text())

    saved = torch.load(src_pt, map_location="cpu", weights_only=False)  # trusted source, operator machine
    for mod in saved.modules():
        if not hasattr(mod, "_non_persistent_buffers_set"):
            mod._non_persistent_buffers_set = set()
    state = {k: v.contiguous() for k, v in saved.state_dict().items() if k != "op_threshs"}
    save_file(state, str(args.out / "model.safetensors"), metadata={"source": f"{SOURCE_REPO}@{SOURCE_REVISION}"})
    config = {
        "labels": src_cfg["labels"],
        "op_threshs": src_cfg["op_threshs"],
        "architecture": "torchxrayvision.models.DenseNet",
        "input_resolution": 224,
        "source": {"repo": SOURCE_REPO, "revision": SOURCE_REVISION, "file": "model.pt",
                   "sha256": sha256(src_pt)},
    }
    (args.out / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    (args.out / "README.md").write_text(CARD.format(src=SOURCE_REPO, rev=SOURCE_REVISION))

    # Verify with the reference miner's own loader against torchxrayvision's.
    spec = importlib.util.spec_from_file_location("ref_model", REPO_ROOT / "radiant_sdk/reference_miner/model.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from radiant_sdk.harness import warmup_images

    ref = mod.DenseNetReference()
    ref.labels = ("pneumonia",)
    ref.load_model(args.out, "cpu")
    original = xrv.models.DenseNet(weights="densenet121-res224-all").eval()
    x = ref._preprocess(warmup_images(4))
    with torch.no_grad():
        diff = float((ref.model(x) - original(x)).abs().max())
    print(f"max output difference vs torchxrayvision: {diff}")
    if diff != 0.0:
        print("refusing to publish: outputs differ", file=sys.stderr)
        return 1

    if args.repo:
        api = HfApi(token=os.environ.get("HF_TOKEN"))
        api.create_repo(args.repo, repo_type="model", private=False, exist_ok=True)
        commit = api.upload_folder(repo_id=args.repo, folder_path=str(args.out),
                                   commit_message=f"safetensors conversion of {SOURCE_REPO}@{SOURCE_REVISION[:12]}")
        print(f"published {args.repo} at {commit.oid}")
        print(f"set in radiant_sdk/reference_miner/.env.template:\n  HF_REPO_ID={args.repo}\n  HF_REVISION={commit.oid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
