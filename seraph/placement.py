"""어디에 job 을 올려야 가장 빨리 시작하나.

이 도구의 핵심 질문에 답한다:
  "지금 바로 학습을 시작할 수 있나?"
  "안 되면 어디에 올려두면 제일 빨리 시작하나?"

**추측하지 않는다.** sinfo 의 여유 GPU 를 세는 대신 `sbatch --test-only` 로
Slurm 에게 직접 물어본다. Slurm 은 우선순위·backfill·QOS 를 다 계산해서 답한다.

왜 세는 것으로는 안 되냐면 — 실제로 이런 일이 있었다:
  ariel-v6: GPU 8개 중 7개 비어 있고 CPU 52개 놀고 있음
  그런데 Slurm: "3시간 10분 뒤에나 시작 가능"
  (우선순위 높은 대기 job 이 그 자원을 잡아두고 있어서)
  같은 시각 debug_grad 에 내면: "지금 즉시 시작"

즉 여유 GPU 를 보고 "지금 가능"이라 말하면 틀린다. Slurm 에게 물어봐야 한다.
"""

from datetime import datetime

from . import sbatch as sbatch_module
from . import services
from .parsers.testonly import parse_test_only, seconds_until, starts_within

# 후보를 물어볼 때 Slurm 에 던지는 임시 job 이름 (제출되지 않는다)
_PROBE_NAME = 'seraph-probe'


def _fmt_wait(seconds):
    if seconds is None:
        return '알 수 없음'
    if seconds <= 90:
        return '지금 바로'
    minutes = seconds // 60
    if minutes < 60:
        return f'약 {minutes}분 뒤'
    hours = minutes // 60
    rem = minutes % 60
    if hours < 24:
        return f'약 {hours}시간 {rem}분 뒤' if rem else f'약 {hours}시간 뒤'
    days = hours // 24
    return f'약 {days}일 {hours % 24}시간 뒤'


def candidate_partitions(snapshot, hours, include_excluded=False):
    """학습을 올릴 수 있는 파티션들.

    빼는 것:
      - 내 계정으로 못 쓰는 파티션 (학부/대학원 불일치, admin)
      - 요청 시간이 파티션 제한을 넘는 경우
      - config 의 exclude_partitions (기본: debug_*)

    debug_* 를 왜 빼냐면 — 거긴 디버깅·짧은 테스트용이다. "지금 바로 시작된다"는
    이유로 학습을 거기 몰면 정작 디버깅하려는 사람이 못 쓴다. 4시간 제한도 있어서
    학습에 부적합하다. (config 에서 조정 가능)
    """
    need = int(hours * 3600)
    excluded = set(snapshot.config.excluded_partitions)
    out = []
    for name, p in snapshot.partitions.items():
        if not services.can_use_partition(snapshot, name):
            continue
        if name in excluded and not include_excluded:
            continue
        limit = p.time_limit_seconds
        if limit is not None and need > limit:
            continue                    # 시간 제한 초과 -> 애초에 못 냄
        out.append(name)
    return sorted(out)


def _probe(conn, snapshot, partition, gpus, hours, high_perf, node):
    """한 후보에 대해 Slurm 에게 "언제 시작하냐" 물어본다."""
    hh = int(hours)
    mm = int(round((hours - hh) * 60))
    time_limit = f'{hh}:{mm:02d}:00'

    built = sbatch_module.generate_sbatch(
        snapshot, name=_PROBE_NAME, command='hostname',
        partition=partition, gpus=gpus, high_perf=high_perf,
        node=node, time_limit=time_limit,
    )
    if not built['ok']:
        # 우리 lint 가 먼저 막은 경우 (계정/노드/한도). Slurm 에 물어볼 필요 없다.
        blocking = next((p for p in built['lint']['problems']
                         if p['level'] == 'block'), None)
        return {
            'partition': partition,
            'node': built.get('node'),
            'ok': False,
            'reason': blocking['message'] if blocking else '제출할 수 없습니다.',
            'source': 'lint',
        }

    prediction = parse_test_only(conn.test_submit(built['script']))
    return {
        'partition': partition,
        'node': built.get('node') or prediction.node,
        'ok': prediction.ok,
        'reason': prediction.reason,
        'start': prediction.start,
        'prediction': prediction,
        'script': built['script'],
        'source': 'slurm',
    }


def _node_candidates(snapshot, partition, gpus, high_perf, limit):
    """이 파티션에서 물어볼 노드 후보. 여유 많은 순.

    노드를 안 정하면 generate_sbatch 가 하나 고르지만, 노드마다 시작 시각이
    크게 다를 수 있어(실측 3일 차이) 몇 개 물어보고 제일 빠른 걸 고른다.
    """
    nodes = [
        n for n in snapshot.nodes
        if partition in n.partitions and n.schedulable
        and n.is_high_perf == high_perf and n.total_gpus >= gpus
    ]
    if snapshot.is_undergrad:
        allowed = set(snapshot.config.undergrad_nodes)
        nodes = [n for n in nodes if n.name in allowed]
    # 지금 여유가 있는 노드 우선, 그다음 여유 GPU/CPU 많은 순
    nodes.sort(key=lambda n: (-(n.usable_gpus >= gpus), -n.usable_gpus,
                              -n.free_gpus, -n.idle_cpus))
    return [n.name for n in nodes[:limit]] or [None]


def find_fastest(conn, snapshot, *, gpus=1, hours=2.0, high_perf=False,
                 node=None, now=None, probe_nodes=None):
    """지금 바로 되는지, 안 되면 어디가 제일 빠른지.

    conn 은 test_submit 을 가진 연결(SSH 또는 Mock). Slurm 에 후보 수만큼
    물어본다(파티션 2~4개, 각 1회). 가볍다 — 실측 5회에 1초 미만.

    반환:
      {
        'can_start_now': bool,
        'best': {...},              # 가장 빨리 시작하는 곳 (없으면 None)
        'options': [...],           # 후보 전부, 빠른 순
        'headline': '...',          # 화면에 그대로 띄울 한 문장
        'blocked': [...],           # 아예 못 내는 후보와 이유
      }
    """
    now = now or datetime.now()
    if probe_nodes is None:
        probe_nodes = snapshot.config.probe_nodes
    partitions = candidate_partitions(snapshot, hours)

    # GPU 종류: 사용자가 고성능을 딱 집어 요구하지 않았고 QOS 가 허용하면,
    # 고성능도 함께 물어본다. 지금은 일반이 막혀도 고성능이 빌 수 있다.
    kinds = [high_perf]
    limit = snapshot.my_qos
    if not high_perf and limit and (limit.max_high_perf_gpus or 0) >= gpus:
        kinds.append(True)

    results = []
    blocked = []
    for partition in partitions:
        for kind in kinds:
            # 노드를 못 박았으면 그것만, 아니면 후보를 물어보고 제일 빠른 걸 고른다.
            targets = [node] if node else _node_candidates(
                snapshot, partition, gpus, kind, probe_nodes)

            best_here = None
            reason = None
            for target in targets:
                r = _probe(conn, snapshot, partition, gpus, hours, kind, target)
                if not r['ok']:
                    reason = reason or r['reason']
                    continue
                r['wait_seconds'] = seconds_until(r['prediction'], now)
                if best_here is None or (r['wait_seconds'] or 1 << 30) < (
                        best_here['wait_seconds'] or 1 << 30):
                    best_here = r
                if r['wait_seconds'] == 0:
                    break           # 지금 바로 되는 걸 찾았으면 더 볼 필요 없다

            if best_here is None:
                if kind == high_perf:   # 요청한 종류가 막힌 것만 알린다
                    blocked.append({'partition': partition,
                                    'reason': reason or '제출할 수 없습니다.'})
                continue

            best_here['high_perf'] = kind
            best_here['wait_text'] = _fmt_wait(best_here['wait_seconds'])
            best_here['starts_now'] = starts_within(best_here['prediction'], now)
            best_here['time_limit_seconds'] = \
                snapshot.partitions[partition].time_limit_seconds
            results.append(best_here)

    # 빨리 시작하는 순. 같으면 일반 GPU 우선(고성능은 귀하다), 그다음 시간 제한 넉넉한 쪽.
    results.sort(key=lambda r: (r['wait_seconds'] if r['wait_seconds'] is not None
                                else 1 << 30,
                                r['high_perf'],
                                -(r['time_limit_seconds'] or 1 << 30)))

    best = _pick_best(results, high_perf, snapshot.config.high_perf_gain_seconds)
    can_now = bool(best and best['starts_now'])

    return {
        'can_start_now': can_now,
        'best': _public(best) if best else None,
        'options': [_public(r) for r in results],
        'blocked': blocked,
        'requested': {'gpus': gpus, 'hours': hours, 'high_perf': high_perf},
        'headline': _headline(can_now, best, results, blocked, gpus),
    }


def _pick_best(results, requested_high_perf, gain_threshold):
    """가장 좋은 후보. 고성능은 "의미 있게" 빠를 때만 고른다.

    고성능 노드(m/k/n)는 귀한 자원이다. 1분 빨리 시작한다고 고성능을 쓰라고
    추천하면 안 된다. gain_threshold(기본 30분) 이상 빨라야 추천한다.
    사용자가 고성능을 직접 요구했으면 그대로 존중한다.
    """
    if not results:
        return None
    if requested_high_perf:
        return results[0]

    standard = [r for r in results if not r['high_perf']]
    if not standard:
        return results[0]           # 일반으로는 아예 못 냄 -> 고성능뿐

    best_std = standard[0]
    best_any = results[0]
    if best_any is best_std:
        return best_std

    gain = (best_std['wait_seconds'] or 0) - (best_any['wait_seconds'] or 0)
    return best_any if gain >= gain_threshold else best_std


def _public(r):
    """내부용 prediction 객체를 빼고 JSON 으로 내보낼 것만."""
    if r is None:
        return None
    out = {k: v for k, v in r.items() if k not in ('prediction',)}
    return out


def _headline(can_now, best, results, blocked, gpus):
    if best is None:
        if blocked:
            return (f'지금 GPU {gpus}개로 낼 수 있는 파티션이 없습니다. '
                    f'{blocked[0]["reason"]}')
        return '조건에 맞는 파티션이 없습니다.'

    kind = '고성능 GPU' if best.get('high_perf') else 'GPU'
    where = f"{best['partition']} / {best['node']}"

    if can_now:
        limit = best.get('time_limit_seconds')
        cap = f' (최대 {limit // 3600}시간 제한)' if limit else ''
        return f'지금 바로 시작할 수 있습니다 → {where} ({kind}){cap}'

    line = (f'지금 바로는 어렵습니다. 가장 빨리 시작하는 곳은 {where} '
            f'({kind}) — {best["wait_text"]}입니다.')

    # 고성능을 골랐다면 왜 그랬는지 밝힌다 (일반보다 얼마나 빠른지).
    if best.get('high_perf'):
        std = next((r for r in results if not r['high_perf']), None)
        if std:
            gain = (std['wait_seconds'] or 0) - (best['wait_seconds'] or 0)
            line += (f' 일반 GPU 는 {std["wait_text"]}라 '
                     f'{_fmt_wait(gain).replace("뒤", "")} 더 걸립니다.')
    return line
