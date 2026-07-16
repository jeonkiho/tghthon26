"""튜토리얼 내용 공급.

화면·단계 진행·피드백은 프론트가 만든다. 백엔드는 "무엇을 가르칠지" 와
"연습에 쓸 데이터" 만 준다. 연습은 mock 스냅샷 위에서 돌아가므로 실제 서버에
아무 영향이 없다.

여기 담긴 주의사항은 도구가 잡아줄 수 없는 것들이다. 사용자가 터미널에서 직접
명령을 치면 lint 가 개입할 수 없기 때문에, 교육으로 커버한다.
"""

from . import services
from .connection import MockConnection


def practice_snapshot():
    """연습용 스냅샷. 실제 서버에 붙지 않는다."""
    return MockConnection().snapshot()


def get_steps(snapshot=None):
    """튜토리얼 단계 목록 (JSON 직렬화 가능).

    각 단계의 `check` 는 프론트가 "해봤는지" 확인할 때 쓸 수 있는 힌트다.
    강제하지는 않는다.
    """
    snapshot = snapshot or practice_snapshot()
    partitions = ', '.join(sorted(snapshot.partitions)) or 'batch_grad'
    limit = snapshot.my_qos

    if limit and limit.max_high_perf_gpus == 0:
        quota_line = (f'내 QOS({limit.name})는 GPU {limit.max_gpus}개까지, '
                      f'고성능 노드는 사용할 수 없습니다.')
    elif limit:
        quota_line = (f'내 QOS({limit.name})는 GPU {limit.max_gpus}개, '
                      f'그중 고성능 {limit.max_high_perf_gpus}개까지 쓸 수 있습니다.')
    else:
        quota_line = 'QOS 한도를 조회하지 못했습니다.'

    return [
        {
            'id': 'ssh',
            'title': 'SSH 로 세라프에 접속하기',
            'body': (
                '실제 SERAPH 사용자명으로 접속합니다. 교외에서는 30080 포트, '
                '교내에서는 22 포트를 사용합니다. GUI도 같은 값을 입력받습니다.'
            ),
            'commands': [
                'ssh -p 30080 <사용자명>@ariel.khu.ac.kr  # 교외',
                'ssh <사용자명>@ariel.khu.ac.kr           # 교내',
            ],
            'pitfall': (
                'VSCode 의 Remote-SSH 로 세라프에 붙는 것은 권장되지 않습니다. '
                '로그인 노드에 무거운 프로세스를 띄우기 때문입니다. '
                '이건 도구가 막아줄 수 없으니 직접 지켜야 합니다.'
            ),
            'check': None,
        },
        {
            'id': 'quota',
            'title': '내 할당량 확인하기',
            'body': (
                f'{quota_line} 한도를 넘겨 job 을 내면 대기만 하다가 끝납니다. '
                '세라프에서 대기의 대부분은 GPU 부족이 아니라 이 할당량 때문입니다.'
            ),
            'commands': ['sacctmgr show assoc user=$USER format=User,QOS'],
            'pitfall': '할당량은 사람마다 다릅니다. 친구의 설정을 그대로 복사하지 마세요.',
            'check': 'usage',
        },
        {
            'id': 'status',
            'title': 'GPU 현황 읽기',
            'body': (
                'squeue 는 대기 줄, sinfo 는 노드 상태를 보여줍니다. '
                'sinfo 의 GPU 숫자는 "총 개수" 라서, 남은 개수를 알려면 '
                'GresUsed 를 함께 봐야 합니다.'
            ),
            'commands': [
                'squeue -u $USER',
                'sinfo -h -N -O "NodeHost:12,Gres:20,GresUsed:24"',
            ],
            'pitfall': (
                'GPU 가 남아 있어도 그 노드에 여유 CPU 가 없으면 job 이 배정되지 '
                '않습니다. 이 도구는 그런 GPU 를 빼고 셉니다.'
            ),
            'check': 'status',
        },
        {
            'id': 'data',
            'title': '학습 데이터 두는 곳',
            'body': (
                '/data는 NAS입니다. 압축 데이터 한 파일을 GPU 노드의 '
                '/local_datasets로 복사하고 그곳에서 압축을 푼 뒤 학습하세요.'
            ),
            'commands': [],
            'pitfall': (
                f"금지: {', '.join(snapshot.config.blocked_paths)} · "
                f"권장하지 않음: {', '.join(snapshot.config.warn_paths)}"
            ),
            'check': 'lint',
        },
        {
            'id': 'submit',
            'title': 'job 제출하기',
            'body': (
                '먼저 srun으로 코드·GPU·Conda 환경을 점검하고, 통과하면 sbatch로 '
                '학습 스크립트를 냅니다. GPU 종류에 따라 표기가 다릅니다. '
                '일반 GPU 는 --gres=gpu:N 과 함께 -w 로 v/g 노드를 반드시 '
                '지정해야 하고, 고성능은 --gres=gpu:high_perf:N 만 쓰면 됩니다. '
                '이 도구가 스크립트를 대신 만들어 주고, 절대 시작되지 않을 job 은 '
                '미리 막아줍니다.'
            ),
            'commands': [
                'srun --gres=gpu:1 --cpus-per-gpu=8 --mem-per-gpu=32G -p debug_grad --pty $SHELL',
                'sbatch train.sh',
                'squeue -u $USER',
                'scancel <JOBID>',
                'sbatch --test-only train.sh   # 제출하지 않고 검사만',
            ],
            'pitfall': (
                '--gres=gpu:1 만 쓰고 노드를 지정하지 않으면 제출이 거절됩니다. '
                '-w 에 노드를 여러 개 적으면 그 노드가 전부 빌 때까지 기다리므로 '
                '오히려 훨씬 늦게 시작합니다. 하나만 적으세요.'
            ),
            'check': 'sbatch',
        },
        {
            'id': 'result',
            'title': '결과 확인하기',
            'body': (
                '끝난 job 의 상태와 실행 시간은 sacct 로 봅니다. 이 도구는 종료 '
                '코드와 메모리 사용량까지 읽어 실패 원인을 알려줍니다.'
            ),
            'commands': [
                'sacct -j <JOBID> --format=JobID,State,ExitCode,Elapsed,MaxRSS',
            ],
            'pitfall': (
                '메모리 부족(OUT_OF_MEMORY)으로 죽은 job 은 종료 코드가 0 입니다. '
                '코드만 보면 성공한 줄 압니다. State 를 꼭 함께 보세요. '
                'MaxRSS 는 메인 행이 아니라 .batch 행에 찍힙니다.'
            ),
            'check': 'history',
        },
    ]


def get_tutorial(snapshot=None):
    """프론트가 한 번에 받아갈 튜토리얼 전체."""
    snapshot = snapshot or practice_snapshot()
    return {
        'mode': 'practice',       # 실제 서버가 아니라 mock 위에서 돈다
        'user': snapshot.me,
        'steps': get_steps(snapshot),
        'sample_status': services.get_gpu_status(snapshot),
    }
