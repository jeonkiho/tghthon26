"""설정 로딩.

config.yaml 이 없어도 기본값으로 동작한다. 설정 파일이 코드보다 우선한다.
비밀(Slack webhook)은 환경변수를 먼저 본다.
"""

import copy
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / 'config.yaml'

DEFAULTS = {
    'connection': {
        'mode': 'mock',
        'host': 'ariel',
        'poll_interval_seconds': 7,
        'login_node_load_limit': 8.0,
    },
    'cluster': {
        'default_partition': 'batch_grad',      # 대학원생 기본
        'undergrad_partition': 'batch_ugrad',   # 학부생 기본
        # 학부생이 실제로 쓸 수 있는 노드 (batch_ugrad + QOS high_perf=0 의 결과).
        # 가이드: ariel-v[6-12]. 서버에서 확인함.
        'undergrad_nodes': ['ariel-v6', 'ariel-v7', 'ariel-v8', 'ariel-v9',
                            'ariel-v10', 'ariel-v11', 'ariel-v12'],
    },
    'placement': {
        # 학습(training) 추천에서 제외할 파티션.
        # debug_* 는 디버깅·짧은 테스트용이다. 4시간 제한이 있고, 학습을 여기로
        # 몰면 정작 디버깅하려는 사람이 못 쓴다. "지금 바로 된다"는 이유로
        # 추천하면 안 된다.
        'exclude_partitions': ['debug_grad', 'debug_ugrad'],
        # 파티션마다 물어볼 노드 후보 수. 노드별로 시작 시각이 크게 다를 수 있다
        # (실측 3일 차이). 질의는 싸다 — 5회에 1초 미만.
        'probe_nodes': 5,
        # 고성능 노드(m/k/n)는 따로 신청해서 받은 사람만 쓴다.
        # (QOS 90개 중 40개가 high_perf=0 으로 금지. 기본 grad/ugrad 도 0.)
        # 자동 추천하지 않는다. 사용자가 명시적으로 요청할 때만 쓴다.
    },
    # 서버가 강제하지 않는 "권장" 정책. 가이드 기준. 넘으면 경고(차단은 아님).
    'policy': {
        'walltime_max_days': 6,       # 상향 신청 시 최대
        'grad_gpu_default': 4,
        'grad_gpu_max': 16,
        'undergrad_gpu_default': 1,
        'undergrad_gpu_max': 16,      # 학부 연구원 기준
    },
    'lint': {
        'blocked_paths': ['/nas2/'],
        'warn_paths': ['/ceph_data/', '/home/'],
    },
    'sbatch': {
        'default_cpus_per_task': 8,
        'default_mem': '32G',
        'default_time': '24:00:00',
        'output_pattern': 'slurm-%j.out',
    },
    'slack': {
        'channel': '공지',      # 읽어올 채널 이름 또는 ID
        'limit': 10,           # 가져올 최근 메시지 수
    },
    'notify': {
        'slack_webhook_url': None,
    },
}


def _merge(base, override):
    """override 의 값이 이긴다. 없는 키는 base 를 쓴다."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    @property
    def mode(self):
        return self._data['connection']['mode']

    @property
    def host(self):
        return self._data['connection']['host']

    @property
    def poll_interval(self):
        return self._data['connection']['poll_interval_seconds']

    @property
    def load_limit(self):
        return float(self._data['connection']['login_node_load_limit'])

    @property
    def default_partition(self):
        return self._data['cluster']['default_partition']

    @property
    def undergrad_partition(self):
        return self._data['cluster']['undergrad_partition']

    @property
    def undergrad_nodes(self):
        return list(self._data['cluster']['undergrad_nodes'])

    @property
    def policy(self):
        return self._data['policy']

    @property
    def excluded_partitions(self):
        """학습 추천에서 뺄 파티션 (debug_* 등)."""
        return list(self._data['placement']['exclude_partitions'])

    @property
    def probe_nodes(self):
        return int(self._data['placement']['probe_nodes'])


    @property
    def blocked_paths(self):
        return list(self._data['lint']['blocked_paths'])

    @property
    def warn_paths(self):
        return list(self._data['lint']['warn_paths'])

    @property
    def sbatch(self):
        return self._data['sbatch']

    @property
    def slack_webhook(self):
        """환경변수가 파일보다 우선한다. 비밀을 커밋하지 않기 위해서."""
        return (os.environ.get('SERAPH_SLACK_WEBHOOK')
                or self._data['notify']['slack_webhook_url'])

    @property
    def slack_token(self):
        """공지를 읽을 때 쓰는 Slack Web API 토큰.

        오직 환경변수에서만 읽는다. config.yaml 에 적을 자리를 주지 않는다 —
        그 파일은 커밋되기 때문이다. 없으면 None (mock 으로 동작).
        """
        return os.environ.get('SERAPH_SLACK_TOKEN') or None

    @property
    def slack_channel(self):
        return self._data['slack']['channel']

    @property
    def slack_limit(self):
        return int(self._data['slack']['limit'])

    def to_dict(self):
        return copy.deepcopy(self._data)


def load(path=DEFAULT_PATH):
    """config.yaml 을 읽는다. 없거나 PyYAML 이 없으면 기본값으로 동작한다."""
    path = pathlib.Path(path)
    if not path.exists():
        return Config(copy.deepcopy(DEFAULTS))

    try:
        import yaml
    except ImportError:
        # PyYAML 이 없어도 mock 개발은 계속할 수 있어야 한다.
        return Config(copy.deepcopy(DEFAULTS))

    with open(path, encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    return Config(_merge(DEFAULTS, data))
