"""`sbatch --test-only` 응답 파싱.

Slurm 에게 "이 job 을 내면 언제 시작하냐"를 물어본 답. **제출은 하지 않는다.**

성공:
    sbatch: Job 368283 to start at 2026-07-13T20:20:00 using 2 processors
            on nodes ariel-v6 in partition batch_grad

거절 (세라프의 submit plugin / 계정·QOS 위반):
    sbatch: error: SUBMISSION REJECTED: GPU type 'high_perf' is REQUIRED.
    allocation failure: Invalid account or account/partition combination specified
    sbatch: error: QOSMaxGRESPerUser

이게 sinfo 로 여유 GPU 를 세는 것보다 정확하다. 여유 GPU 가 있어도 우선순위 높은
대기 job 때문에 못 쓸 수 있는데, Slurm 은 그걸 다 계산해서 답한다.
(실제로 GPU 7개가 비어 있는 노드인데 "3시간 뒤에나 시작 가능"이라고 답한 적 있다.)
"""

import re
from dataclasses import dataclass, asdict
from datetime import datetime

_START = re.compile(
    r'to start at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})'
    r'(?:.*?on nodes (\S+))?'
    r'(?:.*?in partition (\S+))?'
)

# 거절 사유 -> 사람이 읽을 말. lint 코드와 짝을 맞춘다.
_REJECT_HINTS = (
    ("GPU type 'high_perf' is REQUIRED",
     '일반 GPU 는 v/g 노드를 -w 로 지정해야 합니다.'),
    ('Invalid account or account/partition',
     '이 파티션은 당신 계정으로 쓸 수 없습니다.'),
    ('QOSMaxGRESPerUser', '본인 GPU 할당량을 초과했습니다.'),
    ('Invalid partition', '없는 파티션입니다.'),
    ('Requested node configuration is not available',
     '요청한 자원(GPU/CPU/메모리)을 그 노드가 제공하지 못합니다.'),
    ('Job violates accounting/QOS policy', 'QOS 정책에 어긋납니다.'),
)


@dataclass
class Prediction:
    ok: bool                  # Slurm 이 받아들였나
    start: str | None         # ISO 8601 예상 시작 시각
    node: str | None
    partition: str | None
    reason: str               # 거절 사유(사람이 읽는 말). ok 면 ''
    raw: str

    def to_dict(self):
        return asdict(self)


def parse_test_only(text):
    """--test-only 출력 -> Prediction"""
    text = text or ''
    m = _START.search(text)
    if m:
        return Prediction(
            ok=True,
            start=m.group(1),
            node=m.group(2),
            partition=m.group(3),
            reason='',
            raw=text.strip(),
        )

    reason = ''
    for needle, hint in _REJECT_HINTS:
        if needle in text:
            reason = hint
            break
    if not reason:
        # 원문에서 error 줄 하나라도 건져 보여준다.
        for line in text.splitlines():
            if 'error' in line.lower() or 'REJECTED' in line:
                reason = line.replace('sbatch:', '').replace('error:', '').strip()
                break
    return Prediction(ok=False, start=None, node=None, partition=None,
                      reason=reason or '알 수 없는 이유로 거절되었습니다.',
                      raw=text.strip())


def starts_within(prediction, now, seconds=90):
    """예상 시작이 '사실상 지금'인가.

    Slurm 은 즉시 시작 가능하면 현재 시각을 돌려준다. 조회~응답 사이 시간차가
    있으니 여유를 둔다.
    """
    if not prediction.ok or not prediction.start:
        return False
    try:
        start = datetime.fromisoformat(prediction.start)
    except ValueError:
        return False
    return (start - now).total_seconds() <= seconds


def seconds_until(prediction, now):
    """지금부터 시작까지 몇 초. 모르면 None."""
    if not prediction.ok or not prediction.start:
        return None
    try:
        start = datetime.fromisoformat(prediction.start)
    except ValueError:
        return None
    return max(0, int((start - now).total_seconds()))
