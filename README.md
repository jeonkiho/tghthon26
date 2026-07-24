# SERAPH GUI

SSH·Slurm 명령을 직접 작성하지 않아도 로컬 GUI에서 SERAPH GPU 작업을 준비하고 제출할 수 있는 설치형 MVP입니다.

## 구현된 흐름

1. 사용자 PC에서 SERAPH에 SSH로 연결합니다.
2. 기존 `seraph/` 코어가 Slurm 상태, 파티션·QOS, 노드 규칙을 판정합니다.
3. GUI에서 코드와 NAS 경로, GPU·시간 조건을 입력합니다.
4. `/data`의 압축 데이터는 GPU 노드 `/local_datasets`로 복사·해제하도록 강제합니다.
5. 기존 `sbatch --test-only` 추천으로 가장 빠른 위치를 확인합니다.
6. 선택한 코드만 SFTP로 `/data/<사용자명>/.seraph-gui/jobs/{작업}/`에 한 번 업로드합니다.
7. 백엔드가 `job.json`, `preflight.sh`, 검증된 `job.sbatch`를 생성합니다.
8. `srun` 사전 점검으로 GPU·코드·Conda 환경과 `/local_datasets` 사용 가능 여부를 확인합니다.
9. 점검 통과와 사용자 최종 확인 후 한 번만 `sbatch`를 실행합니다.
10. GUI에서 대기·실행·완료·실패 상태, stdout·stderr, 결과 경로를 확인하고 작업을 취소할 수 있습니다.

별도 중앙 서버, 회원 DB, 폴더 실시간 동기화, Slack 알림은 사용하지 않습니다. 로컬 API는 `127.0.0.1`에만 바인딩합니다.

## 가장 빠른 실행

기본 설정은 실제 서버가 없어도 전체 흐름을 보여주는 Mock 모드입니다.

### Windows

`start_windows.bat`을 실행합니다. 첫 실행에는 Python 패키지를 설치하므로 시간이 조금 걸립니다.

### macOS·Linux

```bash
./start_unix.sh
```

브라우저에서 `http://127.0.0.1:8765`가 자동으로 열립니다. API 문서는 `http://127.0.0.1:8765/api/docs`에서 확인할 수 있습니다.

## 실제 SERAPH 연결

1. 처음 한 번은 터미널에서 호스트 지문을 직접 확인해 `known_hosts`에 등록합니다.
2. `config.yaml`의 `connection.mode`를 `ssh`로 바꿉니다.
3. 프로그램을 다시 실행하고 연결 화면에서 실제 SERAPH 사용자명, 호스트, 포트를 입력합니다.
4. 교외에서는 `ariel.khu.ac.kr:30080`, 교내에서는 `ariel.khu.ac.kr:22`를 사용합니다.
5. SSH 키·에이전트를 먼저 사용하고, 필요할 때만 비밀번호를 입력합니다.

교외에서 최초 접속을 확인하는 명령은 다음과 같습니다. `<사용자명>`은 실제 계정으로 바꿉니다.

```bash
ssh -p 30080 <사용자명>@ariel.khu.ac.kr
```

```yaml
connection:
  mode: ssh
  host: ariel.khu.ac.kr
  port: 30080
```

사용자명은 GUI에서 입력받아 SSH 로그인과 `/data/<사용자명>` 경로에 사용합니다. SSH 키와 비밀번호는 YAML, DB, `job.json`, 로그에 저장하지 않습니다.

SSH 모드에서는 시작할 때 비밀번호 없는 자동 로그인을 시도하지 않습니다. 인증 실패는 서버 장애와 구분해 `SSH_AUTH_FAILED`로 표시하며, 비밀번호 앞뒤 공백도 입력 그대로 전달합니다.

## 서버 부하를 줄이는 조회 정책

- 대시보드는 60초마다 갱신합니다.
- 작업 목록의 SFTP 폴더 탐색은 자동 반복하지 않고 최초 연결·작업 변경·사용자 새로고침 때만 수행합니다.
- 실행 중 작업 상태는 상세 화면을 열었을 때만 20초마다 확인합니다.
- stdout·stderr는 자동으로 읽지 않고 `로그 갱신` 버튼을 눌렀을 때만 가져옵니다.
- 브라우저 탭이 숨겨지면 모든 자동 조회를 중단합니다.
- 같은 대시보드 갱신 안의 여러 화면 요청은 하나의 Snapshot 캐시를 공유합니다.
- SFTP 업로드는 작업 준비 시 한 번만 수행하며 자동 동기화하지 않습니다.

## GUI 입력 규칙

- 코드: 폴더, 단일 `.py`, `.zip`, `.tar.gz`, `.tgz`
- 진입 파일: 코드 패키지 안의 상대경로(예: `train.py`, `src/train.py`)
- 실행 인자: 한 줄에 하나씩 입력
- `{dataset}`: GPU 노드에서 사용할 데이터 경로로 치환
- `{output}`: 작업용 임시 결과 폴더로 치환
- 데이터: 사용자가 미리 올린 `/data/...`의 `.tar`, `.tar.gz`, `.tgz`, `.zip` 파일. GUI는 대용량 데이터를 업로드하지 않음
- 학습 데이터: 실행 노드의 `/local_datasets/<사용자명>/...`에 복사·해제한 경로만 사용
- 결과: 학습 중에는 `/local_datasets`에 기록하고, 종료할 때 `/data/<사용자명>/...`로 복사
- 자원: CPU와 메모리는 각각 `--cpus-per-gpu`, `--mem-per-gpu`로 요청
- Conda: 선택한 환경은 `/data/<사용자명>/anaconda3`에서 활성화
- 일반 GPU: 기존 코어가 SERAPH 규칙에 맞는 v/g 노드 하나를 지정
- 고성능 GPU: 권한과 QOS 한도를 기존 코어로 검사

코드 폴더를 묶을 때 `.git`, `.venv`, `node_modules`, `__pycache__`, `.pytest_cache`는 제외합니다. 압축파일의 `..` 경로, 절대경로, 링크·장치 파일은 거부합니다.

## 개발 실행

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-api.txt
cd frontend
npm install
npm run build
cd ..
.venv/bin/python run_gui.py
```

Windows에서는 `.venv/bin/python` 대신 `.venv\Scripts\python.exe`를 사용합니다. React 빌드 결과인 `frontend/dist/`는 배포 ZIP에 포함되므로 일반 사용자는 Node.js가 없어도 됩니다.

## 테스트

```bash
python -m pytest -q
```

테스트 범위에는 기존 코어와 API·캐시·Mock 전체 제출 흐름·사용자별 `/data` 경계·srun 점검·입력 경계·압축 경로 공격·중복 제출 방지가 포함됩니다.

## 주요 구조

```text
backend/
  main.py            FastAPI와 API 경로
  cache.py           Snapshot TTL·동시 갱신 방지
  job_service.py     검증·패키징·준비·제출·모니터링
  remote.py          Mock/Paramiko SFTP·Slurm 어댑터
  schemas.py         Pydantic 입력 검증
frontend/
  src/               React GUI
  dist/              FastAPI가 제공하는 정적 빌드
seraph/               기존 파서·QOS·추천·sbatch 코어
tests/                기존 테스트와 API 통합 테스트
```

세부 구현 상태와 실서버 확인 항목은 [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md)를 참고하세요.
