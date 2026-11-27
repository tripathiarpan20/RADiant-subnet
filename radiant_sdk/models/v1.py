"""Model contract v1: the class every chest X-ray solution subclasses.

The validator's harness owns the entrypoint. Inside the sandbox it:

1. imports the solution's ``model.py`` and finds the one subclass of
   ``ChestXRayClassifier`` (the clock for ``model_load_timeout`` starts here);
2. sets ``labels`` on the class from the campaign manifest and instantiates
   it with no arguments;
3. calls ``load_model(weights_dir, device)`` (the clock stops when it returns);
4. calls ``run_single_evaluation`` and ``run_batched_evaluation`` with raw
   images, and passes each raw output through ``map_to_labels``.

Images are 2-D ``numpy.uint16`` arrays: raw pixels, upright, MONOCHROME2
(higher is brighter), with no windowing or resizing. Preprocessing is the
miner's, and its time counts toward speed. ``map_to_labels`` must return a
probability in [0, 1] for exactly the campaign's labels; anything else
scores as a failed item. The harness rounds every probability to the
campaign's decimals (3) before scoring.

``load_model_checkpoint`` exists so ``load_model`` can delegate the weight
file itself; the harness never calls it directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

REQUIRED_METHODS = (
    "load_model",
    "load_model_checkpoint",
    "run_single_evaluation",
    "run_batched_evaluation",
    "map_to_labels",
)


class ChestXRayClassifier(ABC):
    labels: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def load_model(self, weights_dir: Path, device: str) -> None:
        """Build the model and load weights from ``weights_dir`` (read-only) onto ``device``."""

    @abstractmethod
    def load_model_checkpoint(self, path: Path) -> None:
        """Load one checkpoint file into the already-built model."""

    @abstractmethod
    def run_single_evaluation(self, image: np.ndarray) -> Any:
        """Raw model output for one image."""

    @abstractmethod
    def run_batched_evaluation(self, images: list[np.ndarray]) -> list[Any]:
        """Raw model outputs for a batch, in input order."""

    @abstractmethod
    def map_to_labels(self, raw: Any) -> dict[str, float]:
        """Map one raw output to {label: probability} for exactly ``self.labels``."""
