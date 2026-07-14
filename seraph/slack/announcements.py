"""Slack 공지 -> TUI 가 그릴 JSON.

conversations.history 는 사람이 쓴 공지 말고도 잡음을 함께 준다:
"OOO 님이 채널에 참여했습니다" 같은 시스템 메시지가 섞여 있어서, 그대로 뿌리면
공지 목록이 입장 알림으로 도배된다. subtype 이 있는 메시지는 대부분 시스템
메시지이므로 걸러낸다.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta

from .mrkdwn import to_plain_text, summarize

# 세라프는 국내 서버다. 사용자가 보는 시각은 KST 여야 한다.
KST = timezone(timedelta(hours=9))

# 공지가 아닌 시스템 메시지들. 이 subtype 은 버린다.
# (bot_message 는 버리지 않는다. 자동 공지도 공지다.)
_NOISE_SUBTYPES = {
    'channel_join', 'channel_leave', 'channel_topic', 'channel_purpose',
    'channel_name', 'channel_archive', 'channel_unarchive',
    'group_join', 'group_leave', 'pinned_item', 'unpinned_item',
    'thread_broadcast',
}


@dataclass
class Announcement:
    ts: str                 # Slack 원본 타임스탬프. 고유 ID 로 쓴다
    posted_at: str          # ISO 8601 (KST)
    author: str
    text: str               # 평문. TUI 에 그대로 뿌린다
    summary: str            # 목록용 한 줄
    is_bot: bool = False
    reply_count: int = 0
    reactions: list = field(default_factory=list)   # [{"name","count"}]

    def to_dict(self):
        return asdict(self)


def _epoch(ts):
    """Slack ts("1752120000.000100") -> float. 못 읽으면 0."""
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def _posted_at(ts):
    seconds = _epoch(ts)
    if not seconds:
        return ''
    return datetime.fromtimestamp(seconds, tz=KST).isoformat(timespec='seconds')


def _author(message, users):
    if message.get('bot_id') and not message.get('user'):
        return message.get('username') or '봇'
    user_id = message.get('user', '')
    return users.get(user_id) or user_id or '알 수 없음'


def _reactions(message):
    return [{'name': r.get('name', ''), 'count': int(r.get('count', 0))}
            for r in message.get('reactions', [])]


def is_announcement(message):
    """공지로 볼 메시지인가. 시스템 메시지와 빈 메시지를 거른다."""
    if message.get('type') != 'message':
        return False
    subtype = message.get('subtype')
    if subtype in _NOISE_SUBTYPES:
        return False
    return bool((message.get('text') or '').strip())


def parse_announcements(messages, users=None):
    """conversations.history 의 messages -> [Announcement] (최신순)"""
    users = users or {}
    out = []
    for message in messages:
        if not is_announcement(message):
            continue
        text = to_plain_text(message.get('text', ''), users)
        if not text:
            continue
        out.append(Announcement(
            ts=message.get('ts', ''),
            posted_at=_posted_at(message.get('ts')),
            author=_author(message, users),
            text=text,
            summary=summarize(text),
            is_bot=bool(message.get('bot_id')),
            reply_count=int(message.get('reply_count', 0)),
            reactions=_reactions(message),
        ))
    out.sort(key=lambda a: _epoch(a.ts), reverse=True)
    return out


def get_announcements(client, channel, limit=10):
    """프론트가 부르는 함수. 실패해도 예외를 던지지 않는다.

    공지를 못 읽는다고 TUI 가 죽으면 안 된다. GPU 현황이 본업이고 공지는 곁다리다.
    """
    from .client import SlackError

    try:
        channel_id = client.resolve_channel(channel)
        messages = client.history(channel_id, limit=limit)
        users = client.users()
    except SlackError as exc:
        return {
            'ok': False,
            'channel': channel,
            'error': exc.code,
            'message': str(exc),
            'announcements': [],
        }

    items = parse_announcements(messages, users)
    return {
        'ok': True,
        'channel': channel,
        'count': len(items),
        'announcements': [a.to_dict() for a in items],
    }
