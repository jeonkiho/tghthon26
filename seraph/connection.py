"""세라프 접속과 명령 실행.

services 는 이 레이어를 모른다. 둘 다 Snapshot 을 돌려주므로 mock 과 실서버를
그대로 바꿔 끼울 수 있다.

    conn = MockConnection()          # 개발용, 서버 불필요
    conn = SSHConnection('ariel')    # 실서버
    snap = conn.snapshot()

ControlMaster 는 OpenSSH 클라이언트 기능이라 paramiko 연결에는 적용되지 않는다.
대신 SSHClient 를 한 번 열고 계속 재사용한다. 효과는 같다.
"""

import getpass
import pathlib
import sys

from . import commands
from . import config as config_module
from .services import Snapshot

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / 'tests' / 'fixtures'


def connect(config=None):
    """config 의 mode 에 따라 알맞은 연결을 만든다.

        connection.mode: mock  ->  MockConnection
        connection.mode: ssh   ->  SSHConnection
    """
    config = config or config_module.load()
    if config.mode == 'ssh':
        return SSHConnection(config.host, config=config)
    return MockConnection(config=config)


class MockConnection:
    """저장된 텍스트 파일을 읽어 실서버 흉내를 낸다."""

    def __init__(self, fixtures=FIXTURES, config=None):
        self.fixtures = pathlib.Path(fixtures)
        self.config = config or config_module.load()

    def run(self, key):
        return (self.fixtures / f'{key}.txt').read_text()

    def snapshot(self):
        raw = {k: self.run(k) for k in commands.ALL}
        return Snapshot(config=self.config, **raw)

    def sacct(self, days=7, user=None):
        """끝난 job 기록. mock 은 저장된 출력을 그대로 준다(days 는 무시)."""
        return self.run('sacct')

    def close(self):
        pass


class AuthError(RuntimeError):
    """인증에 실패했다 (키도 비밀번호도)."""


class _AuthFailed(Exception):
    """인증 자격증명이 틀렸다(키/비번). 재시도 대상. 네트워크 오류와 구분한다."""


def resolve_auth(try_connect, *, user, host, password=None,
                 ask_password=None, attempts=3):
    """인증 순서를 처리하는 순수 로직 (paramiko 없이 테스트 가능).

    try_connect(password) 는 성공하면 그냥 반환, 자격증명이 틀리면 _AuthFailed 를
    던진다. 그 밖의 예외(네트워크 등)는 그대로 올라간다.

      1. password 가 주어졌으면 그걸로 한 번.
      2. 아니면 키/에이전트로(=password None). 실패하면
      3. ask_password 로 물어보며 최대 attempts 번 재시도.

    끝까지 실패하면 AuthError.
    """
    if password is not None:
        try:
            return try_connect(password)
        except _AuthFailed:
            raise AuthError(f'{host} 비밀번호가 틀렸습니다.')

    try:
        return try_connect(None)                # 키/에이전트
    except _AuthFailed:
        pass

    ask = ask_password or _default_ask_password
    for attempt in range(attempts):
        pw = ask(user, host, attempt)
        if not pw:
            break                               # 취소/빈 입력/헤드리스
        try:
            return try_connect(pw)
        except _AuthFailed:
            continue                            # 오타. 다시 물어본다
    raise AuthError(
        f'{host} 인증 실패. SSH 키를 등록했는지, 비밀번호가 맞는지 확인하세요.')


def _default_ask_password(user, host, attempt):
    """터미널에서 비밀번호를 물어본다. 화면에 안 보이게(getpass) 받는다.

    비대화형(TTY 없음, 예: 파이프·cron)이면 물어볼 수 없으니 None 을 반환해
    인증을 실패시킨다. TUI 는 자기 화면에서 직접 물어보고 password 인자로 넘기거나
    ask_password 를 주입하는 게 낫다.
    """
    if not sys.stdin.isatty():
        return None
    prompt = f'{user}@{host} 비밀번호'
    if attempt:
        prompt += f' (다시, {attempt + 1}번째)'
    try:
        return getpass.getpass(prompt + ': ')
    except (EOFError, KeyboardInterrupt):
        return None


class SSHConnection:
    """paramiko 로 접속. 연결 하나를 열어두고 명령을 반복 실행한다.

    인증 순서 (사용자가 이미 쓰던 걸 그대로 쓰는 게 안전하다):
      1. ~/.ssh/config 의 Host 항목 + 키/에이전트    (입력 없음)
      2. 위가 실패하면 비밀번호를 물어본다           (메모리에만, 저장하지 않음)

    실행 방식 B: 사용자 노트북에서 TUI 를 돌리고 이 클래스가 세라프에 접속한다.
    로직은 resolve_auth() 에 있고 paramiko 없이 테스트된다(tests/test_auth.py).

    비밀번호를 물어보는 방법은 ask_password 로 주입한다. 기본은 getpass(터미널에서
    화면에 안 보이게 입력). 헤드리스(비대화형)면 물어볼 수 없으니 그냥 실패시킨다.
    최대 password_attempts 번까지 다시 물어본다(오타 대비).
    """

    def __init__(self, host, password=None, config=None,
                 ask_password=None, password_attempts=3):
        import paramiko

        self.config = config or config_module.load()
        cfg = self._ssh_config(host)
        self.host = host
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        # 세라프는 known_hosts 에 이미 있다. 처음 보는 호스트 키는 자동 수락하되
        # (첫 접속 편의), 이후엔 바뀌면 거부된다.
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_args = dict(
            hostname=cfg.get('hostname', host),
            port=int(cfg.get('port', 22)),
            username=cfg.get('user'),
            key_filename=cfg.get('identityfile'),
            timeout=10,
        )

        def try_connect(pw):
            """paramiko 접속 시도. 자격증명이 틀리면 _AuthFailed 로 바꿔 던진다."""
            key_only = pw is None       # 비번 없으면 키/에이전트만
            try:
                self.client.connect(
                    password=pw,
                    allow_agent=key_only,
                    look_for_keys=key_only,
                    **connect_args,
                )
            except paramiko.AuthenticationException:
                raise _AuthFailed()
            except paramiko.SSHException as exc:
                # 키가 아예 없으면 "No authentication methods" 로 온다. 이것도
                # 비밀번호로 넘어갈 신호로 본다. 그 밖의 SSHException 은 진짜 오류.
                if 'No authentication methods' in str(exc):
                    raise _AuthFailed()
                raise

        resolve_auth(
            try_connect,
            user=connect_args['username'] or host,
            host=host,
            password=password,
            ask_password=ask_password,
            attempts=password_attempts,
        )

    @staticmethod
    def _ssh_config(host):
        import paramiko

        path = pathlib.Path.home() / '.ssh' / 'config'
        if not path.exists():
            return {}
        cfg = paramiko.SSHConfig()
        with open(path) as f:
            cfg.parse(f)
        return cfg.lookup(host)

    def run_command(self, command, label='명령', timeout=30):
        _, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        out = stdout.read().decode()
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            raise RuntimeError(f'{label} 실패 (rc={rc}): {stderr.read().decode().strip()}')
        return out

    def run(self, key):
        return self.run_command(commands.ALL[key], label=key)

    def snapshot(self):
        """명령들을 한 연결에서 실행한다. 폴링은 config 의 주기를 지킨다."""
        raw = {k: self.run(k) for k in commands.ALL}
        return Snapshot(config=self.config, **raw)

    def sacct(self, days=7, user=None):
        """끝난 job 기록. 폴링에 넣지 말 것 — 느리고 자주 바뀌지 않는다."""
        return self.run_command(commands.sacct(days, user), label='sacct', timeout=60)

    def close(self):
        self.client.close()
