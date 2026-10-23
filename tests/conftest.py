import hashlib

import numpy as np
import pytest


class FakeEmbedder:
    """Deterministic bag-of-words vectors: enough to test plumbing, no model."""

    dims = 768

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dims, dtype=np.float32)
        for word in text.lower().split():
            h = int(hashlib.md5(word.strip(".,").encode()).hexdigest(), 16)
            v[h % self.dims] += 1.0
        if not v.any():
            v[0] = 1.0
        return v / np.linalg.norm(v)

    def embed_documents(self, texts):
        return np.stack([self._vec(t) for t in texts])

    embed_queries = embed_documents

    def release_memory(self):
        pass


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture(autouse=True)
def app_home(tmp_path, monkeypatch):
    home = tmp_path / "apphome"
    monkeypatch.setenv("FOLDER_SEARCH_HOME", str(home))
    return home
