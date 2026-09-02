from __future__ import annotations

from dataclasses import dataclass

from aegora_runtime.config import CollectionSettings, Settings


@dataclass(frozen=True)
class CollectionRegistry:
    collections: dict[str, CollectionSettings]

    def get(self, name: str) -> CollectionSettings:
        try:
            return self.collections[name]
        except KeyError as exc:
            raise KeyError(f"unknown collection: {name}") from exc

    def postgres(self) -> list[CollectionSettings]:
        return [item for item in self.collections.values() if item.owner == "postgres"]

    def strapi(self) -> list[CollectionSettings]:
        return [item for item in self.collections.values() if item.owner == "strapi"]

    def runtime(self) -> list[CollectionSettings]:
        return [item for item in self.collections.values() if item.runtime]

    def needs_embedding(self) -> list[CollectionSettings]:
        return [item for item in self.collections.values() if item.needs_embedding]


def collection_registry(settings: Settings) -> CollectionRegistry:
    return CollectionRegistry(settings.collections)
