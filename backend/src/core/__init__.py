"""Shared building blocks used by the API, the agent, ingestion and evals."""

import warnings

try:
    from authlib.deprecate import AuthlibDeprecationWarning
except ImportError:  # pragma: no cover - authlib only arrives via weaviate-client
    pass
else:
    # authlib >= 1.8 warns on import that its httpx integration is moving to `httpx2`, and
    # forces the warning visible with simplefilter("always"). weaviate-client triggers it,
    # not our code, and it is informational. Importing authlib.deprecate first and adding
    # this filter afterwards puts ours in front, so exactly this message is ignored and
    # every other warning still shows. (Pinning authlib < 1.8 would hold back an auth
    # library's releases just to hide a notice.)
    warnings.filterwarnings(
        "ignore",
        message="The httpx module is deprecated",
        category=AuthlibDeprecationWarning,
    )
