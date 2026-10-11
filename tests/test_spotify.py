import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import spotify_api as s

class SpotifyTests(unittest.TestCase):
    def test_pkce_rfc7636_vector(self):
        self.assertEqual(s.pkce('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'),
                         'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM')

    def test_token_private_and_refresh_retained(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'SPOTIFY_CLIENT_ID': 'app', 'SPOTIFY_TOKEN_FILE': str(Path(temp)/'token.json')}):
            saved = s.save_token({'access_token': 'test-only', 'expires_in': 3600}, {'refresh_token': 'test-only-refresh'})
            self.assertEqual(saved['refresh_token'], 'test-only-refresh')
            self.assertEqual(s.token_path().stat().st_mode & 0o777, 0o600)
            self.assertEqual(s.read_token()['client_id'], 'app')

    def test_track_search_and_targeted_play(self):
        calls = []
        def api(method, path, data=None, params=None):
            calls.append((method, path, data, params))
            if path == '/me/player/devices': return {'devices': [{'name':'Slavik','id':'target'}]}
            if path == '/search': return {'tracks': {'items': [{'name':'Song','uri':'spotify:track:abc'}]}}
        with patch.object(s, 'api', api), patch.dict(os.environ, {'SPOTIFY_DEVICE':'Slavik'}):
            result = s.spotify_command({'action':'play_track','query':'Song'})
        self.assertIn('принял', result)
        self.assertEqual(calls[-1], ('PUT','/me/player/play', {'uris':['spotify:track:abc']}, {'device_id':'target'}))

    def test_never_plays_on_unrelated_device_or_after_search_failure(self):
        with patch.object(s, 'api', return_value={'devices':[{'name':'Phone','id':'other'}]}) as api:
            result = s.spotify_command({'action':'play_track','query':'Song'})
            self.assertIn('Не выполнено',result)
            self.assertEqual(api.call_count,1)
        with patch.object(s,'device_id',return_value='target'), patch.object(s,'api',side_effect=s.SpotifyError('Spotify HTTP 403')):
            self.assertIn('запретил',s.spotify_command({'action':'play_track','query':'Song'}))

    def test_401_refresh_once(self):
        with patch.object(s,'access_token',side_effect=['old','new']) as tokens, patch.object(s,'request',side_effect=[s.SpotifyError('Spotify HTTP 401'),{'devices':[]}]) as requests:
            self.assertEqual(s.api('GET','/me/player/devices'),{'devices':[]})
            self.assertEqual(tokens.call_args_list[-1].kwargs,{'force':True})
            self.assertEqual(requests.call_count,2)

    def test_volume_is_clamped(self):
        calls=[]
        def api(method,path,data=None,params=None):
            calls.append((method,path,params))
            if path=='/me/player/devices': return {'devices':[{'id':'target','name':'Slavik','volume_percent':95}]}
        with patch.object(s,'api',api), patch.dict(os.environ, {'SPOTIFY_DEVICE':'Slavik'}):
            s.spotify_command({'action':'volume_up'})
        self.assertEqual(calls[-1][2]['volume_percent'],100)

    def test_own_playlist_is_preferred_over_public_namesake(self):
        with patch.object(s, 'api', return_value={'items': [
            {'name': 'My Favorites', 'uri': 'spotify:playlist:mine'}]}) as api:
            self.assertEqual(s.search_uri('playlist', 'My Favorites'),
                             ('spotify:playlist:mine', 'My Favorites'))
        self.assertEqual(api.call_args.args[1], '/me/playlists')

    def test_mood_search_plays_playlist_context(self):
        calls = []
        def api(method, path, data=None, params=None):
            calls.append((method, path, data, params))
            if path == '/search':
                return {'playlists': {'items': [{'uri': 'spotify:playlist:abc', 'name': 'Sad music'}]}}
        with patch.object(s, 'device_id', return_value='target'), patch.object(s, 'api', api):
            result = s.spotify_command({'action': 'play_mood', 'query': 'sad melancholic'})
        self.assertIn('принял', result)
        self.assertEqual(calls[-1][2], {'context_uri': 'spotify:playlist:abc'})

    def test_playlist_track_selection_preserves_context_and_page_position(self):
        calls = []
        def api(method, path, data=None, params=None):
            calls.append((method, path, data, params))
            if path == '/me/player':
                return {'device': {'id': 'target'}, 'context': {'uri': 'spotify:playlist:abc'}}
            if path.endswith('/items'):
                if params['offset'] == 0:
                    return {'items': [{'item': {'name': 'Other', 'uri': 'spotify:track:x'}}], 'next': 'unused'}
                return {'items': [{'item': {'name': 'My Song', 'uri': 'spotify:track:y'}}]}
        with patch.object(s, 'device_id', return_value='target'), patch.object(s, 'api', api):
            result = s.spotify_command({'action': 'play_from_playlist', 'query': 'My Song'})
        self.assertIn('принял', result)
        self.assertEqual(calls[-1][2], {'context_uri': 'spotify:playlist:abc', 'offset': {'position': 1}})

    def test_playlist_missing_song_never_changes_playback(self):
        with patch.object(s, 'device_id', return_value='target'), patch.object(s, 'api') as api:
            api.side_effect = [ {'device': {'id': 'target'}, 'context': {'uri': 'spotify:playlist:abc'}}, {'items': []} ]
            result = s.spotify_command({'action': 'play_from_playlist', 'query': 'missing'})
        self.assertIn('Не выполнено', result)
        self.assertTrue(all(call.args[0] == 'GET' for call in api.call_args_list))

    def test_playlist_number_is_one_based_and_invalid_is_rejected(self):
        with patch.object(s, 'search_uri', return_value=('spotify:playlist:abc', 'Name')):
            body, _ = s.playlist_selection({'playlist_query': 'Name', 'track_number': 3})
            self.assertEqual(body['offset'], {'position': 2})
            for number in [0, -1, True, '3']:
                with self.assertRaises(s.SpotifyError):
                    s.playlist_selection({'playlist_query': 'Name', 'track_number': number})

    def test_volume_absolute_and_bad_value(self):
        def api(method, path, data=None, params=None):
            if path.endswith('/devices'):
                return {'devices': [{'id': 'target', 'volume_percent': 50}]}
        with patch.object(s, 'device_id', return_value='target'), patch.object(s, 'api', side_effect=api) as mocked:
            result = s.spotify_command({'action': 'set_volume', 'percent': 25})
            self.assertIn('25%', result)
            self.assertEqual(mocked.call_args.kwargs['params'], {'device_id': 'target', 'volume_percent': 25})
            result = s.spotify_command({'action': 'set_volume', 'percent': 101})
            self.assertIn('Не выполнено', result)

if __name__ == '__main__': unittest.main()
