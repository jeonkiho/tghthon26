"""Slack 공지 읽기 테스트."""

import json
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from seraph import config as config_module
from seraph import slack
from seraph.slack import client as client_module
from seraph.slack.announcements import parse_announcements, is_announcement
from seraph.slack.mrkdwn import to_plain_text, summarize


# --- mrkdwn 변환 --------------------------------------------------------------

USERS = {'U02TA': '이조교', 'U01ADMIN': '세라프 관리자'}


@pytest.mark.parametrize('raw, expected', [
    ('<@U02TA>', '@이조교'),                       # 멘션 -> 이름
    ('<@U99UNKNOWN>', '@U99UNKNOWN'),              # 모르는 사람은 ID 그대로
    ('<@U02TA|ta.lee>', '@ta.lee'),                # 라벨이 있으면 라벨
    ('<#C02SERAPH|seraph-공지>', '#seraph-공지'),
    ('<#C02SERAPH>', '#C02SERAPH'),
    ('<https://wiki.khu.ac.kr/x|위키 문서>', '위키 문서'),
    ('<https://example.com>', 'https://example.com'),
    ('<!here>', '@here'),
    ('<!channel>', '@channel'),
    ('*굵게*', '굵게'),
    ('_기울임_', '기울임'),
    ('~취소선~', '취소선'),
    ('`코드`', '코드'),
    ('```블록```', '블록'),
    ('a_b_c', 'a_b_c'),                            # 단어 안의 _ 는 서식이 아니다
    ('2*3*4', '2*3*4'),
    ('', ''),
])
def test_to_plain_text(raw, expected):
    assert to_plain_text(raw, USERS) == expected


def test_escapes_are_decoded_last():
    """&lt;NAS&gt; 를 먼저 풀면 링크 문법으로 오인해 내용이 사라진다."""
    assert to_plain_text('&lt;NAS&gt; 에서 읽지 마세요') == '<NAS> 에서 읽지 마세요'
    assert to_plain_text('A &amp; B') == 'A & B'


def test_escaped_text_is_not_parsed_as_entity():
    # 이스케이프된 꺾쇠 안의 @U02TA 는 멘션이 아니다
    assert to_plain_text('&lt;@U02TA&gt;', USERS) == '<@U02TA>'


def test_link_and_escape_together():
    raw = '자세히는 <https://x.com/a?b=1&amp;c=2|여기> 참고'
    assert to_plain_text(raw) == '자세히는 여기 참고'


def test_summarize_truncates_and_collapses():
    assert summarize('a\n\n  b   c') == 'a b c'
    long = '가' * 200
    s = summarize(long, limit=10)
    assert len(s) == 10 and s.endswith('…')
    assert summarize('짧은 글', limit=80) == '짧은 글'


# --- 메시지 필터 ---------------------------------------------------------------

def test_system_messages_are_not_announcements():
    assert not is_announcement(
        {'type': 'message', 'subtype': 'channel_join', 'text': 'x has joined'})
    assert not is_announcement({'type': 'message', 'text': '   '})
    assert not is_announcement({'type': 'reaction_added', 'text': 'x'})


def test_bot_messages_are_kept():
    """자동 공지도 공지다."""
    assert is_announcement({'type': 'message', 'bot_id': 'B1', 'text': '사용률 90%'})


def test_parse_drops_join_messages_and_sorts_newest_first():
    messages = [
        {'type': 'message', 'user': 'U02TA', 'text': '오래된 공지', 'ts': '100.0'},
        {'type': 'message', 'subtype': 'channel_join', 'user': 'U09',
         'text': '<@U09> has joined', 'ts': '200.0'},
        {'type': 'message', 'user': 'U01ADMIN', 'text': '최신 공지', 'ts': '300.0'},
    ]
    items = parse_announcements(messages, USERS)
    assert [a.text for a in items] == ['최신 공지', '오래된 공지']


def test_parse_uses_kst_and_names():
    messages = [{'type': 'message', 'user': 'U02TA', 'text': '안녕',
                 'ts': '1752120000.000100', 'reply_count': 2,
                 'reactions': [{'name': 'eyes', 'count': 7}]}]
    (a,) = parse_announcements(messages, USERS)
    assert a.author == '이조교'
    assert a.posted_at.endswith('+09:00')          # KST
    assert a.reply_count == 2
    assert a.reactions == [{'name': 'eyes', 'count': 7}]
    assert a.is_bot is False


def test_bot_author_falls_back_to_username():
    messages = [{'type': 'message', 'bot_id': 'B1', 'username': 'Slurm Bot',
                 'text': '알림', 'ts': '1.0'}]
    (a,) = parse_announcements(messages, {})
    assert a.author == 'Slurm Bot' and a.is_bot is True


def test_bad_timestamp_does_not_crash():
    (a,) = parse_announcements(
        [{'type': 'message', 'user': 'U02TA', 'text': 'x', 'ts': 'oops'}], USERS)
    assert a.posted_at == ''


# --- mock 클라이언트 -----------------------------------------------------------

def test_mock_client_reads_fixture():
    result = slack.get_announcements(slack.MockSlackClient(), '공지', limit=10)
    assert result['ok'] is True
    assert result['count'] == 5                    # 6개 중 channel_join 1개 제외
    first = result['announcements'][0]
    assert '@이조교' in first['text']              # 멘션이 이름으로 바뀜
    assert '위키 문서' in first['text']            # 링크 라벨만 남음
    assert '<https://' not in first['text']
    assert any(a['is_bot'] for a in result['announcements'])


def test_connect_without_token_returns_mock(monkeypatch, tmp_path):
    monkeypatch.delenv('SERAPH_SLACK_TOKEN', raising=False)
    cfg = config_module.load(tmp_path / 'none.yaml')
    assert isinstance(slack.connect(cfg), slack.MockSlackClient)


def test_connect_with_token_returns_real_client(monkeypatch, tmp_path):
    monkeypatch.setenv('SERAPH_SLACK_TOKEN', 'xoxb-test')
    cfg = config_module.load(tmp_path / 'none.yaml')
    assert isinstance(slack.connect(cfg), slack.SlackClient)


def test_token_never_read_from_config_file(tmp_path, monkeypatch):
    """토큰을 config.yaml 에 적어도 무시한다. 그 파일은 커밋되기 때문."""
    monkeypatch.delenv('SERAPH_SLACK_TOKEN', raising=False)
    path = tmp_path / 'c.yaml'
    path.write_text('slack:\n  token: xoxb-leaked\n', encoding='utf-8')
    assert config_module.load(path).slack_token is None


# --- 실제 클라이언트 (HTTP 를 가짜로) -------------------------------------------

class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, responses):
    """responses: [payload dict] — 호출 순서대로 돌려준다."""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        assert request.headers['Authorization'].startswith('Bearer ')
        return _FakeResponse(json.dumps(responses[len(calls) - 1]).encode())

    monkeypatch.setattr(client_module.urllib.request, 'urlopen', fake_urlopen)
    return calls


def test_client_requires_token():
    with pytest.raises(slack.SlackError) as exc:
        slack.SlackClient('')
    assert exc.value.code == 'not_authed'


def test_client_raises_with_hint_on_api_error(monkeypatch):
    _patch_urlopen(monkeypatch, [{'ok': False, 'error': 'missing_scope'}])
    with pytest.raises(slack.SlackError) as exc:
        slack.SlackClient('xoxb-x').history('C1')
    assert exc.value.code == 'missing_scope'
    assert 'channels:history' in str(exc.value)     # 뭘 고쳐야 하는지 알려준다


def test_get_announcements_returns_error_instead_of_raising(monkeypatch):
    """공지를 못 읽는다고 TUI 가 죽으면 안 된다."""
    _patch_urlopen(monkeypatch, [{'ok': False, 'error': 'not_in_channel'}])
    result = slack.get_announcements(slack.SlackClient('xoxb-x'), 'C0AAAAAAAA')
    assert result['ok'] is False
    assert result['error'] == 'not_in_channel'
    assert result['announcements'] == []
    assert '초대' in result['message']


def test_network_failure_is_reported_not_raised(monkeypatch):
    def boom(request, timeout=None):
        raise OSError('연결 거부')
    monkeypatch.setattr(client_module.urllib.request, 'urlopen', boom)
    result = slack.get_announcements(slack.SlackClient('xoxb-x'), 'C0AAAAAAAA')
    assert result['ok'] is False and result['error'] == 'network_error'


def test_channel_id_is_not_looked_up(monkeypatch):
    """이미 ID 면 conversations.list 를 부르지 않는다 (호출 1번 절약)."""
    calls = _patch_urlopen(monkeypatch, [
        {'ok': True, 'messages': []},
        {'ok': True, 'members': []},
    ])
    slack.get_announcements(slack.SlackClient('xoxb-x'), 'C0ABCDEFGH')
    assert not any('conversations.list' in c for c in calls)
    assert any('conversations.history' in c for c in calls)


def test_channel_name_is_resolved_to_id(monkeypatch):
    calls = _patch_urlopen(monkeypatch, [
        {'ok': True, 'channels': [{'id': 'C0REAL', 'name': '공지'}],
         'response_metadata': {'next_cursor': ''}},
        {'ok': True, 'messages': []},
        {'ok': True, 'members': []},
    ])
    result = slack.get_announcements(slack.SlackClient('xoxb-x'), '#공지')
    assert result['ok'] is True
    assert any('conversations.list' in c for c in calls)
    assert any('channel=C0REAL' in c for c in calls)


def test_channel_not_found_is_reported(monkeypatch):
    _patch_urlopen(monkeypatch, [
        {'ok': True, 'channels': [], 'response_metadata': {'next_cursor': ''}}])
    result = slack.get_announcements(slack.SlackClient('xoxb-x'), '#없는채널')
    assert result['ok'] is False and result['error'] == 'channel_not_found'


def test_users_are_cached(monkeypatch):
    calls = _patch_urlopen(monkeypatch, [
        {'ok': True, 'members': [{'id': 'U1', 'profile': {'display_name': '홍길동'}}]},
        {'ok': True, 'members': []},
    ])
    client = slack.SlackClient('xoxb-x')
    assert client.users() == {'U1': '홍길동'}
    assert client.users() == {'U1': '홍길동'}       # 두 번째는 API 를 안 부른다
    assert len(calls) == 1


def test_display_name_beats_real_name(monkeypatch):
    _patch_urlopen(monkeypatch, [{'ok': True, 'members': [
        {'id': 'U1', 'name': 'lee', 'profile': {'display_name': '', 'real_name': '이조교'}},
        {'id': 'U2', 'name': 'kim', 'profile': {'display_name': '관리자', 'real_name': '김'}},
    ]}])
    users = slack.SlackClient('xoxb-x').users()
    assert users == {'U1': '이조교', 'U2': '관리자'}
