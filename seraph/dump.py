"""프론트에 넘길 JSON 을 그대로 찍어보는 진입점.

    python -m seraph.dump                       # config.yaml 의 mode 를 따른다
    python -m seraph.dump --mock                # 강제로 mock
    python -m seraph.dump --host ariel          # 강제로 실서버
    python -m seraph.dump --section diagnose
"""

import argparse
import json
import sys

from . import config as config_module
from . import services, sbatch as sbatch_module, tutorial as tutorial_module
from . import history, notify, slack


SECTIONS = ('status', 'diagnose', 'nodes', 'usage', 'lint', 'wait',
            'sbatch', 'tutorial', 'announcements', 'history', 'result',
            'notify', 'config', 'partitions', 'whoami', 'clusters')

# sacct 는 스냅샷과 달리 요청할 때만 부른다. 그래서 conn 이 필요하다.
_NEEDS_CONNECTION = ('history', 'result')


def build(snapshot, args, conn=None):
    user = args.user or snapshot.me
    partition = args.partition

    if args.section == 'status':
        return services.get_gpu_status(snapshot, partition)
    if args.section == 'diagnose':
        return services.diagnose_pending(snapshot, user, partition)
    if args.section == 'nodes':
        return {'nodes': services.get_node_availability(snapshot, partition)}
    if args.section == 'usage':
        return services.get_my_usage(snapshot, user)
    if args.section == 'partitions':
        return services.get_partitions(snapshot)     # can_use 포함
    if args.section == 'whoami':
        return services.whoami(snapshot)
    if args.section == 'clusters':
        from . import clusters
        return clusters.overview()
    if args.section == 'lint':
        # 일부러 위반하는 예시. 프론트가 문제 목록 모양을 볼 수 있게 한다.
        return services.lint_job(snapshot, gpus=99, high_perf=True,
                                 paths=['/nas2/data/imagenet'],
                                 time_limit='999:00:00')
    if args.section == 'wait':
        job_id = args.job_id
        if not job_id:
            pending = [j for j in snapshot.jobs if j.is_pending]
            job_id = pending[0].job_id if pending else '0'
        return services.estimate_wait_time(snapshot, job_id)
    if args.section == 'sbatch':
        # 노드를 안 넘기면 v/g 노드를 자동으로 고른다 (세라프가 요구한다).
        return sbatch_module.generate_sbatch(
            snapshot, name='demo-train', command='python train.py',
            gpus=1, paths=['/local/imagenet'])
    if args.section == 'tutorial':
        return tutorial_module.get_tutorial(snapshot)
    if args.section == 'history':
        return history.get_job_history(conn.sacct(args.days, args.user),
                                       limit=args.limit)
    if args.section == 'result':
        if not args.job_id:
            raise SystemExit('--job-id 가 필요합니다.')
        return history.get_job_result(conn.sacct(args.days, args.user),
                                      args.job_id)
    if args.section == 'announcements':
        # SERAPH_SLACK_TOKEN 이 있으면 실제 Slack, 없으면 저장된 mock 공지.
        cfg = snapshot.config
        client = slack.connect(cfg)
        return slack.get_announcements(client, cfg.slack_channel, cfg.slack_limit)
    if args.section == 'notify':
        d = services.diagnose_pending(snapshot, user, partition)
        return notify.send_slack(snapshot.config,
                                 notify.quota_blocked_message(d), send=False)
    if args.section == 'config':
        return snapshot.config.to_dict()
    raise ValueError(args.section)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', help='실서버 접속 (config 의 mode 를 무시)')
    ap.add_argument('--mock', action='store_true', help='강제로 mock 사용')
    ap.add_argument('--section', default='status', choices=SECTIONS)
    ap.add_argument('--partition', help='생략하면 config 의 기본 파티션')
    ap.add_argument('--job-id')
    ap.add_argument('--user', help='생략하면 접속한 본인')
    ap.add_argument('--days', type=int, default=7, help='history: 조회 기간')
    ap.add_argument('--limit', type=int, default=20, help='history: 표시 개수')
    args = ap.parse_args(argv)

    cfg = config_module.load()

    if args.host:
        from .connection import SSHConnection
        conn = SSHConnection(args.host, config=cfg)
    elif args.mock:
        from .connection import MockConnection
        conn = MockConnection(config=cfg)
    else:
        from .connection import connect
        conn = connect(cfg)

    try:
        snapshot = conn.snapshot()
        if not services.should_poll(snapshot):
            print(f'경고: 로그인 노드 부하 {snapshot.load} — 폴링을 쉬세요.',
                  file=sys.stderr)
        payload = build(snapshot, args, conn)
    finally:
        conn.close()

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
