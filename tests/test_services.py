"""config / lint / sbatch / notify / tutorial 테스트."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from seraph import config as config_module
from seraph import clusters, notify, sbatch, services, tutorial
from seraph.connection import MockConnection
from seraph.parsers import parse_partitions, parse_slurm_duration


@pytest.fixture
def snap():
    return MockConnection().snapshot()


# --- config ------------------------------------------------------------------

def test_config_falls_back_to_defaults_when_missing(tmp_path):
    cfg = config_module.load(tmp_path / 'nonexistent.yaml')
    assert cfg.mode == 'mock'
    assert cfg.default_partition == 'batch_grad'


def test_config_file_overrides_defaults(tmp_path):
    path = tmp_path / 'c.yaml'
    path.write_text('connection:\n  mode: ssh\n  host: other\n', encoding='utf-8')
    cfg = config_module.load(path)
    assert cfg.mode == 'ssh' and cfg.host == 'other'
    # 안 적은 키는 기본값이 남아 있어야 한다
    assert cfg.default_partition == 'batch_grad'
    assert cfg.load_limit == 8.0


def test_env_var_beats_config_file_for_secret(tmp_path, monkeypatch):
    path = tmp_path / 'c.yaml'
    path.write_text('notify:\n  slack_webhook_url: https://from-file\n', encoding='utf-8')
    monkeypatch.setenv('SERAPH_SLACK_WEBHOOK', 'https://from-env')
    assert config_module.load(path).slack_webhook == 'https://from-env'


def test_real_config_yaml_loads():
    cfg = config_module.load()
    assert '/data/' in cfg.blocked_paths
    assert cfg.data_root == '/nas2/data'
    assert cfg.local_datasets_root == '/local_datasets'


# --- 파티션 ------------------------------------------------------------------

@pytest.mark.parametrize('raw, seconds', [
    ('infinite', None),
    ('4:00:00', 4 * 3600),
    ('1-12:00:00', 36 * 3600),
    ('30:00', 30 * 60),        # 분:초
    ('10', 10 * 60),           # 분
    ('2-00:00:00', 2 * 86400),
    ('쓰레기', None),
])
def test_parse_slurm_duration(raw, seconds):
    assert parse_slurm_duration(raw) == seconds


def test_parse_partitions_marks_default():
    p = parse_partitions('batch_grad*|infinite|23\ndebug_grad|4:00:00|23\n')
    assert p['batch_grad'].is_default and p['batch_grad'].time_limit_seconds is None
    assert p['debug_grad'].time_limit_seconds == 14400


# --- queue / eta -------------------------------------------------------------

def test_confidence_rules():
    assert services._confidence('Resources', '2026-07-10T21:00:00') == 'medium'
    assert services._confidence('QOSMaxGRESPerUser', '2026-07-10T21:00:00') == 'low'
    assert services._confidence('Dependency', '2026-07-10T21:00:00') == 'low'
    assert services._confidence('Resources', None) == 'unknown'


def test_get_queue_ranks_and_marks_mine(snap):
    q = services.get_queue(snap)
    assert q['pending_count'] == len(q['pending'])
    assert q['running_count'] == len(q['running'])
    # 순번은 1..N 로 연속이며 중복이 없다.
    positions = [row['queue_position'] for row in q['pending']]
    assert positions == list(range(1, len(positions) + 1))
    # is_mine 은 사용자명과 일치해야 한다.
    for row in q['pending'] + q['running']:
        assert row['is_mine'] == (row['user'] == snap.me)
    for row in q['pending']:
        assert row['confidence'] in ('medium', 'low', 'unknown')
        assert row['blocked_by_quota'] == (row['reason'] == 'QOSMaxGRESPerUser')


def test_get_queue_my_next_position(snap):
    q = services.get_queue(snap)
    mine = [r['queue_position'] for r in q['pending'] if r['is_mine']]
    assert q['my_next_position'] == (min(mine) if mine else None)


def test_diagnose_includes_confidence(snap):
    d = services.diagnose_pending(snap, snap.me)
    for job in d['jobs']:
        assert job['confidence'] in ('medium', 'low', 'unknown')


# --- moana (학과별 파티션/노드) readiness ------------------------------------

def test_partition_from_account():
    from seraph import clusters
    assert clusters.partition_from_account('ugrad_ce') == 'batch_ce_ugrad'
    assert clusters.partition_from_account('ugrad_eebme') == 'batch_eebme_ugrad'
    assert clusters.partition_from_account('ugrad') == 'batch_ugrad'
    assert clusters.partition_from_account('grad') == 'batch_grad'
    assert clusters.partition_from_account('ugrad_ce', 'debug') == 'debug_ce_ugrad'


class _FakeSnap:
    """can_use_partition / _node_allowlist 는 account·is_undergrad·nodes·partitions·config 만 본다."""
    def __init__(self, account, is_undergrad, node_names=(), partitions=()):
        self.account = account
        self.is_undergrad = is_undergrad
        self.nodes = [type('N', (), {'name': n})() for n in node_names]
        self.partitions = list(partitions)
        self.config = config_module.load(pathlib.Path('/no/such.yaml'))


def test_can_use_partition_is_department_aware():
    ce = _FakeSnap('ugrad_ce', True)
    assert services.can_use_partition(ce, 'batch_ce_ugrad') is True
    assert services.can_use_partition(ce, 'debug_ce_ugrad') is True
    assert services.can_use_partition(ce, 'batch_eebme_ugrad') is False   # 타 학과
    assert services.can_use_partition(ce, 'batch_grad') is False          # 대학원용


def test_node_allowlist_only_applies_when_present_on_cluster():
    # moana 노드만 있으면 ariel-v* allowlist 는 적용 안 됨(None) -> 학부 노드 오차단 방지
    moana = _FakeSnap('ugrad_ce', True, node_names=['moana-y1', 'moana-r1'])
    assert services._node_allowlist(moana) is None
    # ariel 노드가 있으면 그 목록으로 제한
    ariel = _FakeSnap('ugrad', True, node_names=['ariel-v6', 'ariel-v7', 'ariel-g1'])
    allow = services._node_allowlist(ariel)
    assert allow is not None and 'ariel-v6' in allow and 'ariel-g1' not in allow


def test_parse_sacct_handles_array_job_ids():
    from seraph.parsers.sacct import parse_sacct
    text = (
        "131057_3|arr|COMPLETED|0:0|2026-07-20T10:00:00|2026-07-20T11:00:00|"
        "01:00:00|1-00:00:00|batch_ce_ugrad|moana-y5|gres/gpu=1|32G|\n"
        "131058|solo|FAILED|1:0|2026-07-20T09:00:00|2026-07-20T09:30:00|"
        "00:30:00|1-00:00:00|batch_ce_ugrad|moana-r1|gres/gpu=1|32G|\n"
    )
    jobs = parse_sacct(text)   # 예전엔 int('131057_3') 로 ValueError 크래시
    ids = {j.job_id for j in jobs}
    assert ids == {'131057_3', '131058'}


def test_parse_partitions_aggregates_multi_state_rows():
    p = parse_partitions('batch_ce_ugrad*|1-00:00:00|3\nbatch_ce_ugrad|1-00:00:00|4\n')
    assert p['batch_ce_ugrad'].node_count == 7        # 3 + 4 합산
    assert p['batch_ce_ugrad'].is_default is True     # '*' OR


# --- lint --------------------------------------------------------------------

def test_lint_blocks_over_time_limit(snap):
    """debug_grad 는 4시간 제한. 넘기면 Slurm 이 제출을 거절한다."""
    r = services.lint_job(snap, partition='debug_grad', gpus=1, time_limit='5:00:00')
    assert not r['ok']
    assert any(p['code'] == 'OVER_TIME_LIMIT' for p in r['problems'])


def test_lint_allows_long_job_on_infinite_partition(snap):
    r = services.lint_job(snap, partition='batch_grad', gpus=1, time_limit='999:00:00')
    assert not any(p['code'] == 'OVER_TIME_LIMIT' for p in r['problems'])


def test_lint_blocks_unknown_partition(snap):
    r = services.lint_job(snap, partition='batch_undergrad', gpus=1)
    assert not r['ok']
    assert any(p['code'] == 'UNKNOWN_PARTITION' for p in r['problems'])


def test_lint_path_rules_respect_directory_boundary(snap):
    """'/home/' 규칙이 '/homework/...' 를 잡으면 안 된다."""
    r = services.lint_job(snap, gpus=1, paths=['/homework/data'])
    assert r['ok'] and not r['problems']


def test_lint_blocks_data_nas(snap):
    r = services.lint_job(snap, gpus=1, paths=['/data/datasets/imagenet'])
    assert not r['ok']
    assert any(p['code'] == 'BLOCKED_PATH' for p in r['problems'])


def test_lint_warns_on_home(snap):
    r = services.lint_job(snap, gpus=1, paths=['/home/user01/dataset'])
    assert r['ok']   # 경고일 뿐 막지는 않는다
    assert any(p['code'] == 'DISCOURAGED_PATH' for p in r['problems'])


def test_lint_uses_config_paths(snap, tmp_path):
    path = tmp_path / 'c.yaml'
    path.write_text('lint:\n  blocked_paths: ["/scratch/"]\n  warn_paths: []\n',
                    encoding='utf-8')
    snap.config = config_module.load(path)
    assert not services.lint_job(snap, gpus=1, paths=['/scratch/x'])['ok']
    assert services.lint_job(snap, gpus=1, paths=['/data/x'])['ok']  # 이제 허용


# --- sbatch ------------------------------------------------------------------

def test_generate_sbatch_standard_gres(snap):
    g = sbatch.generate_sbatch(snap, name='t', command='python train.py', gpus=2)
    assert g['ok']
    assert '#SBATCH --gres=gpu:2' in g['script']
    assert 'high_perf' not in g['script']
    assert g['script'].startswith('#!/usr/bin/bash')
    assert '#SBATCH --cpus-per-gpu=8' in g['script']
    assert '#SBATCH --mem-per-gpu=32G' in g['script']
    assert g['script'].rstrip().endswith('python train.py')


def test_standard_gpu_always_gets_a_node(snap):
    """세라프는 노드 지정 없는 gpu:N 을 거절한다. 자동으로 하나 골라야 한다."""
    g = sbatch.generate_sbatch(snap, name='t', command='x', gpus=1)
    assert g['ok']
    assert g['auto_selected_node'] is True
    assert '#SBATCH --nodelist=' in g['script']
    node = next(n for n in snap.nodes if n.name == g['node'])
    assert not node.is_high_perf   # 일반 GPU 는 v/g 노드여야 한다


def test_auto_selected_node_is_only_one(snap):
    """-w 에 여러 노드를 적으면 전부 확보될 때까지 기다려 훨씬 늦어진다."""
    g = sbatch.generate_sbatch(snap, name='t', command='x', gpus=1)
    line = next(l for l in g['script'].splitlines() if '--nodelist=' in l)
    assert ',' not in line


def test_high_perf_needs_no_node(snap):
    g = sbatch.generate_sbatch(snap, name='t', command='x', gpus=1, high_perf=True)
    assert g['ok']
    assert '--nodelist' not in g['script']
    assert g['auto_selected_node'] is False


def test_no_exclude_flag_is_emitted(snap):
    """세라프의 안내와 달리 -x 로는 통과하지 못한다. 쓰지 않는다."""
    g = sbatch.generate_sbatch(snap, name='t', command='x', gpus=1)
    assert '--exclude' not in g['script']


def test_lint_warns_when_chosen_node_is_full(snap):
    full = next((n for n in snap.nodes
                 if not n.is_high_perf and n.schedulable and n.usable_gpus == 0), None)
    if full is None:
        pytest.skip('꽉 찬 일반 노드가 없는 스냅샷')
    r = services.lint_job(snap, gpus=1, node=full.name)
    assert r['ok']   # 막지는 않는다. 기다리면 된다.
    assert any(p['code'] == 'NODE_BUSY' for p in r['problems'])


def test_lint_blocks_standard_gres_on_high_perf_node(snap):
    """`--gres=gpu:1 -w ariel-k1` 은 Slurm 이 거절한다."""
    hp = next(n for n in snap.nodes if n.is_high_perf and n.schedulable)
    r = services.lint_job(snap, gpus=1, high_perf=False, node=hp.name)
    assert not r['ok']
    assert any(p['code'] == 'NODE_TYPE_MISMATCH' for p in r['problems'])


def test_lint_blocks_high_perf_gres_on_standard_node(snap):
    std = next(n for n in snap.nodes if not n.is_high_perf and n.schedulable)
    r = services.lint_job(snap, gpus=1, high_perf=True, node=std.name)
    assert not r['ok']
    assert any(p['code'] == 'NODE_TYPE_MISMATCH' for p in r['problems'])


def test_lint_blocks_unknown_and_drained_node(snap):
    assert not services.lint_job(snap, gpus=1, node='ariel-z9')['ok']
    drained = next((n for n in snap.nodes if not n.schedulable), None)
    if drained:
        r = services.lint_job(snap, gpus=1, high_perf=drained.is_high_perf,
                              node=drained.name)
        assert any(p['code'] == 'NODE_UNAVAILABLE' for p in r['problems'])


def test_generate_sbatch_high_perf_gres(snap):
    """고성능 노드는 gres 표기가 다르다: gpu:high_perf:N"""
    g = sbatch.generate_sbatch(snap, name='t', command='python train.py',
                               gpus=1, high_perf=True)
    assert g['ok']
    assert '#SBATCH --gres=gpu:high_perf:1' in g['script']


def test_generate_sbatch_refuses_impossible_job(snap):
    """절대 시작되지 않을 job 은 스크립트를 만들지 않는다."""
    g = sbatch.generate_sbatch(snap, name='t', command='python x.py', gpus=999)
    assert not g['ok']
    assert g['script'] is None
    assert any(p['code'] == 'OVER_GPU_LIMIT' for p in g['lint']['problems'])


def test_generate_sbatch_refuses_blocked_path(snap):
    g = sbatch.generate_sbatch(snap, name='t', command='python x.py',
                               gpus=1, paths=['/data/datasets'])
    assert not g['ok'] and g['script'] is None


def test_generate_sbatch_quotes_list_command(snap):
    g = sbatch.generate_sbatch(snap, name='t', gpus=1,
                               command=['python', 'train.py', '--tag', 'a b'])
    assert "'a b'" in g['script']


def test_suggest_node_returns_usable_node(snap):
    name = sbatch.suggest_node(snap, gpus=1)
    if name is None:
        pytest.skip('여유 노드가 없는 스냅샷')
    node = next(n for n in snap.nodes if n.name == name)
    assert node.usable_gpus >= 1 and not node.cpu_starved


# --- notify ------------------------------------------------------------------

def test_slack_does_nothing_without_webhook(snap):
    r = notify.send_slack(snap.config, '안녕', send=True)
    assert r['sent'] is False and r['reason'] == 'no_webhook'


def test_slack_is_dry_run_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv('SERAPH_SLACK_WEBHOOK', 'https://example.invalid/hook')
    cfg = config_module.load(tmp_path / 'none.yaml')
    r = notify.send_slack(cfg, '안녕')          # send=False
    assert r['sent'] is False and r['reason'] == 'dry_run'


def test_slack_network_failure_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv('SERAPH_SLACK_WEBHOOK', 'http://127.0.0.1:1/hook')
    cfg = config_module.load(tmp_path / 'none.yaml')
    r = notify.send_slack(cfg, '안녕', send=True)
    assert r['sent'] is False and r['reason'].startswith('error:')


def test_quota_blocked_message_uses_headline(snap):
    d = services.diagnose_pending(snap, 'user25')
    assert notify.quota_blocked_message(d) == d['headline']


# --- tutorial ----------------------------------------------------------------

def test_tutorial_runs_on_mock_only(snap):
    t = tutorial.get_tutorial(snap)
    assert t['mode'] == 'practice'
    assert [s['id'] for s in t['steps']][:2] == ['ssh', 'quota']
    assert t['sample_status']['total_gpus'] > 0


def test_tutorial_quota_text_reflects_real_qos(snap):
    step = next(s for s in tutorial.get_steps(snap) if s['id'] == 'quota')
    assert snap.my_qos.name in step['body']


def test_tutorial_data_step_lists_configured_paths(snap):
    step = next(s for s in tutorial.get_steps(snap) if s['id'] == 'data')
    assert '/data/' in step['pitfall']


# --- 폴링 --------------------------------------------------------------------

def test_should_poll_respects_config_limit(snap):
    snap.load = 99.0
    assert not services.should_poll(snap)
    snap.load = 1.0
    assert services.should_poll(snap)


# --- 지도교수(advisor) 소속 파티션: 서버 네이밍이 학과 계정과 다르다 ---
# 실서버 moana 관측: batch_ugrad_advisor_x (계정을 통째로), batch_ce_ugrad (접미어를 앞으로)

def test_partition_candidates_covers_both_naming_rules():
    cands = clusters.partition_candidates('ugrad_advisor_x', 'batch')
    assert 'batch_ugrad_advisor_x' in cands, '실서버 이름이 후보에 없다'
    assert 'batch_advisor_x_ugrad' in cands, '학과식 이름도 후보로 남긴다'


def test_resolve_partition_picks_the_one_that_exists():
    """추측한 이름을 그대로 쓰지 않고 서버의 실제 목록과 대조한다."""
    real = ['debug_ce_ugrad', 'batch_ce_ugrad', 'debug_ugrad_advisor_x',
            'batch_ugrad_advisor_x', 'admin']
    assert clusters.resolve_partition('ugrad_advisor_x', 'batch', real) == 'batch_ugrad_advisor_x'
    assert clusters.resolve_partition('ugrad_advisor_x', 'debug', real) == 'debug_ugrad_advisor_x'
    assert clusters.resolve_partition('ugrad_ce', 'batch', real) == 'batch_ce_ugrad'
    # 목록을 모르면 기존처럼 첫 후보(추측)
    assert clusters.resolve_partition('ugrad_ce', 'batch', None) == 'batch_ce_ugrad'


def test_partition_position_handles_ugrad_in_the_middle():
    """batch_ugrad_advisor_x 는 _ugrad 로 끝나지 않는다. 끝만 보면 놓친다."""
    assert clusters.partition_position('batch_ce_ugrad') == 'undergrad'
    assert clusters.partition_position('batch_ugrad_advisor_x') == 'undergrad'
    assert clusters.partition_position('debug_ugrad_advisor_x') == 'undergrad'
    assert clusters.partition_position('batch_ce_grad') == 'grad'
    assert clusters.partition_position('batch_grad') == 'grad'
    assert clusters.partition_position('admin') is None


# 실서버 moana 의 파티션 목록(전수 조회 결과)
_MOANA_PARTS = ['admin', 'debug_eebme_ugrad', 'batch_eebme_ugrad',
                'debug_eebme_grad', 'batch_eebme_grad', 'debug_ce_ugrad',
                'batch_ce_ugrad', 'debug_ce_grad', 'batch_ce_grad',
                'debug_ugrad_advisor_x', 'batch_ugrad_advisor_x']


def test_ce_undergrad_cannot_use_another_advisor_partition():
    """CE 학부생에게 지도교수 전용 파티션이 '사용 가능'으로 보이면 안 된다.

    끝만 보던 예전 코드는 batch_ugrad_advisor_x 를 학부·대학원 어느 쪽도 아니라고
    흘려보내 True 를 돌려줬다(실서버에서 실제로 그렇게 보였다).
    """
    ce = _FakeSnap('ugrad_ce', True, partitions=_MOANA_PARTS)
    assert services.can_use_partition(ce, 'batch_ce_ugrad') is True
    assert services.can_use_partition(ce, 'debug_ce_ugrad') is True
    assert services.can_use_partition(ce, 'batch_ugrad_advisor_x') is False
    assert services.can_use_partition(ce, 'debug_ugrad_advisor_x') is False
    assert services.can_use_partition(ce, 'batch_ce_grad') is False
    assert services.can_use_partition(ce, 'admin') is False


def test_advisor_undergrad_gets_the_real_partition():
    """지도교수 소속 학부생은 자기 파티션만. 이름 규칙이 학과 계정과 다르다."""
    adv = _FakeSnap('ugrad_advisor_x', True, partitions=_MOANA_PARTS)
    assert clusters.resolve_partition('ugrad_advisor_x', 'batch', _MOANA_PARTS) == 'batch_ugrad_advisor_x'
    assert services.can_use_partition(adv, 'batch_ugrad_advisor_x') is True
    assert services.can_use_partition(adv, 'debug_ugrad_advisor_x') is True
    assert services.can_use_partition(adv, 'batch_ce_ugrad') is False


# --- squeue 는 끝난 job 에 rc=1 을 낸다 (완료 작업을 못 여는 원인이었다) ------

class _FakeChannel:
    def __init__(self, rc): self._rc = rc
    def recv_exit_status(self): return self._rc


class _FakeStream:
    def __init__(self, text, rc=0):
        self._text = text.encode()
        self.channel = _FakeChannel(rc)
    def read(self): return self._text


class _FakeClient:
    """rc 를 마음대로 주는 가짜 SSH 클라이언트."""
    def __init__(self, out, err, rc):
        self._out, self._err, self._rc = out, err, rc
        self.commands = []
    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        return None, _FakeStream(self._out, self._rc), _FakeStream(self._err)


def _bare_ssh_connection(client):
    from seraph.connection import SSHConnection
    conn = SSHConnection.__new__(SSHConnection)   # __init__ 은 실제 접속을 한다
    conn.client = client
    return conn


def test_run_command_raises_on_failure_by_default():
    conn = _bare_ssh_connection(_FakeClient("", "boom", rc=1))
    with pytest.raises(RuntimeError):
        conn.run_command("false", label="테스트")


def test_run_command_check_false_returns_empty_instead_of_raising():
    """squeue 가 끝난 job 에 rc=1 을 내도 예외가 되면 안 된다.

    이걸 예외로 던지는 바람에 완료된 작업을 클릭하면 500 INTERNAL_ERROR 가 났다.
    """
    conn = _bare_ssh_connection(
        _FakeClient("", "slurm_load_jobs error: Invalid job id specified", rc=1))
    assert conn.run_command("squeue -h -j 1", label="작업 상태", check=False) == ""


def test_run_command_check_false_still_returns_output_on_success():
    conn = _bare_ssh_connection(_FakeClient("RUNNING|None|moana-u1|0:05", "", rc=0))
    out = conn.run_command("squeue -h -j 1", check=False)
    assert out.startswith("RUNNING|")


def test_job_state_survives_accounting_db_outage():
    """slurmdbd 가 죽어도 작업 상세가 500 이 되면 안 된다.

    실측: sacct 가 'Problem talking to the database: Connection timed out' 로 rc=1.
    최신 상태만 포기하고(None) 저장된 상태를 보여줘야 한다.
    """
    from backend.remote import SSHRemote

    class _Conn:
        def run_command(self, command, label='명령', timeout=30, check=True):
            if command.startswith('squeue'):
                return ''            # 큐에 없음(끝난 job)
            assert check is False, 'sacct 실패가 예외가 되면 상세 화면이 통째로 죽는다'
            return ''                # 회계 DB 장애

    remote = SSHRemote.__new__(SSHRemote)   # __init__ 은 실제 SSH/SFTP 를 연다
    remote.connection = _Conn()
    assert remote.job_state('131837') is None
