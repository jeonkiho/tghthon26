"""sbatch 스크립트 생성.

생성 전에 lint_job() 을 돌린다. block 이 하나라도 있으면 스크립트를 만들지 않는다.
"절대 시작되지 않을 job" 을 예쁘게 만들어 주는 건 도움이 안 된다.

세라프에는 제출을 가로채는 submit plugin 이 있고, GRES 표기 규칙이 까다롭다.
`sbatch --test-only` 로 실제 확인한 규칙:

  --gres=gpu:N              일반 GPU. 반드시 -w 로 v/g 노드 하나를 지정해야 한다.
                            지정하지 않으면 제출이 거절된다.
  --gres=gpu:high_perf:N    고성능 GPU. 노드 지정 없이 통과한다.

GRES 타입과 노드 타입은 반드시 맞아야 한다. `--gres=gpu:1 -w ariel-k1` 은 거절된다.

거절 메시지는 -x 로 고성능 노드를 전부 제외하는 방법도 안내하지만, 실제로 해보면
그 경로로는 통과하지 않는다(스크립트 지시자·CLI 플래그·짧은 이름 모두 거절됨).
그래서 -w 로 노드를 지정하는 길만 쓴다.

    SUBMISSION REJECTED: GPU type 'high_perf' is REQUIRED.
    To use 'gpu:1':
      1. Specify v/g nodes with -w (e.g., -w ariel-v1)
      2. OR Exclude ALL high-perf nodes with -x (m1,m2,k1,k2,n1)   <- 실제로는 안 됨

노드를 여러 개 지정하면(-w a,b) Slurm 은 둘 다 확보될 때까지 기다리므로 훨씬 늦게
시작한다. 항상 하나만 지정한다.
"""

import shlex

from . import services
from .parsers import HIGH_PERF


def high_perf_nodes(snapshot):
    """고성능 노드 이름들. 하드코딩하지 않고 sinfo 에서 얻는다."""
    return sorted(n.name for n in snapshot.nodes if n.is_high_perf)


def pick_standard_node(snapshot, partition, gpus):
    """일반 GPU job 을 올릴 v/g 노드 하나를 고른다. 없으면 None.

    지금 바로 시작할 수 있는 노드를 우선하고, 그런 노드가 없으면 가장 빨리 빌
    법한 노드(여유 GPU·idle CPU 가 많은 순)를 고른다. 꽉 찬 노드를 지정해도
    제출은 되고 그 노드가 빌 때까지 기다린다.
    """
    candidates = [
        n for n in snapshot.nodes
        if partition in n.partitions and not n.is_high_perf
        and n.schedulable and n.total_gpus >= gpus
    ]
    if not candidates:
        return None
    ready = [n for n in candidates if n.usable_gpus >= gpus]
    if ready:
        return max(ready, key=lambda n: n.usable_gpus).name
    return max(candidates, key=lambda n: (n.free_gpus, n.idle_cpus)).name


def _sbatch_lines(*, name, partition, gpus, high_perf, cpus, mem, time_limit,
                  node, output):
    gres = f'gpu:{HIGH_PERF}:{gpus}' if high_perf else f'gpu:{gpus}'
    lines = [
        '#!/bin/bash',
        f'#SBATCH --job-name={name}',
        f'#SBATCH --partition={partition}',
        f'#SBATCH --gres={gres}',
        f'#SBATCH --cpus-per-task={cpus}',
        f'#SBATCH --mem={mem}',
        f'#SBATCH --time={time_limit}',
        f'#SBATCH --output={output}',
    ]
    if node:
        # 노드는 항상 하나만. 여러 개 적으면 전부 확보될 때까지 기다린다.
        lines.append(f'#SBATCH --nodelist={node}')
    return lines


def generate_sbatch(snapshot, *, name, command, partition=None, gpus=1,
                    high_perf=False, cpus=None, mem=None, time_limit=None,
                    node=None, paths=()):
    """제출용 sbatch 스크립트를 만든다.

    일반 GPU 인데 node 를 안 넘기면 v/g 노드를 하나 자동으로 고른다. 세라프가
    노드 지정 없는 gpu:N 을 거절하기 때문에, 이건 선택이 아니라 필수다.
    고성능 GPU 는 노드 지정 없이 통과하므로 건드리지 않는다.

    반환: {'ok', 'script', 'lint', 'command_preview', 'node', 'auto_selected_node'}
    ok=False 면 script 는 None 이다.
    """
    cfg = snapshot.config
    defaults = cfg.sbatch
    partition = partition or cfg.default_partition
    cpus = cpus if cpus is not None else defaults['default_cpus_per_task']
    mem = mem or defaults['default_mem']
    time_limit = time_limit or defaults['default_time']
    output = defaults['output_pattern']

    auto_selected = False
    if not high_perf and not node:
        node = pick_standard_node(snapshot, partition, gpus)
        auto_selected = node is not None

    lint = services.lint_job(
        snapshot,
        partition=partition,
        gpus=gpus,
        high_perf=high_perf,
        paths=paths,
        time_limit=time_limit,
        node=node,
    )

    if not high_perf and not node:
        lint['problems'].append({
            'level': 'block',
            'code': 'NO_ELIGIBLE_NODE',
            'message': (f'{partition} 에 GPU {gpus}개를 올릴 수 있는 일반 노드가 '
                        f'없습니다. 고성능 노드를 쓰거나 GPU 수를 줄이세요.'),
        })
        lint['ok'] = False

    if isinstance(command, (list, tuple)):
        command = ' '.join(shlex.quote(str(c)) for c in command)

    result = {
        'node': node,
        'auto_selected_node': auto_selected,
        'lint': lint,               # ok=True 여도 warn 이 있을 수 있다
        'command_preview': f'sbatch {name}.sh',
    }

    if not lint['ok']:
        return {'ok': False, 'script': None, **result}

    lines = _sbatch_lines(
        name=name, partition=partition, gpus=gpus, high_perf=high_perf,
        cpus=cpus, mem=mem, time_limit=time_limit, node=node, output=output,
    )
    return {
        'ok': True,
        'script': '\n'.join(lines) + '\n\n' + command.strip() + '\n',
        **result,
    }


def suggest_node(snapshot, *, partition=None, gpus=1, high_perf=False):
    """지금 가장 빨리 잡힐 노드 하나. 없으면 None.

    노드를 못 박으면 그 노드만 기다린다. 아무 노드나 괜찮다면 generate_sbatch 에
    node 를 넘기지 않는 편이 낫다.
    """
    nodes = services.get_node_availability(
        snapshot, partition=partition, need_gpus=gpus, high_perf=high_perf)
    return nodes[0]['name'] if nodes else None
