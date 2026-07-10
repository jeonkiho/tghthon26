"""squeue 출력 파싱."""

from dataclasses import dataclass, asdict

from .gres import gpus_from_tres

# commands.SQUEUE 의 필드 순서와 반드시 일치해야 한다.
_FIELDS = 8  # Name 앞까지. Name 은 이름에 '|' 가 들어갈 수 있어 나머지를 통째로 받는다.

RUNNING = 'R'
PENDING = 'PD'


@dataclass
class Job:
    job_id: str
    partition: str
    user: str
    state: str          # 'R' | 'PD' | 그 외 Slurm 상태 약어
    time_used: str      # "2-19:04:48" 같은 Slurm 표기 그대로. 표시용.
    qos: str
    gpus: int           # 요청/할당 GPU 총 개수
    high_perf_gpus: int # 그중 고성능 노드 GPU 개수
    nodes: str          # 실행 중이면 노드명, 대기 중이면 "" 또는 사유
    reason: str         # 대기 사유. 실행 중이면 "None"
    name: str

    @property
    def is_running(self):
        return self.state == RUNNING

    @property
    def is_pending(self):
        return self.state == PENDING

    def to_dict(self):
        return asdict(self)


def parse_squeue(text):
    """구분자 '|' 로 나온 squeue 출력 -> [Job]

    각 줄은 뒤에 구분자가 하나 더 붙는다(Slurm 의 suffix 동작). 마지막 빈 칸은 버린다.
    """
    jobs = []
    for line in text.splitlines():
        line = line.rstrip('\n')
        if not line.strip():
            continue
        parts = line.split('|')
        if len(parts) < _FIELDS + 1:
            continue  # 깨진 줄은 조용히 건너뛴다. 폴링 중 한 줄 깨졌다고 죽으면 안 된다.

        job_id, partition, user, state, time_used, qos, tres, nodes = (
            p.strip() for p in parts[:_FIELDS]
        )
        reason = parts[_FIELDS].strip()
        # Name 은 남은 전부에서 마지막 구분자만 떼어낸다.
        name = '|'.join(parts[_FIELDS + 1:]).rstrip('|').strip()

        gpus, high_perf = gpus_from_tres(tres)

        # 대기 중인 job 의 NodeList 는 사유가 괄호로 들어오거나 (null) 이다.
        if nodes.startswith('(') or nodes == 'n/a':
            nodes = ''

        jobs.append(Job(
            job_id=job_id,
            partition=partition,
            user=user,
            state=state,
            time_used=time_used,
            qos=qos,
            gpus=gpus,
            high_perf_gpus=high_perf,
            nodes=nodes,
            reason=reason.strip('()'),
            name=name,
        ))
    return jobs


def parse_squeue_start(text):
    """`squeue --start` -> {job_id: iso8601 시각}

    Slurm 이 시각을 못 내면 'N/A' 를 준다. 그런 건 넣지 않는다.
    """
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split('|')
        if len(parts) < 2:
            continue
        job_id, start = parts[0].strip(), parts[1].strip()
        if not start or start in ('N/A', '(null)'):
            continue
        out[job_id] = start
    return out
