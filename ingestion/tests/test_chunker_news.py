"""Chunker token limits (D-001) and the NewsAPI client (D-010).

The chunker tests build a small WordLevel tokenizer in-process (no model download), so
they check the windowing logic itself; the production tokenizer is bge's WordPiece.
"""

import itertools
import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import WhitespaceSplit

from core.newsapi import (
    NewsApiClient,
    NewsApiError,
    build_query,
    parse_articles,
    short_company_name,
)
from ingestion.load.chunker import (
    HEADER_MAX_TOKENS,
    chunk_text,
    embedding_input,
    make_header,
    truncate_tokens,
)


def _tokenizer(words: list[str]) -> Tokenizer:
    vocab = {w: i for i, w in enumerate(["[UNK]", *sorted(set(words))])}
    tok = Tokenizer(WordLevel(vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = WhitespaceSplit()
    return tok


# Every 17th token is a separate "." (as WordPiece tokenizes sentence ends).
TEXT = " ".join("." if i % 17 == 16 else f"w{i % 50}" for i in range(2000))
TOK = _tokenizer(TEXT.split())


def test_chunks_respect_size_and_overlap() -> None:
    chunks = chunk_text(TEXT, TOK, chunk_tokens=450, overlap_tokens=60)
    assert all(c.tokens <= 450 for c in chunks)
    assert chunks[0].start_char == 0 and chunks[-1].end_char == len(TEXT)
    for a, b in itertools.pairwise(chunks):
        assert b.start_char < a.end_char  # overlapping windows
    assert len(chunks) >= 2000 // 450


def test_chunks_end_on_sentence_boundaries_when_possible() -> None:
    chunks = chunk_text(TEXT, TOK, chunk_tokens=450, overlap_tokens=60)
    assert all(c.text.endswith(".") for c in chunks[:-1])


def test_header_plus_chunk_always_fits_512() -> None:
    long_header = make_header(TOK, *["w1"] * 200)
    assert len(TOK.encode(long_header, add_special_tokens=False).ids) <= HEADER_MAX_TOKENS
    chunk = chunk_text(TEXT, TOK)[0]
    total = len(TOK.encode(embedding_input(long_header, chunk.text), add_special_tokens=False).ids)
    assert total + 2 <= 512  # +[CLS]/[SEP]


def test_oversized_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="512"):
        chunk_text(TEXT, TOK, chunk_tokens=480)


def test_truncate_tokens() -> None:
    cut = truncate_tokens(TOK, TEXT, 100)
    assert len(TOK.encode(cut, add_special_tokens=False).ids) == 100
    assert truncate_tokens(TOK, "w1 w2", 100) == "w1 w2"


@pytest.mark.parametrize(
    ("sec_name", "short"),
    [
        ("NVIDIA CORP", "Nvidia"),
        ("Apple Inc.", "Apple"),
        ("BOEING CO", "Boeing"),
        ("INTEL CORP", "Intel"),
        ("JPMORGAN CHASE & CO", "Jpmorgan Chase"),
        ("ADVANCED MICRO DEVICES INC", "Advanced Micro Devices"),
        ("TEXAS INSTRUMENTS INC", "Texas Instruments"),
        ("Tesla, Inc.", "Tesla"),
    ],
)
def test_short_company_name(sec_name: str, short: str) -> None:
    assert short_company_name(sec_name) == short


def test_query_skips_short_tickers() -> None:
    assert build_query("Apple Inc.", "AAPL") == '"Apple" OR "AAPL"'
    assert build_query("FORD MOTOR CO", "F") == '"Ford Motor"'


def test_parse_articles_dedupes_urls_titles_and_removed() -> None:
    payload = {
        "articles": [
            {
                "url": "https://x.com/a?utm=1",
                "title": "Chip news",
                "publishedAt": "2026-10-01T10:00:00Z",
                "source": {"name": "X"},
            },
            {
                "url": "https://X.com/a/",
                "title": "Chip news (copy)",
                "publishedAt": "2026-10-01T10:00:00Z",
            },
            {
                "url": "https://y.com/b",
                "title": "Chip  News!",
                "publishedAt": "2026-10-01T11:00:00Z",
            },
            {"url": "https://z.com/c", "title": "[Removed]", "publishedAt": "2026-10-01T12:00:00Z"},
        ]
    }
    arts = parse_articles(payload)
    assert [a.url for a in arts] == ["https://x.com/a?utm=1"]
    assert arts[0].source_name == "X"


@respx.mock
async def test_news_client_caches_per_day(tmp_path: Path) -> None:
    route = respx.get("https://newsapi.org/v2/everything").mock(
        return_value=httpx.Response(200, json={"status": "ok", "totalResults": 0, "articles": []})
    )
    client = NewsApiClient("k", "https://newsapi.org/v2", tmp_path, http=httpx.AsyncClient())
    day = date(2026, 11, 4)
    await client.everything("AAPL", '"Apple"', 29, today=day)
    await client.everything("AAPL", '"Apple"', 29, today=day)
    assert route.call_count == 1
    req = route.calls[0].request
    assert req.headers["X-Api-Key"] == "k"
    assert req.url.params["from"] == "2026-10-06"
    assert (
        json.loads((tmp_path / "newsapi" / "AAPL" / "2026-11-04.json").read_text())["status"]
        == "ok"
    )


@respx.mock
async def test_news_client_raises_on_api_error(tmp_path: Path) -> None:
    respx.get("https://newsapi.org/v2/everything").mock(
        return_value=httpx.Response(
            429, json={"status": "error", "code": "rateLimited", "message": "Too many requests"}
        )
    )
    client = NewsApiClient("k", "https://newsapi.org/v2", tmp_path, http=httpx.AsyncClient())
    with pytest.raises(NewsApiError, match="rateLimited"):
        await client.everything("AAPL", '"Apple"', 29)


def test_news_requests_today_counts_cache_files(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    client = NewsApiClient("k", "https://newsapi.org/v2", tmp_path, http=httpx.AsyncClient())
    today = datetime.now(UTC).date().isoformat()
    for t in ("AAPL", "NVDA"):
        (tmp_path / "newsapi" / t).mkdir(parents=True)
        (tmp_path / "newsapi" / t / f"{today}.json").write_text("{}")
    (tmp_path / "newsapi" / "AAPL" / "2020-01-01.json").write_text("{}")
    assert client.requests_today() == 2
    assert client.cached_today("AAPL") and not client.cached_today("TSLA")
