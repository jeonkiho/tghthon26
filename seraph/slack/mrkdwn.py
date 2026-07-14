"""Slack mrkdwn -> TUI 에 그대로 뿌릴 수 있는 평문.

Slack 메시지 원문에는 사람이 읽을 수 없는 표기가 섞여 있다:

    <@U02TA>                        사용자 멘션 (ID 만 있고 이름이 없다)
    <#C02SERAPH|seraph-공지>        채널 링크
    <https://wiki.../doc|위키 문서>  라벨 있는 링크
    <https://example.com>            라벨 없는 링크
    &amp; &lt; &gt;                   HTML 이스케이프 (Slack 이 이 셋만 인코딩한다)
    *굵게* _기울임_ ~취소선~ `코드`

TUI 는 서식을 못 살리므로 표시 문자를 떼고 내용만 남긴다. 링크는 라벨을 살리고,
라벨이 없으면 URL 을 그대로 둔다. 멘션은 이름을 알면 이름으로 바꾼다.

주의: 이스케이프 해제는 반드시 마지막에 한다. 먼저 풀면 본문에 있던 "&lt;"가
진짜 꺾쇠가 되어 링크 문법으로 잘못 파싱된다.
"""

import re

# <타입접두어 + 대상 | 라벨>  — 라벨은 없을 수 있다
_ENTITY = re.compile(r'<([^<>|]+)(?:\|([^<>]*))?>')

# *굵게* _기울임_ ~취소선~  (앞뒤가 단어 문자가 아닐 때만)
_BOLD = re.compile(r'(?<!\w)\*([^*\n]+)\*(?!\w)')
_ITALIC = re.compile(r'(?<!\w)_([^_\n]+)_(?!\w)')
_STRIKE = re.compile(r'(?<!\w)~([^~\n]+)~(?!\w)')

_UNESCAPE = (('&lt;', '<'), ('&gt;', '>'), ('&amp;', '&'))


def _render_entity(match, users):
    target, label = match.group(1), match.group(2)

    if target.startswith('@'):                      # 사용자 멘션
        if label:
            return f'@{label}'
        user_id = target[1:]
        return '@' + (users.get(user_id) or user_id)

    if target.startswith('#'):                      # 채널 링크
        if label:
            return f'#{label}'
        return '#' + target[1:]

    if target.startswith('!'):                      # @here, @channel, @everyone
        special = target[1:]
        if special.startswith('subteam^'):
            return f'@{label}' if label else '@group'
        return f'@{special.split("^")[0]}'

    return label or target                          # 링크: 라벨 우선, 없으면 URL


def to_plain_text(text, users=None):
    """Slack mrkdwn -> 평문. users 는 {user_id: 표시이름}."""
    if not text:
        return ''
    users = users or {}

    out = _ENTITY.sub(lambda m: _render_entity(m, users), text)
    out = _BOLD.sub(r'\1', out)
    out = _ITALIC.sub(r'\1', out)
    out = _STRIKE.sub(r'\1', out)
    out = out.replace('```', '').replace('`', '')

    # 이스케이프 해제는 맨 마지막. 순서를 바꾸면 &lt; 가 링크 문법으로 오인된다.
    for encoded, decoded in _UNESCAPE:
        out = out.replace(encoded, decoded)

    return out.strip()


def summarize(text, limit=80):
    """목록에 한 줄로 보여줄 요약. 줄바꿈을 없애고 자른다."""
    line = ' '.join(text.split())
    if len(line) <= limit:
        return line
    return line[:limit - 1].rstrip() + '…'
