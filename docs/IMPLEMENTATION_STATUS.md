# SERAPH GUI 구현 상태

기준일: 2026-07-16  
기준 버전: MVP 1.1.1 · SERAPH 튜토리얼 준수판

## 완료된 기능

| 영역 | 구현 내용 |
|---|---|
| 로컬 실행 | FastAPI·React를 사용자 PC에서 실행, `127.0.0.1:8765` 고정 |
| 연결 | GUI에서 실제 사용자명·호스트·포트 입력, 교외 30080·교내 22 지원, SSH 시작 자동 인증 없음, 인증 실패와 서버 장애 구분 |
| 캐시 | 60초 TTL, `asyncio.Lock` 중복 갱신 방지, 숨겨진 탭 자동 조회 중단, 작업 목록 SFTP 자동 반복 금지, 로그인 노드 과부하 시 기존 값 유지 |
| 대시보드 | 사용자·GPU·노드·파티션·QOS 사용량·대기 원인 표시, GPU 점유 추세·실시간 대기열(순번)·내 잡 예상 시작, 3-클러스터 안내(ariel/moana/aurora·내 소속 강조) |
| 완료 이력 | `sacct` 완료 작업 이력·실패 진단(OOM/타임아웃/취소·시그널) 화면, 성공률·낭비 GPU 시간, 메모리 사용률(MaxRSS/요청) 표시 |
| 튜토리얼 | **실습 터미널**: 명령어를 직접 입력하면 whoami/show-qos/squeue/slurm-gres-viz/sacct 가 **실제 백엔드 데이터**로 실행되고 6단계로 검증(srun/sbatch 는 시뮬레이션). '안내 보기' 토글로 기존 카탈로그(명령어 복사·pitfall)도 제공. `tutorial.get_tutorial()` 기반 |
| 공지 | `slack.get_announcements()` Slack 공지 채널 읽기 화면, 작성자·시각·반응·답글·BOT·긴급 표시. 토큰 없으면 mock, 실패해도 안내 문구 표시 |
| 추천 | 기존 `placement.find_fastest()`와 Slurm `sbatch --test-only` 결과 사용 |
| 작업 검증 | 로컬 코드, 진입 파일, 원격 데이터·결과 권한, 파티션·노드·QOS·시간 검사 |
| `/data` 정책 | TAR·TAR.GZ·TGZ·ZIP 한 파일만 허용하고 `/local_datasets` 복사를 강제 |
| 코드 전송 | 폴더·Python·ZIP·TAR.GZ 지원, 작업별 1회 SFTP 업로드 |
| 작업 경로 | 입력한 사용자명의 `/data/<사용자명>/.seraph-gui/jobs`만 사용, `/home`에 작업 파일 미생성 |
| 스크립트 | `--cpus-per-gpu`, `--mem-per-gpu`, `/local_datasets` 실행 흐름 적용 |
| 사전 점검 | `debug_*` 파티션에서 5분 `srun`으로 GPU·코드·데이터 파일·Conda·Python 검사 |
| 실제 제출 | 제출 직전 구조화된 설정으로 스크립트 재생성, `--test-only` 재실행, `sbatch` Job ID 파싱 |
| 중복 방지 | 요청 ID 멱등 처리, `SUBMITTING` 선기록, 결과 불명 시 자동 재제출 금지 |
| 모니터링 | 실행 상태는 상세 화면에서 20초 간격, stdout·stderr는 수동 갱신, 결과 경로 확인 |
| 취소 | 자기 작업 폴더에 기록된 숫자 Slurm ID만 `scancel` 가능 |
| Mock 시연 | 준비→제출→대기→실행→완료와 로그·취소를 서버 없이 시연 |
| 보안 | known_hosts 검증, 인증정보 미저장, 작업 폴더 쓰기 경계, 압축 경로 이동·링크 차단 |

## 작업별 서버 구조

```text
/data/<사용자명>/.seraph-gui/jobs/
└─ 20260715-120000-image-train-a82f12c4/
   ├─ code.tar.gz 또는 code.zip
   ├─ job.json
   ├─ preflight.sh
   ├─ job.sbatch
   ├─ stdout.log
   └─ stderr.log
```

Slurm이 최종 상태의 기준이며 `job.json`은 로컬 GUI가 설정과 Job ID를 다시 찾기 위한 보조 자료입니다. 별도 DB를 사용하지 않습니다.

## API

| 메서드 | 경로 | 역할 |
|---|---|---|
| GET | `/api/v1/health` | 로컬 API·SERAPH 연결 상태 |
| POST | `/api/v1/session/connect` | 사용자명·호스트·포트·선택적 일회성 비밀번호로 SSH 연결 |
| GET | `/api/v1/me` | 사용자·계정·QOS·기본 파티션 |
| GET | `/api/v1/cluster/status` | GPU와 작업 현황 |
| GET | `/api/v1/cluster/nodes` | 조건에 맞는 노드 |
| GET | `/api/v1/cluster/partitions` | 파티션과 사용 권한 |
| GET | `/api/v1/cluster/usage` | 내 QOS 사용량 |
| POST | `/api/v1/recommendations` | 가장 빠른 실행 위치 |
| POST | `/api/v1/jobs/validate` | 전체 작업 입력 검사 |
| POST | `/api/v1/jobs/prepare` | 코드 업로드·스크립트 생성·test-only |
| POST | `/api/v1/jobs/{id}/preflight` | `srun` 사전 점검 |
| POST | `/api/v1/jobs/{id}/submit` | 사용자 확인 후 실제 제출 |
| GET | `/api/v1/jobs` | GUI에서 준비한 작업 목록 |
| GET | `/api/v1/jobs/{id}` | 작업과 Slurm 상태 |
| GET | `/api/v1/jobs/{id}/logs` | stdout·stderr 일부 읽기 |
| POST | `/api/v1/jobs/{id}/cancel` | 작업 취소 |
| GET | `/api/v1/jobs/history` | 기존 전체 Slurm 완료 이력 |

## GPU 노드 스크립트 흐름

1. `/local_datasets/<사용자명>/seraph-gui-$SLURM_JOB_ID`를 만듭니다.
2. SFTP로 `/data/<사용자명>`에 올린 코드 패키지를 로컬 공간에 복사해 풉니다.
3. `/data`의 압축 데이터 한 파일을 로컬 공간에 복사해 해제합니다.
4. `/data/<사용자명>/anaconda3`에서 선택한 Conda 환경을 활성화합니다.
5. `SERAPH_DATASET_PATH`, `SERAPH_OUTPUT_PATH`를 제공하고 Python 진입 파일을 실행합니다.
6. 성공·실패 여부와 관계없이 EXIT trap에서 임시 결과물을 영구 결과 경로로 복사합니다.
7. `/local_datasets`의 작업 폴더를 정리합니다.

## 실제 SERAPH에서 최종 확인할 항목

이 실행환경에는 실제 SERAPH 계정이 없으므로 다음은 작은 테스트 작업으로 현장 검증해야 합니다.

1. 실제 계정으로 `/local_datasets/<사용자명>` 생성·정리 권한 확인
2. 작은 압축 데이터로 `/data` → `/local_datasets` 복사·해제 속도 확인
3. `/data/<사용자명>/anaconda3` 초기화 파일과 환경 이름 확인
4. NAS 결과 폴더 생성 권한과 팀별 권장 저장 경로
5. `unzip`, `tar`, `python` 명령의 GPU 노드 기본 제공 여부
6. 실제 작은 데이터로 `PENDING → RUNNING → COMPLETED`와 로그 경로 확인
7. 실패 작업에서도 EXIT trap의 결과·체크포인트 복사가 허용되는지 확인

위 값이 서버마다 다르면 `config.yaml` 설정 항목으로 분리하는 것이 다음 단계입니다.

## 테스트 결과 확인 명령

```bash
python -m pytest -q
cd frontend && npm run build
```

API 테스트는 Mock 전체 흐름, 사용자별 `/data` 경계, `/local_datasets`, srun 선행 조건, GPU당 자원 지시자, Snapshot 동시 요청 중복 제거, 압축 경로 공격, 제출 멱등성, 공통 오류 형식을 검증합니다.
