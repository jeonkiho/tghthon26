"""파티션 정보 파싱.

    batch_grad|infinite|23
    debug_grad|4:00:00|23

시간 제한을 넘겨 제출하면 Slurm 이 즉시 거절한다. 제출 전에 막아야 한다.
"""

from dataclasses import dataclass, asdict


@dataclass
class Partition:
    name: str
    time_limit_seconds: int | None   # None = 무제한
    node_count: int
    is_default: bool = False

    def to_dict(self):
        return asdict(self)


def parse_slurm_duration(raw):
    """Slurm 시간 표기 -> 초. 무제한이면 None, 못 읽으면 None.

    받는 모양: "infinite", "4:00:00", "1-12:00:00", "30:00", "10"
    """
    raw = (raw or '').strip().lower()
    if not raw or raw in ('infinite', 'unlimited', 'n/a'):
        return None

    days = 0
    if '-' in raw:
        day_part, _, raw = raw.partition('-')
        try:
            days = int(day_part)
        except ValueError:
            return None

    parts = raw.split(':')
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None

    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        # 하루 표기가 있으면 "일-시:분", 없으면 "분:초"
        (h, m, s) = (nums[0], nums[1], 0) if days else (0, nums[0], nums[1])
    elif len(nums) == 1:
        h, m, s = (nums[0], 0, 0) if days else (0, nums[0], 0)
    else:
        return None

    return days * 86400 + h * 3600 + m * 60 + s


def parse_partitions(text):
    """sinfo -o "%P|%l|%D" -> {name: Partition}

    기본 파티션은 이름 뒤에 '*' 가 붙는다.
    """
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split('|')
        if len(parts) < 3:
            continue
        name, limit, nodes = (p.strip() for p in parts[:3])
        is_default = name.endswith('*')
        name = name.rstrip('*')
        try:
            node_count = int(nodes)
        except ValueError:
            node_count = 0
        # sinfo 는 (파티션 × 노드상태)별로 한 줄씩 준다. 같은 파티션이 여러 줄이면
        # 노드 수를 합치고 기본(*) 표시를 OR 한다.
        if name in out:
            out[name].node_count += node_count
            out[name].is_default = out[name].is_default or is_default
        else:
            out[name] = Partition(
                name=name,
                time_limit_seconds=parse_slurm_duration(limit),
                node_count=node_count,
                is_default=is_default,
            )
    return out
