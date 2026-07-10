"""세라프 접속과 명령 실행.

services 는 이 레이어를 모른다. 둘 다 Snapshot 을 돌려주므로 mock 과 실서버를
그대로 바꿔 끼울 수 있다.

    conn = MockConnection()          # 개발용, 서버 불필요
    conn = SSHConnection('ariel')    # 실서버
    snap = conn.snapshot()

ControlMaster 는 OpenSSH 클라이언트 기능이라 paramiko 연결에는 적용되지 않는다.
대신 SSHClient 를 한 번 열고 계속 재사용한다. 효과는 같다.
"""

import pathlib

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


class SSHConnection:
    """paramiko 로 접속. 연결 하나를 열어두고 명령을 반복 실행한다.

    인증 순서는 사용자가 이미 쓰던 걸 그대로 쓰는 게 안전하다:
      1. ~/.ssh/config 의 Host 항목 + 키    (입력 없음)
      2. 비밀번호                            (메모리에만, 저장하지 않음)
    """

    def __init__(self, host, password=None, config=None):
        import paramiko  # 선택적 의존성. mock 만 쓸 땐 없어도 된다.

        self.config = config or config_module.load()
        cfg = self._ssh_config(host)
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        self.client.connect(
            hostname=cfg.get('hostname', host),
            port=int(cfg.get('port', 22)),
            username=cfg.get('user'),
            password=password,          # None 이면 키 인증만 시도한다
            key_filename=cfg.get('identityfile'),
            look_for_keys=True,
            allow_agent=True,
            timeout=10,
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
