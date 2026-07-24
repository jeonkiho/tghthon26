"""세라프 = 3개 클러스터. 이 도구는 그중 ariel 만 실제로 접속한다.

출처: SERAPH KHU GPU Cluster User Guide (Notion). 서버 접속 없이도 알 수 있는
"규칙 데이터" 라서 여기 코드로 박아둔다. moana/aurora 는 접속 계정이 없어
실시간 데이터를 못 가져오지만, "당신은 저 서버를 써야 한다" 는 안내는 할 수 있다.

주의: 이 표는 학기마다 바뀔 수 있다. 노드/정책이 달라지면 가이드를 다시 확인할 것.
Major×Position -> 어느 클러스터를 쓰는지가 핵심이다.
"""

from dataclasses import dataclass, field, asdict


@dataclass
class Cluster:
    name: str
    host: str
    total_gpus: int
    nodelist: str
    allowed: str            # 사람이 읽는 설명
    connectable: bool       # 이 도구가 실제로 접속하는가

    def to_dict(self):
        return asdict(self)


CLUSTERS = {
    'ariel': Cluster(
        name='ariel', host='ariel.khu.ac.kr', total_gpus=182,
        nodelist='ariel-v[1-13], g[1-5], k[1-2], m[1-2], n1',
        allowed='AI 학부생 + 모든 대학원생',
        connectable=True,
    ),
    'moana': Cluster(
        name='moana', host='moana.khu.ac.kr', total_gpus=121,
        nodelist='moana-y[1-7], r[1-5], u[1-8]',
        allowed='EE/BME/CE 학부생',
        connectable=False,
    ),
    'aurora': Cluster(
        name='aurora', host='aurora.khu.ac.kr', total_gpus=62,
        nodelist='aurora-g[1-8]',
        allowed='SWCON 학부생',
        connectable=False,
    ),
}

# 이 도구가 접속하는 클러스터
PRIMARY = 'ariel'

# Major × Position -> 클러스터.
# Position: undergrad | grad(석/박/교수 전부). Major: swcon | ai | ce | ee | bme.
# 대학원생은 학과와 무관하게 전부 ariel.
_ROUTING = {
    ('swcon', 'undergrad'): 'aurora',
    ('ai', 'undergrad'): 'ariel',
    ('ce', 'undergrad'): 'moana',
    ('ee', 'undergrad'): 'moana',
    ('bme', 'undergrad'): 'moana',
}

# 계정(account) 접미어 -> 학과. 이건 "추측"이다. 계정 설명에 클러스터가 적혀
# 있으면 그쪽(cluster_from_description)이 우선한다.
#
# 실서버에서 확인한 계정 9개와 실제 job 기록(최근 30일):
#   grad(231명, 9934건) / ugrad(57명, 2932건)  <- ariel 에서 실제로 도는 건 이 둘뿐
#   ugrad_ce(243) / ugrad_eebme(53) / ugrad_advisor_x(254) <- ariel job 0건 (moana)
#   grad_ce(7) / grad_eebme(2) <- 거의 안 쓰임. 어차피 대학원생이라 ariel.
# 즉 회계 DB 는 3개 클러스터가 공유하지만, ariel 에서 도는 건 grad/ugrad 뿐이다.
_ACCOUNT_MAJOR = {
    'ce': 'ce',
    'eebme': 'ee',      # EE/BME 를 한 계정으로 묶는 경우. 어차피 둘 다 moana.
    'ee': 'ee',
    'bme': 'bme',
    'ai': 'ai',
    'swcon': 'swcon',
}


def cluster_for(major, position):
    """(학과, 신분) -> 클러스터 이름. 대학원생은 학과 무관 ariel."""
    if position == 'grad':
        return 'ariel'
    return _ROUTING.get((major, position))


def partition_from_account(account, kind='batch'):
    """계정 -> 실제 제출 파티션 이름.

    학부 파티션은 학과별로 나뉜다(서버가 계정으로 강제):
      ugrad_ce    -> batch_ce_ugrad      (moana, CE 학부)
      ugrad_eebme -> batch_eebme_ugrad   (moana, EE/BME 학부)
      ugrad       -> batch_ugrad         (ariel, AI 학부 · 접미어 없음)
      grad / 그 외 -> batch_grad          (대학원생, 학과 무관)
    kind='debug' 이면 debug_* 를 돌려준다.
    """
    if not account:
        return f'{kind}_grad'
    if account == 'ugrad':
        return f'{kind}_ugrad'
    if account.startswith('ugrad_'):
        return f'{kind}_{account[len("ugrad_"):]}_ugrad'
    return f'{kind}_grad'


def major_from_account(account):
    """계정 이름에서 학과를 추정한다. 못 하면 None.

    grad_ce -> ce, ugrad_eebme -> ee, 그냥 grad/ugrad -> None(학과 불명).
    ariel 의 평범한 ugrad 계정은 AI 학부생으로 본다(가이드 기준).
    """
    if not account:
        return None
    if '_' in account:
        suffix = account.split('_', 1)[1]
        return _ACCOUNT_MAJOR.get(suffix)
    # 접미어 없는 ugrad = ariel 의 AI 학부생
    if account == 'ugrad':
        return 'ai'
    return None


def position_from_account(account):
    """계정에서 학부/대학원 판별. ugrad* -> undergrad, grad*/그 외 -> grad."""
    if not account:
        return None
    return 'undergrad' if account.startswith('ugrad') else 'grad'


def infer_cluster(nodes):
    """실제 접속한 클러스터를 노드 이름 접두어로 추정한다.

    moana-y1 -> 'moana', ariel-v3 -> 'ariel'. 못 하면 None.
    이 도구가 ariel 외 클러스터(moana 등)에도 붙을 수 있게 되면서, "지금 어디에
    붙어 있나"를 PRIMARY 상수가 아니라 실데이터로 판단하기 위한 것.
    """
    for n in nodes or ():
        name = getattr(n, 'name', n) or ''
        head = str(name).split('-', 1)[0]
        if head in CLUSTERS:
            return head
    return None


def cluster_from_description(description):
    """계정 설명에 클러스터 이름이 적혀 있으면 그걸 쓴다 (가장 정확한 근거).

      'advisor managed moana ugrad gpu' -> 'moana'

    서버가 알려주는 사실이라 접미어 추측보다 우선한다. 없으면 None.
    """
    if not description:
        return None
    lowered = description.lower()
    for name in CLUSTERS:
        if name in lowered:
            return name
    return None


def belongs_here(account, description=None):
    """이 계정이 우리가 접속하는 클러스터(ariel) 소속인가.

    근거 우선순위:
      1. 계정 설명에 적힌 클러스터 이름 (서버가 알려주는 사실)
      2. 계정 접미어로 학과 추정 -> Major×Position 표
      3. 대학원생이면 학과 무관 ariel

    반환: {'cluster', 'connectable', 'on_primary', 'advice'}
    """
    position = position_from_account(account)
    major = major_from_account(account)

    # 1순위: 설명에 명시된 클러스터
    target = cluster_from_description(description)

    # 2순위: 학과 × 신분
    if target is None and position:
        target = cluster_for(major, position)

    # 3순위: 학과를 못 알아냈어도 대학원생이면 ariel
    if target is None and position == 'grad':
        target = 'ariel'

    on_primary = target == PRIMARY
    result = {
        'cluster': target,
        'connectable': bool(target and CLUSTERS[target].connectable),
        'on_primary': on_primary,
        'advice': '',
    }
    if target and not on_primary:
        c = CLUSTERS[target]
        result['advice'] = (
            f'당신({_kor(position, major)})은 이 도구가 보는 ariel 이 아니라 '
            f'{c.name}({c.host}) 클러스터를 사용합니다. '
            f'거기로 접속하세요. 이 도구는 아직 ariel 만 지원합니다.'
        )
    elif target is None:
        result['advice'] = ('소속 클러스터를 계정으로 판단하지 못했습니다. '
                            '학과 조교에게 확인하세요.')
    return result


_KOR_POS = {'undergrad': '학부생', 'grad': '대학원생'}
_KOR_MAJOR = {'ce': 'CE', 'ee': 'EE', 'bme': 'BME', 'ai': 'AI', 'swcon': 'SWCON'}


def _kor(position, major):
    parts = []
    if major:
        parts.append(_KOR_MAJOR.get(major, major))
    parts.append(_KOR_POS.get(position, position or '?'))
    return ' '.join(parts)


# 접속 화면용 표시 이름. 사용자에게 "호스트를 고르라"고 묻는 대신 학과·신분을 묻기 위한 것.
MAJOR_LABELS = {
    'ce': '컴퓨터공학과 (CE)',
    'ee': '전자공학과 (EE)',
    'bme': '생체의공학과 (BME)',
    'ai': '인공지능학과 (AI)',
    'swcon': '소프트웨어융합학과 (SWCON)',
}

# 교내/교외 SSH 포트. 가이드 기준(교내 22, 교외 30080).
SSH_PORTS = {'on_campus': 22, 'off_campus': 30080}


def routing_table():
    """학과 × 신분 -> 클러스터. 화면이 이 정책을 다시 구현하지 않도록 서버가 알려준다.

    접속 화면은 사용자에게 ariel/moana 중 무엇을 쓸지 묻지 않는다 — 그건 학과와
    신분으로 이미 결정되는 값이다. 정책이 바뀌면 _ROUTING 만 고치면 화면도 따라온다.
    (프론트에 표를 복사해두면 반드시 어긋난다.)
    """
    assign = {}
    for major in MAJOR_LABELS:
        for position in _KOR_POS:
            target = cluster_for(major, position)
            if target:
                assign[f'{major}:{position}'] = target
    return {
        'majors': [{'key': k, 'label': v} for k, v in MAJOR_LABELS.items()],
        'positions': [{'key': k, 'label': v} for k, v in _KOR_POS.items()],
        'assign': assign,
        'ssh_ports': dict(SSH_PORTS),
    }


def overview():
    """3개 클러스터 전체 그림. 튜토리얼/안내용."""
    return {
        'primary': PRIMARY,
        'note': '이 도구는 ariel 만 실시간 조회합니다. 나머지는 안내만 제공합니다.',
        'clusters': {name: c.to_dict() for name, c in CLUSTERS.items()},
        'routing': routing_table(),
    }
