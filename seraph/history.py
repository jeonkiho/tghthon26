"""끝난 job 의 결과 취합과 실패 원인 진단.

`sacct` 는 상태 이름만 준다("FAILED"). 그것만 보여주면 사용자는 여전히 왜 죽었는지
모른다. 여기서 종료 코드·시그널·메모리·시간을 함께 읽어 원인과 다음 행동을 만든다.

주의할 점:
  - OOM 은 종료 코드가 0 이다 (`0:125`). 코드만 보면 성공으로 오해한다.
  - `CANCELLED by 0` 은 관리자/시스템이 취소한 것이고, 다른 UID 면 사용자가 취소한
    것이다. 둘은 사용자에게 전혀 다른 의미다.
"""

from .parsers import parse_sacct, SUCCESS

# 실패 원인 코드 -> 사람이 읽을 설명
REASON_TEXT = {
    'ok': '정상 종료',
    'out_of_memory': '메모리 부족으로 강제 종료',
    'timeout': '시간 제한 초과로 강제 종료',
    'command_not_found': '명령을 찾을 수 없음',
    'script_error': '스크립트가 오류로 종료',
    'killed_by_signal': '시그널로 종료',
    'cancelled_by_user': '사용자가 취소',
    'cancelled_by_admin': '관리자 또는 시스템이 취소',
    'node_failure': '노드 장애',
    'unknown': '알 수 없는 종료',
}

# 명령을 못 찾았을 때 셸이 내는 종료 코드
_EXIT_COMMAND_NOT_FOUND = 127


def _fmt_mb(mb):
    if mb is None:
        return '알 수 없음'
    if mb >= 1024:
        return f'{mb / 1024:.1f}GB'
    return f'{mb}MB'


def _fmt_seconds(seconds):
    if seconds is None:
        return '알 수 없음'
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f'{hours}시간 {minutes}분'
    if minutes:
        return f'{minutes}분 {secs}초'
    return f'{secs}초'


def diagnose_job(job):
    """끝난 job 하나의 원인과 조언. 반환 dict 는 그대로 JSON 이 된다."""
    if job.succeeded:
        return {'reason': 'ok', 'reason_text': REASON_TEXT['ok'], 'advice': ''}

    state = job.state
    log = f'slurm-{job.job_id}.out'

    if state == 'OUT_OF_MEMORY':
        used, asked = job.max_rss_mb, job.req_mem_mb
        detail = ''
        if used and asked:
            detail = (f' 요청한 {_fmt_mb(asked)} 중 {_fmt_mb(used)}'
                      f'({used / asked:.0%})까지 쓰고 죽었습니다.')
        return {
            'reason': 'out_of_memory',
            'reason_text': REASON_TEXT['out_of_memory'],
            'advice': (f'메모리가 부족했습니다.{detail} '
                       f'--mem 을 늘리거나 배치 크기를 줄이세요. '
                       f'종료 코드는 0 이지만 성공이 아닙니다.'),
        }

    if state == 'TIMEOUT':
        over = ''
        if job.elapsed_seconds and job.time_limit_seconds:
            over = (f' 제한 {_fmt_seconds(job.time_limit_seconds)}을 '
                    f'{_fmt_seconds(job.elapsed_seconds - job.time_limit_seconds)} '
                    f'넘겼습니다.')
        return {
            'reason': 'timeout',
            'reason_text': REASON_TEXT['timeout'],
            'advice': (f'시간 제한에 걸렸습니다.{over} --time 을 늘리거나 '
                       f'중간 체크포인트를 저장해 이어서 학습하세요. '
                       f'debug_* 파티션은 4시간이 상한입니다.'),
        }

    if state == 'CANCELLED':
        if job.cancelled_by in (None, '0'):
            return {
                'reason': 'cancelled_by_admin',
                'reason_text': REASON_TEXT['cancelled_by_admin'],
                'advice': '관리자나 시스템이 취소했습니다. 공지를 확인하세요.',
            }
        return {
            'reason': 'cancelled_by_user',
            'reason_text': REASON_TEXT['cancelled_by_user'],
            'advice': f'사용자(UID {job.cancelled_by})가 scancel 로 취소했습니다.',
        }

    if state == 'NODE_FAIL':
        return {
            'reason': 'node_failure',
            'reason_text': REASON_TEXT['node_failure'],
            'advice': f'{job.nodes} 노드에 장애가 났습니다. 다시 제출하세요.',
        }

    if state == 'FAILED':
        if job.exit_code == _EXIT_COMMAND_NOT_FOUND:
            return {
                'reason': 'command_not_found',
                'reason_text': REASON_TEXT['command_not_found'],
                'advice': ('명령을 찾지 못했습니다(종료 코드 127). conda 환경을 '
                           'activate 했는지, 실행 파일 경로가 맞는지 확인하세요.'),
            }
        if job.signal:
            return {
                'reason': 'killed_by_signal',
                'reason_text': REASON_TEXT['killed_by_signal'],
                'advice': f'시그널 {job.signal} 로 종료되었습니다. {log} 를 보세요.',
            }
        return {
            'reason': 'script_error',
            'reason_text': REASON_TEXT['script_error'],
            'advice': (f'스크립트가 종료 코드 {job.exit_code} 로 끝났습니다. '
                       f'{log} 를 먼저 확인하세요.'),
        }

    return {
        'reason': 'unknown',
        'reason_text': REASON_TEXT['unknown'],
        'advice': f'상태: {job.raw_state}. {log} 를 확인하세요.',
    }


def _gpu_seconds(job):
    if not job.elapsed_seconds:
        return 0
    return job.elapsed_seconds * max(job.gpus, 0)


def summarize(jobs):
    """성공률과 낭비된 GPU 시간.

    실패한 job 이 쓴 GPU 시간은 그대로 버려진 자원이다. 이걸 보여주면 사용자가
    실패를 방치하지 않게 된다.
    """
    total = len(jobs)
    if not total:
        return {'total': 0, 'succeeded': 0, 'failed': 0, 'success_rate': 0.0,
                'by_state': {}, 'wasted_gpu_hours': 0.0, 'total_gpu_hours': 0.0}

    succeeded = sum(1 for j in jobs if j.succeeded)
    by_state = {}
    for job in jobs:
        by_state[job.state] = by_state.get(job.state, 0) + 1

    wasted = sum(_gpu_seconds(j) for j in jobs if not j.succeeded)
    spent = sum(_gpu_seconds(j) for j in jobs)

    return {
        'total': total,
        'succeeded': succeeded,
        'failed': total - succeeded,
        'success_rate': round(succeeded / total, 3),
        'by_state': dict(sorted(by_state.items(), key=lambda kv: -kv[1])),
        'wasted_gpu_hours': round(wasted / 3600, 1),
        'total_gpu_hours': round(spent / 3600, 1),
    }


def _headline(stats):
    if not stats['total']:
        return '최근에 끝난 job 이 없습니다.'
    line = (f"끝난 job {stats['total']}개 중 {stats['succeeded']}개 성공 "
            f"({stats['success_rate']:.0%}).")
    if stats['wasted_gpu_hours'] >= 0.1:
        line += f" 실패한 job 이 GPU {stats['wasted_gpu_hours']}시간을 썼습니다."
    return line


def get_job_history(sacct_text, limit=20):
    """프론트가 부르는 함수. sacct 원문 -> JSON."""
    jobs = parse_sacct(sacct_text)
    stats = summarize(jobs)

    items = []
    for job in jobs[:limit]:
        entry = job.to_dict()
        entry.update(diagnose_job(job))
        items.append(entry)

    return {
        'headline': _headline(stats),
        'stats': stats,
        'jobs': items,
    }


def get_job_result(sacct_text, job_id):
    """job 하나의 결과. 없으면 found:false."""
    for job in parse_sacct(sacct_text):
        if job.job_id == str(job_id):
            entry = job.to_dict()
            entry.update(diagnose_job(job))
            entry['found'] = True
            return entry
    return {'job_id': str(job_id), 'found': False}
