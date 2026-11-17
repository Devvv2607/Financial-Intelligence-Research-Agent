"""Anonymous visitor id: who owns a thread in the public demo (no login).

The browser generates a random UUID once, keeps it in localStorage and sends it as
`X-Visitor-Id` on every API call. It scopes threads (each visitor sees only their own)
and is one of the two keys of the daily live-run cap. It is not authentication: anyone
who knows an id can act as that visitor, which is why it must be unguessable (a v4 UUID)
and is never shown to other visitors.
"""

import re
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

VISITOR_HEADER = "X-Visitor-Id"
_VISITOR_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")


def visitor_id(x_visitor_id: Annotated[str | None, Header()] = None) -> str:
    """FastAPI dependency: the caller's visitor id, lower-cased. 400 if missing/malformed."""
    if x_visitor_id is None or not _VISITOR_RE.match(x_visitor_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Missing or invalid {VISITOR_HEADER} header (a random UUID from the browser).",
        )
    return x_visitor_id.lower()


VisitorId = Annotated[str, Depends(visitor_id)]
