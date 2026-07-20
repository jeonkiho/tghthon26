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
  - CE → `ariel` (`debug_ce_ugrad`), EE/BME → `moana` (`debug_eebme_ugrad`), SWCON/AI → `aurora` (`debug_ugrad`)
  - 대학원생 선택 시 `debug_grad`/`batch_grad`
- **가상 파일시스템**: NAS `/data`, `/home`, 계산 노드 로컬 `/local_datasets`
- **클러스터 토폴로지**: ariel / aurora / moana 실제 노드·GPU 구성
- **Slurm 시뮬레이터**: `srun`(노드 진입), `sbatch`(잡 실행 → 로그 생성), `squeue`, `scancel`,
  `slurm-gres-viz`, `show-qos`, `show-assoc`, `sinfo`
- **핵심 재현**: `ssh` 접속, srun 시 `(base)` 소멸 → `conda init` + `source ~/.bashrc` 복귀,
  NAS 예절(tar → `/local_datasets/`), master에서 무거운 작업 경고
- **8단계 미션**: 배정에 맞춰 명령이 동적으로 바뀌고, 각 단계를 실제로 검증

## 배정 정책 참고

학과 → 클러스터 매핑은 튜토리얼 1 *Cluster Assignment Policy* 기준입니다.
운영 정책이 바뀌면 `seraph-terminal.html`의 `DEPTS` / `CLUSTERS` 상수를 수정하세요.
