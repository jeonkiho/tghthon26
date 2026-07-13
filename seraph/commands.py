"""세라프에서 실행할 Slurm 명령어 정의.

파서는 여기 정의된 포맷에만 의존한다. 명령어를 고치면 파서도 같이 고쳐야 하므로
한 곳에 모아둔다.

주의:
  - `-O "필드:|"` 의 `|` 는 구분자 suffix다. 고정폭 출력은 값이 잘리므로 쓰지 않는다.
  - `--start` 는 `-O` 를 무시하고 자체 포맷을 강제한다. 그래서 이것만 `-o` 를 쓴다.
  - `-h` 는 헤더 제거. 파서가 헤더를 신경쓰지 않게 한다.
"""

# 큐의 모든 job. tres-alloc 에서 GPU 개수를 얻는다.
# (%b 는 --gres 로 낸 job 만 채워지고 --gpus 로 낸 job 은 N/A 라 쓰지 않는다.)
SQUEUE = (
    'squeue -h -O '
    '"JobID:|,Partition:|,UserName:|,StateCompact:|,TimeUsed:|,QOS:|,'
    'tres-alloc:|,NodeList:|,Reason:|,Name:|"'
)

# 노드별 GPU 총량(Gres)과 사용량(GresUsed).
# GresUsed 가 없으면 가용 GPU 를 계산할 수 없다. `-o %G` 로는 총량만 나온다.
# CPUsState 도 필요하다. GPU 가 남아도 idle CPU 가 0 이면 그 GPU 는 배정되지 않는다
# (실제로 ariel-v3 이 GPU 1개 여유 / idle CPU 0 인 상태가 관측된다).
SINFO = (
    'sinfo -h -N -O '
    '"NodeHost:|,Partition:|,StateLong:|,Gres:|,GresUsed:|,CPUsState:|,'
    'AllocMem:|,Memory:|"'
)

# 파티션별 시간 제한. debug_* 는 4시간, batch_* 는 무제한이다.
# config 에 적어두면 정책이 바뀔 때 틀리므로 서버에서 읽는다.
PARTITIONS = 'sinfo -h -o "%P|%l|%D"'

# Slurm 이 추정한 시작 시각. 직접 계산하는 것보다 이게 1순위다.
SQUEUE_START = 'squeue -h --start -o "%i|%S|%R"'

# QOS 별 한도. MaxTRESPerUser 에 gres/gpu 와 gres/gpu:high_perf 가 들어있다.
QOS = (
    'sacctmgr -n -P show qos '
    'format=Name,MaxTRESPerUser,MaxJobsPerUser,MaxSubmitJobsPerUser'
)

# 현재 사용자가 어떤 QOS 에 속하는지. 사람마다 한도가 다르다.
# Account 는 학부/대학원(그리고 학과) 판별의 근거다. ugrad/grad 로 시작하면
# 학부/대학원, _ce·_eebme 접미어는 학과. 파티션 접근이 이걸로 갈린다.
ASSOC = 'sacctmgr -n -P show assoc user=$USER format=User,Account,QOS'

# 로그인 노드 부하. load > 8 이면 폴링을 멈춰야 한다.
UPTIME = 'uptime'

ALL = {
    'squeue': SQUEUE,
    'sinfo': SINFO,
    'partitions': PARTITIONS,
    'squeue_start': SQUEUE_START,
    'qos': QOS,
    'assoc': ASSOC,
    'uptime': UPTIME,
}

# 끝난 job 기록. ALL 에 넣지 않는다 — 5초마다 조회할 정보가 아니고,
# 조회 범위(일수)에 따라 명령이 달라져서 사용자가 요청할 때만 부른다.
#
# `-X`(스텝 제외)를 쓰지 않는 이유: MaxRSS 는 메인 job 행이 비어 있고 `.batch`
# 스텝에만 들어 있다. OOM 을 진단하려면 그 값이 필요하므로 스텝을 함께 받아
# 파서에서 합친다. 반대로 ReqMem 은 메인 행에만 있다.
#
# `-P` 는 구분자 '|' 에 끝에 붙는 구분자가 없는 형식이다(`-p` 는 붙는다).
_SACCT_FIELDS = ('JobID,JobName,State,ExitCode,Start,End,Elapsed,Timelimit,'
                 'Partition,NodeList,AllocTRES,ReqMem,MaxRSS')


def sacct(days=7, user=None):
    """최근 며칠간 끝난 job 기록을 뽑는 명령."""
    who = f'-u {user}' if user else '-u $USER'
    return f'sacct -n -P {who} -S now-{int(days)}days -o "{_SACCT_FIELDS}"'
