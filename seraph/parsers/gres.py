"""GRES(GPU) 표기 파싱.

세라프의 GPU 표기는 자리마다 모양이 다르다. 실제로 관측된 것들:

  sinfo Gres        gpu:8(S:0)                      타입 없음, 소켓 표기 붙음
                    gpu:high_perf:8                 타입 있음
                    gpu:broken:1,gpu:high_perf:7    한 노드에 타입 두 개 (ariel-m1)
  sinfo GresUsed    gpu:(null):2(IDX:0-1)           타입 자리에 리터럴 "(null)"
                    gpu:(null):0(IDX:N/A)           사용 0 이면 IDX 가 N/A
  squeue tres-alloc cpu=8,...,gres/gpu=1,gres/gpu:high_perf=1

tres-alloc 에서 gres/gpu 는 GPU 총 개수이고, gres/gpu:high_perf 는 그중 고성능 개수다.
(둘을 더하면 안 된다. high_perf 1개짜리 job 은 gres/gpu=1, gres/gpu:high_perf=1 로 나온다.)
"""

import re

# (S:0) 이나 (IDX:0-7) 같은 부가 표기. 타입 자리의 (null) 은 건드리면 안 되므로
# 접두어를 명시해서만 지운다.
_ANNOTATION = re.compile(r'\((?:S|IDX):[^)]*\)')

# gpu:8  /  gpu:high_perf:8  /  gpu:(null):2
_ENTRY = re.compile(r'^gpu:(?:(.+):)?(\d+)$')

HIGH_PERF = 'high_perf'


def parse_gres(spec):
    """"gpu:broken:1,gpu:high_perf:7" -> {"broken": 1, "high_perf": 7}

    타입이 없는 일반 GPU 는 키 None 으로 들어간다. GPU 가 아닌 항목은 무시한다.
    """
    counts = {}
    if not spec or spec in ('(null)', 'N/A'):
        return counts

    cleaned = _ANNOTATION.sub('', spec)
    for entry in cleaned.split(','):
        entry = entry.strip()
        if not entry.startswith('gpu:'):
            continue
        m = _ENTRY.match(entry)
        if not m:
            continue
        gpu_type, count = m.group(1), int(m.group(2))
        if gpu_type == '(null)':
            gpu_type = None
        counts[gpu_type] = counts.get(gpu_type, 0) + count
    return counts


def total_gpus(counts):
    """parse_gres 결과의 총합. broken 은 쓸 수 없으므로 제외한다."""
    return sum(n for t, n in counts.items() if t != 'broken')


def parse_tres(spec):
    """"cpu=8,mem=48G,gres/gpu=1,gres/gpu:high_perf=1" -> dict

    값은 문자열 그대로 둔다. 숫자로 쓸 항목은 호출부에서 뽑는다.
    """
    out = {}
    if not spec or spec in ('(null)', 'N/A'):
        return out
    for pair in spec.split(','):
        if '=' not in pair:
            continue
        k, v = pair.split('=', 1)
        out[k.strip()] = v.strip()
    return out


def _tres_int(tres, key):
    raw = tres.get(key)
    if raw is None or raw == 'N/A':
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def gpus_from_tres(spec):
    """tres-alloc 문자열에서 (총 GPU, 고성능 GPU) 를 뽑는다."""
    tres = parse_tres(spec)
    return _tres_int(tres, 'gres/gpu'), _tres_int(tres, f'gres/gpu:{HIGH_PERF}')
