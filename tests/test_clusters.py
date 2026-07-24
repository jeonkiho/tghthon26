"""파티션 접근·학부생 제한·클러스터 라우팅 테스트.

근거: KHU GPU Cluster User Guide + 실서버 확인.
- 세라프는 3개 클러스터(ariel/moana/aurora). 이 도구는 ariel 만 접속.
- 대학원생 → *_grad, 학부생 → *_ugrad (서버가 계정으로 강제. --test-only 로 확인함).
- 학부생은 ariel-v[6-12] 만 사용.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from seraph import clusters, services
from seraph.connection import MockConnection


@pytest.fixture
def grad():
    """대학원생 스냅샷 (mock 기본: account=grad)."""
    return MockConnection().snapshot()


@pytest.fixture
def undergrad():
    """학부생으로 바꾼 스냅샷."""
    snap = MockConnection().snapshot()
    snap.account = 'ugrad'
    snap.my_qos_name = 'ugrad'      # gpu=1, high_perf=0
    return snap


# --- 신분 판별 ----------------------------------------------------------------

def test_grad_identity(grad):
    assert grad.account == 'grad'
    assert grad.is_undergrad is False
    assert grad.default_partition == 'batch_grad'


def test_undergrad_identity(undergrad):
    assert undergrad.is_undergrad is True
    assert undergrad.default_partition == 'batch_ugrad'


def test_unknown_account_is_neither():
    snap = MockConnection().snapshot()
    snap.account = None
    assert snap.is_undergrad is None       # 판단 보류


# --- 파티션 접근 --------------------------------------------------------------

def test_grad_can_use_grad_partitions(grad):
    p = services.get_partitions(grad)
    assert p['batch_grad']['can_use'] is True
    assert p['debug_grad']['can_use'] is True
    assert p['batch_ugrad']['can_use'] is False
    assert p['admin']['can_use'] is False


def test_undergrad_can_use_ugrad_partitions(undergrad):
    p = services.get_partitions(undergrad)
    assert p['batch_ugrad']['can_use'] is True
    assert p['batch_grad']['can_use'] is False
    assert p['admin']['can_use'] is False


def test_unknown_account_not_blocked():
    """신분을 모르면 막지 않는다 (판단 보류)."""
    snap = MockConnection().snapshot()
    snap.account = None
    assert services.can_use_partition(snap, 'batch_grad') is True


# --- lint: 파티션/노드 접근 ---------------------------------------------------

def test_lint_blocks_grad_partition_for_undergrad(undergrad):
    """실서버 확인: Invalid account/partition combination 으로 거절됨."""
    r = services.lint_job(undergrad, partition='batch_grad', gpus=1)
    assert not r['ok']
    assert any(p['code'] == 'PARTITION_NOT_ALLOWED' for p in r['problems'])


def test_lint_blocks_ugrad_partition_for_grad(grad):
    r = services.lint_job(grad, partition='batch_ugrad', gpus=1, node='ariel-v6')
    assert any(p['code'] == 'PARTITION_NOT_ALLOWED' for p in r['problems'])


def test_lint_blocks_restricted_node_for_undergrad(undergrad):
    r = services.lint_job(undergrad, partition='batch_ugrad', gpus=1,
                          node='ariel-g5')
    assert not r['ok']
    assert any(p['code'] == 'UNDERGRAD_NODE_RESTRICTED' for p in r['problems'])


def test_lint_allows_permitted_node_for_undergrad(undergrad):
    r = services.lint_job(undergrad, partition='batch_ugrad', gpus=1,
                          node='ariel-v6')
    assert not any(p['code'] == 'UNDERGRAD_NODE_RESTRICTED' for p in r['problems'])


def test_node_recommendations_limited_for_undergrad(undergrad):
    allowed = set(undergrad.config.undergrad_nodes)
    nodes = services.get_node_availability(undergrad)
    assert nodes                                   # 뭔가는 나와야
    assert all(n['name'] in allowed for n in nodes)


# --- 정책 경고 (차단 아님) ----------------------------------------------------

def test_policy_warns_over_walltime(grad):
    r = services.lint_job(grad, partition='batch_grad', gpus=1,
                          time_limit='7-00:00:00')     # 7일 > 권장 6일
    assert r['ok']                                     # 막지는 않음
    assert any(p['code'] == 'OVER_POLICY_WALLTIME' for p in r['problems'])


def test_policy_warns_over_gpu_default_for_grad(grad):
    r = services.lint_job(grad, partition='batch_grad', gpus=8, node='ariel-v1')
    codes = {p['code'] for p in r['problems']}
    assert 'OVER_POLICY_GPU_DEFAULT' in codes       # 대학원 기본 4 초과


def test_policy_warns_over_gpu_max(grad):
    r = services.lint_job(grad, partition='batch_grad', gpus=99)
    assert any(p['code'] == 'OVER_POLICY_GPU_MAX' for p in r['problems'])


# --- 클러스터 라우팅 ----------------------------------------------------------

@pytest.mark.parametrize('account, major, cluster', [
    ('grad', None, 'ariel'),          # 대학원생은 학과 무관 ariel
    ('grad_ce', 'ce', 'ariel'),       # CE 대학원생도 ariel
    ('ugrad', 'ai', 'ariel'),         # ariel 의 학부 = AI
    ('ugrad_ce', 'ce', 'moana'),      # CE 학부 → moana
    ('ugrad_eebme', 'ee', 'moana'),   # EE/BME 학부 → moana
])
def test_cluster_routing(account, major, cluster):
    assert clusters.major_from_account(account) == major
    pos = clusters.position_from_account(account)
    assert clusters.cluster_for(major, pos) == cluster


def test_belongs_here_grad_is_on_primary():
    info = clusters.belongs_here('grad')
    assert info['on_primary'] is True
    assert info['advice'] == ''


def test_belongs_here_ce_undergrad_redirected():
    info = clusters.belongs_here('ugrad_ce')
    assert info['cluster'] == 'moana'
    assert info['on_primary'] is False
    assert 'moana' in info['advice']


def test_whoami_grad_on_primary(grad):
    w = services.whoami(grad)
    assert w['on_primary'] is True
    assert w['cluster'] == 'ariel'
    assert w['cluster_notice'] == ''


def test_whoami_ce_undergrad_gets_redirect_notice(grad):
    grad.account = 'ugrad_ce'
    w = services.whoami(grad)
    assert w['cluster'] == 'moana'
    assert not w['on_primary']
    assert 'moana' in w['cluster_notice']


# --- 계정 설명 기반 라우팅 (서버가 알려주는 사실) --------------------------------

def test_description_beats_suffix_guess():
    """계정 설명에 클러스터가 적혀 있으면 접미어 추측보다 우선한다."""
    desc = 'advisor managed moana ugrad gpu'
    assert clusters.cluster_from_description(desc) == 'moana'
    info = clusters.belongs_here('ugrad_advisor_x', desc)
    assert info['cluster'] == 'moana'
    assert info['on_primary'] is False


def test_advisor_account_unroutable_without_description():
    """설명이 없으면 advisor_x 는 학과를 못 알아낸다 (그래서 설명이 필요했다)."""
    assert clusters.major_from_account('ugrad_advisor_x') is None
    assert clusters.belongs_here('ugrad_advisor_x')['cluster'] is None


def test_description_without_cluster_name_is_ignored():
    assert clusters.cluster_from_description('grad') is None
    assert clusters.cluster_from_description('') is None
    assert clusters.cluster_from_description(None) is None


def test_every_real_account_routes():
    """실서버에 존재하는 계정 9개가 전부 라우팅되어야 한다 (판단 실패 없음)."""
    from seraph.parsers import parse_accounts
    path = pathlib.Path(__file__).parent / 'fixtures' / 'accounts.txt'
    accounts = parse_accounts(path.read_text())
    assert len(accounts) >= 9
    for account, description in accounts.items():
        info = clusters.belongs_here(account, description)
        assert info['cluster'] is not None, f'{account} 라우팅 실패'


def test_snapshot_exposes_account_description(grad):
    assert grad.accounts                        # 계정 설명 맵
    assert grad.account_description is not None  # 내 계정(grad)의 설명


def test_whoami_includes_description(grad):
    w = services.whoami(grad)
    assert 'account_description' in w


def test_overview_lists_three_clusters():
    o = clusters.overview()
    assert set(o['clusters']) == {'ariel', 'moana', 'aurora'}
    assert o['primary'] == 'ariel'


def test_overview_has_no_connectable_flag():
    """어느 클러스터가 실시간인지는 정적 표가 아니라 '지금 접속한 곳'이 정한다.

    예전 connectable 플래그는 ariel 계정만 있던 시절의 값이라, moana 에 붙어 있어도
    ariel 이 '실시간'으로 표시되는 모순을 만들었다. whoami().connected_cluster 를 쓴다.
    """
    o = clusters.overview()
    for name, c in o['clusters'].items():
        assert 'connectable' not in c, f'{name} 에 connectable 이 남아 있다'


def test_moana_topology_matches_real_server():
    """실서버에서 확인한 moana(2026-07). 가이드의 121/u[1-8]/y[1-7] 은 낡은 값이었다."""
    moana = clusters.CLUSTERS['moana']
    assert moana.total_gpus == 105
    assert moana.nodelist == 'moana-r[1-5], u[1-4,6,8], y[1,3-7]'
