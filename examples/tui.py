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

from seraph import services
from seraph.connection import connect, SSHConnection


def fetch(conn):
    """백엔드에서 화면에 필요한 것을 모아 온다. 실패해도 죽지 않는다."""
    try:
        snap = conn.snapshot()
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

    y += 2
    line(y, "  지금 쓸 수 있는 노드", curses.A_BOLD)
    for n in data['nodes']:
        y += 1
        tag = '[HP]' if n['is_high_perf'] else '    '
        line(y, f"    {tag} {n['name']:<12} 여유 {n['usable_gpus']}개")
    if not data['nodes']:
        y += 1
        line(y, "    (지금 바로 쓸 수 있는 노드 없음)")

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


def run(stdscr, conn, interval):
    curses.curs_set(0)
    stdscr.nodelay(True)
    data = fetch(conn)
    updated = time.time()
    draw(stdscr, data, updated)

    while True:
        ch = stdscr.getch()
        if ch in (ord('q'), ord('Q')):
            return
        force = ch in (ord('r'), ord('R'))
        if force or time.time() - updated >= interval:
            data = fetch(conn)
            updated = time.time()
            draw(stdscr, data, updated)
        time.sleep(0.1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', help='실서버 접속. 생략하면 config 의 mode')
    ap.add_argument('--interval', type=float, default=None, help='갱신 주기(초)')
    args = ap.parse_args()

    conn = SSHConnection(args.host) if args.host else connect()
    interval = args.interval
    if interval is None:
        try:
            interval = conn.config.poll_interval
        except Exception:                          # noqa: BLE001
            interval = 7
    try:
        curses.wrapper(run, conn, interval)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
