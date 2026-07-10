"""Slack 공지 읽기.

    from seraph.slack import connect, get_announcements

    client = connect(config)                     # 토큰 없으면 mock
    get_announcements(client, config.slack_channel)

쓰기(알림 전송)는 seraph/notify.py 에 따로 있다. 이 패키지는 읽기 전용이다.
"""

from .announcements import (
    Announcement, get_announcements, parse_announcements, is_announcement,
)
from .client import MockSlackClient, SlackClient, SlackError
from .mrkdwn import to_plain_text, summarize


def connect(config):
    """config 에 토큰이 있으면 실제 Slack, 없으면 mock.

    토큰이 없다고 실패시키지 않는다. 공지 없이도 TUI 는 돌아가야 하고,
    프론트는 토큰 없이 개발할 수 있어야 한다.
    """
    token = config.slack_token
    if token:
        return SlackClient(token)
    return MockSlackClient()


__all__ = [
    'Announcement', 'get_announcements', 'parse_announcements',
    'is_announcement', 'connect',
    'SlackClient', 'MockSlackClient', 'SlackError',
    'to_plain_text', 'summarize',
]
