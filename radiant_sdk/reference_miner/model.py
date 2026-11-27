"""RADiant reference miner: torchxrayvision DenseNet-121 (densenet121-res224-all).

A starter for miners, not a strong entry. DenseNet's 18 outputs are mapped
onto the campaign's labels where a sensible head exists; for labels it has no
output for (tuberculosis and silicosis among them) ``map_to_labels`` returns
0.0, as decided on 27 Nov 2026. It therefore scores at chance on those labels.

Weights: the public Hugging Face repo torchxrayvision/densenet121-res224-all
(Apache-2.0), pinned by commit in .env. Its model.pt is a pickled
torchxrayvision module, so it is unpickled (weights_only=False) inside the
offline sandbox, and config.json supplies the labels and thresholds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from radiant_sdk.models.v1 import ChestXRayClassifier

# Campaign label (DataHive condition, lower case) -> DenseNet output.
LABEL_TO_HEAD = {
    "pneumonia": "Pneumonia",
    "viral pneumonitis": "Pneumonia",
    "pleural effusion": "Effusion",
    "effusion": "Effusion",
    "lobar collapse": "Atelectasis",
    "atelectasis": "Atelectasis",
    "cardiomegaly": "Cardiomegaly",
    "congestive heart failure": "Edema",
    "edema": "Edema",
    "pulmonary edema": "Edema",
    "consolidation": "Consolidation",
    "pneumothorax": "Pneumothorax",
    "nodule": "Nodule",
    "mass": "Mass",
    "emphysema": "Emphysema",
    "fibrosis": "Fibrosis",
    "fracture": "Fracture",
    "hernia": "Hernia",
    "pleural thickening": "Pleural_Thickening",
}

INPUT_SIZE = 224


class DenseNetReference(ChestXRayClassifier):
    def __init__(self) -> None:
        self.model = None
        self.device = "cpu"
        self.heads: list[str] = []

    # ── loading ─────────────────────────────────────────────────────────────

    def load_model(self, weights_dir: Path, device: str) -> None:
        import torch

        self.device = device if (device != "cuda" or torch.cuda.is_available()) else "cpu"
        self.load_model_checkpoint(Path(weights_dir) / "model.pt")
        # Warm up so the first scored batch doesn't pay for CUDA kernel selection.
        with torch.no_grad():
            self.model(torch.zeros(1, 1, INPUT_SIZE, INPUT_SIZE, device=self.device))

    def load_model_checkpoint(self, path: Path) -> None:
        """Rebuild the network the way torchxrayvision's own loader does.

        model.pt pickles an older torchxrayvision DenseNet, so only its
        state_dict is taken. The label order and operating-point thresholds
        come from the repo's config.json; with thresholds set, forward()
        returns calibrated probabilities.
        """
        import json

        import torch
        import torchxrayvision as xrv

        config = json.loads((Path(path).parent / "config.json").read_text())
        saved = torch.load(path, map_location="cpu", weights_only=False)
        for mod in saved.modules():  # torchxrayvision's patch for old pickles
            if not hasattr(mod, "_non_persistent_buffers_set"):
                mod._non_persistent_buffers_set = set()
        model = xrv.models.DenseNet(num_classes=len(config["labels"]))
        model.load_state_dict(saved.state_dict())
        model.op_threshs = torch.tensor(config["op_threshs"])  # set after loading, as torchxrayvision does
        self.model = model.eval().to(self.device)
        self.heads = list(config["labels"])

    # ── preprocessing (the miner's job; its time counts toward speed) ──────

    @staticmethod
    def _bit_depth_max(image: np.ndarray) -> float:
        """Full-scale value of the image's effective bit depth (e.g. 4095 for 12-bit)."""
        peak = int(image.max()) if image.size else 0
        return float(max(255, (1 << max(8, peak.bit_length())) - 1))

    def _preprocess(self, images: list[np.ndarray]):
        import torch
        import torch.nn.functional as F

        batch = []
        for img in images:
            if img.ndim != 2:
                raise ValueError(f"expected a 2-D image, got shape {img.shape}")
            x = torch.from_numpy(img.astype(np.float32))
            x = (x / self._bit_depth_max(img)) * 2048.0 - 1024.0  # torchxrayvision's [-1024, 1024]
            h, w = x.shape
            side = min(h, w)
            top, left = (h - side) // 2, (w - side) // 2
            x = x[top:top + side, left:left + side]
            x = F.interpolate(x[None, None], size=(INPUT_SIZE, INPUT_SIZE), mode="area")[0]
            batch.append(x)
        return torch.stack(batch).to(self.device)

    # ── evaluation ──────────────────────────────────────────────────────────

    def run_single_evaluation(self, image: np.ndarray) -> Any:
        return self.run_batched_evaluation([image])[0]

    def run_batched_evaluation(self, images: list[np.ndarray]) -> list[Any]:
        import torch

        with torch.no_grad():
            out = self.model(self._preprocess(images))
        return [row for row in out.float().cpu().numpy()]

    def map_to_labels(self, raw: Any) -> dict[str, float]:
        result = {}
        for label in self.labels:
            head = LABEL_TO_HEAD.get(label)
            if head is None or head not in self.heads:
                result[label] = 0.0
            else:
                result[label] = float(np.clip(raw[self.heads.index(head)], 0.0, 1.0))
        return result
