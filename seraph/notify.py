"""Slack 알림 (보내는 쪽만).

외부로 나가는 유일한 통로다. webhook URL 이 없으면 아무것도 보내지 않는다.
URL 은 비밀이므로 환경변수 SERAPH_SLACK_WEBHOOK 를 권장한다.

    export SERAPH_SLACK_WEBHOOK='https://hooks.slack.com/services/...'

send=True 를 넘겨야 실제로 전송한다. 기본은 미리보기(dry-run)다. 폴링 루프에서
실수로 스팸을 보내는 걸 막기 위해서다.
"""

import json
import urllib.error
import urllib.request

TIMEOUT = 5


def job_finished_message(job, exit_state=None):
    """끝난 job 알림 문구. job 은 parsers.Job."""
    head = f'[{job.job_id}] {job.name}'
    if exit_state:
        head += f' — {exit_state}'
    return f'{head}\n파티션 {job.partition} · GPU {job.gpus}개 · 실행 시간 {job.time_used}'


def quota_blocked_message(diagnosis):
    """대기 원인 알림. services.diagnose_pending() 결과를 그대로 받는다."""
    return diagnosis['headline']


def send_slack(config, text, *, send=False):
    """Slack 으로 텍스트 전송.

    반환: {'sent', 'reason', 'text'}
    send=False 면 보내지 않고 무엇을 보낼지만 돌려준다.
    """
    webhook = config.slack_webhook
    if not webhook:
        return {'sent': False, 'reason': 'no_webhook', 'text': text}
    if not send:
        return {'sent': False, 'reason': 'dry_run', 'text': text}

    payload = json.dumps({'text': text}).encode('utf-8')
    request = urllib.request.Request(
        webhook, data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            ok = 200 <= response.status < 300
        return {'sent': ok, 'reason': 'ok' if ok else 'http_error', 'text': text}
    except (urllib.error.URLError, OSError) as exc:
        # 알림 실패로 TUI 가 죽으면 안 된다.
        return {'sent': False, 'reason': f'error: {exc}', 'text': text}
