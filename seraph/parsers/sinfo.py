"""sinfo 출력 파싱.

`sinfo -N` 은 노드를 소속 파티션 수만큼 반복해서 낸다. ariel-g1 은 admin,
debug_grad, batch_grad 세 줄로 나온다. GPU 개수를 그냥 더하면 3배가 된다.
그래서 노드명으로 합치고, 파티션은 리스트로 모은다.

"여유 GPU" 를 GPU 숫자만으로 세면 안 된다. GPU 가 남아 있어도 그 노드에 idle CPU 가
없으면 Slurm 은 job 을 배정하지 못한다. 실제로 ariel-v3 은 GPU 1개가 비어 있는데
CPU 는 64/64 전부 할당된 상태였다. 그 GPU 1개는 아무도 쓸 수 없다.
"""

from dataclasses import dataclass, field, asdict

from .gres import parse_gres, total_gpus, HIGH_PERF

# GPU 를 새로 받을 수 있는 상태. drained/down/drain* 은 제외한다.
_SCHEDULABLE = {'idle', 'mixed', 'allocated'}


@dataclass
class Node:
    name: str
    state: str
    partitions: list = field(default_factory=list)
    total_gpus: int = 0
    used_gpus: int = 0
    is_high_perf: bool = False
    broken_gpus: int = 0
    idle_cpus: int = 0
    total_cpus: int = 0
    free_mem_mb: int = 0

    @property
    def free_gpus(self):
        """GPU 숫자만 본 여유. 실제 배정 가능 여부는 usable_gpus 를 봐야 한다."""
        if not self.schedulable:
            return 0
        return max(0, self.total_gpus - self.used_gpus)

    @property
    def usable_gpus(self):
        """지금 실제로 job 을 받을 수 있는 GPU 수.

        idle CPU 가 없으면 GPU 가 남아도 0 이다.
        """
        if self.idle_cpus <= 0:
            return 0
        return self.free_gpus

    @property
    def schedulable(self):
        return self.state in _SCHEDULABLE

    @property
    def cpu_starved(self):
        """GPU 는 남는데 CPU 가 없어 못 쓰는 상태."""
        return self.free_gpus > 0 and self.idle_cpus <= 0

    def to_dict(self):
        d = asdict(self)
        d['free_gpus'] = self.free_gpus
        d['usable_gpus'] = self.usable_gpus
        d['schedulable'] = self.schedulable
        d['cpu_starved'] = self.cpu_starved
        return d


def _parse_cpus_state(raw):
    """"40/24/0/64" (Allocated/Idle/Other/Total) -> (idle, total)"""
    parts = raw.split('/')
    if len(parts) != 4:
        return 0, 0
    try:
        return int(parts[1]), int(parts[3])
    except ValueError:
        return 0, 0


def _int(raw):
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0


def parse_sinfo(text):
    """구분자 '|' 로 나온 sinfo 출력 -> [Node] (노드명 기준 중복 제거)"""
    nodes = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split('|')
        if len(parts) < 8:
            continue
        (name, partition, state, gres, gres_used,
         cpus_state, alloc_mem, memory) = (p.strip() for p in parts[:8])

        node = nodes.get(name)
        if node is None:
            total = parse_gres(gres)
            used = parse_gres(gres_used)
            idle_cpus, total_cpus = _parse_cpus_state(cpus_state)
            node = Node(
                name=name,
                state=state,
                total_gpus=total_gpus(total),
                used_gpus=total_gpus(used),
                is_high_perf=HIGH_PERF in total,
                broken_gpus=total.get('broken', 0),
                idle_cpus=idle_cpus,
                total_cpus=total_cpus,
                free_mem_mb=max(0, _int(memory) - _int(alloc_mem)),
            )
            nodes[name] = node

        if partition and partition not in node.partitions:
            node.partitions.append(partition)

    return sorted(nodes.values(), key=lambda n: n.name)
