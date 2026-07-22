"""sacct 출력 파싱 — 끝난 job 의 결과.

sacct 는 job 하나를 여러 행으로 준다:

    358796|OUT_OF_MEMORY|0:125||64G|...       메인 행: ReqMem 은 있고 MaxRSS 는 없다
    358796.batch|OUT_OF_MEMORY|0:125|61144768K||...  배치 스텝: MaxRSS 가 여기 있다
    358796.extern|OUT_OF_MEMORY|0:125|0||...   외부 스텝: 항상 0. 무시한다

그래서 `-X`(스텝 제외)를 쓰면 안 된다. OOM 진단에 필요한 MaxRSS 를 잃는다.
메인 행을 기준으로 삼고 스텝들의 MaxRSS 최댓값을 합쳐 넣는다.
srun 으로 낸 job 은 `.batch` 대신 `.0` 스텝을 가지므로 스텝 이름을 가리지 않는다.

State 는 그냥 문자열이 아니다. 취소된 job 은 `CANCELLED by 20301` 처럼 UID 가
붙어 온다. 그대로 비교하면 어떤 분기에도 걸리지 않는다.

ExitCode 는 `종료코드:시그널` 이다. `0:125` 는 종료코드 0 에 시그널 125 로,
OOM killer 에 죽은 경우다. 종료코드만 보면 성공으로 오해한다.
"""

import re
from dataclasses import dataclass, asdict

from .gres import gpus_from_tres
from .partition import parse_slurm_duration

# 값이 없음을 뜻하는 문자열들. sacct 는 빈칸 대신 이런 걸 넣는다.
_EMPTY = {'', 'none', 'unknown', 'none assigned', 'n/a', 'invalid'}

_FIELDS = 13

# 메모리 단위 -> MB 배율. sacct 는 기본적으로 K 를 쓴다.
_MEM_UNITS = {'K': 1 / 1024, 'M': 1.0, 'G': 1024.0, 'T': 1024.0 * 1024}

SUCCESS = 'COMPLETED'


@dataclass
class FinishedJob:
    job_id: str
    name: str
    state: str                  # 정규화됨: CANCELLED, COMPLETED, FAILED, ...
    raw_state: str              # 원문 ("CANCELLED by 20301")
    exit_code: int
    signal: int
    cancelled_by: str | None    # 취소한 사람의 UID (있으면)
    start: str | None           # ISO 8601. 시작 전에 취소되면 None
    end: str | None
    elapsed_seconds: int | None
    time_limit_seconds: int | None
    partition: str
    nodes: str
    gpus: int
    high_perf_gpus: int
    req_mem_mb: int | None
    max_rss_mb: int | None      # 실제로 쓴 메모리 최댓값 (스텝에서 온다)

    @property
    def succeeded(self):
        return self.state == SUCCESS

    def to_dict(self):
        d = asdict(self)
        d['succeeded'] = self.succeeded
        return d


def _clean(value):
    value = (value or '').strip()
    return None if value.lower() in _EMPTY else value


def _parse_state(raw):
    """"CANCELLED by 20301" -> ("CANCELLED", "20301")"""
    raw = (raw or '').strip()
    if ' by ' in raw:
        state, _, who = raw.partition(' by ')
        return state.strip(), who.strip()
    return raw, None


def _parse_exit_code(raw):
    """"127:0" -> (127, 0). 못 읽으면 (0, 0)."""
    raw = (raw or '').strip()
    if ':' not in raw:
        try:
            return int(raw), 0
        except ValueError:
            return 0, 0
    code, _, signal = raw.partition(':')
    try:
        return int(code), int(signal)
    except ValueError:
        return 0, 0


def parse_memory_mb(raw):
    """"61144768K" -> 59711. "64G" -> 65536. "0" -> 0. 없으면 None.

    ReqMem 에 예전 Slurm 처럼 'c'(per-cpu)/'n'(per-node) 접미어가 붙어도 떼어낸다.
    """
    raw = _clean(raw)
    if raw is None:
        return None
    raw = raw.rstrip('cn')          # 64Gn -> 64G
    if not raw:
        return None
    unit = raw[-1].upper()
    if unit in _MEM_UNITS:
        number, factor = raw[:-1], _MEM_UNITS[unit]
    else:
        number, factor = raw, 1 / 1024   # 단위가 없으면 K 로 본다
    try:
        return int(float(number) * factor)
    except ValueError:
        return None


def _is_step(job_id):
    return '.' in job_id


def _base_id(job_id):
    return job_id.split('.', 1)[0]


def parse_sacct(text):
    """sacct -n -P 출력 -> [FinishedJob] (최근 것부터)

    아직 안 끝난 job(RUNNING/PENDING)은 결과가 없으므로 뺀다.
    """
    mains = {}
    max_rss = {}

    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split('|')
        if len(parts) < _FIELDS:
            continue

        job_id = parts[0].strip()
        base = _base_id(job_id)

        if _is_step(job_id):
            # .extern 은 항상 0 이고, .batch 와 .0 중 어느 쪽이 올지 모른다.
            # 스텝들의 최댓값을 쓴다.
            rss = parse_memory_mb(parts[12])
            if rss is not None:
                max_rss[base] = max(max_rss.get(base, 0), rss)
            continue

        state, cancelled_by = _parse_state(parts[2])
        if state in ('RUNNING', 'PENDING', 'REQUEUED', 'SUSPENDED'):
            continue

        exit_code, signal = _parse_exit_code(parts[3])
        gpus, high_perf = gpus_from_tres(parts[10])

        mains[base] = FinishedJob(
            job_id=base,
            name=parts[1].strip(),
            state=state,
            raw_state=parts[2].strip(),
            exit_code=exit_code,
            signal=signal,
            cancelled_by=cancelled_by,
            start=_clean(parts[4]),
            end=_clean(parts[5]),
            elapsed_seconds=parse_slurm_duration(parts[6]),
            time_limit_seconds=parse_slurm_duration(parts[7]),
            partition=_clean(parts[8]) or '',
            nodes=_clean(parts[9]) or '',
            gpus=gpus,
            high_perf_gpus=high_perf,
            req_mem_mb=parse_memory_mb(parts[11]),
            max_rss_mb=None,
        )

    for base, rss in max_rss.items():
        if base in mains:
            mains[base].max_rss_mb = rss

    # job_id 는 순수 정수가 아닐 수 있다(배열잡 '131057_3', het job '131_0+0').
    # 숫자 조각들로 정렬해 크래시를 피한다.
    def _sort_key(j):
        nums = re.findall(r'\d+', j.job_id)
        return tuple(int(n) for n in nums) if nums else (0,)

    return sorted(mains.values(), key=_sort_key, reverse=True)
