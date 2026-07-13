"""QOS 한도 파싱.

세라프는 사용자마다 QOS 가 다르다. 이걸 모르면 "왜 내 job 이 대기 중인가" 를
답할 수 없다. 실제로 관측된 값:

  grad                 gres/gpu:high_perf=0,gres/gpu=4   | 10 | 20
  ugrad                gres/gpu:high_perf=0,gres/gpu=1   | 2  | 10
  qos_user01_2026_1   gres/gpu:high_perf=8,gres/gpu=12  | 12 | 24

high_perf=0 은 "고성능 노드를 쓸 수 없음" 을 뜻한다. 기본 grad QOS 는 m/k/n 노드에
job 을 낼 수 없다.
"""

from dataclasses import dataclass, asdict

from .gres import parse_tres, HIGH_PERF


@dataclass
class QosLimit:
    name: str
    max_gpus: int | None            # None = 한도 없음
    max_high_perf_gpus: int | None
    max_running_jobs: int | None
    max_submit_jobs: int | None

    def to_dict(self):
        return asdict(self)


def _int_or_none(raw):
    if raw is None or raw == '':
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_qos(text):
    """sacctmgr show qos -> {qos_name: QosLimit}"""
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split('|')
        if len(parts) < 4:
            continue
        name, max_tres, max_jobs, max_submit = (p.strip() for p in parts[:4])
        tres = parse_tres(max_tres)
        out[name] = QosLimit(
            name=name,
            max_gpus=_int_or_none(tres.get('gres/gpu')),
            max_high_perf_gpus=_int_or_none(tres.get(f'gres/gpu:{HIGH_PERF}')),
            max_running_jobs=_int_or_none(max_jobs),
            max_submit_jobs=_int_or_none(max_submit),
        )
    return out


def parse_assoc(text):
    """sacctmgr show assoc user=$USER -> (사용자명, 계정, QOS 이름)

    셋 다 못 찾으면 (None, None, None). 사용자명이 여기 들어있어서 whoami 를 안 부른다.
    계정(account)은 학부/대학원 판별의 근거다.

    한 사용자가 여러 계정·QOS 를 가질 수 있다. QOS 가 붙은 줄을 우선하고,
    없으면 계정이라도 있는 첫 줄을 쓴다.
    """
    fallback = None
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split('|')
        if len(parts) < 3:
            continue
        user = parts[0].strip() or None
        account = parts[1].strip() or None
        qos = parts[2].strip()
        if qos:
            return user, account, qos.split(',')[0]
        if fallback is None and account:
            fallback = (user, account, None)
    return fallback or (None, None, None)


def parse_uptime(text):
    """uptime -> 1분 load average (실패하면 None)

    로그인 노드 규칙: load < 8 일 때만 폴링한다.
    """
    marker = 'load average:'
    idx = text.find(marker)
    if idx < 0:
        return None
    try:
        first = text[idx + len(marker):].split(',')[0]
        return float(first.strip())
    except (ValueError, IndexError):
        return None
