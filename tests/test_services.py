"""config / lint / sbatch / notify / tutorial 테스트."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from seraph import config as config_module
from seraph import notify, sbatch, services, tutorial
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
    assert cfg.data_root == '/data'
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
