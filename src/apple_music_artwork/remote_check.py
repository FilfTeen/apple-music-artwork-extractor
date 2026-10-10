"""Read-only fresh metadata observation within a saved task's original scope."""
from argparse import Namespace
from copy import deepcopy

from .client import AppleMusicClient
from .comparison import compare_snapshots, pair_assets, semantic_key
from .errors import ArtworkError
from .execution import _asset_value, _inspect_motion_assets, _new_manifest, EXIT_CODES
from .models import DiscoveryResult, SelectionRequest, SourceEvidence, StorefrontObservation
from .run_state import utc_now
from .selection import select
from .urls import parse_url


def snapshot_reference(task):
    version = task.get('version')
    return {'task_ref': f"{task['task_id']}@{version}" if version is not None else None,
            'version': version, 'snapshot_created_at': task['snapshot'].get('created_at')}


def _selection(original, fresh, identity, target):
    """Keep known declarations when selection is uncertain; never follow fallback."""
    values = fresh['assets']
    assets = tuple(_asset_value(value, target.key) for value in values)
    evidence = fresh['source_evidence']
    source = SourceEvidence(evidence['requested_storefront'], evidence['requested_locale'],
        tuple(StorefrontObservation(**{**item, 'storefront': item['storefront'] or ''}) for item in evidence['observations']))
    result = DiscoveryResult(target, assets, source_evidence=source)
    request = SelectionRequest(**identity['selection'])
    selected_before = original['selection']['selected']
    limitations = []
    try:
        decision = select(result, request)
    except ArtworkError as error:
        decision = None
        limitations.append({'reason': 'selection_unresolved', 'code': error.code})
    collection = request.all_static or request.all_media or request.all_motion
    if collection:
        # Reuse the original eligible-set rules even when the new set is empty.
        candidates = [value for value in values if value['owner'] == target.key.to_dict() and (
            (not request.all_motion and value['kind'] == 'image' and value['role'] != 'unknown') or
            (not request.all_static and value['kind'] == 'video' and value['role'] == 'motion'))]
    else:
        anchors = [semantic_key(value) for value in selected_before]
        candidates = [value for value in values if semantic_key(value) in anchors]
    if source.status == 'conflict':
        return candidates, [], limitations + [{'reason': 'source_evidence_conflict'}]

    # Original exact identity must remain provable; a replacement is comparison-only.
    if request.asset_id and not any(value['asset_id'] == request.asset_id for value in candidates):
        return candidates, [], limitations + [{'reason': 'exact_selection_unavailable'}]
    if request.asset_id:
        candidates = [value for value in candidates if value['asset_id'] == request.asset_id]
    if not collection and decision is not None:
        chosen = decision.to_dict()['selected']
        if not request.asset_id and not request.semantic_path:
            if [semantic_key(value) for value in chosen] != [semantic_key(value) for value in selected_before]:
                return candidates, [], limitations + [{'reason': 'primary_selection_changed'}]
            # Primary chooses one rendition within the frozen semantic anchor.
            # Other same-source sizes are references, not new selected assets.
            candidates = chosen
    if decision is None:
        return candidates, [], limitations

    inspect_ids = set()
    keys = []
    for value in selected_before + candidates:
        key = semantic_key(value)
        if key not in keys:
            keys.append(key)
    for key in keys:
        before = [value for value in selected_before if semantic_key(value) == key]
        after = [value for value in candidates if semantic_key(value) == key]
        pairs, old, new = pair_assets(before, after)
        inspect_ids.update(current['asset_id'] for _, current in pairs)
        if (old and new) or (not collection and (old or new)):
            limitations.append({'reason': 'selection_mapping_ambiguous', 'semantic_key': key})
        elif collection:
            inspect_ids.update(value['asset_id'] for value in new)
    return candidates, [asset for asset in assets if asset.asset_id in inspect_ids and asset.kind == 'video'], limitations


def check_updates(task, *, http, environ, token_env='APPLE_MUSIC_DEVELOPER_TOKEN'):
    """Return query data and mapped failures without writing any task state."""
    identity = task['identity']
    scope = {'resource_metadata': False, 'hls_and_head': False, 'media_bodies': False}
    right, errors, limitations = None, [], []
    if identity['provider'] == 'offline' or identity['source']['kind'] == 'offline':
        errors.append(ArtworkError('OFFLINE_SOURCE', 'An imported response has no remote provider to recheck.', 'input').to_dict())
        limitations.append({'reason': 'offline_source'})
    else:
        original = task['snapshot']['manifest']
        target = parse_url(original['target']['input_url'])
        options = {key: value for key, value in identity['options'].items() if key != 'crop'}
        args = Namespace(response=None, provider=identity['provider'], list=False, **options)
        count = http.request_count
        try:
            view = http.fresh_metadata_view()
            client = AppleMusicClient(view, provider=identity['provider'], developer_token=environ.get(token_env) if identity['provider'] == 'catalog' else None)
            fresh = _new_manifest(args, target, client, None)
            right = {'created_at': utc_now(), 'manifest': fresh}
        except ArtworkError as error:
            errors.append(error.to_dict())
            limitations.append({'reason': 'resource_observation_failed'})
        finally:
            scope['resource_metadata'] = http.request_count > count
        if right is not None:
            candidates, motion, issues = _selection(original, fresh, identity, target)
            limitations.extend(issues)
            fresh['selection'] = {'selected': deepcopy(candidates)}
            count = view.request_count
            _inspect_motion_assets(motion, fresh, view, identity['options']['motion_profile'])
            scope['hls_and_head'] = view.request_count > count
            errors.extend(fresh['errors'])
            if errors:
                limitations.append({'reason': 'motion_observation_failed'})
    data = {'task_id': task['task_id'], 'left': snapshot_reference(task),
            'right': {'snapshot_created_at': right['created_at'] if right else None},
            **compare_snapshots(task['snapshot'], right, uncertainties=limitations), 'network_scope': scope}
    data['limitations'].append({'reason': 'declared_metadata_boundary',
        'message': 'Declared media is blocked before reading. A server mislabelling media as allowed text cannot be ruled out.'})
    code = (130 if any(error['code'] == 'CANCELLED' for error in errors) else
            EXIT_CODES.get(errors[0]['category'], 1) if errors else 0)
    return data, errors, code
