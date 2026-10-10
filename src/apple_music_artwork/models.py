"""Transport-independent values and their versioned JSON representation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class ResourceKey:
    storefront: str
    resource_type: str
    resource_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "storefront": self.storefront,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
        }


@dataclass(frozen=True)
class Target:
    key: ResourceKey
    input_url: str
    locale: str | None = None
    album_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key.to_dict(),
            "input_url": self.input_url,
            "locale": self.locale,
            "album_id": self.album_id,
        }


@dataclass(frozen=True, order=True)
class Observation:
    variant: str
    json_pointer: str

    def to_dict(self) -> dict[str, str]:
        return {"variant": self.variant, "json_pointer": self.json_pointer}


@dataclass(frozen=True)
class VisualDeclaration:
    """A response declaration, not a claim about decoded image properties."""

    name: str
    value: bool | str
    observations: tuple[Observation, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "value": self.value,
                "observations": [item.to_dict() for item in self.observations]}


@dataclass(frozen=True, order=True)
class StorefrontObservation:
    variant: str
    json_pointer: str
    field: str
    state: str
    storefront: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"variant": self.variant, "json_pointer": self.json_pointer,
                "field": self.field, "state": self.state,
                "storefront": self.storefront or None}


@dataclass(frozen=True)
class SourceEvidence:
    requested_storefront: str
    requested_locale: str | None = None
    observations: tuple[StorefrontObservation, ...] = ()

    @property
    def observed_storefronts(self) -> tuple[str, ...]:
        return tuple(sorted({item.storefront for item in self.observations
                             if item.field == "href" and item.state == "observed"}))

    @property
    def status(self) -> str:
        hrefs = tuple(item for item in self.observations if item.field == "href")
        if any(item.state == "observed" and item.storefront != self.requested_storefront for item in hrefs):
            return "conflict"
        if hrefs and all(item.state == "observed" for item in hrefs):
            return "verified"
        return "unverified"

    def to_dict(self) -> dict[str, object]:
        return {"requested_storefront": self.requested_storefront,
                "requested_locale": self.requested_locale, "status": self.status,
                "observed_storefronts": list(self.observed_storefronts),
                "observations": [item.to_dict() for item in self.observations]}


@dataclass(frozen=True, order=True)
class FieldObservation:
    field: str
    variant: str
    json_pointer: str
    state: str

    def to_dict(self) -> dict[str, str]:
        return {"variant": self.variant, "json_pointer": self.json_pointer, "state": self.state}


@dataclass(frozen=True, order=True)
class RequestObservation:
    variant: str
    requested_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"variant": self.variant, "requested_fields": list(self.requested_fields),
                "known": bool(self.requested_fields)}


@dataclass(frozen=True)
class DiscoveryCoverage:
    request_observations: tuple[RequestObservation, ...] = ()
    field_observations: tuple[FieldObservation, ...] = ()
    unknown_candidate_count: int = 0
    scan_visited_nodes: int = 0
    scan_limit: int = 50_000
    scan_limited: bool = False

    def to_dict(self) -> dict[str, object]:
        fields = {}
        for field in sorted({item.field for item in self.field_observations}):
            observations = tuple(item for item in self.field_observations if item.field == field)
            states = {item.state for item in observations}
            fields[field] = {"state": next(iter(states)) if len(states) == 1 else "mixed",
                             "observations": [item.to_dict() for item in observations]}
        return {"scope": "returned_response_fields",
                "requested_fields": sorted({field for item in self.request_observations for field in item.requested_fields}),
                "request_observations": [item.to_dict() for item in self.request_observations],
                "fields": fields, "unknown_candidate_count": self.unknown_candidate_count,
                "scan": {"status": "limited" if self.scan_limited else "completed",
                         "visited_nodes": self.scan_visited_nodes, "limit": self.scan_limit}}


@dataclass(frozen=True, order=True)
class Issue:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def positive_integer(value: object) -> bool:
    """JSON booleans are not dimensions, despite Python's bool/int relation."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True)
class VisualAsset:
    owner: ResourceKey
    semantic_path: str
    kind: str
    role: str
    url: str
    source_identity: str
    width: int | None = None
    height: int | None = None
    traits: tuple[str, ...] = ()
    observations: tuple[Observation, ...] = ()
    declarations: tuple[VisualDeclaration, ...] = ()

    @property
    def asset_id(self) -> str:
        # Include the rendition and its declarations. Observation order must not
        # change the identity, and a source URL alone cannot identify a rendition.
        identity = [
            self.owner.storefront,
            self.owner.resource_type,
            self.owner.resource_id,
            self.semantic_path,
            self.kind,
            self.url,
            self.width,
            self.height,
            list(self.traits),
        ]
        encoded = json.dumps(
            identity, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return "asset_" + hashlib.sha256(encoded).hexdigest()

    @property
    def shape(self) -> str:
        if not positive_integer(self.width) or not positive_integer(self.height):
            return "unknown"
        assert self.width is not None and self.height is not None
        if 100 * abs(self.width - self.height) <= 2 * self.height:
            return "square"
        if 2 * self.width >= 3 * self.height:
            return "wide"
        if 5 * self.width <= 4 * self.height:
            return "tall"
        return "other"

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "owner": self.owner.to_dict(),
            "semantic_path": self.semantic_path,
            "kind": self.kind,
            "role": self.role,
            "shape": self.shape,
            "url": self.url,
            "source_identity": self.source_identity,
            "width": self.width,
            "height": self.height,
            "traits": list(self.traits),
            "observations": [item.to_dict() for item in self.observations],
            "declarations": [item.to_dict() for item in self.declarations],
        }


@dataclass(frozen=True)
class DiscoveryResult:
    target: Target
    assets: tuple[VisualAsset, ...] = ()
    related_assets: tuple[VisualAsset, ...] = ()
    warnings: tuple[Issue, ...] = ()
    resource_name: str | None = None
    source_evidence: SourceEvidence | None = None
    coverage: DiscoveryCoverage | None = None

    def to_dict(self) -> dict[str, object]:
        evidence = self.source_evidence or SourceEvidence(self.target.key.storefront, self.target.locale)
        return {
            "schema_version": 2,
            "target": self.target.to_dict(),
            "resource_name": self.resource_name,
            "assets": [asset.to_dict() for asset in self.assets],
            "related_assets": [asset.to_dict() for asset in self.related_assets],
            "warnings": [warning.to_dict() for warning in self.warnings],
            "source_evidence": evidence.to_dict(),
            "coverage": (self.coverage or DiscoveryCoverage()).to_dict(),
        }


@dataclass(frozen=True)
class SelectionRequest:
    semantic_path: str | None = None
    asset_id: str | None = None
    all_static: bool = False
    all_media: bool = False
    all_motion: bool = False


@dataclass(frozen=True)
class RejectedAsset:
    asset_id: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"asset_id": self.asset_id, "reason": self.reason}


@dataclass(frozen=True)
class SelectionDecision:
    policy: str
    selected: tuple[VisualAsset, ...]
    reason: str
    rejected: tuple[RejectedAsset, ...] = ()
    references: tuple[VisualAsset, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy,
            "selected": [asset.to_dict() for asset in self.selected],
            "reason": self.reason,
            "rejected": [asset.to_dict() for asset in self.rejected],
            "references": [asset.to_dict() for asset in self.references],
        }


@dataclass(frozen=True)
class ImageOptions:
    width: int | None = None
    height: int | None = None
    format: str = "jpg"
    crop: str = "bb"


@dataclass(frozen=True)
class ImageRequest:
    url: str
    width: int
    height: int
    format: str
    source_identity: str
    asset_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "width": self.width,
            "height": self.height,
            "format": self.format,
            "source_identity": self.source_identity,
            "asset_id": self.asset_id,
        }
