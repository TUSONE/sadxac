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

if __name__ == '__main__': unittest.main()
