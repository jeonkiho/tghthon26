"""sacct 파싱과 끝난 job 진단 테스트.

fixture(tests/fixtures/sacct.txt)는 상태별로 2개씩 골라 뽑은 것이라 성공률이
실제와 다르다. 실제 30일 통계는 623개 중 216개 성공(약 35%)이었다.
성공률 자체를 검증하지 말고 계산이 맞는지만 본다.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from seraph import history
from seraph.connection import MockConnection
from seraph.parsers import parse_sacct, parse_memory_mb


FIXTURE = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'sacct.txt'


@pytest.fixture(scope='module')
def text():
    return FIXTURE.read_text()


@pytest.fixture(scope='module')
def jobs(text):
    return parse_sacct(text)


def _by_id(jobs, job_id):
    return next(j for j in jobs if j.job_id == job_id)


# --- 메모리 표기 ---------------------------------------------------------------

@pytest.mark.parametrize('raw, mb', [
    ('61144768K', 59711),      # sacct 기본 단위는 K
    ('5M', 5),
    ('64G', 65536),
    ('4Gn', 4096),             # 예전 Slurm 의 per-node 접미어
    ('4Gc', 4096),
    ('0', 0),
    ('', None),
    ('None', None),
    ('abc', None),
])
def test_parse_memory_mb(raw, mb):
    assert parse_memory_mb(raw) == mb


# --- 행 병합 ------------------------------------------------------------------

def test_steps_are_merged_into_one_job(jobs):
    """sacct 는 job 하나를 메인/.batch/.extern 여러 행으로 준다."""
    ids = [j.job_id for j in jobs]
    assert len(ids) == len(set(ids))       # 중복 없음
    assert all('.' not in i for i in ids)  # 스텝 행이 job 으로 새지 않음


def test_max_rss_comes_from_step_not_main_row(jobs):
    """MaxRSS 는 메인 행에 없고 .batch 스텝에만 있다. -X 를 쓰면 잃는다."""
    oom = _by_id(jobs, '358796')
    assert oom.req_mem_mb == 65536         # 메인 행에서
    assert oom.max_rss_mb == 59711         # .batch 스텝에서


def test_max_rss_from_srun_step(jobs):
    """srun 으로 낸 job 은 .batch 가 없고 .0 스텝을 가진다."""
    job = _by_id(jobs, '358283')
    assert job.max_rss_mb is not None


def test_extern_step_zero_does_not_win(jobs):
    """.extern 은 항상 0 이다. 스텝 중 최댓값을 써야 한다."""
    assert _by_id(jobs, '358825').max_rss_mb > 0


# --- 상태·종료코드 -------------------------------------------------------------

def test_cancelled_state_is_normalized(jobs):
    """'CANCELLED by 20301' 을 그대로 비교하면 어떤 분기에도 안 걸린다."""
    job = _by_id(jobs, '358792')
    assert job.state == 'CANCELLED'
    assert job.raw_state == 'CANCELLED by 20301'
    assert job.cancelled_by == '20301'


def test_oom_exit_code_is_zero(jobs):
    """OOM 은 종료 코드가 0 이고 시그널이 125 다. 코드만 보면 성공으로 오해한다."""
    oom = _by_id(jobs, '358796')
    assert (oom.exit_code, oom.signal) == (0, 125)
    assert not oom.succeeded


def test_command_not_found_exit_127(jobs):
    job = _by_id(jobs, '358283')
    assert job.state == 'FAILED' and job.exit_code == 127


def test_gpus_come_from_alloc_tres(jobs):
    oom = _by_id(jobs, '358825')
    assert oom.gpus == 4


def test_running_jobs_are_excluded():
    text = ('1|a|RUNNING|0:0|2026-01-01T00:00:00|Unknown|00:01:00|01:00:00|p|n|'
            'cpu=1,gres/gpu=1|4G|\n'
            '2|b|COMPLETED|0:0|2026-01-01T00:00:00|2026-01-01T00:01:00|00:01:00|'
            '01:00:00|p|n|cpu=1,gres/gpu=1|4G|\n')
    assert [j.job_id for j in parse_sacct(text)] == ['2']


def test_cancelled_before_start_has_no_node():
    text = ('7|bash|CANCELLED by 20301|0:0|None|2026-06-26T20:51:21|00:00:00|'
            '02:00:00|debug_grad|None assigned||32G|\n')
    (job,) = parse_sacct(text)
    assert job.start is None and job.nodes == ''
    assert job.elapsed_seconds == 0


def test_broken_line_is_skipped():
    assert parse_sacct('쓰레기\n\n||\n') == []


def test_sorted_newest_first(jobs):
    ids = [int(j.job_id) for j in jobs]
    assert ids == sorted(ids, reverse=True)


# --- 진단 ---------------------------------------------------------------------

def test_diagnose_oom_mentions_memory_and_zero_exit_code(jobs):
    d = history.diagnose_job(_by_id(jobs, '358796'))
    assert d['reason'] == 'out_of_memory'
    assert '64.0GB' in d['advice'] and '58.3GB' in d['advice']
    assert '종료 코드는 0' in d['advice']    # 오해를 미리 짚어준다


def test_diagnose_timeout_shows_overrun(jobs):
    d = history.diagnose_job(_by_id(jobs, '359153'))
    assert d['reason'] == 'timeout'
    assert '5초' in d['advice']             # 7205 - 7200


def test_diagnose_command_not_found(jobs):
    d = history.diagnose_job(_by_id(jobs, '358283'))
    assert d['reason'] == 'command_not_found'
    assert 'conda' in d['advice']


def test_diagnose_cancelled_by_user_vs_admin(jobs):
    user = history.diagnose_job(_by_id(jobs, '358792'))
    assert user['reason'] == 'cancelled_by_user'
    assert '20301' in user['advice']

    admin = _by_id(jobs, '358792')
    admin.cancelled_by = '0'               # 시스템이 취소한 경우
    assert history.diagnose_job(admin)['reason'] == 'cancelled_by_admin'
    admin.cancelled_by = '20301'           # 원복 (module scope fixture)


def test_diagnose_completed_has_no_advice(jobs):
    d = history.diagnose_job(_by_id(jobs, '358284'))
    assert d['reason'] == 'ok' and d['advice'] == ''


def test_diagnose_signal_failure(jobs):
    d = history.diagnose_job(_by_id(jobs, '358643'))
    assert d['reason'] == 'killed_by_signal'
    assert 'slurm-358643.out' in d['advice']


# --- 집계 ---------------------------------------------------------------------

def test_summarize_counts_and_gpu_hours(jobs):
    s = history.summarize(jobs)
    assert s['total'] == len(jobs)
    assert s['succeeded'] + s['failed'] == s['total']
    assert 0 <= s['success_rate'] <= 1
    assert s['wasted_gpu_hours'] <= s['total_gpu_hours']
    assert s['by_state']['OUT_OF_MEMORY'] == 2


def test_summarize_empty():
    s = history.summarize([])
    assert s['total'] == 0 and s['success_rate'] == 0.0


def test_gpu_hours_multiply_by_gpu_count():
    text = ('1|a|FAILED|1:0|2026-01-01T00:00:00|2026-01-01T01:00:00|01:00:00|'
            '02:00:00|p|n1|cpu=8,gres/gpu=4|4G|\n')
    s = history.summarize(parse_sacct(text))
    assert s['wasted_gpu_hours'] == 4.0     # 1시간 x GPU 4개


def test_headline_reports_wasted_hours(jobs):
    h = history.get_job_history(FIXTURE.read_text())
    assert '성공' in h['headline']
    assert 'GPU' in h['headline']


# --- 진입점 -------------------------------------------------------------------

def test_get_job_history_limit(text):
    h = history.get_job_history(text, limit=3)
    assert len(h['jobs']) == 3
    assert h['stats']['total'] == 10       # 통계는 전체 기준
    assert all('advice' in j for j in h['jobs'])


def test_get_job_result_found_and_missing(text):
    found = history.get_job_result(text, '358796')
    assert found['found'] and found['reason'] == 'out_of_memory'
    assert history.get_job_result(text, '999999') == {'job_id': '999999',
                                                      'found': False}


def test_mock_connection_provides_sacct():
    conn = MockConnection()
    h = history.get_job_history(conn.sacct(days=7))
    assert h['stats']['total'] > 0
