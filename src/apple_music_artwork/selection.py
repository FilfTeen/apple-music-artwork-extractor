"""Explicit, versioned product policies; no claim of a universal best cover."""

from __future__ import annotations

from .errors import ArtworkError
from .models import DiscoveryResult, RejectedAsset, SelectionDecision, SelectionRequest, VisualAsset, positive_integer

_PRIMARY_PATHS = {
    "playlists": ("editorialArtwork.staticDetailSquare", "artwork", "editorialArtwork.subscriptionCover"),
    "albums": ("artwork",),
    "artists": ("artwork",),
    "songs": ("artwork",),
}


def _error(code: str, message: str) -> ArtworkError:
    return ArtworkError(code, message, "selection")


def _order(asset: VisualAsset) -> tuple[object, ...]:
    pixels = asset.width * asset.height if positive_integer(asset.width) and positive_integer(asset.height) else 0
    return (-pixels, asset.url.count("{"), asset.url, asset.asset_id)


def _single_source(candidates: tuple[VisualAsset, ...]) -> None:
    if len({(asset.kind, asset.source_identity) for asset in candidates}) > 1:
        raise _error("AMBIGUOUS_PRIMARY_ASSET", "Several sources have equal semantic priority; choose an asset ID.")


def select(result: DiscoveryResult, request: SelectionRequest) -> SelectionDecision:
    if any(not isinstance(value, bool) for value in (request.all_static, request.all_media, request.all_motion)):
        raise ArtworkError("INVALID_SELECTION_OPTIONS", "Media selection switches must be booleans.", "input")
    for value in (request.semantic_path, request.asset_id):
        if value is not None and (not isinstance(value, str) or not value):
            raise ArtworkError("INVALID_SELECTION_OPTIONS", "Explicit selectors must be nonempty strings.", "input")
    if sum((request.semantic_path is not None, request.asset_id is not None,
            request.all_static, request.all_media, request.all_motion)) > 1:
        raise ArtworkError("INVALID_SELECTION_OPTIONS", "Choose one explicit selection mode.", "input")
    if result.source_evidence is not None and result.source_evidence.status == "conflict":
        raise ArtworkError("RESOURCE_STOREFRONT_CONFLICT", "The target catalog href conflicts with the requested storefront; downloads are blocked.", "discovery")
    owned = tuple(asset for asset in result.assets if asset.owner == result.target.key)
    known_images = tuple(asset for asset in owned if asset.kind == "image" and asset.role != "unknown")
    known_motion = tuple(asset for asset in owned if asset.kind == "video" and asset.role == "motion")
    if request.asset_id is not None:
        selected = tuple(asset for asset in owned if asset.asset_id == request.asset_id)
        if not selected:
            raise _error("ASSET_NOT_FOUND", "The requested asset ID is not present on the target.")
        if any(asset.kind not in {"image", "video"} for asset in selected):
            raise _error("UNSUPPORTED_ASSET_KIND", "The selected asset is not an identified image or video.")
        policy, reason = "explicit-asset-v1", "The caller selected this exact asset ID."
    elif request.semantic_path is not None:
        selected = tuple(asset for asset in owned if asset.semantic_path == request.semantic_path)
        if not selected:
            raise _error("ASSET_NOT_FOUND", "The requested semantic path is not present on the target.")
        if any(asset.kind not in {"image", "video"} for asset in selected):
            raise _error("UNSUPPORTED_ASSET_KIND", "The selected semantic path contains an unidentified media kind.")
        if all(asset.kind == "image" for asset in selected):
            _single_source(selected)
        selected = tuple(sorted(selected, key=_order))
        policy, reason = "explicit-semantic-v1", "The caller selected this semantic path; distinct renditions and independent motion sources are retained."
    elif request.all_static:
        selected = known_images
        if not selected:
            raise _error("NO_VISUAL_ASSET", "The target has no known static image assets.")
        policy, reason = "all-static-v1", "All known target-owned images are retained, including distinct renditions."
    elif request.all_media:
        selected = tuple(asset for asset in owned if (asset.kind == "image" and asset.role != "unknown")
                         or (asset.kind == "video" and asset.role == "motion"))
        if not selected:
            raise _error("NO_VISUAL_ASSET", "The target has no known image or motion assets.")
        policy, reason = "all-media-v1", "All known target-owned images and independent motion sources are retained."
    elif request.all_motion:
        selected = known_motion
        if not selected:
            raise _error("NO_MOTION_ASSET", "The observed response has no known target-owned motion asset.")
        policy, reason = "all-motion-v1", "All known target-owned motion sources are retained; matching shapes do not imply matching animations."
    else:
        paths = _PRIMARY_PATHS.get(result.target.key.resource_type)
        if paths is None:
            raise _error("UNSUPPORTED_RESOURCE", "No primary selection policy exists for this resource type.")
        selected = ()
        chosen_path = ""
        for path in paths:
            candidates = tuple(asset for asset in known_images if asset.semantic_path == path)
            if candidates:
                _single_source(candidates)
                selected = (min(candidates, key=_order),)
                chosen_path = path
                break
        if not selected:
            raise _error("NO_PRIMARY_STATIC_ASSET", "The target has no image covered by its primary selection policy.")
        policy = result.target.key.resource_type.removesuffix("s") + "-primary-v1"
        reason = f"Policy selected {chosen_path}; declaration size and concrete template preference only break ties within that semantic source."
    source_keys = {(asset.kind, asset.source_identity) for asset in selected}
    references = tuple(asset for asset in owned if (asset.kind, asset.source_identity) in source_keys)
    selected_ids = {asset.asset_id for asset in selected}
    reference_ids = {asset.asset_id for asset in references}
    rejected = tuple(
        RejectedAsset(asset.asset_id, "same_source_reference" if asset.asset_id in reference_ids else "not_selected_by_policy")
        for asset in result.assets if asset.asset_id not in selected_ids
    )
    return SelectionDecision(policy, selected, reason, rejected, references)
