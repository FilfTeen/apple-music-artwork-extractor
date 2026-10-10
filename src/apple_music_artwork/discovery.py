"""Read catalog resource envelopes while retaining ownership and observations."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from .errors import ArtworkError
from .models import (
    DiscoveryCoverage, DiscoveryResult, FieldObservation, Issue, Observation,
    RequestObservation, ResourceKey, SourceEvidence, StorefrontObservation, Target,
    VisualAsset, VisualDeclaration, positive_integer,
)
from .rendition import image_host, source_identity
from .urls import parse_url

_EDITORIAL_FIELDS = {
    "playlists": {
        "emailFeature", "staticDetailSquare", "staticDetailTall", "storeFlowcase",
        "subscriptionCover", "subscriptionHero", "superHeroTall", "superHeroWide",
    },
    "albums": {
        "emailFeature", "staticDetailSquare", "staticDetailTall", "storeFlowcase",
        "subscriptionHero", "superHeroTall",
    },
    "artists": {
        "bannerUber", "centeredFullscreenBackground", "originalFlowcaseBrick",
        "storeFlowcase", "subscriptionHero",
    },
    "songs": set(),
}
_MOTION_FIELDS = {
    "playlists": {"motionDetailSquare", "motionDetailTall", "motionSquareVideo1x1", "motionWideVideo21x9"},
    "albums": {"motionDetailSquare", "motionDetailTall", "motionSquareVideo1x1", "motionTallVideo3x4"},
    "artists": {"motionArtistFullscreen16x9", "motionArtistSquare1x1", "motionArtistWide16x9"},
    "songs": set(),
}
_VISUAL_WORDS = re.compile(r"artwork|image|cover|logo|hero|banner|visual|motion|video|preview|masterart|thumbnail|poster", re.I)
_AUDIO_EXTENSIONS = (".m4a", ".mp3", ".aac", ".wav", ".flac", ".ogg", ".aiff")
_VIDEO_EXTENSIONS = (".m3u8", ".mp4", ".mov")
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic", ".gif")
_VISUAL_FIELDS = ("artwork", "editorialArtwork", "editorialVideo")
_COLOR_FIELDS = ("bgColor", "textColor1", "textColor2", "textColor3", "textColor4")
MAX_CANDIDATE_NODES = 50_000


def _pointer(path: tuple[str, ...]) -> str:
    return "".join("/" + part.replace("~", "~0").replace("/", "~1") for part in path)


def _schema_error(message: str) -> ArtworkError:
    return ArtworkError("API_SCHEMA_CHANGED", message, "discovery")


def _url_kind(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        path = parsed.path.lower()
        if host == "audio-ssl.itunes.apple.com" or host.endswith(".audio-ssl.itunes.apple.com") or path.endswith(_AUDIO_EXTENSIONS):
            return None
        if host in {"music.apple.com", "itunes.apple.com"}:
            return None
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or parsed.port is not None:
            return "unknown"
        if host == "mvod.itunes.apple.com" or host.endswith(".mvod.itunes.apple.com"):
            return "video"
        if image_host(host):
            return "video" if path.endswith(_VIDEO_EXTENSIONS) else "image"
        return "unknown"
    except ValueError:
        return "unknown"


def _role(resource_type: str, semantic: tuple[str, ...], kind: str) -> str:
    if semantic == ("artwork",) and kind == "image":
        return "primary_static"
    if len(semantic) == 2 and semantic[0] == "editorialArtwork" and kind == "image":
        field = semantic[1]
        if field not in _EDITORIAL_FIELDS.get(resource_type, set()):
            return "unknown"
        if field in {"subscriptionHero", "superHeroTall", "superHeroWide"}:
            return "hero"
        if field == "bannerUber":
            return "banner"
        return "editorial_static"
    if len(semantic) == 3 and semantic[0] == "editorialVideo":
        if semantic[1] not in _MOTION_FIELDS.get(resource_type, set()):
            return "unknown"
        if semantic[2] == "previewFrame" and kind == "image":
            return "motion_preview"
        if semantic[2] == "video" and kind == "video":
            return "motion"
    return "unknown"


def _merge_declarations(declarations: Iterable[VisualDeclaration]) -> tuple[VisualDeclaration, ...]:
    merged: dict[tuple[str, bool | str], VisualDeclaration] = {}
    for declaration in declarations:
        key = (declaration.name, declaration.value)
        previous = merged.get(key)
        observations = set(declaration.observations)
        if previous is not None:
            observations.update(previous.observations)
        merged[key] = replace(declaration, observations=tuple(sorted(observations)))
    return tuple(sorted(merged.values(), key=lambda item: (item.name, type(item.value).__name__, str(item.value))))


def _merge_assets(assets: Iterable[VisualAsset]) -> tuple[VisualAsset, ...]:
    merged: dict[str, VisualAsset] = {}
    for asset in assets:
        existing = merged.get(asset.asset_id)
        if existing is None:
            merged[asset.asset_id] = replace(asset, observations=tuple(sorted(set(asset.observations))),
                                             declarations=_merge_declarations(asset.declarations))
        else:
            observations = tuple(sorted(set(existing.observations) | set(asset.observations)))
            declarations = _merge_declarations((*existing.declarations, *asset.declarations))
            merged[asset.asset_id] = replace(existing, observations=observations, declarations=declarations)
    return tuple(sorted(merged.values(), key=lambda a: (a.owner, a.semantic_path, a.kind, a.asset_id)))


def _declaration_conflicts(assets: Iterable[VisualAsset]) -> set[Issue]:
    values: dict[tuple[ResourceKey, str, str], set[bool | str]] = {}
    for asset in assets:
        for declaration in asset.declarations:
            values.setdefault((asset.owner, asset.url, declaration.name), set()).add(declaration.value)
    if any(len(items) > 1 for items in values.values()):
        return {Issue("VISUAL_DECLARATION_CONFLICT", "Visual references disagree about an image declaration; all observations are retained.")}
    return set()


def _href_storefront(resource: dict[str, Any], resource_type: str, resource_id: str) -> tuple[str, str]:
    if "href" not in resource:
        return "missing", ""
    raw = resource["href"]
    if raw is None:
        return "null", ""
    if not isinstance(raw, str) or len(raw) > 8192 or any(ord(char) <= 32 or ord(char) == 127 for char in raw):
        return "malformed", ""
    try:
        parsed = urlsplit(raw)
        if parsed.netloc:
            if (parsed.scheme != "https" or parsed.hostname not in {"api.music.apple.com", "amp-api.music.apple.com"}
                    or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)):
                return "malformed", ""
        elif parsed.scheme or not parsed.path.startswith("/"):
            return "malformed", ""
        if parsed.fragment:
            return "malformed", ""
        parts = parsed.path.rstrip("/").split("/")
        if (len(parts) != 6 or parts[:3] != ["", "v1", "catalog"]
                or re.fullmatch(r"[a-z]{2}", parts[3]) is None
                or unquote(parts[4], errors="strict") != resource_type
                or unquote(parts[5], errors="strict") != resource_id):
            return "malformed", ""
        return "observed", parts[3]
    except (ValueError, UnicodeError):
        return "malformed", ""


def _page_storefront(attributes: dict[str, Any], resource_type: str, resource_id: str) -> tuple[str, str]:
    if "url" not in attributes:
        return "missing", ""
    if attributes["url"] is None:
        return "null", ""
    try:
        key = parse_url(attributes["url"]).key
        if (key.resource_type, key.resource_id) != (resource_type, resource_id):
            return "malformed", ""
        return "observed", key.storefront
    except ArtworkError:
        return "malformed", ""


def _field_state(attributes: dict[str, Any], name: str) -> str:
    if name not in attributes:
        return "missing"
    value = attributes[name]
    if value is None:
        return "null"
    if not isinstance(value, dict):
        return "malformed"
    if not value:
        return "empty"
    if name == "artwork":
        return "present" if isinstance(value.get("url"), str) and "://" in value["url"] else "malformed"
    return "present" if all(isinstance(item, dict) for item in value.values()) else "malformed"


def discover(payload: Any, target: Target, variant: str = "import", *,
             requested_fields: tuple[str, ...] | list[str] = ()) -> DiscoveryResult:
    if not isinstance(payload, dict):
        raise _schema_error("Catalog response must be an object.")
    if not isinstance(variant, str) or not variant or len(variant) > 128 or any(ord(c) < 32 for c in variant):
        raise ArtworkError("INVALID_VARIANT", "Variant must be a short nonempty label.", "input")
    if "data" not in payload and "resources" not in payload:
        raise _schema_error("Catalog response has neither data nor a resources map.")
    if not isinstance(requested_fields, (tuple, list)) or any(field not in _VISUAL_FIELDS for field in requested_fields):
        raise ArtworkError("INVALID_REQUEST_FIELDS", "Requested fields must be known visual field names.", "input")
    requested_fields = tuple(sorted(set(requested_fields)))
    found_target = False
    names: set[str] = set()
    assets: list[VisualAsset] = []
    warnings: set[Issue] = set()
    source_observations: set[StorefrontObservation] = set()
    field_observations: set[FieldObservation] = set()
    observed_resource_fronts: dict[tuple[str, str], set[str]] = {}
    unverified_asset_indices: set[int] = set()
    scan_visited_nodes = 0
    scan_limited = False

    def observe_url(
        url: str,
        owner: ResourceKey,
        semantic: tuple[str, ...],
        path: tuple[str, ...],
        container: dict[str, Any],
    ) -> None:
        kind = _url_kind(url)
        if kind is None:
            return
        role = _role(owner.resource_type, semantic, kind)
        width, height = container.get("width"), container.get("height")
        if width is not None and not positive_integer(width):
            warnings.add(Issue("INVALID_DIMENSIONS", "A visual field has an invalid declared width."))
            width = None
        if height is not None and not positive_integer(height):
            warnings.add(Issue("INVALID_DIMENSIONS", "A visual field has an invalid declared height."))
            height = None
        raw_traits = container.get("imageTraits", [])
        if not isinstance(raw_traits, list) or any(not isinstance(x, str) for x in raw_traits):
            warnings.add(Issue("INVALID_IMAGE_TRAITS", "A visual field has invalid image traits."))
            traits = ()
        else:
            traits = tuple(sorted(set(raw_traits)))
        if role == "unknown":
            warnings.add(Issue("UNKNOWN_VISUAL_FIELD", "A visual field has no known semantic classification."))
        declarations = []
        for name in ("hasP3", *_COLOR_FIELDS):
            if name not in container:
                continue
            value = container[name]
            valid = isinstance(value, bool) if name == "hasP3" else (
                isinstance(value, str) and re.fullmatch(r"[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?", value) is not None)
            if valid:
                declarations.append(VisualDeclaration(name, value, (Observation(variant, _pointer((*path[:-1], name))),)))
            else:
                warnings.add(Issue("INVALID_VISUAL_DECLARATION", "A visual declaration has an unsupported value."))
        assets.append(VisualAsset(
            owner=owner,
            semantic_path=".".join(semantic),
            kind=kind,
            role=role,
            url=url,
            source_identity=source_identity(url, kind),
            width=width,
            height=height,
            traits=traits,
            observations=(Observation(variant, _pointer(path)),),
            declarations=tuple(declarations),
        ))

    def walk_visual(value: Any, owner: ResourceKey, semantic: tuple[str, ...], path: tuple[str, ...], depth: int = 0) -> None:
        if depth > 64:
            raise _schema_error("Visual response nesting exceeds the supported depth.")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise _schema_error("Catalog object keys must be strings.")
                child_semantic = semantic if key == "url" else (*semantic, key)
                child_path = (*path, key)
                if isinstance(child, str) and "://" in child:
                    observe_url(child, owner, child_semantic, child_path, value)
                elif isinstance(child, (dict, list)):
                    walk_visual(child, owner, child_semantic, child_path, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk_visual(child, owner, (*semantic, str(index)), (*path, str(index)), depth + 1)
        elif isinstance(value, str) and "://" in value:
            observe_url(value, owner, semantic, path, {})

    def scan_unconsumed(value: Any, owner: ResourceKey, semantic: tuple[str, ...],
                        path: tuple[str, ...], container: dict[str, Any] | None = None, depth: int = 0) -> None:
        nonlocal scan_visited_nodes, scan_limited
        if scan_visited_nodes >= MAX_CANDIDATE_NODES or depth > 64:
            scan_limited = True
            return
        scan_visited_nodes += 1
        if isinstance(value, dict):
            for key, child in value.items():
                if scan_limited:
                    break
                if not isinstance(key, str):
                    raise _schema_error("Catalog object keys must be strings.")
                child_semantic = semantic if key == "url" else (*semantic, key)
                scan_unconsumed(child, owner, child_semantic, (*path, key), value, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if scan_limited:
                    break
                scan_unconsumed(child, owner, (*semantic, str(index)), (*path, str(index)), depth=depth + 1)
        elif isinstance(value, str) and "://" in value:
            kind = _url_kind(value)
            if kind is None:
                return
            try:
                media_extension = urlsplit(value).path.lower().endswith((*_IMAGE_EXTENSIONS, *_VIDEO_EXTENSIONS))
            except ValueError:
                media_extension = False
            if kind in {"image", "video"} or media_extension or _VISUAL_WORDS.search(".".join(semantic)):
                observe_url(value, owner, semantic, path, container or {})

    def read_resource(value: Any, path: tuple[str, ...], hint: ResourceKey | None = None, depth: int = 0) -> None:
        nonlocal found_target
        if depth > 64:
            raise _schema_error("Resource response nesting exceeds the supported depth.")
        if not isinstance(value, dict):
            raise _schema_error("Catalog resource entries must be objects.")
        resource_type = value.get("type", hint.resource_type if hint else None)
        resource_id = value.get("id", hint.resource_id if hint else None)
        if not isinstance(resource_type, str) or not resource_type or not isinstance(resource_id, str) or not resource_id:
            raise _schema_error("A catalog resource is missing its string type or ID.")
        if hint is not None and (resource_type, resource_id) != (hint.resource_type, hint.resource_id):
            raise ArtworkError("RESOURCE_IDENTITY_CONFLICT", "A resources-map entry disagrees with its type or ID key.", "discovery")
        href_state, observed_storefront = _href_storefront(value, resource_type, resource_id)
        resource_identity = (resource_type, resource_id)
        target_identity = (target.key.resource_type, target.key.resource_id)
        if observed_storefront:
            observed_resource_fronts.setdefault(resource_identity, set()).add(observed_storefront)
        owner = ResourceKey(observed_storefront or target.key.storefront, resource_type, resource_id)
        if resource_identity == target_identity and ("href" in value or "attributes" in value):
            source_observations.add(StorefrontObservation(variant, _pointer((*path, "href")), "href", href_state, observed_storefront))
        if "attributes" in value:
            attributes = value["attributes"]
            if not isinstance(attributes, dict):
                raise _schema_error("Resource attributes must be an object.")
            if resource_identity == target_identity:
                found_target = True
                page_state, page_storefront = _page_storefront(attributes, resource_type, resource_id)
                source_observations.add(StorefrontObservation(variant, _pointer((*path, "attributes", "url")), "attributes.url", page_state, page_storefront))
                if page_storefront and page_storefront != (observed_storefront or target.key.storefront):
                    warnings.add(Issue("PUBLIC_URL_STOREFRONT_CONFLICT", "The public resource URL names a different storefront; it does not override catalog href evidence."))
                for field in _VISUAL_FIELDS:
                    state = _field_state(attributes, field)
                    field_observations.add(FieldObservation(field, variant, _pointer((*path, "attributes", field)), state))
                    if state == "malformed":
                        warnings.add(Issue("MALFORMED_VISUAL_FIELD", "A visual field has an unexpected representation; inspect field coverage."))
                name = attributes.get("name")
                if isinstance(name, str):
                    names.add(name)
            first_asset = len(assets)
            for key, visual in attributes.items():
                if not isinstance(key, str):
                    raise _schema_error("Resource attribute keys must be strings.")
                if key in _VISUAL_FIELDS:
                    walk_visual(visual, owner, (key,), (*path, "attributes", key))
                else:
                    scan_unconsumed(visual, owner, (key,), (*path, "attributes", key))
            if not observed_storefront:
                unverified_asset_indices.update(range(first_asset, len(assets)))
        relationships = value.get("relationships", {})
        if not isinstance(relationships, dict):
            raise _schema_error("Resource relationships must be an object.")
        for relationship, linked in relationships.items():
            if not isinstance(relationship, str) or not isinstance(linked, dict):
                raise _schema_error("Resource relationships must contain named objects.")
            if "data" in linked:
                linked_data = linked["data"]
                linked_path = (*path, "relationships", relationship, "data")
                if isinstance(linked_data, list):
                    for index, entry in enumerate(linked_data):
                        read_resource(entry, (*linked_path, str(index)), depth=depth + 1)
                elif isinstance(linked_data, dict):
                    read_resource(linked_data, linked_path, depth=depth + 1)
                elif linked_data is not None:
                    raise _schema_error("Relationship data must be a resource, array or null.")

    if "data" in payload:
        data = payload["data"]
        if not isinstance(data, list):
            raise _schema_error("Catalog data must be an array.")
        for index, resource in enumerate(data):
            read_resource(resource, ("data", str(index)))
    if "resources" in payload:
        resource_map = payload["resources"]
        if not isinstance(resource_map, dict):
            raise _schema_error("Catalog resources must be a map.")
        for resource_type, mapping in resource_map.items():
            if not isinstance(resource_type, str) or not isinstance(mapping, dict):
                raise _schema_error("Resource-type map entries must be objects.")
            for resource_id, resource in mapping.items():
                if not isinstance(resource_id, str):
                    raise _schema_error("Resource map IDs must be strings.")
                hint = ResourceKey(target.key.storefront, resource_type, resource_id)
                read_resource(resource, ("resources", resource_type, resource_id), hint)
    if "included" in payload:
        included = payload["included"]
        if not isinstance(included, list):
            raise _schema_error("Included resources must be an array.")
        for index, resource in enumerate(included):
            read_resource(resource, ("included", str(index)))
    if not found_target:
        raise ArtworkError("RESOURCE_NOT_FOUND", "The response has no attributes for the requested resource.", "discovery")
    if len(names) > 1:
        warnings.add(Issue("RESOURCE_METADATA_CONFLICT", "The response contains multiple names for the requested resource."))
    # Map attributes may rely on a catalog href carried by a separate resource
    # reference. Apply only unambiguous evidence for the same type/ID.
    for index in unverified_asset_indices:
        asset = assets[index]
        storefronts = observed_resource_fronts.get((asset.owner.resource_type, asset.owner.resource_id), set())
        if len(storefronts) == 1:
            owner = ResourceKey(next(iter(storefronts)), asset.owner.resource_type, asset.owner.resource_id)
            assets[index] = replace(asset, owner=owner)
    def is_target(asset):
        return (asset.owner.resource_type, asset.owner.resource_id) == (target.key.resource_type, target.key.resource_id)
    target_assets = _merge_assets(a for a in assets if is_target(a))
    related_assets = _merge_assets(a for a in assets if not is_target(a))
    evidence = SourceEvidence(target.key.storefront, target.locale, tuple(sorted(source_observations)))
    if evidence.status == "conflict":
        warnings.add(Issue("RESOURCE_STOREFRONT_CONFLICT", "The target catalog href names a different storefront from the request; downloads are blocked."))
    elif evidence.status == "unverified":
        warnings.add(Issue("STOREFRONT_UNVERIFIED", "The target response does not fully verify its storefront through catalog href evidence."))
    if scan_limited:
        warnings.add(Issue("VISUAL_SCAN_LIMIT", "Unconsumed visual candidate scanning reached its bound; coverage is limited."))
    warnings.update(_declaration_conflicts((*target_assets, *related_assets)))
    if not target_assets:
        warnings.add(Issue("NO_VISUAL_ASSET", "No target-owned visual asset was found."))
    coverage = DiscoveryCoverage((RequestObservation(variant, requested_fields),), tuple(sorted(field_observations)),
                                 sum(asset.role == "unknown" for asset in target_assets), scan_visited_nodes,
                                 MAX_CANDIDATE_NODES, scan_limited)
    return DiscoveryResult(target, target_assets, related_assets, tuple(sorted(warnings)), min(names) if names else None,
                           evidence, coverage)


def merge_discoveries(results: Iterable[DiscoveryResult]) -> DiscoveryResult:
    results = tuple(results)
    if not results:
        raise ArtworkError("NO_RESPONSES", "At least one discovery result is required.", "input")
    target = results[0].target
    if any(result.target != target for result in results):
        raise ArtworkError("TARGET_MISMATCH", "Only results for the same target and locale can be merged.", "input")
    warnings = {warning for result in results for warning in result.warnings}
    names = {result.resource_name for result in results if result.resource_name is not None}
    if len(names) > 1:
        warnings.add(Issue("RESOURCE_METADATA_CONFLICT", "Variants contain multiple names for the requested resource."))
    assets = _merge_assets(asset for result in results for asset in result.assets)
    related_assets = _merge_assets(asset for result in results for asset in result.related_assets)
    warnings.update(_declaration_conflicts((*assets, *related_assets)))
    evidence = set()
    for result in results:
        if result.source_evidence is None:
            evidence.add(StorefrontObservation("unspecified", "", "href", "missing"))
        else:
            evidence.update(result.source_evidence.observations)
    coverages = {result.coverage for result in results if result.coverage is not None}
    coverage = DiscoveryCoverage(
        tuple(sorted({item for value in coverages for item in value.request_observations})),
        tuple(sorted({item for value in coverages for item in value.field_observations})),
        sum(asset.role == "unknown" for asset in assets),
        sum(value.scan_visited_nodes for value in coverages),
        sum(value.scan_limit for value in coverages) or MAX_CANDIDATE_NODES,
        any(value.scan_limited for value in coverages),
    )
    return DiscoveryResult(
        target,
        assets,
        related_assets,
        tuple(sorted(warnings)),
        min(names) if names else None,
        SourceEvidence(target.key.storefront, target.locale, tuple(sorted(evidence))),
        coverage,
    )
