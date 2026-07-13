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

def test_debug_excluded_for_long_jobs(snap):
    """debug_* 는 4시간 제한. 12시간 학습은 아예 못 낸다."""
    short = placement.candidate_partitions(snap, hours=2)
    long = placement.candidate_partitions(snap, hours=12)
    assert 'debug_grad' in short
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

def test_finds_immediate_start(conn, snap):
    """mock: debug 는 즉시, batch 는 3시간 뒤 -> debug 를 골라야 한다."""
    r = placement.find_fastest(conn, snap, gpus=1, hours=2)
    assert r['can_start_now'] is True
    assert r['best']['partition'] == 'debug_grad'
    assert r['best']['starts_now'] is True
    assert '지금 바로' in r['headline']


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


def test_headline_mentions_time_limit_when_starting_now(conn, snap):
    r = placement.find_fastest(conn, snap, gpus=1, hours=2)
    assert '4시간 제한' in r['headline']       # debug 의 함정을 알려준다


def test_best_includes_ready_script(conn, snap):
    """추천한 곳에 바로 낼 수 있는 스크립트가 함께 온다."""
    r = placement.find_fastest(conn, snap, gpus=1, hours=2)
    script = r['best']['script']
    assert script.startswith('#!/bin/bash')
    assert '#SBATCH --partition=debug_grad' in script
    assert '--gres=gpu:1' in script


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
