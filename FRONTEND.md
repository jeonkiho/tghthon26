# 프론트 담당에게 — 백엔드 사용법

백엔드는 세라프에서 데이터를 받아 **판단·계산까지 끝낸 JSON**을 준다.
프론트는 그 JSON을 받아 **화면에 그리기만** 하면 된다. 세라프 접속이나 명령어
파싱은 신경 쓸 필요 없다.

전체 상세 계약은 [CONTRACT.md](CONTRACT.md)에 있다. 이 문서는 시작용 요약이다.

---

## 1. 세라프 계정 없이 바로 시작할 수 있다

백엔드는 `mock` 모드가 기본이라, 서버 접속 없이 **저장된 실제 데이터**로 돈다.
파이썬만 있으면 된다.

```bash
cd tghthon26
python3 -m seraph.dump --mock --section status      # GPU 현황 JSON이 찍힌다
python3 -m seraph.dump --mock --section diagnose --user user25
python3 -m seraph.dump --mock --section whoami
```

각 `--section`이 화면 하나에 대응한다고 보면 된다. 나오는 JSON 모양을 보고
화면을 짜면 된다. **숫자가 안 바뀌는 건 정상**(저장된 스냅샷이라서). 실서버는
나중에 `--host ariel`로 붙이면 7초마다 갱신된다.

---

## 2. 파이썬에서 부르는 법 (프론트가 파이썬일 때)

```python
from seraph.connection import connect
from seraph import services, sbatch, history, slack, clusters

conn = connect()            # config.yaml의 mode를 따름 (기본 mock)
snap = conn.snapshot()      # ← 백엔드에서 데이터 한 번 받아옴. 5~10초에 한 번만!

# 받은 snap으로 필요한 걸 뽑는다. 전부 dict(JSON)를 돌려준다.
me = snap.me
services.whoami(snap)                     # 내가 누구인가
services.get_gpu_status(snap)             # 클러스터 현황
services.diagnose_pending(snap, me)       # 내 job이 왜 대기 중인가 (핵심)
services.get_my_usage(snap, me)           # 내 GPU 사용량/한도
services.get_node_availability(snap)      # 지금 쓸 수 있는 노드
services.get_partitions(snap)             # 파티션별 사용 가능 여부
```

동작하는 예시가 [examples/demo_frontend.py](examples/demo_frontend.py)(한 번 그리기)와
[examples/tui.py](examples/tui.py)(실시간 갱신)에 있다. 그대로 실행해봐도 된다.

> **핵심 규칙 하나:** `conn.snapshot()`은 세라프에 SSH로 명령 7개를 날린다.
> 화면 갱신마다 부르지 말고, **한 번 불러서 여러 함수에 재사용**하고, 5~10초에
> 한 번만 새로 부를 것. `services.should_poll(snap)`이 `False`면 쉰다(서버 과부하).

---

## 3. 화면별로 어떤 함수를 쓰면 되나

| 만들 화면 | 부를 함수 | 핵심 필드 |
|---|---|---|
| 상단 바 (내 정보) | `whoami(snap)` | `user`, `is_undergrad`, `cluster`, `cluster_notice` |
| GPU 현황 대시보드 | `get_gpu_status(snap)` | `used_gpus`/`total_gpus`, `free_gpus`, `pending_jobs` |
| **대기 원인 진단 (메인 기능)** | `diagnose_pending(snap, me)` | `headline` (완성된 한 문장) |
| 내 사용량 게이지 | `get_my_usage(snap, me)` | `gpus_in_use`/`gpus_limit` 등 |
| 노드 추천 목록 | `get_node_availability(snap)` | `name`, `usable_gpus` |
| 파티션 선택 | `get_partitions(snap)` | `can_use` (색 결정) |
| job 제출 폼 | `sbatch.generate_sbatch(...)` | `script`, `ok`, `lint.problems` |
| 제출 전 검사 | `services.lint_job(snap, ...)` | `ok`, `problems[]` |
| 끝난 job 결과 | `history.get_job_history(conn.sacct())` | `headline`, `jobs[].advice` |
| 예상 대기 시간 | `estimate_wait_time(snap, job_id)` | `estimated_start`, `confidence` |
| Slack 공지 | `slack.get_announcements(...)` | `announcements[].text` |
| 튜토리얼 | `tutorial.get_tutorial()` | `steps[]` |
| 3클러스터 안내 | `clusters.overview()` | `clusters` |

---

## 4. 화면 짤 때 꼭 알아야 할 규칙 (UI가 바뀌는 것들)

이건 백엔드가 "판단"해서 필드로 내려주니, 프론트는 그 필드만 보고 그리면 된다.

**① 대기의 대부분은 GPU 부족이 아니라 개인 할당량 초과다.**
`diagnose_pending()`의 `headline`이 이 프로젝트의 킬러 기능이다. 완성된 한국어
문장이라 **그대로 크게 띄우면** 된다. 예: *"대기 중인 14개 중 14개는 GPU 부족이
아니라 본인 고성능 GPU 할당량(8/8) 때문입니다. 클러스터엔 36개가 놀고 있습니다."*
- job별로 `blocked_by_quota: true`면 노란색, `reason == "Resources"`(진짜 GPU 부족)면 빨간색.

**② 세라프는 서버가 3개인데 이 도구는 ariel만 본다.**
`whoami()`의 `on_primary`가 `false`면 이 사용자는 다른 서버(moana/aurora) 소속이다.
`cluster_notice`에 *"당신은 moana를 쓰세요..."* 안내가 들어오니 크게 띄우고,
실시간 화면은 참고용으로만 보여준다.

**③ 파티션·노드는 사람마다 쓸 수 있는 게 다르다.**
`get_partitions()`의 `can_use`로 색을 정한다: `true` → 파랑/선택 가능,
`false` → 회색+자물쇠(숨기지 말고 회색으로 남겨서 왜 못 쓰는지 보이게).
대학원생은 `*_grad`, 학부생은 `*_ugrad`만 쓸 수 있다.

**④ GPU 막대는 `usable_gpus`로 그린다.**
`free_gpus`가 아니라 `usable_gpus`가 "지금 실제로 쓸 수 있는" 수다. GPU가 비어도
그 노드에 CPU가 없으면 못 쓰기 때문. drained 노드나 `cpu_starved` 노드는 회색.

**⑤ 예상 시간엔 `confidence`를 꼭 같이 보여준다.**
`estimate_wait_time()`의 `confidence`가 `"low"`/`"unknown"`이면 숫자를 흐리게.
할당량으로 막힌 job은 Slurm 추정이 부정확하다.

**⑥ 실패해도 화면이 죽으면 안 된다.**
Slack이나 job 기록 함수는 실패해도 예외를 던지지 않고 `{"ok": false, "message": ...}`
를 준다. `ok`가 `false`면 그 패널에 `message`를 띄우고 나머지 화면은 정상 동작.

---

## 5. 역할 경계

| 백엔드가 하는 것 | 프론트가 하는 것 |
|---|---|
| 세라프 접속, 명령 실행, 파싱 | 화면 배치, 색, 테두리 |
| 계산 (여유 GPU, 대기 원인, 예상 시간) | 막대·게이지·표 그리기 |
| 판단 (쓸 수 있냐, 막아야 하냐) | 방향키 이동, 탭 전환, 새로고침 |
| 완성된 안내 문장 만들기 | 그 문장을 어디에 띄울지 |

한 줄로: **"빨갛게 칠할지 말지는 프론트가 정하되, 무엇이 문제인지는 백엔드가 알려준다."**

---

## 6. 자주 나올 질문

- **Q. 백엔드 코드를 봐야 하나?** 아니다. [CONTRACT.md](CONTRACT.md)의 JSON 모양만
  보면 된다. 함수 이름과 반환 필드가 거기 다 있다.
- **Q. 세라프 계정이 있어야 개발되나?** 아니다. `--mock`으로 저장된 실제 데이터로
  개발하면 된다. 실서버는 마지막에 붙이면 된다.
- **Q. 어떤 언어로 만들든 되나?** 파이썬이면 함수를 직접 부르는 게 제일 간단하다.
  다른 언어면 `python -m seraph.dump --section X`를 실행해 stdout의 JSON을 파싱하면
  된다(느리므로 실시간엔 데몬 모드가 필요 — 필요하면 백엔드에 요청).

막히면 백엔드 담당(도운)에게 물어보면 된다.
