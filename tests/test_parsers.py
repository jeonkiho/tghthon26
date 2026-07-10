"""파서 테스트. 실제 세라프 출력에서 관측된 모양만 검사한다.

    python -m pytest tests/ -q
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from seraph.parsers import (
    parse_gres, gpus_from_tres, parse_squeue, parse_squeue_start,
    parse_sinfo, parse_qos, parse_assoc, parse_uptime,
)
from seraph.connection import MockConnection
from seraph import services


@pytest.fixture(scope='module')
def snap():
    return MockConnection().snapshot()


# --- GRES 표기 ---------------------------------------------------------------

@pytest.mark.parametrize('spec, expected', [
    ('gpu:8(S:0)',                    {None: 8}),          # 소켓 표기 붙음
    ('gpu:high_perf:8',               {'high_perf': 8}),
    ('gpu:(null):2(IDX:0-1)',         {None: 2}),          # 타입 자리에 (null)
    ('gpu:(null):0(IDX:N/A)',         {None: 0}),          # 사용 0 이면 IDX 가 N/A
    ('gpu:high_perf:7(IDX:0-1,3-7)',  {'high_perf': 7}),   # IDX 안에 쉼표
    ('gpu:broken:1,gpu:high_perf:7',  {'broken': 1, 'high_perf': 7}),
    ('', {}),
    ('(null)', {}),
])
def test_parse_gres(spec, expected):
    assert parse_gres(spec) == expected


def test_gpus_from_tres_separates_high_perf():
    # gres/gpu 는 총 개수이고 gres/gpu:high_perf 는 그중 고성능이다. 더하면 안 된다.
    total, hp = gpus_from_tres('cpu=8,mem=48G,gres/gpu=1,gres/gpu:high_perf=1')
    assert (total, hp) == (1, 1)

    total, hp = gpus_from_tres('cpu=1,mem=16G,node=1,billing=1,gres/gpu=4')
    assert (total, hp) == (4, 0)

    # --gpus 로 낸 job 은 gres 항목이 아예 없을 수 있다.
    assert gpus_from_tres('cpu=1,mem=16G') == (0, 0)
    assert gpus_from_tres('') == (0, 0)


# --- squeue ------------------------------------------------------------------

def test_parse_squeue_basic():
    line = ('366126|batch_grad|user25|PD|0:00|qos_user25_2026_1|'
            'cpu=8,mem=48G,gres/gpu=1,gres/gpu:high_perf=1||QOSMaxGRESPerUser|'
            'base_soup|')
    (job,) = parse_squeue(line)
    assert job.job_id == '366126'
    assert job.user == 'user25'
    assert job.is_pending and not job.is_running
    assert (job.gpus, job.high_perf_gpus) == (1, 1)
    assert job.reason == 'QOSMaxGRESPerUser'   # 괄호 제거
    assert job.name == 'base_soup'
    assert job.nodes == ''                     # 대기 중이면 노드 없음


def test_parse_squeue_skips_broken_lines():
    assert parse_squeue('쓰레기\n\n|||\n') == []


def test_parse_squeue_start_drops_na():
    text = '1|2026-07-10T21:18:28|(Priority)\n2|N/A|(Dependency)\n'
    assert parse_squeue_start(text) == {'1': '2026-07-10T21:18:28'}


# --- sinfo -------------------------------------------------------------------

def test_parse_sinfo_dedupes_nodes():
    """-N 은 노드를 파티션 수만큼 반복해서 낸다. GPU 를 3배로 세면 안 된다."""
    text = (
        'ariel-g1|admin|allocated|gpu:8(S:0)|gpu:(null):8(IDX:0-7)|64/0/0/64|1|2|\n'
        'ariel-g1|debug_grad|allocated|gpu:8(S:0)|gpu:(null):8(IDX:0-7)|64/0/0/64|1|2|\n'
        'ariel-g1|batch_grad|allocated|gpu:8(S:0)|gpu:(null):8(IDX:0-7)|64/0/0/64|1|2|\n'
    )
    (node,) = parse_sinfo(text)
    assert node.total_gpus == 8
    assert sorted(node.partitions) == ['admin', 'batch_grad', 'debug_grad']


def test_cpu_starved_node_has_no_usable_gpu():
    """GPU 는 남았지만 idle CPU 가 0 이면 아무도 그 GPU 를 못 쓴다 (ariel-v3)."""
    text = ('ariel-v3|batch_grad|allocated|gpu:8(S:0)|gpu:(null):7(IDX:0-6)|'
            '64/0/0/64|203776|611431|\n')
    (node,) = parse_sinfo(text)
    assert node.free_gpus == 1      # 숫자상으로는 1개 비었지만
    assert node.usable_gpus == 0    # 실제로는 못 쓴다
    assert node.cpu_starved


def test_drained_node_offers_nothing():
    """ariel-m1: broken GPU 1개 + drained."""
    text = ('ariel-m1|batch_grad|drained|gpu:broken:1,gpu:high_perf:7|'
            'gpu:broken:0(IDX:N/A),gpu:high_perf:0(IDX:N/A)|0/0/144/144|0|489868|\n')
    (node,) = parse_sinfo(text)
    assert node.broken_gpus == 1
    assert node.total_gpus == 7     # broken 은 총량에서 뺀다
    assert node.free_gpus == 0      # drained 면 0
    assert not node.schedulable


# --- QOS ---------------------------------------------------------------------

def test_parse_qos_limits():
    text = ('grad|gres/gpu:high_perf=0,gres/gpu=4|10|20\n'
            'qos_user01_2026_1|gres/gpu:high_perf=8,gres/gpu=12|12|24\n'
            'normal|cpu=1000,mem=10000G|1000|1000\n')
    q = parse_qos(text)
    assert q['grad'].max_gpus == 4
    assert q['grad'].max_high_perf_gpus == 0     # 고성능 노드 사용 금지
    assert q['qos_user01_2026_1'].max_gpus == 12
    assert q['normal'].max_gpus is None          # GPU 한도 없음


def test_parse_assoc_and_uptime():
    assert parse_assoc('user01|qos_user01_2026_1\n') == ('user01', 'qos_user01_2026_1')
    assert parse_assoc('') == (None, None)
    assert parse_uptime(' 13:24:35 up 5 days, 32 users,  load average: 4.61, 3.64, 3.10') == 4.61
    assert parse_uptime('nonsense') is None


# --- services (fixture 전체) --------------------------------------------------

def test_snapshot_loads(snap):
    assert snap.jobs and snap.nodes and snap.qos_limits
    assert snap.me == 'user01'


def test_gpu_status_consistency(snap):
    s = services.get_gpu_status(snap)
    assert s['used_gpus'] + s['free_gpus'] + s['idle_but_unusable_gpus'] == s['total_gpus']
    assert s['free_high_perf_gpus'] + s['free_standard_gpus'] == s['free_gpus']
    assert 0 <= s['utilization'] <= 1


def test_high_perf_and_standard_gpus_are_not_interchangeable(snap):
    s = services.get_gpu_status(snap)
    assert s['free_high_perf_gpus'] <= s['total_high_perf_gpus']


def test_diagnose_names_the_binding_quota(snap):
    """총 GPU 는 여유가 있는데 고성능 한도에 걸린 경우를 구분해야 한다."""
    d = services.diagnose_pending(snap, 'user25')
    if d['quota_blocked_count'] == 0:
        pytest.skip('이 fixture 에는 쿼터로 막힌 job 이 없다')
    blocked = [j for j in d['jobs'] if j['blocked_by_quota']]
    assert all(j['quota_kind'] for j in blocked)
    # 고성능 한도에 걸렸다면 총 GPU 한도를 원인으로 지목하면 안 된다.
    hp = [j for j in blocked if j['quota_kind'] == 'high_perf_gpu']
    if hp:
        assert d['usage']['high_perf_in_use'] >= d['usage']['high_perf_limit']
        assert '고성능' in d['headline']


def test_diagnose_uses_the_right_users_qos(snap):
    """QOS 는 사람마다 다르다. 남의 job 에 내 한도를 쓰면 안 된다."""
    other = services.get_my_usage(snap, 'user06')
    assert other['qos'] != snap.my_qos_name
    assert other['gpus_limit'] == 4      # grad QOS


def test_node_recommendations_exclude_unusable(snap):
    nodes = services.get_node_availability(snap, need_gpus=1)
    names = {n['name'] for n in nodes}
    for n in snap.nodes:
        if n.cpu_starved or not n.schedulable:
            assert n.name not in names
    assert all(n['usable_gpus'] >= 1 for n in nodes)


def test_lint_blocks_forbidden_high_perf():
    """grad QOS 는 고성능 노드를 못 쓴다. 내면 영원히 대기한다."""
    snap = MockConnection().snapshot()
    snap.my_qos_name = 'grad'          # grad 사용자인 척
    r = services.lint_job(snap, gpus=1, high_perf=True)
    assert not r['ok']
    assert any(p['code'] == 'HIGH_PERF_FORBIDDEN' for p in r['problems'])


def test_lint_blocks_nas_and_over_limit(snap):
    # 실제 마운트는 /nas2 다. /nas 라는 경로는 세라프에 존재하지 않는다.
    r = services.lint_job(snap, gpus=999, paths=['/nas2/data/x'])
    codes = {p['code'] for p in r['problems']}
    assert {'OVER_GPU_LIMIT', 'BLOCKED_PATH'} <= codes
    assert not r['ok']


def test_lint_passes_clean_job(snap):
    r = services.lint_job(snap, gpus=1, paths=['/local/imagenet'])
    assert r['ok']


def test_estimate_wait_time_flags_low_confidence(snap):
    quota = next((j for j in snap.jobs
                  if j.is_pending and j.reason == 'QOSMaxGRESPerUser'), None)
    if quota is None:
        pytest.skip('쿼터로 막힌 job 이 없다')
    e = services.estimate_wait_time(snap, quota.job_id)
    # Slurm 이 시각을 줘도 쿼터로 막힌 job 은 믿으면 안 된다.
    assert e['confidence'] in ('low', 'unknown')


def test_estimate_wait_time_unknown_job(snap):
    assert services.estimate_wait_time(snap, '999999')['found'] is False
