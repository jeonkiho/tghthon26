"""운영체제 알림 (브라우저를 닫아도 닿아야 한다).

브라우저 알림만으로는 탭을 닫는 순간 아무것도 오지 않는다. 그런데 학습은
몇 시간씩 걸리고, 그동안 브라우저를 열어두라고 요구하는 건 "터미널을 띄워두고
기다리라"와 같은 말이다. 백엔드는 어차피 사용자 PC 에서 돌고 있으니 여기서
직접 띄운다.

윈도우는 PowerShell 로 WinRT 토스트를 부른다 — **새 의존성이 없다.** 이 기능
하나 때문에 팀원 전부가 pip install 을 더 하게 만들 이유가 없다.

문구는 base64 로 넘긴다. 사용자 작업 이름이나 서버 오류 메시지가 그대로 셸
스크립트에 들어가면 따옴표 하나로 임의 명령이 되기 때문이다. 인코딩해서 넘기면
문구가 무엇이든 데이터로만 남는다.
"""

from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import sys

log = logging.getLogger("seraph_gui.notify")

TIMEOUT_SECONDS = 15

# 등록된 AppUserModelID 가 있어야 토스트가 뜬다. PowerShell 것을 빌려 쓴다 —
# 우리 앱을 시작 메뉴에 등록시키지 않으려고(그건 사용자 PC 를 건드리는 일이다).
_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_WINDOWS_SCRIPT = """
$ErrorActionPreference = 'Stop'
$title = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{title}'))
$body  = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{body}'))
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($xml.CreateTextNode($title)) | Out-Null
$nodes.Item(1).AppendChild($xml.CreateTextNode($body)) | Out-Null
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{aumid}').Show(
    [Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def available() -> bool:
    """이 PC 에서 OS 알림을 띄울 수 있나. 화면에서 '왜 안 오는지'를 말해주려고 있다."""
    if sys.platform == "win32":
        return bool(_find_powershell())
    if sys.platform == "darwin":
        return bool(shutil.which("osascript"))
    return bool(shutil.which("notify-send"))


def _find_powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def send(title: str, body: str) -> bool:
    """알림 하나를 띄운다. 실패해도 절대 예외를 올리지 않는다.

    알림이 안 뜨는 것보다 알림 때문에 감시 루프가 죽는 게 훨씬 나쁘다 — 그러면
    그 뒤의 작업 완료를 전부 놓친다.
    """
    try:
        if sys.platform == "win32":
            return _send_windows(title, body)
        if sys.platform == "darwin":
            return _send_mac(title, body)
        return _send_linux(title, body)
    except Exception as exc:                      # noqa: BLE001 - 알림은 실패해도 조용해야 한다
        log.warning("OS 알림 실패: %s", exc)
        return False


def _run(argv: list[str]) -> bool:
    result = subprocess.run(
        argv,
        capture_output=True,
        timeout=TIMEOUT_SECONDS,
        # 셸을 거치지 않는다. 인자를 리스트로 넘기면 따옴표 해석이 아예 일어나지 않는다.
        shell=False,
    )
    if result.returncode != 0:
        log.warning("OS 알림 실패 (rc=%s): %s", result.returncode,
                    result.stderr.decode("utf-8", errors="replace").strip()[:300])
        return False
    return True


def _send_windows(title: str, body: str) -> bool:
    powershell = _find_powershell()
    if not powershell:
        return False
    script = _WINDOWS_SCRIPT.format(title=_b64(title), body=_b64(body), aumid=_AUMID)
    return _run([
        powershell, "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-Command", script,
    ])


def _send_mac(title: str, body: str) -> bool:
    # osascript 도 문자열을 그대로 넣으면 따옴표가 문제가 된다. base64 로 넘긴다.
    script = (
        'set t to do shell script "echo {title} | base64 --decode"\n'
        'set b to do shell script "echo {body} | base64 --decode"\n'
        'display notification b with title t'
    ).format(title=_b64(title), body=_b64(body))
    return _run(["osascript", "-e", script])


def _send_linux(title: str, body: str) -> bool:
    # notify-send 는 인자를 그대로 받는다. shell=False 라 해석되지 않는다.
    return _run(["notify-send", "--app-name=SERAPH", title, body])
