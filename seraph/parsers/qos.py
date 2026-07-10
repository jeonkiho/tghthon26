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
    """sacctmgr show assoc user=$USER -> (사용자명, QOS 이름)

    둘 다 못 찾으면 (None, None). 사용자명이 여기 들어있어서 whoami 를 따로 안 부른다.
    """
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split('|')
        if len(parts) < 2:
            continue
        user, qos = parts[0].strip(), parts[1].strip()
        if qos:
            # 여러 개면 쉼표로 온다. 첫 번째를 쓴다.
            return user or None, qos.split(',')[0]
    return None, None


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
