# Seraph 실습 터미널 (Trainer)

경희대 Vision·AI Lab **Seraph** GPU 클러스터 튜토리얼을 브라우저에서 직접 손으로 쳐보며 익히는
**인터랙티브 터미널 시뮬레이터**입니다. 단일 HTML 파일(외부 의존성 0)로, 그냥 열면 동작합니다.

> ⚠️ 이것은 **학습용 시뮬레이터**입니다. 실제 Slurm 서버에 접속하지 않으며, GPU 점유·잡 상태는
> 모두 가상 데이터입니다. 실제 서버 현황/배치는 `backend/` + `seraph/` (실제 Slurm 질의)를 사용하세요.

## 실행

```bash
# 아무 정적 서버로 열거나, 파일을 브라우저로 바로 열어도 됩니다.
python -m http.server -d trainer 8080   # → http://localhost:8080/seraph-terminal.html
```

## 무엇을 하나

- **로그인/자동 배정**: 경희대 이메일(`@khu.ac.kr`) + 학과 선택 → 학과별 클러스터·파티션 자동 배정
  (실서버 `seraph/clusters.py` 의 라우팅 기준)
  - CE → `moana` (`debug_ce_ugrad`), EE/BME → `moana` (`debug_eebme_ugrad`),
    AI → `ariel` (`debug_ugrad`), SWCON → `aurora` (`debug_swcon_ugrad`)
  - **대학원생은 학과와 무관하게 전부 `ariel`** (`debug_grad`/`batch_grad`)
- **가상 파일시스템**: NAS `/data`, `/home`, 계산 노드 로컬 `/local_datasets`
- **클러스터 토폴로지**: ariel / aurora / moana 실제 노드·GPU 구성
- **Slurm 시뮬레이터**: `srun`(노드 진입), `sbatch`(잡 실행 → 로그 생성), `squeue`, `scancel`,
  `slurm-gres-viz`, `show-qos`, `show-assoc`, `sinfo`
- **핵심 재현**: `ssh` 접속, srun 시 `(base)` 소멸 → `conda init` + `source ~/.bashrc` 복귀,
  NAS 예절(tar → `/local_datasets/`), master에서 무거운 작업 경고
- **8단계 미션**: 배정에 맞춰 명령이 동적으로 바뀌고, 각 단계를 실제로 검증

## 배정 정책 참고

학과 × 신분 → 클러스터·계정·파티션 매핑은 실서버 코드(`seraph/clusters.py` 의 `_ROUTING`·
`cluster_for`, `seraph/placement.py` 의 `partition_from_account`)와 QOS(`seraph/parsers/qos.py`:
학부 `ugrad` = GPU 1, 대학원 `grad` = GPU 4, 둘 다 `high_perf=0`) 기준입니다.
클러스터별 총 GPU 수·노드 이름도 `clusters.py`(ariel 182 / **moana 105** / aurora 62)를 따릅니다.
운영 정책이 바뀌면 `seraph-terminal.html`의 `DEPTS` / `CLUSTERS` / `profileFor` 를 수정하세요.

**moana 는 실서버에서 직접 확인한 값입니다**(2026-07, 파티션 전수 조회):
`r[1-5]` 4장씩 · `u[1-4,6,8]` 8장씩(u6 만 5장) · `y[1,3-7]` 8장(y6·y7 만 4장) = 105.
가이드에 있던 `u5·u7·y2` 는 실제로 없습니다. ariel/aurora 는 접속해 보지 못해
노드별 장수는 총합만 맞춘 추정입니다.
