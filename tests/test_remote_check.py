"""Readonly queries against real synthetic saved tasks; socket I/O is forbidden."""
from argparse import Namespace
from copy import deepcopy
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from apple_music_artwork.client import HttpClient, HttpResponse
from apple_music_artwork.jsonio import dumps_json
from apple_music_artwork.workflow import dispatch
from test_execution import response, NoNetwork
from test_download import image_bytes
import test_task_queries


class MetadataTransport:
    def __init__(self, body, *, status=200):
        self.body, self.status, self.requests = body, status, []

    def request(self, url, **options):
        self.requests.append(('GET', url))
        if '.mzstatic.com/' in url:
            return HttpResponse(200, {'Content-Type': 'image/jpeg'}, image_bytes())
        return HttpResponse(self.status, {'Content-Type': 'application/json'}, dumps_json(self.body).encode())

    def request_guarded(self, url, *, before_body, control, deadline, category, on_progress=None, **options):
        value = self.request(url, **options)
        if value.status == 200:
            before_body(url, value.status, value.headers)
            control.received(len(value.body))
        return value


class RemoteCheckTests(TestCase):
    tree = test_task_queries.TaskQueryTests.tree
    task_path = test_task_queries.TaskQueryTests.task_path

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.library = self.root / 'library'
        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            guard = patch(name, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def create(self, body=None, *, new_version=False, offline=False, **selector):
        body = body or response()
        args = Namespace(command='download', url='https://music.apple.com/us/album/example/1?l=en-US',
            library=str(self.library), provider='catalog', new_version=new_version,
            no_input=True, json=True, progress='never', **selector)
        if offline:
            source = self.root / 'source.json'
            source.write_text(dumps_json(body))
            args.response = str(source)
        result, code = dispatch(args, http=HttpClient(MetadataTransport(body)), environ={'APPLE_MUSIC_DEVELOPER_TOKEN': 'synthetic'})
        self.assertEqual(code, 0, result)
        return result['control']['resume']['task_ref']

    def query(self, reference, body=None, *, status=200, **options):
        transport = MetadataTransport(body or response(), status=status)
        old = self.tree()
        with patch('apple_music_artwork.library.allocate', side_effect=AssertionError('allocation forbidden')), \
             patch('apple_music_artwork.run_state.RunState.__enter__', side_effect=AssertionError('writer forbidden')), \
             patch('fcntl.flock', side_effect=AssertionError('lock forbidden')):
            result, code = dispatch(Namespace(command='tasks.check-updates', task_ref=reference,
                library=str(self.library), **options), http=HttpClient(transport),
                environ={'APPLE_MUSIC_DEVELOPER_TOKEN': 'synthetic'})
        self.assertEqual(self.tree(), old)
        return result, code, transport

    def test_same_snapshot_and_two_explicit_versions_readonly(self):
        first = self.create()
        changed = response()
        changed['data'][0]['attributes']['artwork']['url'] = 'https://a.mzstatic.com/changed/{w}x{h}.jpg'
        second = self.create(changed, new_version=True)
        original = self.tree()
        for left, right, outcome in [(first, first, 'no_observed_change'), (first, second, 'observed_change')]:
            result, code = dispatch(Namespace(command='tasks.diff', left_ref=left, right_ref=right, library=str(self.library)),
                                    http=HttpClient(NoNetwork()), environ={})
            self.assertEqual(code, 0, result)
            self.assertEqual(result['schema_version'], 1)
            self.assertEqual(result['data']['outcome'], outcome)
            self.assertEqual(result['data']['network_scope'], {'resource_metadata': False, 'hls_and_head': False, 'media_bodies': False})
        self.assertEqual(original, self.tree())

    def test_admitted_legacy_scan_gaps_remain_readonly_and_do_not_prove_absence(self):
        first = self.create(all_static=True)
        fresh = response(2)
        fresh['data'][0]['attributes']['artwork']['url'] = 'https://a.mzstatic.com/new/{w}x{h}.jpg'
        second = self.create(fresh, new_version=True, all_static=True)
        path = self.task_path(first)
        state_file = path / '.apple-artwork-state' / 'state.json'
        original_state = state_file.read_bytes()
        for empty_coverage in (False, True):
            with self.subTest(empty_coverage=empty_coverage):
                state_file.write_bytes(original_state)
                def remove_scan(payload):
                    manifest = payload['snapshot']['manifest']
                    if empty_coverage:
                        manifest['coverage'] = {}
                    else:
                        del manifest['coverage']['scan']
                test_task_queries.TaskQueryTests.rewrite_state(self, path, remove_scan)
                before = self.tree()
                for reverse in (False, True):
                    left, right = (second, first) if reverse else (first, second)
                    with patch('fcntl.flock', side_effect=AssertionError('lock forbidden')), \
                         patch('apple_music_artwork.run_state.RunState.__enter__', side_effect=AssertionError('writer forbidden')):
                        result, code = dispatch(Namespace(command='tasks.diff', left_ref=left, right_ref=right,
                            library=str(self.library)), http=HttpClient(NoNetwork()), environ={})
                    self.assertEqual(code, 0, result)
                    self.assertEqual(result['data']['outcome'], 'indeterminate')
                    presence = next(change for change in result['data']['changes'] if change['field']=='presence')
                    self.assertFalse(presence['after' if reverse else 'before']['available'])
                    self.assertTrue(any(change['field']=='url' for change in result['data']['changes']))
                    self.assertEqual(self.tree(), before)
                result, code, _ = self.query(first, fresh)
                self.assertEqual(code, 0, result)
                self.assertEqual(result['data']['outcome'], 'indeterminate')
                presence = next(change for change in result['data']['changes'] if change['field']=='presence')
                self.assertFalse(presence['before']['available'])
                self.assertTrue(any(change['field']=='url' for change in result['data']['changes']))
                self.assertEqual(self.tree(), before)

    def test_diff_rejects_missing_version_different_identity_and_directory(self):
        reference = self.create()
        for options in [{'left_ref': reference.split('@')[0], 'right_ref': reference},
                        {'left_ref': 'f'*64+'@1', 'right_ref': reference},
                        {'left_ref': reference, 'right_ref': reference, 'task_dir': str(self.task_path(reference))}]:
            result, code = dispatch(Namespace(command='tasks.diff', library=str(self.library), **options), http=HttpClient(NoNetwork()), environ={})
            self.assertEqual(code, 2, result)

    def test_original_provider_locale_selection_and_no_media(self):
        reference = self.create()
        body = response(2)
        result, code, transport = self.query(reference, body, provider='web', all_media=True)
        self.assertEqual(code, 0, result)
        self.assertTrue(all('api.music.apple.com/v1/catalog/us/albums/1' in url and 'l=en-US' in url for _, url in transport.requests))
        self.assertFalse(any(c['kind'] == 'asset' for c in result['data']['changes']))
        self.assertNotIn('version', result['data']['right'])
        self.assertNotIn('task_ref', result['data']['right'])
        self.assertEqual(result['data']['network_scope'], {'resource_metadata': True, 'hls_and_head': False, 'media_bodies': False})

    def test_http_failure_is_indeterminate_not_deletion(self):
        reference = self.create()
        result, code, transport = self.query(reference, status=404)
        self.assertEqual(code, 4, result)
        self.assertEqual(result['data']['outcome'], 'indeterminate')
        self.assertIsNone(result['data']['right']['snapshot_created_at'])
        self.assertFalse(any(c['field']=='presence' and c['after']['available'] for c in result['data']['changes']))

    def test_offline_source_refused_before_http(self):
        result, code, transport = self.query(self.create(offline=True))
        self.assertEqual(code, 2, result)
        self.assertIn('offline_source', [item['reason'] for item in result['data']['limitations']])
        self.assertFalse(transport.requests)
        self.assertFalse(result['data']['network_scope']['resource_metadata'])

    def test_exact_id_disappearance_keeps_url_change_and_uncertainty(self):
        from apple_music_artwork.discovery import discover
        from apple_music_artwork.urls import parse_url
        body = response()
        item = discover(body, parse_url('https://music.apple.com/us/album/example/1?l=en-US')).assets[0]
        reference = self.create(asset_id=item.asset_id)
        body['data'][0]['attributes']['artwork']['url'] = 'https://a.mzstatic.com/new/{w}x{h}.jpg'
        result, code, _ = self.query(reference, body)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['data']['outcome'], 'indeterminate')
        self.assertTrue(any(c['field']=='url' for c in result['data']['changes']))

    def test_scope_rules_allow_new_all_static_asset_and_conflicting_locators_fail(self):
        reference = self.create(all_static=True)
        result, code, _ = self.query(reference, response(2))
        self.assertEqual(code, 0, result)
        self.assertTrue(any(c['field']=='presence' and c['after']['present'] for c in result['data']['changes']))
        result, code, transport = self.query(reference, task_dir=str(self.task_path(reference)))
        self.assertEqual(code, 2, result)
        self.assertFalse(transport.requests)

    def test_exact_directory_and_custom_token_env_are_supported(self):
        reference = self.create()
        transport = MetadataTransport(response())
        original = self.tree()
        result, code = dispatch(Namespace(command='tasks.check-updates', task_dir=str(self.task_path(reference)),
            token_env='SYNTHETIC_KEY'), http=HttpClient(transport), environ={'SYNTHETIC_KEY': 'synthetic'})
        self.assertEqual(code, 0, result)
        self.assertEqual(original, self.tree())
        self.assertNotIn('SYNTHETIC_KEY', str(result))

    def test_saved_collection_diff_and_updates_keep_malformed_presence_unknown_readonly(self):
        complete = response(2)
        unreadable = response()
        unreadable['data'][0]['attributes']['editorialArtwork'] = {'malformed': 'unknown-structure'}
        unreadable['data'][0]['attributes']['artwork']['url'] = 'https://a.mzstatic.com/changed/{w}x{h}.jpg'
        first = self.create(complete, all_static=True)
        second = self.create(unreadable, new_version=True, all_static=True)
        before = self.tree()
        for reverse in (False, True):
            left, right = (second, first) if reverse else (first, second)
            with self.subTest(reverse=reverse), \
                 patch('fcntl.flock', side_effect=AssertionError('lock forbidden')), \
                 patch('apple_music_artwork.run_state.RunState.__enter__', side_effect=AssertionError('writer forbidden')):
                result, code = dispatch(Namespace(command='tasks.diff', left_ref=left, right_ref=right,
                    library=str(self.library)), http=HttpClient(NoNetwork()), environ={})
            self.assertEqual(code, 0, result)
            self.assertEqual(result['schema_version'], 1)
            self.assertEqual(result['data']['outcome'], 'indeterminate')
            presence = next(c for c in result['data']['changes'] if c['field'] == 'presence')
            self.assertFalse(presence['before' if reverse else 'after']['available'])
            self.assertTrue(any(c['kind'] == 'field_state' and c['field'] == 'editorialArtwork'
                                for c in result['data']['changes']))
            self.assertTrue(any(c['field'] == 'url' for c in result['data']['changes']))
            reference, body = (second, complete) if reverse else (first, unreadable)
            result, code, transport = self.query(reference, body)
            self.assertEqual(code, 0, result)
            self.assertEqual(result['schema_version'], 1)
            self.assertEqual(result['data']['outcome'], 'indeterminate')
            presence = next(c for c in result['data']['changes'] if c['field'] == 'presence')
            self.assertFalse(presence['before' if reverse else 'after']['available'])
            self.assertEqual(len(transport.requests), 1)
            self.assertEqual(self.tree(), before)


def synthetic_task(body, *, provider='catalog', **selection):
    """A synthetic original snapshot assembled by the actual discovery/selection model."""
    from apple_music_artwork.discovery import discover
    from apple_music_artwork.models import SelectionRequest
    from apple_music_artwork.selection import select
    from apple_music_artwork.urls import parse_url
    from apple_music_artwork.run_state import fingerprint
    target = parse_url('https://music.apple.com/us/album/example/1?l=en-US')
    if body['data'][0]['type'] == 'playlists':
        target = parse_url('https://music.apple.com/us/playlist/example/pl.example?l=en-US')
    result = discover(body, target, variant='catalog' if provider=='catalog' else 'typed-extend-web')
    request = SelectionRequest(**selection)
    options = {'width': None, 'height': None, 'format': 'jpg', 'crop': 'bb', 'motion_profile': 'best'}
    identity = {'provider': provider, 'source': {'kind': 'remote'}, 'options': options,
                'selection': vars(request)}
    manifest = {**result.to_dict(), 'selection': select(result, request).to_dict(), 'motion_inspections': [], 'errors': []}
    return {'task_id': fingerprint(identity), 'version': 1, 'identity': identity,
            'snapshot': {'created_at': '2026-01-01T00:00:00Z', 'manifest': manifest}}


class SelectionObservationTests(TestCase):
    def setUp(self):
        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            guard = patch(name, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def check(self, task, body, transport=None):
        from apple_music_artwork.remote_check import check_updates
        transport = transport or MetadataTransport(body)
        result = check_updates(task, http=HttpClient(transport, clock=lambda: 1000),
                               environ={'APPLE_MUSIC_DEVELOPER_TOKEN': 'synthetic'})
        return (*result, transport)

    def test_primary_same_source_renditions_keep_only_highest_selected_scope(self):
        body = response()
        body['data'] = [deepcopy(body['data'][0]) for _ in range(3)]
        for item, width in zip(body['data'], (1000, 2000, 3000)):
            item['attributes']['artwork'].update(width=width, height=width)
        task = synthetic_task(body)
        self.assertEqual(len(task['snapshot']['manifest']['assets']), 3)
        self.assertEqual([item['width'] for item in task['snapshot']['manifest']['selection']['selected']], [3000])
        result, errors, code, _ = self.check(task, body)
        self.assertEqual(code, 0, errors)
        self.assertEqual(result['outcome'], 'no_observed_change')
        self.assertEqual(result['changes'], [])
        body['data'][2]['attributes']['artwork'].update(width=4000, height=4000)
        result, errors, code, _ = self.check(task, body)
        self.assertEqual(code, 0, errors)
        self.assertEqual(result['outcome'], 'observed_change')
        self.assertEqual({item['field'] for item in result['changes']}, {'width', 'height'})
        self.assertFalse(result['ambiguities'])

    def test_primary_does_not_follow_playlist_fallback(self):
        body = response(2)
        body['data'][0].update(id='pl.example', type='playlists', href='/v1/catalog/us/playlists/pl.example')
        task = synthetic_task(body)
        del body['data'][0]['attributes']['editorialArtwork']
        result, errors, code, transport = self.check(task, body)
        self.assertEqual(code, 0, errors)
        self.assertEqual(result['outcome'], 'indeterminate')
        self.assertIn('primary_selection_changed', [item['reason'] for item in result['limitations']])
        self.assertFalse(any(c['kind']=='asset' and c['semantic_key']['semantic_path']=='artwork' for c in result['changes']))
        self.assertEqual(len(transport.requests), 1)

    def test_semantic_disappearance_retains_field_changes(self):
        body = response(2)
        task = synthetic_task(body, semantic_path='editorialArtwork.staticDetailSquare')
        body['data'][0]['attributes']['editorialArtwork'] = None
        result, errors, code, _ = self.check(task, body)
        self.assertEqual(code, 0, errors)
        self.assertEqual(result['outcome'], 'indeterminate')
        self.assertTrue(any(c['kind']=='field_state' and c['field']=='editorialArtwork' for c in result['changes']))

    def test_semantic_multisource_is_indeterminate_and_keeps_known_color_changes(self):
        body = response()
        task = synthetic_task(body, semantic_path='artwork')
        second = deepcopy(body['data'][0])
        second['attributes']['artwork']['url'] = 'https://a.mzstatic.com/another/{w}x{h}.jpg'
        body['data'].append(second)
        body['data'][0]['attributes']['artwork']['bgColor'] = 'abcdef'
        result, errors, code, _ = self.check(task, body)
        self.assertEqual(code, 0, errors)
        self.assertEqual(result['outcome'], 'indeterminate')
        self.assertTrue(any(c['field']=='declarations' for c in result['changes']))

    def test_unselected_motion_never_requires_optional_parser_or_hls(self):
        body = response()
        body['data'][0]['attributes']['editorialVideo'] = {'motionDetailSquare': {'video': 'https://mvod.itunes.apple.com/a.m3u8'}}
        task = synthetic_task(body)
        with patch('apple_music_artwork.hls.require_parser', side_effect=AssertionError('unselected motion')):
            result, errors, code, transport = self.check(task, body)
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(transport.requests), 1)
        self.assertFalse(result['network_scope']['hls_and_head'])

    def test_original_web_provider_uses_guarded_page_bundle_and_web_api(self):
        from test_client import token
        class WebTransport(MetadataTransport):
            def request(self, url, **options):
                self.requests.append(('GET', url))
                if url == 'https://music.apple.com/':
                    return HttpResponse(200, {'Content-Type': 'text/html'}, b'<script src="/assets/app.js"></script>')
                if url.endswith('.js'):
                    return HttpResponse(200, {'Content-Type': 'application/javascript'}, token('synthetic').encode())
                return HttpResponse(200, {'Content-Type': 'application/json'}, dumps_json(self.body).encode())
        body = response()
        result, errors, code, transport = self.check(synthetic_task(body, provider='web'), body, WebTransport(body))
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(transport.requests), 3)
        self.assertIn('amp-api.music.apple.com/v1/catalog/us/albums/1', transport.requests[-1][1])
        self.assertNotIn('Bearer', str(result))

    def test_missing_credential_has_no_attempted_network_scope(self):
        from apple_music_artwork.remote_check import check_updates
        transport = MetadataTransport(response())
        data, errors, code = check_updates(synthetic_task(response()), http=HttpClient(transport), environ={})
        self.assertEqual(code, 3)
        self.assertFalse(transport.requests)
        self.assertFalse(data['network_scope']['resource_metadata'])

    def test_actual_discovery_mixed_malformed_and_empty_collection_presence(self):
        for shape in ('malformed', 'mixed', 'null', 'empty', 'missing'):
            complete, fresh = response(2), response()
            attrs = fresh['data'][0]['attributes']
            attrs['artwork']['url'] = 'https://a.mzstatic.com/changed/{w}x{h}.jpg'
            if shape in ('malformed', 'mixed'):
                attrs['editorialArtwork'] = {'malformed': 'unknown-structure'}
                if shape == 'mixed':
                    other = deepcopy(fresh['data'][0])
                    other['attributes']['editorialArtwork'] = {}
                    fresh['data'].append(other)
            elif shape != 'missing':
                attrs['editorialArtwork'] = None if shape == 'null' else {}
            for reverse in (False, True):
                with self.subTest(shape=shape, reverse=reverse):
                    old, current = (fresh, complete) if reverse else (complete, fresh)
                    task = synthetic_task(old, all_static=True)
                    frozen = deepcopy(task)
                    result, errors, code, transport = self.check(task, current)
                    self.assertEqual((code, errors), (0, []))
                    known = shape in ('null', 'empty', 'missing')
                    self.assertEqual(result['outcome'], 'observed_change' if known else 'indeterminate')
                    presence = next(c for c in result['changes'] if c['field'] == 'presence')
                    self.assertEqual(presence['before' if reverse else 'after'],
                                     {'available': known, 'present': False, 'value': None})
                    self.assertTrue(any(c['kind'] == 'field_state' and c['field'] == 'editorialArtwork'
                                        for c in result['changes']))
                    self.assertTrue(any(c['field'] == 'url' and c['before']['available'] and c['after']['available']
                                        for c in result['changes']))
                    self.assertEqual(len(transport.requests), 1)
                    self.assertFalse(result['network_scope']['hls_and_head'])
                    self.assertFalse(result['network_scope']['media_bodies'])
                    self.assertEqual(task, frozen)


class MotionObservationTests(TestCase):
    """Requires motion extra; base runs select the two static classes explicitly."""
    setUp = SelectionObservationTests.setUp
    check = SelectionObservationTests.check
    def motion_task(self, **selection):
        from test_hls import MASTER_URL, MASTER, MEDIA
        from apple_music_artwork.execution import _asset_value, _inspect_motion_assets
        body = response()
        body['data'][0]['attributes']['editorialVideo'] = {'motionDetailSquare': {'video': MASTER_URL}}
        task = synthetic_task(body, **(selection or {'all_motion': True}))
        transport = self.motion_transport(body)
        selected = [_asset_value(value) for value in task['snapshot']['manifest']['selection']['selected']]
        _inspect_motion_assets(selected, task['snapshot']['manifest'], HttpClient(transport), 'best')
        self.assertFalse(task['snapshot']['manifest']['errors'])
        return task, body

    def motion_transport(self, body, *, head_status=200, hls_status=200, master=None):
        from test_hls import MASTER, MEDIA
        class MotionTransport(MetadataTransport):
            def request(self, url, **options):
                if 'mvod.itunes.apple.com/' in url:
                    self.requests.append(('GET', url))
                    if url.endswith('.mp4'):
                        raise AssertionError('media GET forbidden')
                    text = (master or MASTER) if url.endswith('master.m3u8') else MEDIA
                    return HttpResponse(hls_status, {'Content-Type': 'application/vnd.apple.mpegurl'}, text.encode())
                return super().request(url, **options)
            def head(self, url, **options):
                self.requests.append(('HEAD', url))
                return HttpResponse(head_status, {'Content-Type': 'video/mp4', 'Content-Length': '10'}, b'')
        return MotionTransport(body)

    def test_two_fresh_checks_bypass_warmed_hls_head_without_cache_mutation(self):
        from apple_music_artwork.remote_check import check_updates
        from apple_music_artwork.execution import _asset_value, _inspect_motion_assets
        task, body = self.motion_task()
        transport = self.motion_transport(body)
        parent = HttpClient(transport)
        manifest = deepcopy(task['snapshot']['manifest'])
        _inspect_motion_assets([_asset_value(manifest['selection']['selected'][0])], manifest, parent, 'best')
        self.assertEqual(parent.request_count, 3)
        before = parent.control.budget_snapshot()
        for index in range(2):
            result, errors, code = check_updates(task, http=parent, environ={'APPLE_MUSIC_DEVELOPER_TOKEN': 'synthetic'})
            self.assertEqual(code, 0, errors)
            self.assertEqual(result['outcome'], 'no_observed_change')
            self.assertTrue(result['network_scope']['hls_and_head'])
            self.assertEqual(parent.request_count, 3 + (index+1)*4)
        self.assertEqual(parent.control.budget_snapshot()['window_id'], before['window_id'])
        _inspect_motion_assets([_asset_value(manifest['selection']['selected'][0])], manifest, parent, 'best')
        self.assertEqual(parent.request_count, 11)

    def test_head_failure_retains_selected_change_and_does_not_evict_parent(self):
        from apple_music_artwork.remote_check import check_updates
        from apple_music_artwork.execution import _asset_value, _inspect_motion_assets
        from test_hls import variant
        task, body = self.motion_task()
        transport = self.motion_transport(body)
        parent = HttpClient(transport)
        manifest = deepcopy(task['snapshot']['manifest'])
        selected = [_asset_value(manifest['selection']['selected'][0])]
        _inspect_motion_assets(selected, manifest, parent, 'best')
        changed = self.motion_transport(body, head_status=404, master='#EXTM3U\n'+variant(resolution='2160x2160'))
        # Same control/cache, changed adapter on a normal fork before the fresh view.
        current = parent.fork(transport=changed)
        data, errors, code = check_updates(task, http=current, environ={'APPLE_MUSIC_DEVELOPER_TOKEN': 'synthetic'})
        self.assertEqual(code, 6, errors)
        self.assertEqual(data['outcome'], 'indeterminate')
        self.assertTrue(any(c['field']=='selected' for c in data['changes']))
        count = parent.request_count
        _inspect_motion_assets(selected, manifest, parent, 'best')
        self.assertEqual(parent.request_count, count)

    def test_exact_missing_motion_does_not_follow_replacement_hls(self):
        task, body = self.motion_task()
        item = task['snapshot']['manifest']['selection']['selected'][0]
        task['identity']['selection'].update(all_motion=False, asset_id=item['asset_id'])
        body['data'][0]['attributes']['editorialVideo']['motionDetailSquare']['video'] = 'https://mvod.itunes.apple.com/new.m3u8'
        data, errors, code, transport = self.check(task, body, self.motion_transport(body))
        self.assertEqual(code, 0, errors)
        self.assertEqual(data['outcome'], 'indeterminate')
        self.assertTrue(any(c['field']=='url' for c in data['changes']))
        self.assertEqual(len(transport.requests), 1)

    def test_hls_failure_and_missing_dependency_return_mapped_failure(self):
        task, body = self.motion_task()
        data, errors, code, _ = self.check(task, body, self.motion_transport(body, hls_status=404))
        self.assertEqual(code, 6, errors)
        self.assertEqual(data['outcome'], 'indeterminate')
        from apple_music_artwork.errors import ArtworkError
        with patch('apple_music_artwork.hls.require_parser', side_effect=ArtworkError('MOTION_DEPENDENCY_MISSING', 'synthetic', 'download')):
            data, errors, code, transport = self.check(task, body, self.motion_transport(body))
        self.assertEqual(code, 6)
        self.assertEqual(len(transport.requests), 1)
        self.assertFalse(data['network_scope']['hls_and_head'])
