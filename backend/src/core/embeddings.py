"""Local embeddings (fastembed / ONNX) and exact token counting for chunking.

bge-small-en-v1.5 accepts at most 512 tokens and its tokenizer is configured to
truncate silently at 512 (verified from the loaded tokenizer's config). Chunking
therefore counts tokens with *this* tokenizer, with truncation disabled, so we can
see and enforce the real length (DECISIONS D-001).
"""

import asyncio
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding
from numpy.typing import NDArray
from tokenizers import Tokenizer

MODEL_MAX_TOKENS = 512  # includes [CLS] and [SEP]


class Embedder:
    def __init__(self, model_name: str, cache_dir: Path) -> None:
        self._model = TextEmbedding(model_name, cache_dir=str(cache_dir))
        # Copy of the model's tokenizer without truncation, for honest counting.
        base: Tokenizer = self._model.model.tokenizer  # type: ignore[attr-defined]
        self.tokenizer = Tokenizer.from_str(base.to_str())
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        # ONNX inference is CPU-bound; serialize it so concurrent companies don't thrash.
        self._lock = asyncio.Lock()

    @property
    def dim(self) -> int:
        return 384

    def count_tokens(self, text: str) -> int:
        """Token count including [CLS]/[SEP], as the model will see it."""
        return len(self.tokenizer.encode(text).ids)

    def embed_sync(self, texts: list[str]) -> NDArray[np.float32]:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vectors = np.asarray(list(self._model.embed(texts, batch_size=32)), dtype=np.float32)
        return vectors  # fastembed returns L2-normalized vectors for bge

    async def embed(self, texts: list[str]) -> NDArray[np.float32]:
        async with self._lock:
            return await asyncio.to_thread(self.embed_sync, texts)
