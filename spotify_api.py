"""Spotify Web API + Authorization Code/PKCE. Never logs credentials."""
import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

API = 'https://api.spotify.com/v1'
TOKEN_URL = 'https://accounts.spotify.com/api/token'
SCOPES = 'user-read-playback-state user-modify-playback-state user-read-currently-playing'


class SpotifyError(RuntimeError):
    pass


def token_path():
    return Path(os.getenv('SPOTIFY_TOKEN_FILE', str(Path.home() / '.config/slavik/spotify-token.json')))


def client_id():
    value = os.getenv('SPOTIFY_CLIENT_ID', '').strip()
    if not value:
        raise SpotifyError('Добавьте SPOTIFY_CLIENT_ID из Developer Dashboard в ~/.config/slavik/.env.')
    return value


def request(method, url, data=None, token=None, form=False):
    headers = {}
    if token: headers['Authorization'] = 'Bearer ' + token
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        else:
            body = json.dumps(data).encode()
            headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        # Exclude URL/request/response bodies: they can contain OAuth credentials.
        error.close()
        raise SpotifyError(f'Spotify HTTP {error.code}') from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise SpotifyError('Нет ответа Spotify: проверьте сеть и повторите.') from None


def save_token(data, previous=None):
    previous = previous or {}
    if not data.get('access_token'):
        raise SpotifyError('Spotify не выдал access token.')
    saved = dict(data)
    saved['refresh_token'] = data.get('refresh_token') or previous.get('refresh_token')
    saved['expires_at'] = time.time() + int(data.get('expires_in', 3600)) - 60
    saved['client_id'] = client_id()
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.spotify-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f: json.dump(saved, f)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return saved


def read_token():
    try:
        data = json.loads(token_path().read_text())
    except (OSError, ValueError):
        raise SpotifyError('Сначала выполните --spotify-login.') from None
    if data.get('client_id') != client_id():
        raise SpotifyError('Spotify Client ID изменён. Повторите --spotify-login.')
    return data


def access_token(force=False):
    data = read_token()
    if force or data.get('expires_at', 0) <= time.time():
        refresh = data.get('refresh_token')
        if not refresh: raise SpotifyError('Нет refresh token. Повторите --spotify-login.')
        data = save_token(request('POST', TOKEN_URL, {
            'grant_type': 'refresh_token', 'refresh_token': refresh, 'client_id': client_id(),
        }, form=True), data)
    return data['access_token']


def api(method, path, data=None, params=None):
    url = API + path
    if params: url += '?' + urllib.parse.urlencode(params)
    try:
        return request(method, url, data, access_token())
    except SpotifyError as error:
        if str(error) != 'Spotify HTTP 401': raise
        return request(method, url, data, access_token(force=True))


def pkce(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()


def login(stopped=None):
    identifier = client_id()
    port = int(os.getenv('SPOTIFY_AUTH_PORT', '8888'))
    if not 1024 <= port <= 65535: raise SpotifyError('SPOTIFY_AUTH_PORT должен быть 1024..65535.')
    redirect = f'http://127.0.0.1:{port}/callback'
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    result = {}
    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args): pass  # includes code in URL; never log it
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            valid = parsed.path == '/callback' and hmac.compare_digest(query.get('state', [''])[0], state)
            if valid:
                result['code'] = query.get('code', [''])[0]
                result['error'] = bool(query.get('error'))
            self.send_response(200 if valid else 400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(('Можно закрыть окно и вернуться в терминал.' if valid else 'Неверный callback/state.').encode())
    url = 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode({
        'client_id': identifier, 'response_type': 'code', 'redirect_uri': redirect,
        'scope': SCOPES, 'state': state, 'code_challenge_method': 'S256',
        'code_challenge': pkce(verifier),
    })
    try:
        server = HTTPServer(('127.0.0.1', port), Callback)
    except OSError:
        raise SpotifyError('Порт авторизации занят. Закройте старый login или выберите другой SPOTIFY_AUTH_PORT.') from None
    with server:
        server.timeout = 1
        print(f'В Dashboard укажите Redirect URI: {redirect}')
        print('При SSH с Windows нужен туннель -L 8888:127.0.0.1:8888 (или выбранный порт).')
        print('Откройте в браузере на Windows:\n' + url, flush=True)
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline and not (stopped and stopped.is_set()):
            server.handle_request()
    if not result.get('code') or result.get('error'):
        raise SpotifyError('Авторизация отменена или истёк срок 5 минут. Повторите login.')
    save_token(request('POST', TOKEN_URL, {
        'grant_type': 'authorization_code', 'code': result['code'], 'redirect_uri': redirect,
        'client_id': identifier, 'code_verifier': verifier,
    }, form=True))
    print('Spotify авторизован. Токены сохранены локально с правами 600.')


def devices():
    return (api('GET', '/me/player/devices') or {}).get('devices', [])


def device_id():
    available = [d for d in devices() if d.get('id') and not d.get('is_restricted')]
    requested = os.getenv('SPOTIFY_DEVICE', 'Slavik')
    matches = [d for d in available if d['id'] == requested or d.get('name', '').casefold() == requested.casefold()]
    if len(matches) != 1:
        raise SpotifyError(f'Устройство {requested!r} не найдено однозначно. Откройте Spotify на телефоне и выберите Slavik; затем --spotify-status.')
    return matches[0]['id']


def search_uri(kind, query):
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        raise SpotifyError('Нужно название трека, исполнителя или плейлиста.')
    result = api('GET', '/search', params={'q': query, 'type': kind, 'limit': 1}) or {}
    items = result.get(kind + 's', {}).get('items', [])
    if not items or not items[0] or not items[0].get('uri'):
        raise SpotifyError('Spotify ничего не нашёл по этому запросу.')
    return items[0]['uri'], items[0].get('name', query)


def spotify_command(args):
    try:
        action = args.get('action')
        if action == 'status':
            names = [d.get('name', '?') for d in devices()]
            return 'Устройства Spotify: ' + (', '.join(names) or 'нет; выберите Slavik в приложении Spotify')
        supported = {'play_track', 'play_artist', 'play_playlist', 'pause', 'stop', 'resume',
                     'play_pause', 'next', 'previous', 'volume_up', 'volume_down'}
        if action not in supported:
            return 'Эта команда не поддерживается. Можно включить трек, исполнителя, плейлист или управлять воспроизведением.'
        selected = device_id()
        params = {'device_id': selected}
        if action in ('play_track', 'play_artist', 'play_playlist'):
            kind = {'play_track': 'track', 'play_artist': 'artist', 'play_playlist': 'playlist'}[action]
            uri, name = search_uri(kind, args.get('query'))
            api('PUT', '/me/player/play', {'uris': [uri]} if kind == 'track' else {'context_uri': uri}, params)
            return f'Spotify принял команду воспроизведения: {name}.'
        if action == 'play_pause':
            current = api('GET', '/me/player') or {}
            if (current.get('device') or {}).get('id') != selected:
                raise SpotifyError('Выбранное устройство не активно. Сначала включите на нём трек.')
            action = 'pause' if current.get('is_playing') else 'resume'
        if action in ('volume_up', 'volume_down'):
            current = next(d for d in devices() if d.get('id') == selected)
            if current.get('supports_volume') is False or current.get('volume_percent') is None:
                raise SpotifyError('Это устройство не поддерживает регулировку громкости через Spotify.')
            amount = max(0, min(100, current['volume_percent'] + (10 if action == 'volume_up' else -10)))
            api('PUT', '/me/player/volume', params={**params, 'volume_percent': amount})
            return f'Spotify принял громкость {amount}%.'
        method, path = {
            'pause': ('PUT', 'pause'), 'stop': ('PUT', 'pause'), 'resume': ('PUT', 'play'),
            'next': ('POST', 'next'), 'previous': ('POST', 'previous'),
        }[action]
        api(method, '/me/player/' + path, params=params)
        return 'Spotify принял команду: ' + action + '.'
    except SpotifyError as error:
        message = str(error)
        if message == 'Spotify HTTP 403':
            message = 'Spotify запретил команду. Проверьте Premium, разрешённого пользователя приложения и scopes; может понадобиться повторный login.'
        elif message == 'Spotify HTTP 404':
            message = 'Spotify не видит активного Connect-плеера. Выберите Slavik в приложении Spotify.'
        elif message == 'Spotify HTTP 429':
            message = 'Лимит запросов Spotify. Подождите перед следующей командой.'
        return 'Не выполнено: ' + message


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['login', 'status'])
    args = parser.parse_args()
    try:
        if args.command == 'login': login()
        else: print(spotify_command({'action': 'status'}))
    except SpotifyError as error:
        print(str(error))
        raise SystemExit(1)

if __name__ == '__main__': main()
