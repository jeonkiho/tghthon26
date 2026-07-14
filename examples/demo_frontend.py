"""백엔드 연결 확인용 최소 프론트 데모.

진짜 TUI 가 아니라, "프론트가 백엔드를 부르면 화면을 그릴 수 있다"를 증명하는
30줄짜리 예시다. 외부 라이브러리 없이 stdlib 만 쓴다.

    python examples/demo_frontend.py            # mock (서버 없이)
    python examples/demo_frontend.py --host ariel   # 실서버

실제 TUI 는 여기서 print 하는 자리에 Textual/curses 위젯을 그리면 된다.
데이터를 받아오는 부분(services.* 호출)은 그대로다.
"""

import pathlib
import sys

# 저장소 루트를 import 경로에 추가 (examples/ 안에서 바로 실행할 수 있게)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from seraph.connection import connect, SSHConnection
from seraph import services


def render(snap):
    """백엔드가 준 dict 를 화면 문자열로. 이 함수가 '프론트' 다."""
    me = snap.me
    status = services.get_gpu_status(snap)
    usage = services.get_my_usage(snap, me)
    diag = services.diagnose_pending(snap, me)
    nodes = services.get_node_availability(snap)[:3]

    bar_len = 24
    used_ratio = status['used_gpus'] / status['total_gpus'] if status['total_gpus'] else 0
    filled = round(bar_len * used_ratio)
    bar = '█' * filled + '·' * (bar_len - filled)

    lines = [
        '╔══════════════════════════════════════════════════╗',
        f"║  SERAPH · {status['partition']:<20}  {me:>15} ║",
        '╠══════════════════════════════════════════════════╣',
        f"║  GPU  [{bar}] {status['used_gpus']:>3}/{status['total_gpus']:<3}  ║",
        f"║  여유 {status['free_gpus']:>3}개 (고성능 {status['free_high_perf_gpus']}, 일반 {status['free_standard_gpus']})"
        + ' ' * 20 + '║',
        f"║  대기 {status['pending_jobs']:>3}건 · 실행 {status['running_jobs']:>3}건"
        + ' ' * 24 + '║',
        '╠══════════════════════════════════════════════════╣',
        f"║  내 사용량: GPU {usage['gpus_in_use']}/{usage['gpus_limit']}"
        f" · 고성능 {usage['high_perf_in_use']}/{usage['high_perf_limit']}"
        + ' ' * 15 + '║',
        '╠══════════════════════════════════════════════════╣',
        '║  지금 쓸 수 있는 노드:                            ║',
    ]
    for n in nodes:
        tag = 'HP' if n['is_high_perf'] else '  '
        lines.append(f"║    {n['name']:<10} {tag} 여유 {n['usable_gpus']}개"
                     + ' ' * 24 + '║')
    if not nodes:
        lines.append('║    (없음)' + ' ' * 40 + '║')

    lines.append('╠══════════════════════════════════════════════════╣')
    # headline 은 완성된 한 문장이라 그대로 띄운다 (여러 줄로 자름)
    head = diag['headline']
    lines.append('║  진단:' + ' ' * 43 + '║')
    for i in range(0, len(head), 40):
        chunk = head[i:i + 40]
        lines.append(f"║    {chunk:<44}║")
    lines.append('╚══════════════════════════════════════════════════╝')
    return '\n'.join(lines)


def main():
    if '--host' in sys.argv:
        host = sys.argv[sys.argv.index('--host') + 1]
        conn = SSHConnection(host)
    else:
        conn = connect()          # config.yaml mode (기본 mock)

    try:
        snap = conn.snapshot()    # ← 프론트가 백엔드를 부르는 지점
        if not services.should_poll(snap):
            print(f'(로그인 노드 부하 {snap.load} — 실제 TUI 면 폴링을 쉰다)')
        print(render(snap))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
