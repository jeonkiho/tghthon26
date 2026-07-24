# SERAPH TUI — 백엔드 (코어 엔진)

세라프(KHU 소융대 GPU 서버)에서 데이터를 가져와 판단·계산하고 JSON으로 내보낸다.
화면을 그리는 건 프론트. 둘의 경계는 [CONTRACT.md](CONTRACT.md)다.

## 빠른 시작

서버 없이 바로 돌아간다. 저장된 실제 출력(fixture)을 읽는다.

```bash
python -m seraph.dump --mock --section status
python -m seraph.dump --mock --section diagnose --user user25
python -m seraph.dump --mock --section sbatch
```

실서버에 붙으려면:

```bash
pip install -r requirements.txt
python -m seraph.dump --host ariel --section status
```

`~/.ssh/config` 의 `Host ariel` 설정과 키를 그대로 쓴다. 키가 없으면 비밀번호를
물어보고 그 세션에서만 쓴다(저장하지 않는다).

## 구조

아래에서 위로 쌓이고, 위 레이어만 아래를 부른다.

| 레이어 | 파일 | 하는 일 |
|---|---|---|
| config | [config.py](seraph/config.py), [config.yaml](config.yaml) | 설정·경로·모드 전환 |
| connection | [connection.py](seraph/connection.py) | SSH 접속, 명령 실행 (mock ↔ 실서버) |
| parsers | [parsers/](seraph/parsers/) | 텍스트 출력 → 데이터 |
| services | [services.py](seraph/services.py) | 계산·판단 → JSON |

`commands.py` 에 실행할 Slurm 명령이 모여 있다. 파서는 그 포맷에만 의존한다.
`sbatch.py`(스크립트 생성), `history.py`(끝난 job 결과·실패 진단), `tutorial.py`
(튜토리얼 내용)는 services 위에 얹힌다.

`sacct`(끝난 job)는 폴링 스냅샷에 넣지 않았다. 느리고 자주 바뀌지 않으므로
`conn.sacct(days=7)` 로 사용자가 요청할 때만 부른다.

[clusters.py](seraph/clusters.py) — 세라프는 클러스터가 3개(ariel/moana/aurora)다.
계정만 있으면 **셋 다 접속**한다(접속 호스트가 설정값이다. moana 접속을 실제로 확인했다).
어느 클러스터가 실시간인지는 정적 표가 아니라 `infer_cluster()` 가 노드 이름으로 판단하고
`whoami().connected_cluster` 가 알려준다. 라우팅 규칙(학과×신분)은 데이터로 갖고 있어
"당신은 moana 를 쓰세요" 같은 안내를 한다. 파티션 접근(대학원 `*_grad` /
학부 `*_ugrad`)과 학부생 노드 제한(`ariel-v[6-12]`)도 여기 규칙을 따른다.

[slack/](seraph/slack/)은 세라프와 무관한 별도 계통이다. Slack 공지 채널을 읽어
TUI 에 보여준다. 토큰이 없으면 저장된 mock 공지를 준다.

```bash
export SERAPH_SLACK_TOKEN='xoxb-...'      # 없으면 mock
python -m seraph.dump --section announcements
```

필요한 scope 는 `channels:history`, `channels:read`, `users:read` 이고, 봇 토큰이면
공지 채널에 봇을 초대해야 한다. 토큰은 환경변수로만 읽는다(`config.yaml` 에 적어도
무시한다 — 그 파일은 커밋되므로). 읽어올 채널은 `config.yaml` 의 `slack.channel`.

`notify.py` 는 반대로 **보내는** 쪽이다(Incoming Webhook). 지금 TUI 는 쓰지 않는다.

## 테스트

```bash
python -m pytest tests/ -q
```

fixture는 실제 세라프 출력이다(`tests/fixtures/`). 다시 뽑으려면:

```bash
python -c "
from seraph import commands
print('\n'.join(f'echo \"@@@{k}\"; {v}' for k,v in commands.ALL.items()))
" | ssh -p 30080 <사용자명>@ariel.khu.ac.kr bash -s
```

## 세라프에서 실제로 확인한 것들

문서나 추측이 아니라 `sbatch --test-only` 와 실제 명령 출력으로 확인했다.

- **`sinfo` 의 GPU 숫자는 총량이다.** 여유를 알려면 `GresUsed` 가 필요하다.
- **GPU가 비어도 그 노드에 idle CPU 가 없으면 배정되지 않는다.** (`ariel-v3`)
- **고성능(m/k/n)과 일반(v/g) GPU 는 서로 대체 불가.** 합쳐 세면 틀린 안내가 나온다.
- **QOS 한도는 사람마다 다르다.** `grad` 는 GPU 4개 + 고성능 금지, 개인 QOS 는 보통 12개 + 고성능 8개.
- **대기의 대부분(27/38)은 GPU 부족이 아니라 개인 QOS 한도 초과다.** 진짜 GPU 부족은 1건이었다.
- **일반 GPU job 은 `-w` 로 v/g 노드를 지정해야 제출된다.** 안 하면 거절된다.
  거절 메시지가 안내하는 `-x` 로 고성능 노드를 제외하는 방법은 실제로는 통하지 않는다.
- **`-w` 에 노드를 여러 개 적으면 전부 확보될 때까지 기다려 훨씬 늦어진다.**
- **Slurm 22.05.2 에 JSON 플러그인이 없다.** `squeue --json` 은 죽는다. 텍스트 파싱이 필수다.
- **현재 제공된 SERAPH 튜토리얼은 `/data` 전체를 NAS로 정의한다.** 학습 데이터는 `/local_datasets`로 복사한 뒤 읽는다.
- **메모리 부족(OOM)으로 죽은 job 은 종료 코드가 0 이다** (`0:125`). 코드만 보면 성공으로 오해한다.
- **`sacct` 의 MaxRSS 는 메인 행이 아니라 `.batch` 스텝 행에 있다.** `-X` 를 쓰면 그 값을 잃는다.
  반대로 ReqMem 은 메인 행에만 있다. 둘을 합쳐야 OOM 을 진단할 수 있다.
- **`sacct` 의 State 는 `CANCELLED by 20301` 처럼 UID 가 붙어 온다.** 그대로 비교하면 안 걸린다.
- 최근 30일 기준 끝난 job 623개 중 성공 216개(35%). 실패한 job 이 GPU 176시간을 태웠다.

## 아직 안 된 것

- 경로 정책(`config.yaml` 의 `lint.blocked_paths`)은 팀이 정한 값이다. 관리자 확인 필요.
- **Slack 공지 읽기는 실제 워크스페이스에 붙여본 적이 없다.** 토큰이 없어서
  mock 과 에러 경로(가짜 토큰 → `invalid_auth`)까지만 확인했다. 토큰과 채널
  이름을 넣고 한 번 돌려봐야 한다.
- job 제출(`sbatch` 실제 실행)은 넣지 않았다. 지금은 스크립트를 만들어 주기만 한다.
