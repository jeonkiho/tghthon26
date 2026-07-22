"""프론트가 부르는 함수들. 최종 산출물은 전부 JSON 직렬화 가능한 dict.

여기서만 판단/계산을 한다. 화면에 뭘 빨갛게 칠할지는 프론트가 정한다.
"""

from . import config as config_module
from . import clusters
from .parsers import (
    parse_squeue, parse_squeue_start, parse_sinfo,
    parse_qos, parse_assoc, parse_uptime, parse_partitions,
    parse_slurm_duration, parse_accounts,
)

# config 를 안 넘겼을 때 쓰는 기본 한도. (설정 파일이 있으면 그쪽이 이긴다.)
LOAD_LIMIT = 8.0

# 대기 사유 -> 사용자에게 보여줄 설명. Slurm 원문은 영어 약어라 그대로 쓰면 못 알아본다.
REASON_TEXT = {
    'QOSMaxGRESPerUser': '본인 GPU 할당량을 모두 사용 중',
    'Resources': '클러스터에 여유 GPU 부족',
    'Priority': '우선순위가 높은 다른 job 이 먼저 대기 중',
    'Dependency': '먼저 끝나야 하는 job 이 있음',
    'DependencyNeverSatisfied': '의존하는 job 이 실패·취소되어 시작될 수 없음 (취소 필요)',
    'ReqNodeNotAvail': '요청한 노드를 지금 쓸 수 없음',
    'QOSMaxJobsPerUserLimit': '동시 실행 job 한도 초과',
    'AssocMaxJobsLimit': '계정 동시 실행 한도 초과',
    'None': '',
}


class Snapshot:
    """한 번의 폴링으로 받은 원본 텍스트 묶음.

    connection 이 채워서 넘겨준다. services 는 SSH 를 모른다.
    """

    def __init__(self, squeue, sinfo, partitions, squeue_start, qos, assoc,
                 accounts, uptime, config=None):
        self.jobs = parse_squeue(squeue)
        self.nodes = parse_sinfo(sinfo)
        self.partitions = parse_partitions(partitions)
        self.start_times = parse_squeue_start(squeue_start)
        self.qos_limits = parse_qos(qos)
        self.me, self.account, self.my_qos_name = parse_assoc(assoc)
        self.accounts = parse_accounts(accounts)    # {계정: 설명}
        self.load = parse_uptime(uptime)
        self.config = config or config_module.load()

    @property
    def account_description(self):
        """내 계정의 설명. 소속 클러스터가 여기 적혀 있을 수 있다."""
        return self.accounts.get(self.account)

    @property
    def my_qos(self):
        return self.qos_limits.get(self.my_qos_name)

    @property
    def is_undergrad(self):
        """학부생인가. 계정이 ugrad 로 시작하면 학부. 모르면 None."""
        if not self.account:
            return None
        return self.account.startswith('ugrad')

    @property
    def default_partition(self):
        """내 계정에 맞는 기본 파티션.

        학부 파티션은 학과별로 갈리므로 계정에서 유도한다(ugrad_ce -> batch_ce_ugrad).
        실서버에 그 파티션이 실제로 있으면 그것을, 없으면(예: mock) 신분별 config
        기본값으로 떨어진다.
        """
        want = clusters.partition_from_account(self.account)
        if self.partitions and want in self.partitions:
            return want
        if self.is_undergrad:
            return self.config.undergrad_partition
        return self.config.default_partition


def _nodes_in(snapshot, partition):
    return [n for n in snapshot.nodes if partition in n.partitions]


def _partition(snapshot, partition):
    """None 이면 config 의 기본 파티션(신분에 맞춰)."""
    return partition or snapshot.default_partition


def can_use_partition(snapshot, partition):
    """이 사용자가 이 파티션에 job 을 낼 수 있는가.

    세라프 파티션은 계정으로 갈린다: *_ugrad 는 학부, *_grad 는 대학원, admin 은 root.
    사용자 계정을 모르면(mock 등) 막지 않고 True 로 둔다.
    """
    undergrad = snapshot.is_undergrad
    if 'admin' in partition:
        return False                    # 학생은 admin 파티션을 못 쓴다
    if undergrad is None:
        return True                     # 신분 불명이면 판단 보류
    if partition.endswith('_ugrad'):
        if not undergrad:
            return False
        # 학부생은 자기 계정(학과)의 파티션만 쓸 수 있다. 타 학과 *_ugrad 는 서버가 거절.
        own = {clusters.partition_from_account(snapshot.account, 'batch'),
               clusters.partition_from_account(snapshot.account, 'debug')}
        return partition in own
    if partition.endswith('_grad'):
        return not undergrad
    return True


def _node_allowlist(snapshot):
    """학부 노드 제한(config.undergrad_nodes)을, 그 노드가 지금 접속한 클러스터에
    실제로 존재할 때만 적용한다.

    ariel(=ariel-v[6-12] 존재)이면 그 목록으로 제한하고, moana 등 다른 클러스터면
    그 이름이 아예 없으므로 None(추가 제한 없음 — 파티션 멤버십이 곧 제약)을 준다.
    학부생이 아니면 None.
    """
    if not snapshot.is_undergrad:
        return None
    cfg = set(snapshot.config.undergrad_nodes)
    present = {n.name for n in snapshot.nodes}
    return cfg if (cfg & present) else None


def get_partitions(snapshot):
    """모든 파티션 + 내가 쓸 수 있는지(can_use). 프론트가 회색/자물쇠로 그린다."""
    out = {}
    for name, p in snapshot.partitions.items():
        d = p.to_dict()
        d['can_use'] = can_use_partition(snapshot, name)
        out[name] = d
    return out


def whoami(snapshot):
    """접속한 사용자의 신분과 소속 클러스터. 화면 상단/안내에 쓴다."""
    account = snapshot.account
    # 계정 설명에 클러스터가 적혀 있으면 그게 접미어 추측보다 정확하다.
    info = clusters.belongs_here(account, snapshot.account_description)
    # 실제 접속한 클러스터를 노드 이름으로 추정. 계정 소속과 맞으면 '제 클러스터'로 본다
    # (PRIMARY 상수 대신 실데이터 기준). moana 등에 제대로 붙었으면 안내를 띄우지 않는다.
    connected = clusters.infer_cluster(snapshot.nodes)
    on_primary = info['on_primary']
    notice = info['advice']
    if connected and info['cluster']:
        on_primary = (info['cluster'] == connected)
        notice = '' if on_primary else info['advice']
    return {
        'user': snapshot.me,
        'account': account,
        'account_description': snapshot.account_description,
        'qos': snapshot.my_qos_name,
        'is_undergrad': snapshot.is_undergrad,
        'position': clusters.position_from_account(account),
        'major': clusters.major_from_account(account),
        'cluster': info['cluster'],           # 소속 클러스터 (ariel/moana/aurora)
        'connected_cluster': connected,        # 지금 실제로 붙어 있는 클러스터
        'on_primary': on_primary,              # 내 소속 클러스터에 제대로 붙었는가
        'default_partition': snapshot.default_partition,
        # 소속과 다른 클러스터에 붙었을 때만 "저 서버로 가라" 안내가 채워진다
        'cluster_notice': notice,
    }


def get_gpu_status(snapshot, partition=None):
    """파티션 전체 GPU 현황."""
    partition = _partition(snapshot, partition)
    nodes = _nodes_in(snapshot, partition)
    jobs = [j for j in snapshot.jobs if j.partition == partition]
    pending = [j for j in jobs if j.is_pending]

    total = sum(n.total_gpus for n in nodes)

    # GPU 가 남아도 그 노드에 idle CPU 가 없으면 배정되지 않는다. 실제로 쓸 수 있는
    # 수(usable)와 단순히 비어 있는 수(free)를 나눠서 내려보낸다. 프론트는 usable 을
    # 크게 보여주고, 둘이 다르면 그 차이를 설명해 주면 된다.
    free = sum(n.free_gpus for n in nodes)
    usable = sum(n.usable_gpus for n in nodes)

    # 고성능(m/k/n) GPU 와 일반 GPU 는 서로 대체할 수 없다. 합쳐서 세면
    # "40개 여유인데 Resources 로 대기 중" 같은 모순된 안내가 나온다.
    usable_hp = sum(n.usable_gpus for n in nodes if n.is_high_perf)

    return {
        'partition': partition,
        'total_gpus': total,
        'used_gpus': total - free,
        'free_gpus': usable,                       # 실제로 쓸 수 있는 GPU
        'idle_but_unusable_gpus': free - usable,   # 비었지만 CPU 가 없어 못 쓰는 GPU
        'free_high_perf_gpus': usable_hp,
        'free_standard_gpus': usable - usable_hp,
        'total_high_perf_gpus': sum(n.total_gpus for n in nodes if n.is_high_perf),
        'utilization': round((total - free) / total, 3) if total else 0.0,
        'running_jobs': sum(1 for j in jobs if j.is_running),
        'pending_jobs': len(pending),
        'pending_by_reason': _count_by(pending, lambda j: j.reason),
        'cpu_starved_nodes': [n.name for n in nodes if n.cpu_starved],
        'nodes': [n.to_dict() for n in nodes],
    }


def _count_by(items, key):
    out = {}
    for it in items:
        k = key(it)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def get_node_availability(snapshot, partition=None, need_gpus=1,
                          high_perf=False):
    """지금 job 을 받을 수 있는 노드를, 여유 GPU 가 많은 순으로.

    high_perf=True 면 고성능 노드만 본다. QOS 가 고성능을 금지하면 빈 목록이 나온다.
    """
    partition = _partition(snapshot, partition)
    limit = snapshot.my_qos
    if high_perf and limit and limit.max_high_perf_gpus == 0:
        return []

    # 학부생은 정해진 노드만 쓸 수 있다(ariel 한정). moana 등에선 파티션 멤버십으로 충분.
    allowed = _node_allowlist(snapshot)

    candidates = [
        n for n in _nodes_in(snapshot, partition)
        if n.usable_gpus >= need_gpus and n.is_high_perf == high_perf
        and (allowed is None or n.name in allowed)
    ]
    candidates.sort(key=lambda n: -n.usable_gpus)
    return [n.to_dict() for n in candidates]


def _qos_for(snapshot, user):
    """이 사용자의 QOS 한도. QOS 는 사람마다 다르므로 남의 job 에 내 한도를 쓰면 안 된다.

    본인이면 assoc 에서 알 수 있고, 남이면 그 사람 job 에 찍힌 QOS 를 본다.
    job 이 하나도 없는 남이면 알 방법이 없다 -> (None, None).
    """
    if user == snapshot.me and snapshot.my_qos_name:
        name = snapshot.my_qos_name
    else:
        name = next((j.qos for j in snapshot.jobs if j.user == user and j.qos), None)
    return name, snapshot.qos_limits.get(name)


def get_my_usage(snapshot, user):
    """이 사용자가 지금 쓰고 있는 GPU 와 QOS 한도."""
    mine = [j for j in snapshot.jobs if j.user == user]
    running = [j for j in mine if j.is_running]
    pending = [j for j in mine if j.is_pending]
    qos_name, limit = _qos_for(snapshot, user)

    used = sum(j.gpus for j in running)
    used_hp = sum(j.high_perf_gpus for j in running)

    return {
        'user': user,
        'qos': qos_name,
        'gpus_in_use': used,
        'gpus_limit': limit.max_gpus if limit else None,
        'high_perf_in_use': used_hp,
        'high_perf_limit': limit.max_high_perf_gpus if limit else None,
        'running_jobs': len(running),
        'running_jobs_limit': limit.max_running_jobs if limit else None,
        'submitted_jobs': len(mine),
        'submit_jobs_limit': limit.max_submit_jobs if limit else None,
        'pending_jobs': len(pending),
    }


def _binding_quota(usage, job):
    """이 job 을 막고 있는 한도가 정확히 무엇인지 찾는다.

    QOSMaxGRESPerUser 만 보고 "GPU 총량 초과" 라고 말하면 틀린다. 실제로 총 GPU 는
    8/12 로 여유가 있는데 고성능 GPU 가 8/8 이라서 막힌 경우가 있다. 어떤 한도에
    걸렸는지 짚어주지 않으면 사용자는 원인을 못 찾는다.

    반환: (한도 종류, 현재, 한도) 또는 None
    """
    hp_limit = usage['high_perf_limit']
    if (job.high_perf_gpus and hp_limit is not None
            and usage['high_perf_in_use'] + job.high_perf_gpus > hp_limit):
        return ('high_perf_gpu', usage['high_perf_in_use'], hp_limit)

    gpu_limit = usage['gpus_limit']
    if gpu_limit is not None and usage['gpus_in_use'] + job.gpus > gpu_limit:
        return ('gpu', usage['gpus_in_use'], gpu_limit)

    job_limit = usage['running_jobs_limit']
    if job_limit is not None and usage['running_jobs'] >= job_limit:
        return ('running_jobs', usage['running_jobs'], job_limit)

    return None


_QUOTA_LABEL = {
    'high_perf_gpu': '고성능 GPU',
    'gpu': 'GPU',
    'running_jobs': '동시 실행 job',
}


def diagnose_pending(snapshot, user, partition=None):
    """"내 job 은 왜 안 도는가" 에 답한다.

    세라프에서 대기의 대부분은 GPU 부족이 아니라 개인 QOS 한도 초과다. 둘을 구분해서
    알려주는 게 이 도구의 핵심이다. 여유 GPU 가 40개 있는데도 대기 중일 수 있다.
    """
    partition = _partition(snapshot, partition)
    usage = get_my_usage(snapshot, user)
    cluster = get_gpu_status(snapshot, partition)
    pending = [j for j in snapshot.jobs if j.user == user and j.is_pending]

    findings = []
    for job in pending:
        reason = job.reason
        start = snapshot.start_times.get(job.job_id)
        detail = {
            'job_id': job.job_id,
            'name': job.name,
            'reason': reason,
            'reason_text': REASON_TEXT.get(reason, reason),
            'requested_gpus': job.gpus,
            'estimated_start': start,
            'confidence': _confidence(reason, start),   # medium | low | unknown
            'blocked_by_quota': False,
            'quota_kind': None,     # 'gpu' | 'high_perf_gpu' | 'running_jobs'
            'advice': '',
        }

        if reason == 'QOSMaxGRESPerUser':
            detail['blocked_by_quota'] = True
            binding = _binding_quota(usage, job)
            if binding:
                kind, current, limit = binding
                detail['quota_kind'] = kind
                label = _QUOTA_LABEL[kind]
                detail['advice'] = (
                    f"클러스터에 GPU 가 {cluster['free_gpus']}개 남아 있습니다. "
                    f"막고 있는 건 본인의 {label} 할당량입니다 "
                    f"({current}/{limit} 사용 중). "
                    f"돌고 있는 job 이 끝나야 이 job 이 시작됩니다."
                )
            else:
                # Slurm 은 한도 초과라는데 우리 계산으로는 여유가 있다.
                # 다른 사용자와 공유하는 한도이거나, 방금 job 이 끝났을 수 있다.
                detail['advice'] = (
                    'QOS 할당량에 걸려 있습니다. 잠시 후 다시 확인해 보세요.'
                )
        elif reason == 'Resources':
            # 고성능 job 이면 고성능 여유만 따진다. 일반 GPU 가 아무리 남아도 못 쓴다.
            if job.high_perf_gpus:
                avail = cluster['free_high_perf_gpus']
                detail['advice'] = (
                    f'고성능 노드(m/k/n)의 여유 GPU 가 부족합니다 '
                    f'(고성능 {avail}개 여유, {job.high_perf_gpus}개 필요). '
                    f"일반 GPU 는 {cluster['free_standard_gpus']}개 남아 있으니, "
                    f'고성능이 꼭 필요하지 않다면 일반 노드로 내보세요.'
                )
            else:
                detail['advice'] = (
                    f"여유 GPU 가 부족합니다 "
                    f"(일반 {cluster['free_standard_gpus']}개 여유, {job.gpus}개 필요). "
                    f'요청한 CPU/메모리 조건 때문일 수도 있습니다.'
                )
        elif reason == 'Priority':
            detail['advice'] = '자원은 있지만 앞선 job 이 먼저 배정받습니다.'
        elif reason == 'Dependency':
            detail['advice'] = '선행 job 이 끝나면 자동으로 시작합니다.'

        findings.append(detail)

    blocked = [f for f in findings if f['blocked_by_quota']]
    return {
        'user': user,
        'usage': usage,
        'cluster_free_gpus': cluster['free_gpus'],
        'pending_count': len(pending),
        'quota_blocked_count': len(blocked),
        # 프론트가 큰 글씨로 띄울 한 줄.
        'headline': _headline(blocked, len(pending), cluster['free_gpus'], usage),
        'jobs': findings,
    }


def _headline(blocked, pending, free_gpus, usage):
    if pending == 0:
        return '대기 중인 job 이 없습니다.'
    if not blocked:
        return f'{pending}개 job 이 대기 중입니다.'

    kinds = {f['quota_kind'] for f in blocked if f['quota_kind']}
    if len(kinds) == 1:
        kind = kinds.pop()
        label = _QUOTA_LABEL[kind]
        if kind == 'high_perf_gpu':
            current, limit = usage['high_perf_in_use'], usage['high_perf_limit']
        elif kind == 'gpu':
            current, limit = usage['gpus_in_use'], usage['gpus_limit']
        else:
            current, limit = usage['running_jobs'], usage['running_jobs_limit']
        quota = f'본인 {label} 할당량({current}/{limit})'
    else:
        quota = '본인 QOS 할당량'

    return (
        f'대기 중인 {pending}개 중 {len(blocked)}개는 GPU 부족이 아니라 '
        f'{quota} 때문입니다. 클러스터에는 GPU 가 {free_gpus}개 놀고 있습니다.'
    )


def _confidence(reason, start):
    """예상 시작 시각의 신뢰도.

    Slurm 이 시각을 못 내면 unknown. QOS 한도/의존성으로 막힌 job 은 같은 사용자
    대기 job 전부에 똑같은 시각이 찍혀 부정확하므로 low. 그 외에는 medium.
    """
    if start is None:
        return 'unknown'
    if reason in ('QOSMaxGRESPerUser', 'Dependency'):
        return 'low'
    return 'medium'


def estimate_wait_time(snapshot, job_id):
    """예상 시작 시각.

    1순위는 Slurm 이 계산한 값(`squeue --start`)이다. 직접 계산하지 않는다.
    다만 QOS 한도로 막힌 job 은 Slurm 도 정확히 모른다 — 같은 사용자의 대기 job 에
    전부 똑같은 시각이 찍힌다. 그래서 신뢰도를 같이 내려보낸다.
    """
    job = next((j for j in snapshot.jobs if j.job_id == str(job_id)), None)
    if job is None:
        return {'job_id': str(job_id), 'found': False}

    start = snapshot.start_times.get(job.job_id)
    reason = job.reason
    confidence = _confidence(reason, start)

    if start is None:
        note = 'Slurm 이 시작 시각을 추정하지 못했습니다.'
    elif reason == 'QOSMaxGRESPerUser':
        note = ('할당량으로 막힌 job 은 Slurm 추정이 부정확합니다. '
                '앞선 본인 job 이 끝나면 더 빨라집니다.')
    elif reason == 'Dependency':
        note = '선행 job 의 종료 시각에 따라 달라집니다.'
    else:
        note = 'Slurm 추정 기준입니다.'

    return {
        'job_id': job.job_id,
        'found': True,
        'state': job.state,
        'estimated_start': start,
        'confidence': confidence,   # 'medium' | 'low' | 'unknown'
        'source': 'squeue --start',
        'reason': reason,
        'note': note,
    }


def get_queue(snapshot, partition=None):
    """대기열 전체 뷰. "내 작업이 언제쯤 들어가고 어디쯤 있나" 에 답한다.

    실행 중 / 대기 중 job 을 나눠서 준다. 대기 job 에는 추정 순번(queue_position)과
    예상 시작 시각·신뢰도를 붙인다.

    순번은 Slurm 이 추정한 시작 시각 순이다. 진짜 우선순위 순위는 아니고
    (특히 QOS 한도로 막힌 job 은 순번이 큰 의미가 없다 -> blocked_by_quota 로 표시),
    "내 job 앞에 몇 개가 있나" 의 대략적인 감을 주는 값이다.
    """
    partition = _partition(snapshot, partition)
    me = snapshot.me
    in_part = [j for j in snapshot.jobs if j.partition == partition]
    running = [j for j in in_part if j.is_running]
    pending = [j for j in in_part if j.is_pending]

    def order_key(j):
        start = snapshot.start_times.get(j.job_id)
        # 시각이 있는 job 을 앞으로, 없는 job 은 뒤로. 동률은 job_id 로 안정 정렬.
        return (0, start) if start else (1, j.job_id)

    ordered = sorted(pending, key=order_key)
    rank = {j.job_id: i + 1 for i, j in enumerate(ordered)}

    def pending_row(job):
        start = snapshot.start_times.get(job.job_id)
        return {
            'job_id': job.job_id,
            'name': job.name,
            'user': job.user,
            'is_mine': job.user == me,
            'gpus': job.gpus,
            'high_perf_gpus': job.high_perf_gpus,
            'reason': job.reason,
            'reason_text': REASON_TEXT.get(job.reason, job.reason),
            'estimated_start': start,
            'confidence': _confidence(job.reason, start),
            'blocked_by_quota': job.reason == 'QOSMaxGRESPerUser',
            'queue_position': rank[job.job_id],
        }

    def running_row(job):
        return {
            'job_id': job.job_id,
            'name': job.name,
            'user': job.user,
            'is_mine': job.user == me,
            'gpus': job.gpus,
            'high_perf_gpus': job.high_perf_gpus,
            'nodes': job.nodes,
            'time_used': job.time_used,
        }

    my_positions = [rank[j.job_id] for j in ordered if j.user == me]
    return {
        'partition': partition,
        'pending_count': len(pending),
        'running_count': len(running),
        'my_pending_count': sum(1 for j in pending if j.user == me),
        'my_running_count': sum(1 for j in running if j.user == me),
        # 내 대기 job 중 가장 앞선 순번. 없으면 None.
        'my_next_position': min(my_positions) if my_positions else None,
        'pending': [pending_row(j) for j in ordered],
        'running': [running_row(j) for j in sorted(running, key=lambda x: x.job_id)],
    }


def _matches_path(path, prefixes):
    """경로가 금지/경고 접두어에 해당하는지. 디렉터리 경계를 지켜서 비교한다.

    '/home/' 규칙이 '/homework/data' 를 잡으면 안 된다.
    """
    normalized = path if path.endswith('/') else path + '/'
    for prefix in prefixes:
        prefix = prefix if prefix.endswith('/') else prefix + '/'
        if normalized.startswith(prefix):
            return prefix
    return None


def _lint_node_type(snapshot, node, high_perf):
    """지정한 노드의 종류가 GRES 타입과 맞는지.

    세라프는 `--gres=gpu:1 -w ariel-k1` 을 거절한다. 고성능 노드에는 high_perf
    타입을 써야 하고, 그 반대도 마찬가지다.
    """
    target = next((n for n in snapshot.nodes if n.name == node), None)
    if target is None:
        known = ', '.join(sorted(n.name for n in snapshot.nodes)[:5])
        return {
            'level': 'block',
            'code': 'UNKNOWN_NODE',
            'message': f"'{node}' 노드가 없습니다. (예: {known} ...)",
        }
    if target.is_high_perf and not high_perf:
        return {
            'level': 'block',
            'code': 'NODE_TYPE_MISMATCH',
            'message': (f'{node} 은 고성능 노드입니다. 일반 GPU(gpu:N)로는 쓸 수 '
                        f'없습니다. 고성능으로 요청하거나 v/g 노드를 고르세요.'),
        }
    if high_perf and not target.is_high_perf:
        return {
            'level': 'block',
            'code': 'NODE_TYPE_MISMATCH',
            'message': (f'{node} 은 일반 노드입니다. 고성능 GPU를 요청했으므로 '
                        f'm/k/n 노드를 고르거나 노드 지정을 빼세요.'),
        }
    if not target.schedulable:
        return {
            'level': 'block',
            'code': 'NODE_UNAVAILABLE',
            'message': f'{node} 은 지금 사용할 수 없는 상태입니다 ({target.state}).',
        }
    if target.usable_gpus == 0:
        # 제출은 되지만 그 노드가 빌 때까지 기다린다. 막지는 않는다.
        return {
            'level': 'warn',
            'code': 'NODE_BUSY',
            'message': (f'{node} 은 지금 여유 GPU 가 없습니다. 제출은 되지만 이 '
                        f'노드가 빌 때까지 기다립니다.'),
        }
    return None


def lint_job(snapshot, *, partition=None, gpus=1, high_perf=False, paths=(),
             time_limit=None, node=None):
    """job 을 내기 전에 막을 것들.

    지금은 도구를 통해 낼 때만 검사할 수 있다. 사용자가 터미널에서 직접
    sbatch 를 치면 우리가 개입할 수 없다. 이 도구는 감시자가 아니라
    "안전하게 제출하는 통로" 다.
    """
    cfg = snapshot.config
    partition = partition or cfg.default_partition
    problems = []
    limit = snapshot.my_qos

    if high_perf and limit and limit.max_high_perf_gpus == 0:
        problems.append({
            'level': 'block',
            'code': 'HIGH_PERF_FORBIDDEN',
            'message': (f"'{limit.name}' QOS 는 고성능 노드(m/k/n)를 쓸 수 없습니다. "
                        f'이대로 내면 영원히 대기합니다.'),
        })

    if high_perf and limit and limit.max_high_perf_gpus is not None \
            and 0 < limit.max_high_perf_gpus < gpus:
        problems.append({
            'level': 'block',
            'code': 'OVER_HIGH_PERF_LIMIT',
            'message': (f'고성능 GPU {gpus}개는 QOS 한도 '
                        f'{limit.max_high_perf_gpus}개를 넘습니다.'),
        })

    if limit and limit.max_gpus is not None and gpus > limit.max_gpus:
        problems.append({
            'level': 'block',
            'code': 'OVER_GPU_LIMIT',
            'message': (f'GPU {gpus}개는 QOS 한도 {limit.max_gpus}개를 넘습니다. '
                        f'이 job 은 절대 시작되지 않습니다.'),
        })

    if partition not in snapshot.partitions:
        known = ', '.join(sorted(snapshot.partitions)) or '(조회 실패)'
        problems.append({
            'level': 'block',
            'code': 'UNKNOWN_PARTITION',
            'message': f"'{partition}' 파티션이 없습니다. 사용 가능: {known}",
        })
    elif not can_use_partition(snapshot, partition):
        mine = snapshot.default_partition
        who = '학부생' if snapshot.is_undergrad else '대학원생'
        problems.append({
            'level': 'block',
            'code': 'PARTITION_NOT_ALLOWED',
            'message': (f"'{partition}' 파티션은 당신({who}) 계정으로 쓸 수 없습니다. "
                        f"'{mine}' 를 쓰세요. 이대로 내면 세라프가 거절합니다."),
        })

    if partition in snapshot.partitions and time_limit is not None:
        allowed = snapshot.partitions[partition].time_limit_seconds
        requested = parse_slurm_duration(time_limit)
        if allowed is not None and requested is not None and requested > allowed:
            problems.append({
                'level': 'block',
                'code': 'OVER_TIME_LIMIT',
                'message': (f"'{partition}' 파티션의 시간 제한은 "
                            f'{_fmt_duration(allowed)} 입니다 (요청: {time_limit}). '
                            f'Slurm 이 제출을 거절합니다.'),
            })

    if node:
        problem = _lint_node_type(snapshot, node, high_perf)
        if problem:
            problems.append(problem)
        # 학부생 노드 제한(ariel 한정). moana 등에선 파티션 멤버십이 제약이라 여기선 통과.
        allow = _node_allowlist(snapshot)
        if allow is not None and node not in allow:
            problems.append({
                'level': 'block',
                'code': 'UNDERGRAD_NODE_RESTRICTED',
                'message': (f'학부생은 {node} 를 쓸 수 없습니다. '
                            f"쓸 수 있는 노드: {', '.join(sorted(allow))}."),
            })

    # 서버가 강제하지 않는 "권장" 정책 경고 (차단 아님)
    problems.extend(_policy_warnings(snapshot, gpus, time_limit))

    for path in paths:
        blocked = _matches_path(path, cfg.blocked_paths)
        if blocked:
            problems.append({
                'level': 'block',
                'code': 'BLOCKED_PATH',
                'message': (f'{blocked} 는 직접 접근이 금지된 경로입니다: {path}. '
                            f'노드 로컬로 복사해서 쓰세요.'),
            })
            continue
        warned = _matches_path(path, cfg.warn_paths)
        if warned:
            problems.append({
                'level': 'warn',
                'code': 'DISCOURAGED_PATH',
                'message': f'{warned} 는 학습 데이터용으로 권장되지 않습니다: {path}',
            })

    if snapshot.load is not None and snapshot.load >= cfg.load_limit:
        problems.append({
            'level': 'warn',
            'code': 'LOGIN_NODE_BUSY',
            'message': f'로그인 노드 부하가 높습니다 (load {snapshot.load}).',
        })

    return {
        'ok': not any(p['level'] == 'block' for p in problems),
        'problems': problems,
    }


def _fmt_duration(seconds):
    if seconds is None:
        return '무제한'
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f'{hours}시간 {minutes}분'
    if hours:
        return f'{hours}시간'
    return f'{minutes}분'


def _policy_warnings(snapshot, gpus, time_limit):
    """세라프가 강제하지 않지만 가이드가 정한 "권장" 한도 경고.

    서버는 batch_grad walltime 을 무제한으로 두지만, 가이드는 최대 6일을 권장한다.
    GPU 개수도 기본값(학부 1/대학원 4)을 넘으면 "상향 신청" 이 필요하다.
    전부 warn 이다 — 실제로 제출은 되니 막지 않는다.
    """
    policy = snapshot.config.policy
    undergrad = snapshot.is_undergrad
    out = []

    max_days = policy.get('walltime_max_days')
    if max_days and time_limit is not None:
        requested = parse_slurm_duration(time_limit)
        if requested is not None and requested > max_days * 86400:
            out.append({
                'level': 'warn',
                'code': 'OVER_POLICY_WALLTIME',
                'message': (f'권장 최대 실행 시간은 {max_days}일입니다 '
                            f'(요청: {_fmt_duration(requested)}). 서버가 막지는 '
                            f'않지만 관리자 정책에 어긋납니다.'),
            })

    if undergrad is None:
        return out          # 신분 불명이면 GPU 정책 판단 보류

    key = 'undergrad' if undergrad else 'grad'
    default = policy.get(f'{key}_gpu_default')
    hard_max = policy.get(f'{key}_gpu_max')
    who = '학부생' if undergrad else '대학원생'
    if hard_max and gpus > hard_max:
        out.append({
            'level': 'warn',
            'code': 'OVER_POLICY_GPU_MAX',
            'message': (f'{who} 권장 최대 GPU 는 {hard_max}개입니다 (요청: {gpus}). '
                        f'상향 신청이 필요할 수 있습니다.'),
        })
    elif default and gpus > default:
        out.append({
            'level': 'warn',
            'code': 'OVER_POLICY_GPU_DEFAULT',
            'message': (f'{who} 기본 GPU 한도는 {default}개입니다 (요청: {gpus}). '
                        f'상향 신청 시 최대 {hard_max}개까지 가능합니다.'),
        })
    return out


def should_poll(snapshot):
    """로그인 노드가 바쁘면 폴링을 쉰다."""
    return snapshot.load is None or snapshot.load < snapshot.config.load_limit
