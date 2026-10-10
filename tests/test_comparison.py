"""Synthetic metadata comparison; no file or network observations are implied."""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from apple_music_artwork.comparison import compare_snapshots


def asset(label='a', **changes):
    return {'asset_id': label, 'owner': {'storefront': 'us', 'resource_type': 'albums', 'resource_id': '1'},
            'semantic_path': 'artwork', 'kind': 'image', 'role': 'primary', 'url': 'https://a.mzstatic.com/a.jpg',
            'source_identity': 'a', 'width': 100, 'height': 100, 'traits': [], 'declarations': [], **changes}


def snapshot(*assets, state='present'):
    return {'created_at': '2026-01-01T00:00:00Z', 'manifest': {
        'coverage': {'fields': {'artwork': {'state': state, 'observations': [
            {'variant': 'catalog', 'state': state, 'json_pointer': '/data/0/attributes/artwork'}]}},
            'scan': {'status': 'completed'}, 'unknown_candidate_count': 0},
        'selection': {'selected': list(assets)}, 'motion_inspections': []}}


class ComparisonTests(TestCase):
    def setUp(self):
        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            guard = patch(name, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_url_is_change_in_same_semantic_asset_not_add_delete(self):
        result = compare_snapshots(snapshot(asset()), snapshot(asset('b', url='https://a.mzstatic.com/b.jpg')))
        self.assertEqual(result['outcome'], 'observed_change')
        self.assertEqual([c['field'] for c in result['changes']], ['url'])

    def test_same_asset_id_color_declaration_changes(self):
        left = snapshot(asset(declarations=[{'name': 'bgColor', 'value': 'ffffff', 'observations': []}]))
        right = snapshot(asset(declarations=[{'name': 'bgColor', 'value': '000000', 'observations': []}]))
        original = deepcopy((left, right))
        result = compare_snapshots(left, right)
        self.assertEqual([c['field'] for c in result['changes']], ['declarations'])
        self.assertEqual((left, right), original)

    def test_missing_null_empty_and_unavailable_are_distinct(self):
        for before, after in [('missing', 'null'), ('null', 'empty'), ('empty', 'present')]:
            result = compare_snapshots(snapshot(asset(), state=before), snapshot(asset(), state=after))
            change = result['changes'][0]
            self.assertEqual(change['kind'], 'field_state')
            self.assertTrue(change['before']['available'])
        right = snapshot(asset())
        del right['manifest']['coverage']
        result = compare_snapshots(snapshot(asset()), right)
        self.assertEqual(result['outcome'], 'indeterminate')
        self.assertFalse(result['changes'][0]['after']['available'])

    def test_unavailable_response_never_reports_assets_removed(self):
        result = compare_snapshots(snapshot(asset()), None)
        self.assertEqual(result['outcome'], 'indeterminate')
        self.assertFalse(any(c['field'] == 'presence' and c['after']['available'] for c in result['changes']))

    def test_exact_matches_removed_before_unique_pair(self):
        first, second = asset('one'), asset('two', width=200)
        result = compare_snapshots(snapshot(first, second), snapshot(asset('three', width=300), first))
        self.assertEqual([c['field'] for c in result['changes']], ['width'])
        self.assertEqual(result['outcome'], 'observed_change')

    def test_remaining_many_candidates_are_ambiguous_and_keep_known_changes(self):
        left = snapshot(asset('same'), asset('a', width=1), asset('b', width=2))
        right = snapshot(asset('same', height=2), asset('c', width=3), asset('d', width=4))
        result = compare_snapshots(left, right)
        self.assertEqual(result['outcome'], 'indeterminate')
        self.assertEqual([c['field'] for c in result['changes']], ['height'])
        self.assertTrue(result['ambiguities'])

    def test_order_and_provenance_positions_do_not_change_observations(self):
        left = snapshot(asset(), asset('b', width=200))
        right = deepcopy(left)
        right['manifest']['selection']['selected'].reverse()
        right['manifest']['coverage']['fields']['artwork']['observations'][0]['json_pointer'] = '/data/90/attributes/artwork'
        self.assertEqual(compare_snapshots(left, right)['outcome'], 'no_observed_change')

    def test_unselected_and_related_asset_changes_are_outside_asset_scope(self):
        left, right = snapshot(asset()), snapshot(asset())
        left['manifest']['assets'] = [asset('unselected')]
        right['manifest']['assets'] = [asset('changed')]
        right['manifest']['related_assets'] = [asset('related')]
        self.assertEqual(compare_snapshots(left, right)['outcome'], 'no_observed_change')

    def test_dynamic_specs_variant_order_and_partial_head_failure(self):
        video = asset(kind='video')
        left = snapshot(video)
        inspection = {'status': 'supported', 'master_url': 'https://mvod.itunes.apple.com/a.m3u8',
            'resolved_master_url': 'https://mvod.itunes.apple.com/a.m3u8', 'profile': 'best',
            'variants': [{'width': 100}, {'width': 200}], 'selected': {'width': 200},
            'layout': {'content_length': 1000, 'ranges': [{'offset': 0, 'length': 1000}]}}
        left['manifest']['motion_inspections'] = [{'asset_id': 'a', 'inspection': inspection, 'status': 'supported'}]
        right = deepcopy(left)
        right['manifest']['motion_inspections'][0]['inspection']['variants'].reverse()
        self.assertEqual(compare_snapshots(left, right)['outcome'], 'no_observed_change')
        entry = right['manifest']['motion_inspections'][0]
        entry['status'] = entry['inspection']['status'] = 'failed'
        entry['inspection']['selected']['width'] = 300
        entry['inspection']['layout'] = None
        result = compare_snapshots(left, right)
        self.assertEqual(result['outcome'], 'indeterminate')
        self.assertTrue(any(c['field'] == 'selected' for c in result['changes']))
        self.assertFalse(next(c for c in result['changes'] if c['field'] == 'layout')['after']['available'])

    def test_exact_pairing_includes_motion_specs_before_same_id_pairing(self):
        left = snapshot(asset('a', kind='video'), asset('b', kind='video'))
        right = snapshot(asset('a', kind='video'), asset('b', kind='video'))
        def entries(widths):
            return [{'asset_id': name, 'status': 'supported', 'inspection': {
                'status': 'supported', 'selected': {'width': width}, 'variants': [], 'layout': {'content_length': width}}}
                for name, width in widths]
        left['manifest']['motion_inspections'] = entries([('a', 100), ('b', 200)])
        right['manifest']['motion_inspections'] = entries([('a', 200), ('b', 100)])
        self.assertEqual(compare_snapshots(left, right)['outcome'], 'no_observed_change')

    def test_added_motion_with_incomplete_inspection_is_not_complete_observation(self):
        left = snapshot(asset())
        right = snapshot(asset(), asset('new', kind='video'))
        self.assertEqual(compare_snapshots(left, right)['outcome'], 'indeterminate')

    def test_unknown_property_versus_null_and_empty_is_preserved(self):
        left = snapshot(asset())
        del left['manifest']['selection']['selected'][0]['declarations']
        for value in (None, []):
            right = snapshot(asset(declarations=value))
            data = compare_snapshots(left, right)
            self.assertEqual(data['outcome'], 'indeterminate')
            change = next(c for c in data['changes'] if c['field']=='declarations')
            self.assertFalse(change['before']['available'])
            self.assertTrue(change['after']['available'])
            self.assertEqual(change['after']['value'], value)

    def test_dynamic_byte_ranges_keep_order(self):
        left = snapshot(asset(kind='video'))
        left['manifest']['motion_inspections'] = [{'asset_id': 'a', 'status': 'supported', 'inspection': {
            'status': 'supported', 'layout': {'ranges': [{'offset': 0, 'length': 4}, {'offset': 4, 'length': 3}]}}}]
        right = deepcopy(left)
        right['manifest']['motion_inspections'][0]['inspection']['layout']['ranges'].reverse()
        self.assertEqual(compare_snapshots(left, right)['outcome'], 'observed_change')

    def test_truncated_discovery_does_not_prove_absence(self):
        left, right = snapshot(asset()), snapshot()
        right['manifest']['coverage']['scan']['status'] = 'limited'
        result = compare_snapshots(left, right)
        self.assertEqual(result['outcome'], 'indeterminate')
        self.assertFalse(next(c for c in result['changes'] if c['field']=='presence')['after']['available'])

    def test_failed_master_does_not_make_default_empty_variants_an_observation(self):
        left = snapshot(asset(kind='video'))
        entry = {'asset_id': 'a', 'status': 'supported', 'inspection': {'status': 'supported',
            'resolved_master_url': 'https://mvod.itunes.apple.com/a.m3u8', 'variants': [{'width': 100}],
            'selected': {'width': 100}, 'layout': {'content_length': 100}}}
        left['manifest']['motion_inspections'] = [entry]
        right = deepcopy(left)
        right['manifest']['motion_inspections'][0].update(status='failed', inspection={
            'status': 'failed', 'resolved_master_url': None, 'variants': [], 'selected': None, 'layout': None})
        result = compare_snapshots(left, right)
        self.assertFalse(next(c for c in result['changes'] if c['field']=='variants')['after']['available'])

    def test_missing_or_unknown_scan_keeps_absence_unavailable_in_both_directions(self):
        complete = snapshot(asset('changed', url='https://a.mzstatic.com/new.jpg'),
                            asset('new', semantic_path='editorialArtwork.staticDetailSquare'))
        fields = deepcopy(snapshot(asset())['manifest']['coverage']['fields'])
        for label, coverage in [('empty_coverage', {}), ('missing_scan', {'fields': fields}),
                ('empty_scan', {'fields': fields, 'scan': {}}),
                ('null_scan', {'fields': fields, 'scan': None}),
                ('unknown_scan', {'fields': fields, 'scan': {'status': 'unknown'}}),
                ('null_status', {'fields': fields, 'scan': {'status': None}})]:
            incomplete = snapshot(asset())
            incomplete['manifest']['coverage'] = deepcopy(coverage)
            for reverse in (False, True):
                with self.subTest(coverage=label, reverse=reverse):
                    left, right = (complete, incomplete) if reverse else (incomplete, complete)
                    original = deepcopy((left, right))
                    result = compare_snapshots(left, right)
                    presence = next(change for change in result['changes'] if change['field']=='presence')
                    unknown = presence['after' if reverse else 'before']
                    self.assertEqual(unknown, {'available': False, 'present': False, 'value': None})
                    self.assertEqual(result['outcome'], 'indeterminate')
                    self.assertTrue(any(change['field']=='url' for change in result['changes']))
                    self.assertEqual((left, right), original)

    def test_completed_scan_proves_absence_in_both_directions(self):
        smaller = snapshot(asset())
        larger = snapshot(asset(), asset('new', semantic_path='editorialArtwork.staticDetailSquare'))
        for current, state in ((smaller, 'missing'), (larger, 'present')):
            current['manifest']['coverage']['fields']['editorialArtwork'] = {
                'state': state, 'observations': [{'variant': 'catalog', 'state': state}]}
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                result = compare_snapshots(*( (larger, smaller) if reverse else (smaller, larger)))
                presence = next(change for change in result['changes'] if change['field']=='presence')
                self.assertEqual(presence['after' if reverse else 'before'],
                                 {'available': True, 'present': False, 'value': None})
                self.assertEqual(result['outcome'], 'observed_change')

    def test_unknown_scan_is_indeterminate_even_when_known_assets_match(self):
        left, right = snapshot(asset()), snapshot(asset())
        del left['manifest']['coverage']['scan']
        result = compare_snapshots(left, right)
        self.assertEqual(result['outcome'], 'indeterminate')
        self.assertEqual(result['changes'], [])


class AbsenceEvidenceTests(TestCase):
    setUp = ComparisonTests.setUp

    def snapshots(self, state, observations=None, *, semantic='editorialArtwork.staticDetailSquare'):
        larger = snapshot(asset(), asset('editorial', semantic_path=semantic))
        smaller = snapshot(asset(url='https://a.mzstatic.com/changed.jpg'))
        field = semantic.split('.')[0]
        larger['manifest']['coverage']['fields'][field] = {
            'state': 'present', 'observations': [{'variant': 'catalog', 'state': 'present'}]}
        smaller['manifest']['coverage']['fields'][field] = {
            'state': state, 'observations': observations if observations is not None else [
                {'variant': 'catalog', 'state': state}]}
        return larger, smaller

    def check_presence(self, larger, smaller, *, available):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                left, right = (smaller, larger) if reverse else (larger, smaller)
                original = deepcopy((left, right))
                result = compare_snapshots(left, right)
                presence = next(change for change in result['changes'] if change['field'] == 'presence')
                absence = presence['before' if reverse else 'after']
                self.assertEqual(absence, {'available': available, 'present': False, 'value': None})
                self.assertEqual(result['outcome'], 'observed_change' if available else 'indeterminate')
                self.assertTrue(any(change['field'] == 'url' and change['before']['available']
                                    and change['after']['available'] for change in result['changes']))
                self.assertEqual((left, right), original)
                if not available:
                    self.assertTrue(any(item['reason'] == 'asset_presence_unavailable'
                                        for item in result['limitations']))

    def test_completed_malformed_or_unavailable_field_does_not_prove_absence(self):
        for state in ('malformed', 'unavailable', 'unknown'):
            with self.subTest(state=state):
                larger, smaller = self.snapshots(state)
                self.check_presence(larger, smaller, available=False)
                change = next(c for c in compare_snapshots(larger, smaller)['changes']
                              if c['kind'] == 'field_state' and c['field'] == 'editorialArtwork')
                self.assertTrue(change['after']['available'])
                self.assertEqual(change['after']['value']['state'], state)

    def test_mixed_field_requires_every_observation_to_be_interpretable(self):
        for state in ('malformed', 'unavailable', 'unknown', None):
            with self.subTest(state=state):
                self.check_presence(*self.snapshots('mixed', [
                    {'variant': 'catalog', 'state': 'present'},
                    {'variant': 'web', 'state': state}]), available=False)
        # Even a misleading summary cannot override an unreadable portion.
        self.check_presence(*self.snapshots('empty', [
            {'variant': 'catalog', 'state': 'empty'},
            {'variant': 'web', 'state': 'malformed'}]), available=False)

    def test_missing_field_or_observation_evidence_remains_unknown(self):
        for missing in ('field', 'state', 'observations', 'empty_observations'):
            with self.subTest(missing=missing):
                larger, smaller = self.snapshots('empty')
                fields = smaller['manifest']['coverage']['fields']
                if missing == 'field':
                    del fields['editorialArtwork']
                elif missing == 'empty_observations':
                    fields['editorialArtwork']['observations'] = []
                else:
                    del fields['editorialArtwork'][missing]
                self.check_presence(larger, smaller, available=False)

    def test_interpretable_missing_null_empty_present_and_mixed_are_positive_controls(self):
        for state in ('missing', 'null', 'empty', 'present'):
            with self.subTest(state=state):
                self.check_presence(*self.snapshots(state), available=True)
        self.check_presence(*self.snapshots('mixed', [
            {'variant': 'catalog', 'state': 'present'},
            {'variant': 'web', 'state': 'null'}]), available=True)

    def test_only_corresponding_visual_field_controls_absence(self):
        larger, smaller = self.snapshots('null')
        for current in (larger, smaller):
            current['manifest']['coverage']['fields']['editorialVideo'] = {
                'state': 'malformed', 'observations': [{'variant': 'catalog', 'state': 'malformed'}]}
        self.check_presence(larger, smaller, available=True)
        for semantic in ('artwork', 'editorialVideo.motionDetailSquare.previewFrame'):
            with self.subTest(semantic=semantic):
                self.check_presence(*self.snapshots('malformed', semantic=semantic), available=False)
