"""Pure comparison of frozen and freshly observed metadata, never media bytes."""
from collections import defaultdict
from copy import deepcopy
import json


_ASSET_FIELDS = ('url', 'source_identity', 'width', 'height', 'traits', 'declarations')
_LIMITATIONS = (
    {'reason': 'metadata_only', 'message': 'Address changes do not prove content changes; unchanged declarations do not prove identical media bytes.'},
    {'reason': 'comparison_scope', 'message': 'Field states cover the target response; assets and motion cover the original selected scope. Related assets are excluded.'},
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def semantic_key(asset):
    return {key: deepcopy(asset.get(key)) for key in ('owner', 'semantic_path', 'kind', 'role')}


def _fact(value=None, *, available=True, present=True):
    return {'available': available, 'present': present if available else False,
            'value': deepcopy(value) if available and present else None}


def _property(record, key):
    return _fact(record[key]) if isinstance(record, dict) and key in record else _fact(available=False)


def asset_projection(asset):
    result = {key: _property(asset, key) for key in _ASSET_FIELDS}
    for key in ('traits', 'declarations'):
        value = result[key]['value']
        if isinstance(value, list):
            if key == 'declarations':
                value = [{name: item[name] for name in ('name', 'value') if name in item} for item in value]
            result[key]['value'] = sorted(value, key=_canonical)
    return result


def pair_assets(left, right, *, left_projection=asset_projection, right_projection=asset_projection):
    """Exact projections, then exact IDs, then a unique remaining semantic pair."""
    left, right = list(left), list(right)
    pairs = []
    for predicate in (lambda a, b: _canonical(left_projection(a)) == _canonical(right_projection(b)),
                      lambda a, b: a.get('asset_id') is not None and a.get('asset_id') == b.get('asset_id')):
        for item in tuple(left):
            matches = [other for other in right if predicate(item, other)]
            if len(matches) == 1:
                other = matches[0]
                pairs.append((item, other))
                left.remove(item)
                right.remove(other)
    if len(left) == len(right) == 1:
        pairs.append((left.pop(), right.pop()))
    return pairs, left, right


def _selected(manifest):
    selection = manifest.get('selection')
    return selection.get('selected') if isinstance(selection, dict) and isinstance(selection.get('selected'), list) else None


def _completed_scan(coverage):
    scan = coverage.get('scan')
    return isinstance(scan, dict) and scan.get('status') == 'completed'


def _interpretable_field(coverage, key):
    """Traversal completion cannot explain an unreadable semantic field."""
    semantic = key.get('semantic_path')
    field = semantic.split('.')[0] if isinstance(semantic, str) else None
    if field not in {'artwork', 'editorialArtwork', 'editorialVideo'}:
        return False
    fields = coverage.get('fields')
    record = fields.get(field) if isinstance(fields, dict) else None
    observations = record.get('observations') if isinstance(record, dict) else None
    if not isinstance(observations, list) or not observations:
        return False
    states = []
    for observation in observations:
        state = observation.get('state') if isinstance(observation, dict) else None
        if state not in ('present', 'missing', 'null', 'empty'):
            return False
        states.append(state)
    summary = states[0] if len(set(states)) == 1 else 'mixed'
    return record.get('state') == summary


def _observed_absence(manifest, selected, key):
    coverage = manifest.get('coverage', {})
    complete = (selected is not None and _completed_scan(coverage)
                and not coverage.get('unknown_candidate_count')
                and manifest.get('source_evidence', {}).get('status') != 'conflict'
                and _interpretable_field(coverage, key))
    return _fact(present=False) if complete else _fact(available=False)


def _groups(assets):
    groups = defaultdict(list)
    for asset in assets or ():
        groups[_canonical(semantic_key(asset))].append(asset)
    return groups


def _inspection(manifest, asset):
    for entry in manifest.get('motion_inspections', ()):
        ids = {entry.get('asset_id'), *(ref.get('asset_id') for ref in entry.get('references', ()))}
        if asset.get('asset_id') in ids:
            return entry
    return None


def _motion(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get('inspection'), dict):
        return {}, True
    value = entry['inspection']
    uncertain = entry.get('status') != 'supported' or bool(entry.get('error')) or bool(value.get('error'))
    projection = {}
    for key in ('status', 'profile', 'master_url', 'resolved_master_url', 'variants', 'selected',
                'layout', 'provisional_layout', 'iframe_variant_count'):
        if key not in value:
            continue
        projection[key] = _property(value, key)
        if key == 'variants' and isinstance(value[key], list):
            projection[key]['value'] = sorted(value[key], key=_canonical)
        if uncertain and key in {'layout', 'provisional_layout', 'selected', 'resolved_master_url'} and value[key] is None:
            projection[key] = _fact(available=False)
        if uncertain and key == 'variants' and not value[key]:
            projection[key] = _fact(available=False)
        if uncertain and key == 'iframe_variant_count' and value.get('resolved_master_url') is None:
            projection[key] = _fact(available=False)
    return projection, uncertain


def _complete_projection(manifest, asset):
    result = {'asset': asset_projection(asset)}
    if asset.get('kind') == 'video':
        result['motion'] = _motion(_inspection(manifest, asset))[0]
    return result


def _fields(manifest):
    coverage = manifest.get('coverage')
    fields = coverage.get('fields') if isinstance(coverage, dict) else None
    if not isinstance(fields, dict):
        return None
    result = {}
    for key, value in fields.items():
        if not isinstance(value, dict) or 'state' not in value:
            result[key] = _fact(available=False)
            continue
        observations = sorted({(item.get('variant'), item.get('state')) for item in value.get('observations', ())}, key=_canonical)
        result[key] = _fact({'state': value['state'], 'observations': [list(item) for item in observations]})
    return result


def compare_snapshots(left, right, *, uncertainties=()):
    """Return comparison facts; a missing snapshot is an unavailable observation."""
    changes, ambiguities, limitations = [], [], deepcopy(list(_LIMITATIONS))
    limitations.extend(deepcopy(list(uncertainties)))
    uncertain = bool(uncertainties)
    a = left.get('manifest', {}) if isinstance(left, dict) else {}
    b = right.get('manifest', {}) if isinstance(right, dict) else {}

    def compare(kind, key, field, before, after):
        nonlocal uncertain
        if not before['available'] or not after['available']:
            uncertain = True
            if kind == 'asset' and field == 'presence':
                limitations.append({'reason': 'asset_presence_unavailable', 'semantic_key': deepcopy(key)})
        if _canonical(before) != _canonical(after):
            changes.append({'kind': kind, 'semantic_key': deepcopy(key), 'field': field,
                            'before': before, 'after': after})

    first, second = _fields(a), _fields(b)
    if first is None or second is None:
        uncertain = True
        limitations.append({'reason': 'field_observation_unavailable'})
    for field in sorted(set(first or ()) | set(second or ())):
        compare('field_state', None, field, (first or {}).get(field, _fact(available=False)),
                (second or {}).get(field, _fact(available=False)))
    for manifest in (a, b):
        coverage = manifest.get('coverage', {})
        if not _completed_scan(coverage) or coverage.get('unknown_candidate_count', 0):
            uncertain = True
            limitations.append({'reason': 'discovery_incomplete'})

    selected_a, selected_b = _selected(a), _selected(b)
    if selected_a is None or selected_b is None:
        uncertain = True
        limitations.append({'reason': 'selection_observation_unavailable'})
    for manifest, selected in ((a, selected_a), (b, selected_b)):
        for asset in selected or ():
            if any(not value['available'] for value in asset_projection(asset).values()):
                uncertain = True
                limitations.append({'reason': 'asset_observation_incomplete', 'semantic_key': semantic_key(asset)})
            if asset.get('kind') == 'video' and _motion(_inspection(manifest, asset))[1]:
                uncertain = True
                limitations.append({'reason': 'motion_observation_incomplete', 'semantic_key': semantic_key(asset)})
    groups_a, groups_b = _groups(selected_a), _groups(selected_b)
    for group in sorted(set(groups_a) | set(groups_b)):
        key = json.loads(group)
        pairs, unpaired_a, unpaired_b = pair_assets(groups_a[group], groups_b[group],
            left_projection=lambda asset: _complete_projection(a, asset),
            right_projection=lambda asset: _complete_projection(b, asset))
        for original, current in pairs:
            old, new = asset_projection(original), asset_projection(current)
            for field in _ASSET_FIELDS:
                compare('asset', key, field, old[field], new[field])
            if key['kind'] == 'video':
                old, old_uncertain = _motion(_inspection(a, original))
                new, new_uncertain = _motion(_inspection(b, current))
                uncertain |= old_uncertain or new_uncertain
                for field in sorted(set(old) | set(new)):
                    compare('motion', key, field, old.get(field, _fact(available=False)), new.get(field, _fact(available=False)))
        if unpaired_a and unpaired_b:
            ambiguities.append({'semantic_key': key, 'reason': 'multiple_candidates',
                                'left_count': len(unpaired_a), 'right_count': len(unpaired_b)})
            uncertain = True
        else:
            for original in unpaired_a:
                compare('asset', key, 'presence', _fact(_complete_projection(a, original)),
                        _observed_absence(b, selected_b, key))
            for current in unpaired_b:
                compare('asset', key, 'presence',
                        _observed_absence(a, selected_a, key), _fact(_complete_projection(b, current)))
    return {'outcome': 'indeterminate' if uncertain else 'observed_change' if changes else 'no_observed_change',
            'changes': changes, 'ambiguities': ambiguities,
            'limitations': list({_canonical(item): item for item in limitations}.values())}
