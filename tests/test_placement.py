"""어디에 올려야 제일 빨리 시작하나 (placement).

핵심: sinfo 로 여유 GPU 를 세는 것으로는 알 수 없다. Slurm 에게 직접 물어야 한다.

실서버에서 실제로 관측한 것:
  ariel-v6 = GPU 7개 여유, CPU 52개 여유
  그런데 batch_grad 에 내면 -> "3시간 10분 뒤에나 시작"
  같은 시각 debug_grad 에 내면 -> "지금 즉시"
따라서 "여유 GPU 있음 = 지금 가능" 은 틀린 판단이다.
"""

import pathlib
import sys
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from seraph import placement
from seraph.connection import MockConnection
from seraph.parsers.testonly import (
    parse_test_only, seconds_until, starts_within,
)


@pytest.fixture
def conn():
    return MockConnection()


@pytest.fixture
def snap(conn):
    return conn.snapshot()


# --- --test-only 응답 파싱 ----------------------------------------------------

def test_parse_accepted():
    text = ('sbatch: Job 368283 to start at 2026-07-13T20:20:00 using 2 '
            'processors on nodes ariel-v6 in partition batch_grad')
    p = parse_test_only(text)
    assert p.ok
    assert p.start == '2026-07-13T20:20:00'
    assert p.node == 'ariel-v6'
    assert p.partition == 'batch_grad'


@pytest.mark.parametrize('text, hint', [
    ("sbatch: error: SUBMISSION REJECTED: GPU type 'high_perf' is REQUIRED.",
     'v/g 노드'),
    ('allocation failure: Invalid account or account/partition combination',
     '계정으로 쓸 수 없습니다'),
    ('sbatch: error: QOSMaxGRESPerUser', '할당량'),
])
def test_parse_rejections(text, hint):
    p = parse_test_only(text)
    assert not p.ok
    assert p.start is None
    assert hint in p.reason


def test_parse_empty_is_rejected():
    p = parse_test_only('')
    assert not p.ok and p.reason


def test_starts_within_and_seconds_until():
    now = datetime(2026, 7, 13, 17, 0, 0)
    soon = parse_test_only(
        'Job 1 to start at 2026-07-13T17:00:30 on nodes n in partition p')
    later = parse_test_only(
        'Job 1 to start at 2026-07-13T20:10:00 on nodes n in partition p')
    assert starts_within(soon, now)               # 30초 뒤 = 사실상 지금
    assert not starts_within(later, now)
    assert seconds_until(soon, now) == 30
    assert seconds_until(later, now) == 3 * 3600 + 600


def test_seconds_until_rejected_is_none():
    assert seconds_until(parse_test_only('error'), datetime.now()) is None


# --- 후보 파티션 --------------------------------------------------------------

def test_debug_never_recommended_for_training(snap):
    """debug_* 는 디버깅용이다. "지금 바로 된다"는 이유로 학습을 몰면 안 된다.

    (config 의 placement.exclude_partitions)
    """
    assert 'debug_grad' not in placement.candidate_partitions(snap, hours=2)
    assert 'debug_grad' not in placement.candidate_partitions(snap, hours=12)
    assert 'batch_grad' in placement.candidate_partitions(snap, hours=2)


def test_debug_can_be_included_explicitly(snap):
    """빼는 건 정책이지 불가능이 아니다. 필요하면 볼 수 있어야 한다."""
    both = placement.candidate_partitions(snap, hours=2, include_excluded=True)
    assert 'debug_grad' in both


def test_time_limit_still_filters(snap):
    """debug 를 억지로 포함해도 12시간 학습은 4시간 제한에 걸려 빠진다."""
    long = placement.candidate_partitions(snap, hours=12, include_excluded=True)
    assert 'debug_grad' not in long
    assert 'batch_grad' in long


def test_only_usable_partitions_are_candidates(snap):
    """대학원생에게 학부 파티션을 추천하면 안 된다."""
    cands = placement.candidate_partitions(snap, hours=2)
    assert all(not c.endswith('_ugrad') for c in cands)
    assert 'admin' not in cands


def test_undergrad_gets_ugrad_partitions(conn):
    snap = conn.snapshot()
    snap.account = 'ugrad'
    snap.my_qos_name = 'ugrad'
    cands = placement.candidate_partitions(snap, hours=2)
    assert all(c.endswith('_ugrad') for c in cands)


# --- find_fastest -------------------------------------------------------------

def test_recommends_batch_not_debug(conn, snap):
    """debug 가 "지금 바로" 라도 학습은 batch 에 추천해야 한다."""
    r = placement.find_fastest(conn, snap, gpus=1, hours=2)
    assert r['best']['partition'] == 'batch_grad'
    assert all(o['partition'] != 'debug_grad' for o in r['options'])


def test_options_sorted_by_wait(conn, snap):
    r = placement.find_fastest(conn, snap, gpus=1, hours=2)
    waits = [o['wait_seconds'] for o in r['options']]
    assert waits == sorted(waits)


def test_long_job_cannot_start_now(conn, snap):
    """12시간 학습은 debug 를 못 쓰니 batch 에서 기다려야 한다."""
    r = placement.find_fastest(conn, snap, gpus=1, hours=12)
    assert r['can_start_now'] is False
    assert r['best']['partition'] == 'batch_grad'
    assert '가장 빨리' in r['headline']
    assert r['best']['wait_seconds'] > 0


def test_headline_gives_wait_time(conn, snap):
    """지금 안 되면 언제 되는지 알려준다 (올려두면 되니까)."""
    r = placement.find_fastest(conn, snap, gpus=1, hours=2)
    assert '가장 빨리' in r['headline']
    assert r['best']['wait_text'] in r['headline']


def test_best_includes_ready_script(conn, snap):
    """추천한 곳에 바로 낼 수 있는 스크립트가 함께 온다."""
    r = placement.find_fastest(conn, snap, gpus=1, hours=2)
    script = r['best']['script']
    assert script.startswith('#!/usr/bin/bash')
    assert '#SBATCH --partition=batch_grad' in script
    assert '--gres=gpu:1' in script


# --- 고성능 GPU: 신청자만 쓰는 자원이라 자동 추천 안 함 -------------------------

def test_high_perf_never_auto_recommended(conn, snap):
    """고성능 노드(m/k/n)는 따로 신청한 사람만 쓴다.

    QOS 90개 중 40개가 high_perf=0 으로 아예 금지(기본 grad/ugrad 포함).
    신청해서 받은 자원이니 도구가 임의로 몰아주면 안 된다.
    """
    r = placement.find_fastest(conn, snap, gpus=1, hours=2)
    assert all(not o['high_perf'] for o in r['options'])
    assert r['best']['high_perf'] is False


def test_explicit_high_perf_is_respected(conn, snap):
    """사용자가 직접 요청하면 (그리고 QOS 가 허용하면) 쓴다."""
    r = placement.find_fastest(conn, snap, gpus=1, hours=2, high_perf=True)
    assert all(o['high_perf'] for o in r['options'])
    assert r['best']['high_perf'] is True


def test_high_perf_request_without_entitlement_is_blocked(conn):
    """미신청자(high_perf=0)가 고성능을 요청하면 막고 이유를 알려준다."""
    snap = conn.snapshot()
    snap.my_qos_name = 'grad'           # high_perf = 0
    r = placement.find_fastest(conn, snap, gpus=1, hours=2, high_perf=True)
    assert r['best'] is None
    assert r['blocked']
    assert '고성능' in r['headline']


def test_node_is_chosen(conn, snap):
    r = placement.find_fastest(conn, snap, gpus=1, hours=2)
    assert r['best']['node']                    # 일반 GPU 는 노드 지정 필수


def test_explicit_node_is_respected(conn, snap):
    r = placement.find_fastest(conn, snap, gpus=1, hours=2, node='ariel-v6')
    assert all(o['node'] == 'ariel-v6' for o in r['options'])


def test_impossible_request_is_blocked(conn, snap):
    """QOS 한도를 넘는 요청은 Slurm 에 묻기 전에 lint 가 막는다."""
    r = placement.find_fastest(conn, snap, gpus=999, hours=2)
    assert r['can_start_now'] is False
    assert r['best'] is None
    assert r['blocked']
    assert '한도' in r['blocked'][0]['reason']


def test_requested_echoed_back(conn, snap):
    r = placement.find_fastest(conn, snap, gpus=2, hours=5, high_perf=True)
    assert r['requested'] == {'gpus': 2, 'hours': 5, 'high_perf': True}


def test_wait_text_is_human_readable():
    assert placement._fmt_wait(0) == '지금 바로'
    assert placement._fmt_wait(30) == '지금 바로'
    assert placement._fmt_wait(600) == '약 10분 뒤'
    assert placement._fmt_wait(3600) == '약 1시간 뒤'
    assert placement._fmt_wait(3 * 3600 + 600) == '약 3시간 10분 뒤'
    assert '일' in placement._fmt_wait(50 * 3600)
    assert placement._fmt_wait(None) == '알 수 없음'
