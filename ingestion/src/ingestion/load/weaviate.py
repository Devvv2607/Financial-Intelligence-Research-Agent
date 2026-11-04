"""Weaviate loading: replace a company's objects per collection (idempotent).

Delete-then-insert by CIK makes a re-run converge exactly to the current Postgres
state (no orphans when a re-extraction yields fewer chunks). UUIDs are deterministic
(uuid5 of a natural key) so object ids are stable across runs for citations.
"""

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import weaviate
from numpy.typing import NDArray
from weaviate.classes.data import DataObject
from weaviate.classes.query import Filter
from weaviate.util import generate_uuid5

BATCH = 200


class WeaviateLoadError(RuntimeError):
    pass


@dataclass
class PendingObjects:
    collection: str
    keys: list[str] = field(default_factory=list)
    props: list[dict[str, Any]] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)  # embedding inputs

    def add(self, key: str, props: dict[str, Any], embed_text: str) -> None:
        self.keys.append(key)
        self.props.append(props)
        self.texts.append(embed_text)


async def replace_company_objects(
    client: weaviate.WeaviateAsyncClient,
    pending: PendingObjects,
    vectors: NDArray[np.float32],
    cik: int,
) -> int:
    col = client.collections.get(pending.collection)
    await col.data.delete_many(where=Filter.by_property("cik").equal(cik))
    inserted = 0
    for start in range(0, len(pending.keys), BATCH):
        objs = [
            DataObject(
                properties=pending.props[i],
                uuid=generate_uuid5(f"{pending.collection}:{pending.keys[i]}"),
                vector=vectors[i].tolist(),
            )
            for i in range(start, min(start + BATCH, len(pending.keys)))
        ]
        result = await col.data.insert_many(objs)
        if result.has_errors:
            first = next(iter(result.errors.values()))
            raise WeaviateLoadError(f"{pending.collection}: {len(result.errors)} errors: {first}")
        inserted += len(objs)
    return inserted
