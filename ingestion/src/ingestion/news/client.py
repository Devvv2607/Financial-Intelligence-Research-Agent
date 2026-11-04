"""NewsAPI client: one cached `everything` query per company per day (DECISIONS D-010).

Free plan: 100 requests/day, ~1 month of history, `content` truncated to ~200 chars.
So ingestion makes exactly one request per company per day (cached on disk under
data/raw/newsapi/), and articles are stored for the agent to read from the store.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

_SUFFIXES = re.compile(
    r"\b(incorporated|inc|corp|corporation|co|company|plc|ltd|limited|llc|holdings?|"
    r"group|n\.?v|s\.?a|the|de|md|ma|mn|oh|tx|nv|new)\b\.?",
    re.I,
)


class NewsApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    description: str | None
    content_snippet: str | None
    url: str
    source_name: str | None
    published_at: datetime


def short_company_name(sec_name: str) -> str:
    """'NVIDIA CORP' -> 'Nvidia', 'Advanced Micro Devices Inc' -> 'Advanced Micro Devices'."""
    name = re.sub(r"/[A-Z]{2,3}/?", " ", sec_name)  # state suffixes like /DE/
    name = _SUFFIXES.sub(" ", name.replace(",", " ").replace("&", " & "))
    name = re.sub(r"\s+", " ", name).strip(" .&")
    return name.title() if name.isupper() else name


def build_query(sec_name: str, ticker: str) -> str:
    q = f'"{short_company_name(sec_name)}"'
    # Tickers shorter than 3 chars (F, T, C, GS...) match too much unrelated text.
    if len(ticker) >= 3 and ticker.isalpha():
        q += f' OR "{ticker}"'
    return q


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def article_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()[:32]


def parse_articles(payload: dict[str, Any]) -> list[Article]:
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    out: list[Article] = []
    for a in payload.get("articles", []):
        url, title = a.get("url"), (a.get("title") or "").strip()
        if not url or not title or title == "[Removed]":
            continue
        aid = article_id(url)
        title_key = re.sub(r"\W+", " ", title.lower()).strip()
        if aid in seen_ids or title_key in seen_titles:  # syndicated duplicates
            continue
        seen_ids.add(aid)
        seen_titles.add(title_key)
        out.append(
            Article(
                id=aid,
                title=title,
                description=a.get("description"),
                content_snippet=a.get("content"),
                url=url,
                source_name=(a.get("source") or {}).get("name"),
                published_at=datetime.fromisoformat(a["publishedAt"].replace("Z", "+00:00")),
            )
        )
    return out


class NewsApiClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        cache_dir: Path,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = api_key
        self._base = base_url
        self._cache_dir = cache_dir / "newsapi"
        self._http = http or httpx.AsyncClient(timeout=20.0)
        self.network_requests = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def everything(
        self, ticker: str, query: str, window_days: int, today: date | None = None
    ) -> dict[str, Any]:
        today = today or datetime.now(UTC).date()
        path = self._cache_dir / ticker / f"{today.isoformat()}.json"
        if path.exists():
            cached: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return cached
        params: dict[str, str | int] = {
            "q": query,
            "searchIn": "title,description",
            "from": (today - timedelta(days=window_days)).isoformat(),
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 50,
        }
        self.network_requests += 1
        resp = await self._http.get(
            f"{self._base}/everything", params=params, headers={"X-Api-Key": self._key}
        )
        payload: dict[str, Any] = resp.json()
        if resp.status_code != 200 or payload.get("status") != "ok":
            raise NewsApiError(
                f"NewsAPI HTTP {resp.status_code}: {payload.get('code')} {payload.get('message')}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload
