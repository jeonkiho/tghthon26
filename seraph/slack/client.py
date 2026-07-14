"""Slack Web API 클라이언트 (읽기 전용).

공지를 읽으려면 Web API 가 필요하다. Incoming Webhook 으로는 읽을 수 없다.
필요한 권한(scope): `channels:history`, `channels:read`, `users:read`
비공개 채널이면 `groups:history`, `groups:read`.

토큰은 환경변수로 준다. 소스나 config.yaml 에 넣지 않는다.

    export SERAPH_SLACK_TOKEN='xoxb-...'

봇 토큰(xoxb)이면 그 봇을 공지 채널에 초대해야 한다.
"""

import json
import pathlib
import urllib.error
import urllib.parse
import urllib.request

API = 'https://slack.com/api/'
TIMEOUT = 10

FIXTURES = pathlib.Path(__file__).resolve().parent.parent.parent / 'tests' / 'fixtures'


class SlackError(RuntimeError):
    """Slack 이 ok:false 를 돌려줬거나 연결이 실패했다."""

    #  자주 나오는 것들. 사용자에게 뭘 고쳐야 하는지 알려준다.
    HINTS = {
        'invalid_auth': '토큰이 잘못되었습니다. SERAPH_SLACK_TOKEN 을 확인하세요.',
        'not_authed': '토큰이 없습니다. SERAPH_SLACK_TOKEN 을 설정하세요.',
        'account_inactive': '토큰이 비활성화되었습니다.',
        'missing_scope': '권한이 부족합니다. channels:history, channels:read, '
                         'users:read 를 앱에 추가하고 다시 설치하세요.',
        'not_in_channel': '봇이 채널에 없습니다. 공지 채널에 봇을 초대하세요 '
                          '(/invite @봇이름).',
        'channel_not_found': '채널을 찾을 수 없습니다. 이름 또는 ID 를 확인하세요.',
        'ratelimited': '요청이 너무 잦습니다. 잠시 후 다시 시도하세요.',
    }

    def __init__(self, code, message=None):
        self.code = code
        hint = self.HINTS.get(code, '')
        super().__init__(message or f'Slack 오류: {code}. {hint}'.strip())


class SlackClient:
    """읽기 전용. 쓰기 메서드는 두지 않는다."""

    def __init__(self, token, timeout=TIMEOUT):
        if not token:
            raise SlackError('not_authed')
        self._token = token
        self._timeout = timeout
        self._users = None          # users.list 결과 캐시

    def _call(self, method, **params):
        url = API + method
        if params:
            url += '?' + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url, headers={'Authorization': f'Bearer {self._token}'})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode('utf-8'))
        except (urllib.error.URLError, OSError) as exc:
            raise SlackError('network_error', f'Slack 에 연결하지 못했습니다: {exc}')
        except json.JSONDecodeError as exc:
            raise SlackError('bad_response', f'Slack 응답을 읽지 못했습니다: {exc}')

        if not payload.get('ok'):
            raise SlackError(payload.get('error', 'unknown'))
        return payload

    def resolve_channel(self, channel):
        """채널 이름(#공지 / 공지)을 ID 로 바꾼다. 이미 ID 면 그대로 둔다."""
        name = channel.lstrip('#')
        if _looks_like_channel_id(name):
            return name

        cursor = ''
        while True:
            payload = self._call(
                'conversations.list', limit=200, cursor=cursor,
                types='public_channel,private_channel', exclude_archived='true')
            for item in payload.get('channels', []):
                if item.get('name') == name:
                    return item['id']
            cursor = payload.get('response_metadata', {}).get('next_cursor', '')
            if not cursor:
                break
        raise SlackError('channel_not_found', f"'{channel}' 채널을 찾지 못했습니다.")

    def users(self):
        """{user_id: 표시이름}. 멘션을 이름으로 바꾸는 데 쓴다. 한 번만 부른다."""
        if self._users is None:
            self._users = _users_map(self._call('users.list', limit=500))
        return self._users

    def history(self, channel_id, limit=10):
        """최근 메시지. 최신순으로 온다."""
        payload = self._call('conversations.history',
                             channel=channel_id, limit=limit)
        return payload.get('messages', [])


class MockSlackClient:
    """저장된 JSON 을 읽어 실제 Slack 흉내를 낸다. 토큰 없이 개발할 수 있다."""

    def __init__(self, fixtures=FIXTURES):
        self.fixtures = pathlib.Path(fixtures)

    def _load(self, name):
        with open(self.fixtures / name, encoding='utf-8') as f:
            return json.load(f)

    def resolve_channel(self, channel):
        return channel.lstrip('#') or 'C0MOCK'

    def users(self):
        return _users_map(self._load('slack_users.json'))

    def history(self, channel_id, limit=10):
        return self._load('slack_history.json')['messages'][:limit]


def _looks_like_channel_id(value):
    """C01ABC / G01ABC / D01ABC 형태면 ID 로 본다."""
    return (len(value) > 8 and value[0] in 'CGD'
            and value[1:].isalnum() and value.isupper())


def _users_map(payload):
    users = {}
    for member in payload.get('members', []):
        profile = member.get('profile') or {}
        name = (profile.get('display_name')
                or profile.get('real_name')
                or member.get('name')
                or member['id'])
        users[member['id']] = name
    return users
