"""SSH 인증 흐름 테스트 (키 → 비밀번호 폴백).

실서버·paramiko 없이 resolve_auth 순수 로직만 검증한다. 시스템 파이썬에서 돈다.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from seraph.connection import resolve_auth, AuthError, _AuthFailed


def connector(ok):
    """ok(집합)에 든 비번(또는 None=키)일 때만 성공하는 가짜 접속."""
    tried = []

    def try_connect(pw):
        tried.append(pw)
        if pw in ok:
            return True
        raise _AuthFailed()

    try_connect.tried = tried
    return try_connect


def test_key_success_skips_password():
    tc = connector({None})
    asked = []
    resolve_auth(tc, user='u', host='h',
                 ask_password=lambda *a: asked.append(1) or 'pw')
    assert tc.tried == [None]
    assert asked == []                      # 키 되면 안 물어본다


def test_key_fails_then_password_succeeds():
    tc = connector({'secret'})
    resolve_auth(tc, user='u', host='h', ask_password=lambda *a: 'secret')
    assert tc.tried == [None, 'secret']


def test_password_retries_then_gives_up():
    tc = connector({'right'})
    asked = []
    with pytest.raises(AuthError):
        resolve_auth(tc, user='u', host='h', attempts=3,
                     ask_password=lambda u, h, a: asked.append(a) or 'wrong')
    assert asked == [0, 1, 2]               # 정확히 3번, 그 뒤 포기


def test_headless_no_prompt_gives_auth_error():
    """비대화형이면 ask 가 None 을 준다. 무한 루프 없이 바로 실패."""
    tc = connector({'x'})
    with pytest.raises(AuthError):
        resolve_auth(tc, user='u', host='h', ask_password=lambda *a: None)
    assert tc.tried == [None]              # 키만 시도


def test_given_password_skips_key():
    tc = connector({'given'})
    resolve_auth(tc, user='u', host='h', password='given')
    assert tc.tried == ['given']


def test_given_wrong_password_does_not_prompt():
    tc = connector({'right'})
    asked = []
    with pytest.raises(AuthError):
        resolve_auth(tc, user='u', host='h', password='wrong',
                     ask_password=lambda *a: asked.append(1) or 'right')
    assert asked == []                     # 명시적 비번이 틀리면 물어보지 않는다


def test_empty_input_cancels():
    tc = connector({'x'})
    with pytest.raises(AuthError):
        resolve_auth(tc, user='u', host='h', ask_password=lambda *a: '')
    # 키(None) 한 번 + 빈 입력으로 취소
    assert tc.tried == [None]


def test_network_error_propagates_not_swallowed():
    """네트워크 오류를 인증 실패로 오인하면 안 된다 (재시도 대상 아님)."""
    def neterr(pw):
        raise ConnectionRefusedError('no route')
    with pytest.raises(ConnectionRefusedError):
        resolve_auth(neterr, user='u', host='h', ask_password=lambda *a: 'x')
