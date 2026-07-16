# 백엔드 → 프론트 JSON 계약

백엔드는 세라프에서 데이터를 받아 판단·계산해서 JSON으로 내려준다.
화면을 그리는 건 프론트다. 이 문서에 적힌 키만 쓰면 된다.

프론트는 서버 없이 바로 개발을 시작할 수 있다:

```bash
python -m seraph.dump --mock --section status      # fixture 사용, SSH 불필요
python -m seraph.dump --mock --section diagnose
python -m seraph.dump --mock --section sbatch
python -m seraph.dump --mock --section tutorial
python -m seraph.dump --mock --section announcements   # Slack 공지 (토큰 없으면 mock)
python -m seraph.dump --mock --section history         # 끝난 job 결과
python -m seraph.dump --host ariel --section status    # 실서버
python -m seraph.dump --host ariel --section result --job-id 358796 --days 60
```

인자 없이 부르면 `config.yaml` 의 `connection.mode` 를 따른다 (기본 `mock`).

파이썬에서 직접 부를 때:

```python
from seraph.connection import connect     # config 의 mode 를 따른다
from seraph import services, sbatch, slack, tutorial, history

conn = connect()
snap = conn.snapshot()          # SSH 명령 7개. 5~10초에 한 번만 부른다

history.get_job_history(conn.sacct(days=7))    # 끝난 job. 요청할 때만 부른다

services.get_gpu_status(snap)                  # 클러스터 현황
services.diagnose_pending(snap, snap.me)       # 내 job 이 왜 대기 중인가
services.get_node_availability(snap)           # 지금 쓸 수 있는 노드
services.get_my_usage(snap, snap.me)           # 내 GPU 사용량 / 한도
services.estimate_wait_time(snap, job_id)      # 예상 시작 시각
services.lint_job(snap, gpus=1, paths=[...])   # 제출 전 검사

sbatch.generate_sbatch(snap, name='t', command='python train.py', gpus=1)
tutorial.get_tutorial()                        # 튜토리얼 (mock 위에서 돈다)

client = slack.connect(snap.config)            # 토큰 없으면 mock
slack.get_announcements(client, snap.config.slack_channel)   # Slack 공지 읽기
```

Slack 공지는 세라프 SSH 와 무관하다. `snapshot()` 폴링에 묶지 말고 따로,
더 느리게(예: 1분에 한 번) 부르면 된다.

---

## 반드시 알아야 할 것 다섯 가지

**1. `free_gpus` 는 "지금 실제로 쓸 수 있는" GPU다.**
GPU가 비어 있어도 그 노드에 idle CPU가 없으면 Slurm은 job을 배정하지 않는다.
그런 GPU는 `free_gpus` 에서 빼고 `idle_but_unusable_gpus` 로 따로 보고한다.
`used + free + idle_but_unusable == total` 이 항상 성립한다.

**2. 고성능 GPU와 일반 GPU는 서로 대체할 수 없다.**
고성능은 `ariel-m*/k*/n*` 노드에 있다. 일반 GPU가 30개 남아도 고성능 job은
못 뜬다. `free_high_perf_gpus` 와 `free_standard_gpus` 를 나눠서 준다.

**3. 대기의 대부분은 GPU 부족이 아니라 개인 QOS 한도 초과다.**
관측된 실제 비율: `QOSMaxGRESPerUser` 27건, `Priority` 8건, `Dependency` 3건,
`Resources`(진짜 GPU 부족) 1건. 이걸 구분해서 보여주는 게 이 도구의 핵심이다.
QOS 한도는 **사람마다 다르다** (`grad` 는 GPU 4개 + 고성능 금지,
개인 QOS는 보통 GPU 12개 + 고성능 8개).

**4. 일반 GPU job 은 노드를 지정하지 않으면 제출이 거절된다.**
세라프에는 제출을 검사하는 submit plugin 이 있다. `--gres=gpu:1` 만 쓰면
`SUBMISSION REJECTED: GPU type 'high_perf' is REQUIRED` 로 막힌다.
`generate_sbatch()` 가 v/g 노드를 자동으로 골라 넣으므로 프론트는 신경 쓸 필요
없지만, 튜토리얼에서 사용자에게 알려줄 내용이다. 고성능(`gpu:high_perf:N`)은
노드 지정 없이 통과한다. 아래 모든 규칙은 `sbatch --test-only` 로 실제 확인했다.

**5. 세라프는 클러스터가 3개다. 이 도구는 ariel 만 접속한다.**
ariel(AI 학부+모든 대학원생) / moana(EE/BME/CE 학부) / aurora(SWCON 학부).
접속한 사용자가 ariel 소속이 아니면 `whoami()` 가 "moana 로 가세요" 안내를 준다.
ariel 안에서도 **대학원생은 `*_grad`, 학부생은 `*_ugrad`** 파티션만 쓸 수 있고
(서버가 강제), 학부생은 `ariel-v[6-12]` 노드만 쓴다. `get_partitions()` 의
`can_use` 로 프론트가 회색/자물쇠 처리하면 된다.

---

## ⭐ `placement.find_fastest(conn, snap, gpus=1, hours=2.0, high_perf=False)`

**이 도구의 핵심 기능.** "지금 바로 학습을 시작할 수 있나? 안 되면 어디에 올려야
제일 빨리 시작하나?"에 답한다.

```python
from seraph import placement
r = placement.find_fastest(conn, snap, gpus=1, hours=2)
```

```json
{
  "can_start_now": false,
  "headline": "지금 바로는 어렵습니다. 가장 빨리 시작하는 곳은 batch_grad / ariel-v7 (GPU) — 약 4시간 11분 뒤입니다.",
  "best": {
    "partition": "batch_grad", "node": "ariel-v7", "high_perf": false,
    "start": "2026-07-13T21:30:00",
    "wait_seconds": 15060, "wait_text": "약 4시간 11분 뒤", "starts_now": false,
    "time_limit_seconds": null,
    "script": "#!/bin/bash\n#SBATCH --partition=batch_grad\n..."
  },
  "options": [ {...일반...}, {"high_perf": true, "node": "ariel-n1", ...} ],
  "blocked": [ {"partition": "...", "reason": "..."} ],
  "requested": {"gpus": 1, "hours": 2, "high_perf": false}
}
```

- `headline` 은 완성된 한 문장이다. **화면에 그대로 크게 띄우면 된다.**
- `can_start_now` 로 색을 정한다 (초록=지금 가능, 노랑=기다려야 함).
- `options` 는 **빠른 순 정렬**. `high_perf` 로 일반/고성능을 구분해 표시하면 된다.
- `best.script` 는 **바로 제출할 수 있는 sbatch 스크립트**다. 지금 못 시작해도
  올려두면 그 시각에 시작한다 — 사용자가 확인 후 제출하면 된다.
- `blocked` 는 아예 못 내는 후보와 이유 (계정 불일치, QOS 한도 등).

### 두 가지 정책이 들어 있다

**① `debug_*` 는 학습 추천에서 뺀다.** 거긴 디버깅·짧은 테스트용이다(4시간 제한).
"지금 바로 시작된다"는 이유로 학습을 debug 로 몰면 정작 디버깅하려는 사람이 못 쓴다.
`config.yaml` 의 `placement.exclude_partitions` 로 조정 가능.

**② 고성능 GPU(m/k/n)는 자동 추천하지 않는다.** 따로 **신청해서 받은 사람만** 쓰는
자원이기 때문이다(QOS 90개 중 40개가 `high_perf=0` 으로 아예 금지. 기본 `grad`/`ugrad`
도 0). 도구가 임의로 몰아주면 안 된다. 사용자가 `high_perf=True` 로 명시할 때만 쓰고,
자격이 없으면 `blocked` 에 "이 QOS 는 고성능 노드를 쓸 수 없습니다" 로 알려준다.

프론트가 고성능 옵션을 노출하려면 `get_my_usage()` 의 `high_perf_limit` 을 먼저
확인할 것 — `0` 이면 그 사용자는 신청을 안 한 것이니 아예 안 보여주는 게 낫다.

### ⚠️ 왜 이게 필요한가 — `free_gpus` 로는 알 수 없다

실서버에서 실제로 관측한 상황:

```
ariel-v6 : GPU 8개 중 7개 여유, CPU 52개 여유       ← 다 비어 보인다
그런데 batch_grad 에 내면 → "3시간 10분 뒤에나 시작"  ← 못 쓴다!
같은 시각 debug_grad 에 내면 → "지금 즉시 시작"       ← 여긴 된다
```

우선순위 높은 대기 job 이 그 자원을 잡아두고 있어서다. **여유 GPU 를 세어서
"지금 가능"이라고 말하면 틀린다.** 그래서 이 함수는 `sbatch --test-only` 로
Slurm 에게 직접 물어본다(우선순위·backfill·QOS 를 다 계산한 답). **job 은 제출되지
않는다.**

### 호출 규칙

`snapshot()` 폴링(5~10초)에 **넣지 말 것.** Slurm 에 후보 수만큼(보통 2~6회)
질의하므로, 사용자가 "지금 되나?" 를 눌렀을 때나 화면 진입 시에만 부른다.
(실측: 5회 질의에 1초 미만이라 가볍긴 하다.)

`hours` 를 바꾸면 결과가 달라진다 — `debug_*` 는 4시간 제한이라 12시간 학습은
후보에서 자동으로 빠진다. 프론트는 사용자에게 GPU 수와 학습 시간을 물어보고
넘기면 된다.

---

## `whoami(snap)` — 접속한 사용자가 누구인가

화면 상단과 "여기 아님" 안내에 쓴다.

```json
{
  "user": "user01", "account": "grad", "qos": "qos_user01_2026_1",
  "is_undergrad": false, "position": "grad", "major": null,
  "cluster": "ariel", "on_primary": true,
  "default_partition": "batch_grad",
  "cluster_notice": ""
}
```

- `is_undergrad` 로 화면을 학부/대학원 모드로 나눌 수 있다. (모르면 `null`)
- **`on_primary` 가 `false` 면 이 사용자는 ariel 소속이 아니다.** `cluster_notice`
  에 "당신(CE 학부생)은 moana 를 쓰세요..." 같은 완성된 안내가 들어온다. 그걸 크게
  띄우고 나머지 실시간 화면은 참고용으로만 보여주면 된다.
- `default_partition` 은 이 사용자에게 맞는 기본 파티션(대학원=batch_grad,
  학부=batch_ugrad). `get_gpu_status` 등에 partition 을 안 넘기면 이게 쓰인다.

## `get_partitions(snap)` — 파티션별 사용 가능 여부

```json
{
  "batch_grad":  {"name": "batch_grad", "time_limit_seconds": null,
                  "node_count": 23, "is_default": false, "can_use": true},
  "batch_ugrad": {"... ", "can_use": false},
  "admin":       {"... ", "can_use": false}
}
```

`can_use` 로 색을 정한다: `true` → 파랑/선택 가능, `false` → 회색+자물쇠.
숨기지 말고 회색으로 남기는 걸 권장(왜 못 쓰는지 보이게).

## `clusters.overview()` — 3개 클러스터 전체 그림

튜토리얼/안내용. 실시간 아님.

```json
{
  "primary": "ariel",
  "note": "이 도구는 ariel 만 실시간 조회합니다. 나머지는 안내만 제공합니다.",
  "clusters": {
    "ariel":  {"host": "ariel.khu.ac.kr",  "total_gpus": 182, "allowed": "AI 학부생 + 모든 대학원생", "connectable": true},
    "moana":  {"host": "moana.khu.ac.kr",  "total_gpus": 121, "allowed": "EE/BME/CE 학부생", "connectable": false},
    "aurora": {"host": "aurora.khu.ac.kr", "total_gpus": 62,  "allowed": "SWCON 학부생", "connectable": false}
  }
}
```

---

## `get_gpu_status(snap, partition='batch_grad')`

클러스터 현황. 대시보드 상단용.

```json
{
  "partition": "batch_grad",
  "total_gpus": 181,
  "used_gpus": 150,
  "free_gpus": 30,
  "idle_but_unusable_gpus": 1,
  "free_high_perf_gpus": 1,
  "free_standard_gpus": 29,
  "total_high_perf_gpus": 39,
  "utilization": 0.829,
  "running_jobs": 72,
  "pending_jobs": 39,
  "pending_by_reason": {
    "QOSMaxGRESPerUser": 27, "Priority": 8, "Dependency": 3, "Resources": 1
  },
  "cpu_starved_nodes": ["ariel-v3"],
  "nodes": [ Node, ... ]
}
```

`utilization` 은 0~1 실수. 게이지에 그대로 쓰면 된다.
`cpu_starved_nodes` 가 비어 있지 않으면 "GPU N개는 CPU가 없어 사용 불가"라고
각주를 달아주면 좋다.

### Node

```json
{
  "name": "ariel-v1",
  "state": "mixed",              // idle | mixed | allocated | drained | down
  "partitions": ["admin", "debug_grad", "batch_grad"],
  "total_gpus": 8,
  "used_gpus": 1,
  "free_gpus": 7,                // 숫자상 여유
  "usable_gpus": 7,              // 실제 배정 가능 (CPU 없으면 0)
  "is_high_perf": false,
  "broken_gpus": 0,              // ariel-m1 에 1개 있다
  "idle_cpus": 56,
  "total_cpus": 64,
  "free_mem_mb": 582454,
  "schedulable": true,           // drained/down 이면 false
  "cpu_starved": false           // GPU 는 남는데 CPU 가 없음
}
```

막대그래프는 `used_gpus / total_gpus` 로 그리고, `cpu_starved` 나
`!schedulable` 인 노드는 회색 처리하면 된다.

---

## `diagnose_pending(snap, user, partition='batch_grad')`

**이 도구의 핵심.** "GPU는 남는데 왜 내 job이 안 돌지?"에 답한다.

```json
{
  "user": "user25",
  "usage": { ...get_my_usage 와 동일... },
  "cluster_free_gpus": 35,
  "pending_count": 14,
  "quota_blocked_count": 14,
  "headline": "대기 중인 14개 중 14개는 GPU 부족이 아니라 본인 고성능 GPU 할당량(8/8) 때문입니다. 클러스터에는 GPU 가 35개 놀고 있습니다.",
  "jobs": [
    {
      "job_id": "366126",
      "name": "base_soup-cheese_alldata",
      "reason": "QOSMaxGRESPerUser",          // Slurm 원문
      "reason_text": "본인 GPU 할당량을 모두 사용 중",  // 사람이 읽을 말
      "requested_gpus": 1,
      "estimated_start": "2026-07-10T21:18:28",  // null 일 수 있음
      "blocked_by_quota": true,
      "quota_kind": "high_perf_gpu",          // gpu | high_perf_gpu | running_jobs | null
      "advice": "클러스터에 GPU 가 35개 남아 있습니다. 막고 있는 건 본인의 고성능 GPU 할당량입니다 (8/8 사용 중). ..."
    }
  ]
}
```

`headline` 은 완성된 한 문장이다. 화면 상단에 그대로 띄우면 된다.
`quota_kind` 는 **어느 한도**에 걸렸는지다. 총 GPU가 8/12로 여유가 있는데도
고성능 한도 8/8 때문에 막히는 경우가 실제로 있으므로, "GPU 초과"라고
뭉뚱그리면 안 된다.

`blocked_by_quota` 가 `true` 인 job은 노란색, `reason == "Resources"` 인 job만
빨간색으로 하면 의미가 맞는다.

---

## `get_my_usage(snap, user)`

한도 대비 사용량. 게이지 3개(GPU / 고성능 / 실행 중 job)로 그리면 된다.
`*_limit` 은 한도가 없으면 `null`.

```json
{
  "user": "user01",
  "qos": "qos_user01_2026_1",
  "gpus_in_use": 4,          "gpus_limit": 12,
  "high_perf_in_use": 4,     "high_perf_limit": 8,
  "running_jobs": 1,         "running_jobs_limit": 12,
  "submitted_jobs": 1,       "submit_jobs_limit": 24,
  "pending_jobs": 0
}
```

`high_perf_limit == 0` 이면 그 사용자는 고성능 노드를 아예 못 쓴다
(기본 `grad` QOS가 그렇다). 그 게이지는 "사용 불가"로 표시하는 게 맞다.

---

## `get_node_availability(snap, partition, need_gpus=1, high_perf=False)`

지금 job을 받을 수 있는 노드를 여유 GPU 많은 순으로. `Node` 배열.
CPU가 없는 노드와 drained 노드는 빠져 있다. 그대로 위에서부터 추천하면 된다.
`high_perf=True` 인데 QOS가 고성능을 금지하면 빈 배열이 온다.

---

## `estimate_wait_time(snap, job_id)`

```json
{
  "job_id": "366126", "found": true, "state": "PD",
  "estimated_start": "2026-07-10T21:18:28",   // null 가능
  "confidence": "low",                        // medium | low | unknown
  "source": "squeue --start",
  "reason": "QOSMaxGRESPerUser",
  "note": "할당량으로 막힌 job 은 Slurm 추정이 부정확합니다. ..."
}
```

Slurm이 계산한 값을 쓴다. 직접 계산하지 않는다.
**`confidence` 를 반드시 같이 보여줄 것.** 쿼터로 막힌 job은 같은 사용자의
대기 job 전부에 동일한 시각이 찍히므로 숫자를 그대로 믿으면 안 된다.
`confidence != "medium"` 이면 "약 21:18 (추정 부정확)" 처럼 흐리게 표시할 것.
`found: false` 면 나머지 키는 없다.

---

## `lint_job(snap, partition, gpus, high_perf, paths, time_limit, node)`

제출 전 검사.

```json
{
  "ok": false,
  "problems": [
    {"level": "block", "code": "HIGH_PERF_FORBIDDEN", "message": "..."},
    {"level": "warn",  "code": "LOGIN_NODE_BUSY",     "message": "..."}
  ]
}
```

`level` 은 `block`(제출 막기) 또는 `warn`(경고만). `ok` 는 block이 하나도
없을 때만 `true`. 코드 목록:

| code | level | 뜻 |
|---|---|---|
| `HIGH_PERF_FORBIDDEN` | block | 이 QOS는 고성능 노드 사용 불가. 내면 영원히 대기 |
| `OVER_GPU_LIMIT` | block | QOS의 GPU 한도 초과. job이 절대 시작 안 됨 |
| `OVER_HIGH_PERF_LIMIT` | block | QOS의 고성능 GPU 한도 초과 |
| `OVER_TIME_LIMIT` | block | 파티션 시간 제한 초과 (`debug_*` 는 4시간) |
| `UNKNOWN_PARTITION` | block | 없는 파티션 |
| `UNKNOWN_NODE` | block | 없는 노드 |
| `NODE_TYPE_MISMATCH` | block | GRES 타입과 노드 종류 불일치 (`gpu:1` + `ariel-k1`) |
| `NODE_UNAVAILABLE` | block | 노드가 drained/down |
| `NO_ELIGIBLE_NODE` | block | 그 GPU 수를 올릴 일반 노드가 없음 |
| `PARTITION_NOT_ALLOWED` | block | 내 계정으로 못 쓰는 파티션 (학부/대학원 불일치) |
| `UNDERGRAD_NODE_RESTRICTED` | block | 학부생이 `ariel-v[6-12]` 밖 노드 지정 |
| `BLOCKED_PATH` | block | 금지 경로 (`config.yaml` 의 `lint.blocked_paths`) |
| `NODE_BUSY` | warn | 지정한 노드에 여유 GPU 없음. 제출은 되지만 기다림 |
| `OVER_POLICY_WALLTIME` | warn | 권장 최대 실행 시간(6일) 초과. 서버는 막지 않음 |
| `OVER_POLICY_GPU_DEFAULT` | warn | 기본 GPU 한도 초과(학부 1/대학원 4). 상향 신청 필요 |
| `OVER_POLICY_GPU_MAX` | warn | 권장 최대 GPU 초과 |
| `DISCOURAGED_PATH` | warn | 권장하지 않는 경로 (`lint.warn_paths`) |
| `LOGIN_NODE_BUSY` | warn | 로그인 노드 load ≥ 8 |

`block` 은 세라프가 실제로 거절하는 것(계정/파티션, 노드, QOS 한도).
`OVER_POLICY_*` 는 서버가 강제하지 않는 **권장 정책** 경고라 `warn` 이다(제출은 됨).

경로 규칙은 `config.yaml` 에서 온다. 코드에 박혀 있지 않다.
디렉터리 경계를 지켜 비교하므로 `/home/` 규칙이 `/homework/data` 를 잡지 않는다.

한계: 사용자가 터미널에서 직접 `sbatch` 를 치면 우리가 막을 수 없다.
이 도구는 "감시자"가 아니라 "안전하게 제출하는 통로"다.

---

## `generate_sbatch(snap, name, command, gpus, high_perf, node, paths, ...)`

제출용 스크립트를 만든다. 내부에서 `lint_job()` 을 돌리고, **block 이 하나라도
있으면 스크립트를 만들지 않는다** (`script: null`). 절대 시작되지 않을 job 을
예쁘게 만들어 주는 건 도움이 안 되기 때문이다.

```json
{
  "ok": true,
  "script": "#!/bin/bash\n#SBATCH --job-name=demo-train\n...",
  "node": "ariel-v6",
  "auto_selected_node": true,
  "lint": { "ok": true, "problems": [] },
  "command_preview": "sbatch demo-train.sh"
}
```

`command` 는 문자열 또는 리스트. 리스트면 `shlex` 로 안전하게 인용한다.
일반 GPU 인데 `node` 를 안 넘기면 v/g 노드를 자동으로 골라 `auto_selected_node:
true` 로 알려준다. 사용자가 노드를 직접 고르게 하려면
`sbatch.suggest_node(snap, gpus=N)` 로 후보를 얻어 보여주면 된다.

`ok: true` 여도 `lint.problems` 에 `warn` 이 들어 있을 수 있다. 그대로 보여줄 것.

생성된 스크립트는 실제 세라프에서 `sbatch --test-only` 로 검증했다 (일반 1/2/8개,
노드 명시, 고성능 1/4개 — 전부 통과).

---

## `tutorial.get_tutorial(snap=None)`

튜토리얼 내용. **항상 mock 스냅샷 위에서 돈다** (`mode: "practice"`). 실제 서버에
아무 영향이 없다. 프론트는 화면·단계 진행·피드백만 만들면 된다.

```json
{
  "mode": "practice",
  "user": "user01",
  "steps": [
    {
      "id": "ssh",
      "title": "SSH 로 세라프에 접속하기",
      "body": "...",
      "commands": ["ssh -p 30080 <사용자명>@ariel.khu.ac.kr"],
      "pitfall": "VSCode 의 Remote-SSH 로 붙는 것은 권장되지 않습니다...",
      "check": null
    }
  ],
  "sample_status": { ...get_gpu_status 와 동일... }
}
```

단계 id 순서: `ssh` → `quota` → `status` → `data` → `submit` → `result`.
`pitfall` 은 도구가 막아줄 수 없는 것들(VSCode-SSH 등)이라 교육으로 커버한다.
`check` 는 그 단계에서 보여주면 좋을 섹션 이름이다(없으면 `null`).

---

## `history.get_job_history(sacct_text, limit=20)`

끝난 job 의 결과와 실패 원인. `sacct` 는 폴링 스냅샷에 없다 — 느리고 자주 바뀌지
않으므로 **사용자가 요청할 때만** 부른다.

```python
from seraph import history
text = conn.sacct(days=7)          # SSH 명령 1회
history.get_job_history(text, limit=20)
```

```json
{
  "headline": "끝난 job 623개 중 216개 성공 (35%). 실패한 job 이 GPU 176.0시간을 썼습니다.",
  "stats": {
    "total": 623, "succeeded": 216, "failed": 407, "success_rate": 0.347,
    "by_state": {"CANCELLED": 309, "COMPLETED": 216, "FAILED": 89,
                 "TIMEOUT": 7, "OUT_OF_MEMORY": 2},
    "wasted_gpu_hours": 176.0, "total_gpu_hours": 480.9
  },
  "jobs": [
    {
      "job_id": "358796", "name": "gr00t_libero",
      "state": "OUT_OF_MEMORY", "raw_state": "OUT_OF_MEMORY",
      "exit_code": 0, "signal": 125, "cancelled_by": null,
      "start": "2026-06-27T03:12:41", "end": "2026-06-27T03:50:39",
      "elapsed_seconds": 2278, "time_limit_seconds": 86400,
      "partition": "batch_grad", "nodes": "ariel-k1",
      "gpus": 2, "high_perf_gpus": 2,
      "req_mem_mb": 65536, "max_rss_mb": 59711,
      "succeeded": false,
      "reason": "out_of_memory",
      "reason_text": "메모리 부족으로 강제 종료",
      "advice": "메모리가 부족했습니다. 요청한 64.0GB 중 58.3GB(91%)까지 쓰고 죽었습니다. --mem 을 늘리거나..."
    }
  ]
}
```

- **최신순**. `stats` 는 `limit` 과 무관하게 전체 기준이다.
- `state` 는 정규화된 값이다. 원문(`"CANCELLED by 20301"`)은 `raw_state` 에 있다.
- `wasted_gpu_hours` 는 실패한 job 이 태운 GPU 시간이다. 이걸 보여주면 사용자가
  실패를 방치하지 않는다.
- 아직 안 끝난 job(RUNNING/PENDING)은 들어 있지 않다.

`reason` 목록:

| reason | 뜻 |
|---|---|
| `ok` | 정상 종료 |
| `out_of_memory` | 메모리 부족. **종료 코드가 0 이라 성공으로 오해하기 쉽다** |
| `timeout` | 시간 제한 초과 |
| `command_not_found` | 종료 코드 127. conda 환경/경로 문제 |
| `script_error` | 스크립트가 0 아닌 코드로 종료 |
| `killed_by_signal` | 시그널로 종료 |
| `cancelled_by_user` | 사용자가 `scancel` |
| `cancelled_by_admin` | 관리자/시스템이 취소 (UID 0) |
| `node_failure` | 노드 장애. 재제출하면 된다 |
| `unknown` | 그 외 |

실패한 job 은 `advice` 를 그대로 띄우면 된다. 성공이면 `advice` 는 빈 문자열이다.

### `history.get_job_result(sacct_text, job_id)`

job 하나만. 위 항목과 같은 모양에 `found` 가 붙는다.
없으면 `{"job_id": "999999", "found": false}` 만 온다.

---

## `slack.get_announcements(client, channel, limit=10)`

Slack 공지 채널을 읽어 TUI 에 뿌린다.

```python
from seraph import slack
client = slack.connect(snap.config)     # 토큰 없으면 자동으로 mock
slack.get_announcements(client, snap.config.slack_channel, snap.config.slack_limit)
```

```json
{
  "ok": true,
  "channel": "공지",
  "count": 5,
  "announcements": [
    {
      "ts": "1752120000.000100",
      "posted_at": "2025-07-10T13:00:00+09:00",
      "author": "세라프 관리자",
      "text": "[긴급] ariel-m1 노드 점검\n\nGPU 1개 고장으로 ...",
      "summary": "[긴급] ariel-m1 노드 점검 GPU 1개 고장으로 ...…",
      "is_bot": false,
      "reply_count": 2,
      "reactions": [{"name": "eyes", "count": 7}]
    }
  ]
}
```

- **최신순**으로 온다. `ts` 가 고유 ID다(읽음 표시에 쓰면 된다).
- `text` 는 **평문**이다. Slack 의 `<@U123>`, `<url|라벨>`, `&lt;` 같은 표기를 전부
  풀어서 준다. 그대로 출력하면 된다. 목록에는 `summary`(한 줄) 를 쓴다.
- `posted_at` 은 KST(`+09:00`) ISO 8601.
- "OOO 님이 채널에 참여했습니다" 같은 시스템 메시지는 걸러져 있다. 봇이 올린
  자동 공지는 남긴다(`is_bot: true`).

**실패해도 예외를 던지지 않는다.** 공지를 못 읽는다고 TUI 가 죽으면 안 되기 때문이다.

```json
{"ok": false, "channel": "공지", "error": "not_in_channel",
 "message": "봇이 채널에 없습니다. 공지 채널에 봇을 초대하세요 (/invite @봇이름).",
 "announcements": []}
```

`error` 는 `invalid_auth` | `not_authed` | `missing_scope` | `not_in_channel` |
`channel_not_found` | `ratelimited` | `network_error` 등. `message` 는 사용자에게
그대로 보여줄 수 있는 한국어 안내다. `ok: false` 면 공지 패널에 이 문구를 띄우고
나머지 화면은 정상 동작시키면 된다.

### 토큰 설정

읽기에는 Slack **Web API 토큰**이 필요하다. Incoming Webhook 으로는 읽을 수 없다.

```bash
export SERAPH_SLACK_TOKEN='xoxb-...'
```

- 필요한 scope: `channels:history`, `channels:read`, `users:read`
  (비공개 채널이면 `groups:history`, `groups:read`)
- 봇 토큰(`xoxb`)이면 그 봇을 공지 채널에 **초대**해야 한다 (`/invite @봇이름`).
- 토큰은 **환경변수로만** 읽는다. `config.yaml` 에 적어도 무시한다(커밋되므로).
- 토큰이 없으면 저장된 mock 공지를 보여준다. 프론트는 토큰 없이 개발할 수 있다.

읽어올 채널은 `config.yaml` 의 `slack.channel` (이름 또는 채널 ID).

---

## `notify.send_slack(config, text, send=False)`

이건 **보내는** 쪽이다. 공지 읽기와 무관하다(위의 `slack` 패키지를 쓸 것).
지금 TUI 에서는 쓰지 않는다. job 이 끝났을 때 알림을 보내고 싶으면 그때 쓴다.

```json
{"sent": false, "reason": "no_webhook", "text": "..."}
```

`reason` 은 `ok` | `no_webhook` | `dry_run` | `http_error` | `error: ...`.
**기본은 dry-run 이다.** `send=True` 를 넘겨야 실제로 전송한다.
webhook URL 은 `SERAPH_SLACK_WEBHOOK` 환경변수로 준다(읽기용 토큰과 다른 값이다).

---

## 설정 (`config.yaml`)

코드를 고치지 않고 여기만 바꾼다. 파일이 없으면 기본값으로 동작한다.

```yaml
connection:
  mode: mock              # mock | ssh   <- 이거 하나로 서버/가짜를 바꾼다
  host: ariel.khu.ac.kr
  port: 30080             # 교내는 22
  poll_interval_seconds: 7
  login_node_load_limit: 8.0
cluster:
  default_partition: batch_grad
lint:
  blocked_paths: ["/data/"]
  warn_paths: ["/home/"]
storage:
  data_root: /data
  local_datasets_root: /local_datasets
```

`python -m seraph.dump --section config` 로 현재 값을 확인할 수 있다.

경로 정책(`blocked_paths` / `warn_paths`)은 팀이 정한 값이지 세라프 운영 규칙을
그대로 옮긴 게 아니다. **조교/관리자에게 확인한 뒤 확정할 것.**

---

## 폴링 규칙

`snapshot()` 한 번이 SSH 명령 7개다. **5~10초에 한 번만** 부른다
(`config.yaml` 의 `poll_interval_seconds`).
`services.should_poll(snap)` 가 `False` 면(로그인 노드 load ≥ 8) 쉰다.
`snap.load` 에 1분 load average가 들어 있다.
