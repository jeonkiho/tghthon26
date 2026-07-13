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

# 계정(account) 접미어 -> 학과. sacctmgr 의 account 에서 뽑는다.
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


def belongs_here(account):
    """이 계정이 우리가 접속하는 클러스터(ariel) 소속인가.

    반환: {'cluster', 'connectable', 'on_primary', 'advice'}
    ariel 이 아니면 어디로 가야 하는지 안내를 담는다.
    """
    position = position_from_account(account)
    major = major_from_account(account)
    target = cluster_for(major, position) if position else None

    # 학과를 못 알아냈지만 ariel 에 접속했고 대학원생이면 ariel 이 맞다.
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


def overview():
    """3개 클러스터 전체 그림. 튜토리얼/안내용."""
    return {
        'primary': PRIMARY,
        'note': '이 도구는 ariel 만 실시간 조회합니다. 나머지는 안내만 제공합니다.',
        'clusters': {name: c.to_dict() for name, c in CLUSTERS.items()},
    }
