"""Spotify Web API + Authorization Code/PKCE. Never logs credentials."""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

API = 'https://api.spotify.com/v1'
TOKEN_URL = 'https://accounts.spotify.com/api/token'
SCOPES = 'user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private playlist-read-collaborative'


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
    except TimeoutError:
        raise SpotifyError('Таймаут Spotify. Команда могла быть принята; проверьте --spotify-status перед повтором.') from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise SpotifyError('Таймаут Spotify. Команда могла быть принята; проверьте --spotify-status перед повтором.') from None
        raise SpotifyError(f'Нет соединения со Spotify ({type(error.reason).__name__}). Проверьте сеть.') from None
    except json.JSONDecodeError:
        raise SpotifyError('Spotify вернул неожиданный формат ответа; проверьте --spotify-status.') from None


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


def artist_key(name):
    # Explicit spoken spelling, not a fuzzy match to an unrelated performer.
    value = unicodedata.normalize('NFKC', name).casefold().strip()
    aliases = {'кизару': 'kizaru', 'кизары': 'kizaru'}
    return aliases.get(value, value)


def search_uri(kind, query):
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        raise SpotifyError('Нужно название трека, исполнителя или плейлиста.')
    if kind == 'playlist':
        # Prefer the user's saved playlists over an unrelated public namesake.
        matches = []
        offset = 0
        while offset < 500:
            try:
                page = api('GET', '/me/playlists', params={'limit': 50, 'offset': offset}) or {}
            except SpotifyError as error:
                if str(error) != 'Spotify HTTP 403':
                    raise
                break  # existing tokens may lack the new playlist-read scopes
            items = page.get('items', [])
            for item in items:
                if not item or not item.get('uri'):
                    continue
                name = item.get('name', '')
                if name.casefold() == query.strip().casefold():
                    return item['uri'], name
                if query.strip().casefold() in name.casefold():
                    matches.append((item['uri'], name))
            offset += len(items)
            if not items or not page.get('next'):
                break
        if len(matches) == 1:
            return matches[0]
    search_query = artist_key(query) if kind == 'artist' else query
    result = api('GET', '/search', params={'q': search_query, 'type': kind,
                                         'limit': 10 if kind == 'artist' else 1}) or {}
    items = result.get(kind + 's', {}).get('items', [])
    if kind == 'artist':
        matches = [item for item in items if item and item.get('uri') and
                   artist_key(item.get('name', '')) == search_query]
        if not matches:
            found = ', '.join(item.get('name', '?') for item in items if item) or 'пустой результат'
            raise SpotifyError(f'Нет совпадения имени {query!r}. Spotify вернул: {found}.')
        # Homonymous artists are common. Prefer established popularity when the
        # API supplies it, otherwise retain Spotify's relevance ordering.
        matches.sort(key=lambda item: ((item.get('followers') or {}).get('total', 0),
                                       item.get('popularity', 0)), reverse=True)
        return matches[0]['uri'], matches[0]['name']
    if not items or not items[0] or not items[0].get('uri'):
        raise SpotifyError('Spotify ничего не нашёл по этому запросу.')
    name = items[0].get('name', query)
    if kind == 'track':
        artists = ', '.join(a['name'] for a in items[0].get('artists', []) if a and a.get('name'))
        if artists:
            name += f' — {artists}'
    return items[0]['uri'], name


def playlist_selection(args, selected=None):
    """Retain playlist context instead of replacing its queue with a single URI."""
    if args.get('playlist_query'):
        uri, _ = search_uri('playlist', args['playlist_query'])
    else:
        current = api('GET', '/me/player') or {}
        if (current.get('device') or {}).get('id') != (selected or device_id()):
            raise SpotifyError('На Slavik нет активного плейлиста. Назовите плейлист.')
        uri = (current.get('context') or {}).get('uri', '')
    if not re.fullmatch(r'spotify:playlist:[A-Za-z0-9]+', uri):
        raise SpotifyError('Сейчас играет не плейлист. Назовите плейлист для выбора трека.')
    number = args.get('track_number')
    if number is not None:
        if type(number) is not int or number < 1:
            raise SpotifyError('Номер трека должен быть целым числом от 1.')
        return {'context_uri': uri, 'offset': {'position': number - 1}}, f'трек {number}'
    query = args.get('query')
    if not isinstance(query, str) or not query.strip():
        raise SpotifyError('Назовите песню или номер трека в плейлисте.')
    def norm(text):
        return re.sub(r'[^\w]+', ' ', text.casefold()).strip()
    target = norm(query)
    if not target:
        raise SpotifyError('Нужно название песни.')
    identifier = uri.rsplit(':', 1)[1]
    endpoint = f'/playlists/{identifier}/items'
    matches = []
    offset = 0
    while True:
        try:
            page = api('GET', endpoint, params={'limit': 50, 'offset': offset}) or {}
        except SpotifyError as error:
            # Older Spotify versions expose the same collection as /tracks.
            if str(error) != 'Spotify HTTP 404' or not endpoint.endswith('/items'):
                raise
            endpoint = f'/playlists/{identifier}/tracks'
            page = api('GET', endpoint, params={'limit': 50, 'offset': offset}) or {}
        entries = page.get('items', [])
        for index, entry in enumerate(entries):
            track = (entry or {}).get('item') or (entry or {}).get('track') or {}
            if not track.get('uri') or track.get('is_local') or track.get('is_playable') is False:
                continue
            name = track.get('name', '')
            full = name + ' ' + ' '.join(a.get('name', '') for a in track.get('artists', []))
            if target == norm(name):
                return {'context_uri': uri, 'offset': {'position': offset + index}}, name
            if target in norm(full):
                matches.append((offset + index, name))
        offset += len(entries)
        if not entries or not page.get('next'):
            break
        if offset >= 1000:
            raise SpotifyError('Плейлист большой: назовите номер трека или более точное название.')
    if len(matches) != 1:
        raise SpotifyError('Трек не найден однозначно в плейлисте. Уточните название или номер.')
    position, name = matches[0]
    return {'context_uri': uri, 'offset': {'position': position}}, name


def playback_summary(current):
    device = current.get('device') or {}
    track = current.get('item') or {}
    if not device:
        return 'Снимок Spotify: активного воспроизведения нет.'
    def duration(value):
        if not isinstance(value, (int, float)):
            return 'неизвестно'
        minutes, seconds = divmod(max(0, int(value)) // 1000, 60)
        return f'{minutes}:{seconds:02}'
    artists = ', '.join(a.get('name', '?') for a in track.get('artists', []))
    state = 'играет' if current.get('is_playing') is True else 'пауза' if current.get('is_playing') is False else 'неизвестно'
    return (f"Снимок на момент проверки {time.strftime('%H:%M:%S UTC', time.gmtime())}: "
            f"устройство {device.get('name', '?')}; {state}; "
            f"{track.get('name', 'трек неизвестен')} — {artists or 'исполнитель неизвестен'}; "
            f"позиция {duration(current.get('progress_ms'))} из {duration(track.get('duration_ms'))}; "
            f"громкость музыки {device.get('volume_percent', '?')}%. "
            "Для нового вопроса о текущем моменте снова запроси status, не угадывай по старому снимку.")


def confirm_transport(action, selected, accepted=True):
    prefix = ('Spotify принял команду: ' + action + '. ' if accepted else
              'Ответ на команду потерян; выполнена проверка состояния. ')
    try:
        current = api('GET', '/me/player') or {}
    except SpotifyError:
        return prefix + 'Состояние прочитать не удалось; это не доказывает отказ команды.'
    if (current.get('device') or {}).get('id') != selected:
        return prefix + 'Состояние выбранного устройства пока не подтверждено. ' + playback_summary(current)
    expected = action == 'resume'
    if current.get('is_playing') is expected:
        return prefix + ('Подтверждено: воспроизведение.' if expected else 'Подтверждено: пауза.') + ' ' + playback_summary(current)
    return prefix + 'Снимок пока не подтверждает ожидаемое состояние; не утверждай отказ или успех исполнения. ' + playback_summary(current)


def spotify_command(args):
    try:
        action = args.get('action')
        if action == 'status':
            available = devices()
            names = [d.get('name', '?') for d in available]
            current = api('GET', '/me/player') or {}
            device = current.get('device') or {}
            track = current.get('item') or {}
            context = current.get('context') or {}
            lines = ['Устройства Spotify: ' + (', '.join(names) or 'нет; выберите Slavik в приложении Spotify')]
            lines.extend(f"  {d.get('name', '?')}: active={d.get('is_active')}, restricted={d.get('is_restricted')}, volume={d.get('volume_percent')}, supports_volume={d.get('supports_volume')}" for d in available)
            lines.append(f"Активное: {device.get('name', 'нет')}; playing={current.get('is_playing')}; progress_ms={current.get('progress_ms')}; shuffle={current.get('shuffle_state')}; repeat={current.get('repeat_state')}")
            lines.append(f"Трек: {track.get('name', 'нет')}; uri={track.get('uri')}; context={context.get('uri')}")
            lines.append(playback_summary(current))
            lines.append('Ограничения действий: ' + ', '.join(k for k, v in (current.get('actions') or {}).get('disallows', {}).items() if v))
            return '\n'.join(lines)
        supported = {'play_track', 'play_artist', 'play_playlist', 'pause', 'stop', 'resume',
                     'play_pause', 'next', 'previous', 'volume_up', 'volume_down',
                     'set_volume', 'play_mood', 'play_from_playlist'}
        if action not in supported:
            return 'Эта команда не поддерживается. Можно включить трек, исполнителя, плейлист или управлять воспроизведением.'
        selected = device_id()
        params = {'device_id': selected}
        if action == 'play_from_playlist':
            body, name = playlist_selection(args, selected)
            api('PUT', '/me/player/play', body, params)
            return f'Spotify принял выбор в плейлисте: {name}.'
        if action == 'play_mood':
            action = 'play_playlist'
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
        if action in ('volume_up', 'volume_down', 'set_volume'):
            current = next((d for d in devices() if d.get('id') == selected), None)
            if current is None:
                raise SpotifyError('Slavik отключился; повторите команду.')
            if current.get('supports_volume') is False or current.get('volume_percent') is None:
                raise SpotifyError('Это устройство не поддерживает регулировку громкости через Spotify.')
            if action == 'set_volume':
                amount = args.get('percent')
                if type(amount) is not int or not 0 <= amount <= 100:
                    raise SpotifyError('Громкость должна быть целым числом от 0 до 100%.')
            else:
                amount = max(0, min(100, current['volume_percent'] + (10 if action == 'volume_up' else -10)))
            api('PUT', '/me/player/volume', params={**params, 'volume_percent': amount})
            return f'Spotify принял громкость {amount}%.'
        method, path = {
            'pause': ('PUT', 'pause'), 'stop': ('PUT', 'pause'), 'resume': ('PUT', 'play'),
            'next': ('POST', 'next'), 'previous': ('POST', 'previous'),
        }[action]
        try:
            api(method, '/me/player/' + path, params=params)
        except SpotifyError as error:
            if action in ('pause', 'stop', 'resume') and 'могла быть принята' in str(error):
                return confirm_transport(action, selected, accepted=False)
            raise
        if action in ('pause', 'stop', 'resume'):
            return confirm_transport(action, selected)
        return 'Spotify принял команду: ' + action + '.'
    except SpotifyError as error:
        message = str(error)
        if message == 'Spotify HTTP 403':
            message = 'Spotify запретил команду. Проверьте Premium, разрешённого пользователя приложения и scopes; может понадобиться повторный login.'
        elif message == 'Spotify HTTP 404':
            message = 'Spotify не видит активного Connect-плеера. Выберите Slavik в приложении Spotify.'
        elif message == 'Spotify HTTP 429':
            message = 'Лимит запросов Spotify. Подождите перед следующей командой.'
        return ('Результат не подтверждён: ' if 'могла быть принята' in message else 'Не выполнено: ') + message


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
