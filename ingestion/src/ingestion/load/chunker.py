"""Token-exact chunking for bge-small (DECISIONS D-001).

Chunks are ~450 tokens with 60 tokens of overlap, counted by the embedding model's own
tokenizer. A short contextual header ("AAPL · 10-K · FY2025 · Item 1A") is prepended
*for embedding only*; header + chunk + [CLS]/[SEP] must fit in 512 tokens or the model
would silently truncate the end of the chunk.
"""

from dataclasses import dataclass

from tokenizers import Tokenizer

from core.embeddings import MODEL_MAX_TOKENS

HEADER_MAX_TOKENS = 40
SNAP_WINDOW_TOKENS = 80  # look back this far for a sentence end to cut on


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    start_char: int
    end_char: int
    tokens: int


def chunk_text(
    text: str, tokenizer: Tokenizer, chunk_tokens: int = 450, overlap_tokens: int = 60
) -> list[Chunk]:
    if chunk_tokens + HEADER_MAX_TOKENS + 2 > MODEL_MAX_TOKENS:
        raise ValueError("chunk + header would exceed the model's 512-token window")
    enc = tokenizer.encode(text, add_special_tokens=False)
    offsets = enc.offsets
    n = len(offsets)
    chunks: list[Chunk] = []
    start = 0
    while start < n:
        end = min(start + chunk_tokens, n)
        if end < n:
            end = _snap_to_sentence(text, offsets, start, end)
        start_char, end_char = offsets[start][0], offsets[end - 1][1]
        chunks.append(
            Chunk(len(chunks), text[start_char:end_char].strip(), start_char, end_char, end - start)
        )
        if end >= n:
            break
        start = max(end - overlap_tokens, start + 1)
    return chunks


def _snap_to_sentence(text: str, offsets: list[tuple[int, int]], start: int, end: int) -> int:
    """Move `end` back to just after a sentence-ending token, if one is close."""
    for i in range(end - 1, max(start, end - SNAP_WINDOW_TOKENS), -1):
        if text[offsets[i][0] : offsets[i][1]] in {".", "?", "!"}:
            return i + 1
    return end


def make_header(tokenizer: Tokenizer, *parts: str) -> str:
    header = " · ".join(p for p in parts if p)
    ids = tokenizer.encode(header, add_special_tokens=False).ids
    if len(ids) > HEADER_MAX_TOKENS:
        header = tokenizer.decode(ids[:HEADER_MAX_TOKENS])
    return header


def truncate_tokens(tokenizer: Tokenizer, text: str, max_tokens: int) -> str:
    """Cut text to at most `max_tokens` content tokens (+2 special tokens = model input)."""
    enc = tokenizer.encode(text, add_special_tokens=False)
    if len(enc.ids) <= max_tokens:
        return text
    return text[: enc.offsets[max_tokens - 1][1]]


def embedding_input(header: str, chunk: str) -> str:
    return f"{header}\n{chunk}" if header else chunk
