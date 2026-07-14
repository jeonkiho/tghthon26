"""세라프 TUI 데모 — 실제로 갱신되는 화면.

터미널에서 직접 실행할 것 (TTY 필요):

    python examples/tui.py                # mock (서버 없이)
    python examples/tui.py --host ariel   # 실서버

  q 또는 Ctrl-C : 종료
  r             : 즉시 새로고침

이건 최소 데모다. 진짜 프론트는 여기 render_* 자리에 위젯을 그리면 된다.
데이터를 받아오는 부분(services.* 호출)은 그대로 쓴다.
"""

import argparse
import curses
import pathlib
import sys
import time
import unicodedata

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from seraph import placement, services
from seraph.connection import connect, SSHConnection


def fetch(conn, gpus=1, hours=2.0):
    """백엔드에서 화면에 필요한 것을 모아 온다. 실패해도 죽지 않는다."""
    try:
        snap = conn.snapshot()
        # "지금 바로 되나? 안 되면 어디가 제일 빠른가" — Slurm 에게 직접 물어본다.
        fastest = placement.find_fastest(conn, snap, gpus=gpus, hours=hours)
        return {
            'ok': True,
            'me': snap.me,
            'load': snap.load,
            'should_poll': services.should_poll(snap),
            'poll_interval': snap.config.poll_interval,
            'status': services.get_gpu_status(snap),
            'usage': services.get_my_usage(snap, snap.me),
            'diag': services.diagnose_pending(snap, snap.me),
            'nodes': services.get_node_availability(snap)[:5],
            'fastest': fastest,
            'want': {'gpus': gpus, 'hours': hours},
        }
    except Exception as exc:                       # noqa: BLE001
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}


def bar(used, total, width=30):
    if not total:
        return '·' * width
    filled = round(width * used / total)
    return '█' * filled + '·' * (width - filled)


def _cell_width(ch):
    """한글·이모지는 터미널에서 2칸을 차지한다."""
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


def fit(text, width):
    """표시 폭이 정확히 width 칸이 되도록 자르고 공백으로 채운다.

    글자 수가 아니라 '터미널 칸 수' 기준. 한글이 섞이면 이걸 안 하면 넘쳐서 깨진다.
    """
    out, used = [], 0
    for ch in text:
        cw = _cell_width(ch)
        if used + cw > width:
            break
        out.append(ch)
        used += cw
    return ''.join(out) + ' ' * (width - used)


def draw(stdscr, data, updated_at):
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    def line(y, text, attr=0):
        # 마지막 칸(맨 오른쪽 아래)에 쓰면 커서가 넘어가며 ERR 이 난다.
        # 그래서 폭을 w-1 로 잡고, 그래도 나는 예외는 무시한다.
        if not (0 <= y < h):
            return
        try:
            stdscr.addstr(y, 0, fit(text, w - 1), attr)
        except curses.error:
            pass

    if not data['ok']:
        line(0, ' SERAPH TUI ', curses.A_REVERSE)
        line(2, f"  백엔드 오류: {data['error']}", curses.A_BOLD)
        line(4, '  r=재시도  q=종료')
        stdscr.refresh()
        return

    st, us, dg = data['status'], data['usage'], data['diag']

    header = f" SERAPH · {st['partition']} · {data['me']} "
    line(0, header + ' ' * (w - len(header)), curses.A_REVERSE)

    y = 2
    line(y, f"  GPU  {bar(st['used_gpus'], st['total_gpus'])}  "
            f"{st['used_gpus']}/{st['total_gpus']} 사용")
    y += 1
    line(y, f"       여유 {st['free_gpus']}개  "
            f"(고성능 {st['free_high_perf_gpus']} · 일반 {st['free_standard_gpus']})")
    y += 1
    line(y, f"       대기 {st['pending_jobs']}건 · 실행 {st['running_jobs']}건")

    y += 2
    line(y, "  내 사용량", curses.A_BOLD)
    y += 1
    line(y, f"    GPU {us['gpus_in_use']}/{us['gpus_limit']}   "
            f"고성능 {us['high_perf_in_use']}/{us['high_perf_limit']}   "
            f"실행 job {us['running_jobs']}/{us['running_jobs_limit']}")

    # ── 핵심: 지금 바로 학습을 시작할 수 있나? ──────────────────────────
    want = data['want']
    fast = data['fastest']
    y += 2
    line(y, f"  ▶ 지금 학습 시작 (GPU {want['gpus']}개 · {want['hours']:g}시간)",
         curses.A_BOLD)
    y += 1
    mark = '✔' if fast['can_start_now'] else '…'
    line(y, f"    {mark} {fast['headline']}", curses.A_BOLD)

    for o in fast['options']:
        y += 1
        limit = o['time_limit_seconds']
        cap = f"최대 {limit // 3600}h" if limit else '무제한'
        flag = '←' if o is fast['options'][0] else ' '
        line(y, f"      {flag} {o['partition']:<12} {str(o['node']):<10} "
                f"{o['wait_text']:<14} ({cap})")
    for b in fast['blocked']:
        y += 1
        line(y, f"      × {b['partition']:<12} {b['reason'][:40]}")

    y += 2
    line(y, "  진단", curses.A_BOLD)
    y += 1
    line(y, f"    {dg['headline']}")

    # 하단 상태줄
    stamp = time.strftime('%H:%M:%S', time.localtime(updated_at))
    poll = '폴링중' if data['should_poll'] else f"부하높음({data['load']}) 대기"
    footer = f" {stamp} 갱신 · {poll} · r=새로고침 q=종료 "
    line(h - 1, footer + ' ' * (w - len(footer)), curses.A_REVERSE)
    stdscr.refresh()


def run(stdscr, conn, interval, gpus, hours):
    curses.curs_set(0)
    stdscr.nodelay(True)
    data = fetch(conn, gpus, hours)
    updated = time.time()
    draw(stdscr, data, updated)

    while True:
        ch = stdscr.getch()
        if ch in (ord('q'), ord('Q')):
            return
        force = ch in (ord('r'), ord('R'))
        if force or time.time() - updated >= interval:
            data = fetch(conn, gpus, hours)
            updated = time.time()
            draw(stdscr, data, updated)
        time.sleep(0.1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', help='실서버 접속. 생략하면 config 의 mode')
    ap.add_argument('--interval', type=float, default=None, help='갱신 주기(초)')
    ap.add_argument('--gpus', type=int, default=1, help='필요한 GPU 수')
    ap.add_argument('--hours', type=float, default=2.0, help='학습할 시간')
    args = ap.parse_args()

    conn = SSHConnection(args.host) if args.host else connect()
    interval = args.interval
    if interval is None:
        try:
            interval = conn.config.poll_interval
        except Exception:                          # noqa: BLE001
            interval = 7
    try:
        curses.wrapper(run, conn, interval, args.gpus, args.hours)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
