"""Embedding model wrapper and first-run download.

EmbeddingGemma is trained with task-specific prompts. sentence-transformers
reads the prompt strings from the model's own config_sentence_transformers.json,
so we always embed via prompt_name rather than hard-coding prompt text:
queries with the "query" prompt, chunks with the "document" prompt. Mixing
these up measurably degrades retrieval.

Once the model is in the Hugging Face cache, the app sets HF_HUB_OFFLINE so
that loading it makes no network call at all. The model download is the only
time the app touches the network.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import lru_cache

import numpy as np

from ..settings import DIMENSIONS, MODEL_NAME

REQUIRED_PROMPTS = ("query", "document")


def detect_device() -> str:
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def model_is_cached(name: str = MODEL_NAME) -> bool:
    from huggingface_hub import snapshot_download

    try:
        snapshot_download(name, local_files_only=True)
        return True
    except Exception:
        return False


def download_model(
    progress: Callable[[int, int], None] | None = None, name: str = MODEL_NAME
) -> None:
    """Fetch the model into the Hugging Face cache, reporting bytes done/total.

    The model is ungated (Apache 2.0), so no login or token is needed.
    """
    from huggingface_hub import snapshot_download
    from tqdm.auto import tqdm

    class Reporter(tqdm):
        # huggingface_hub drives one bar per file, plus a bar over the file
        # count. Only byte-unit bars are summed, so the UI sees one overall
        # figure.
        done = 0
        total = 0

        def __init__(self, *a, **kw):
            kw["disable"] = False
            super().__init__(*a, **kw)
            if self.unit == "B" and self.total:
                Reporter.total += self.total

        def update(self, n=1):
            super().update(n)
            if self.unit == "B" and progress:
                Reporter.done += n
                progress(Reporter.done, max(Reporter.total, Reporter.done))

    snapshot_download(name, tqdm_class=Reporter)


@lru_cache(maxsize=1)
def _load(name: str, device: str, truncate_dim: int):
    from sentence_transformers import SentenceTransformer

    if model_is_cached(name):
        os.environ["HF_HUB_OFFLINE"] = "1"
    model = SentenceTransformer(name, device=device, truncate_dim=truncate_dim)
    missing = [p for p in REQUIRED_PROMPTS if p not in (model.prompts or {})]
    if missing:
        raise RuntimeError(
            f"Model {name} lacks expected prompts {missing}; "
            f"found {sorted((model.prompts or {}).keys())}."
        )
    return model


class Embedder:
    def __init__(
        self, name: str = MODEL_NAME, dimensions: int = DIMENSIONS, device: str | None = None
    ):
        self.device = device or detect_device()
        self.dimensions = dimensions
        self.model = _load(name, self.device, dimensions)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return _unit(self.model.encode(texts, prompt_name="document"))

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return _unit(self.model.encode(texts, prompt_name="query"))

    def release_memory(self) -> None:
        """MPS keeps cached allocations across encode calls; long index runs
        were SIGKILLed ~75 files in without this."""
        try:
            import torch

            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:
            pass


def _unit(vecs: np.ndarray) -> np.ndarray:
    # MPS float32 leaves the model's own normalisation ~0.2% off unit length;
    # renormalise so dot products are true cosine similarities.
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
