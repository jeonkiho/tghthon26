"""fixture 의 실제 계정명을 익명 이름으로 바꾼다.

세라프에서 뽑은 출력에는 다른 학생 98명의 계정(대부분 학번)과 개인 QOS 할당량이
들어 있다. 저장소에 그대로 올릴 수 없다. 파싱 로직 검증에는 이름이 아무 영향이
없으므로 user01, user02 ... 로 치환한다.

    python tools/anonymize_fixtures.py --check     # 남은 실명이 있는지만 확인
    python tools/anonymize_fixtures.py             # 익명화해서 덮어쓴다

원본은 tests/fixtures_real/ 에 남긴다 (.gitignore 에 있으므로 커밋되지 않는다).
실서버에서 fixture 를 다시 뽑았다면 커밋 전에 이 스크립트를 반드시 돌릴 것.

치환 주의:
  실제 계정 중 한쪽이 다른 쪽의 접두어인 쌍이 있다 (예: 'abc1234' 와 'abc12345').
  짧은 쪽을 먼저 바꾸면 긴 쪽이 'user07_5' 처럼 깨진다. 긴 이름부터, sentinel 을
  거쳐 한 번씩만 바꾼다. '_' 는 단어 문자라 정규식 \\b 로는 경계를 잡을 수 없다.
"""

import argparse
import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / 'tests' / 'fixtures'
ORIGINALS = ROOT / 'tests' / 'fixtures_real'

# 계정명이 들어 있는 파일들
TARGETS = ('squeue.txt', 'qos.txt', 'assoc.txt', 'squeue_start.txt')

# 모두가 공유하는 QOS. 사람 이름이 아니므로 그대로 둔다.
SHARED_QOS = {'normal', 'grad', 'ugrad', 'grad_test'}

_QOS_NAME = re.compile(r'^qos_(.+)_\d{4}_\d+\|', re.M)


def collect_users(read):
    """fixture 들에서 계정명을 모은다."""
    users = set()

    for line in read('squeue.txt').splitlines():
        parts = line.split('|')
        if len(parts) > 6:
            users.add(parts[2].strip())

    users.update(_QOS_NAME.findall(read('qos.txt')))

    assoc = read('assoc.txt').split('|')
    if assoc:
        users.add(assoc[0].strip())

    users.discard('')
    return users


def build_mapping(users, me):
    """본인은 user01. 나머지는 이름순으로 user02, user03 ..."""
    others = sorted(u for u in users if u != me)
    mapping = {me: 'user01'} if me else {}
    start = 2 if me else 1
    for i, user in enumerate(others, start=start):
        mapping[user] = f'user{i:02d}'
    return mapping


def substitute(text, mapping):
    """긴 이름부터 sentinel 로 바꾼 뒤 한 번에 되돌린다 (접두어 충돌 방지)."""
    sentinels = {}
    for i, user in enumerate(sorted(mapping, key=len, reverse=True)):
        token = f'\x00{i}\x00'
        sentinels[token] = mapping[user]
        text = text.replace(user, token)
    for token, name in sentinels.items():
        text = text.replace(token, name)
    return text


def find_leaks(text, users):
    return sorted(u for u in users if u in text)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='익명화하지 않고, 실명이 남아 있는지만 검사한다')
    args = ap.parse_args(argv)

    source = ORIGINALS if (ORIGINALS.exists() and not args.check) else FIXTURES
    read = lambda name: (source / name).read_text(encoding='utf-8')

    if args.check:
        # 원본이 남아 있다면 그 이름들이 fixture 에 새지 않았는지 본다.
        if not ORIGINALS.exists():
            print('원본이 없어 검사할 실명 목록을 알 수 없습니다.', file=sys.stderr)
            return 0
        real = collect_users(lambda n: (ORIGINALS / n).read_text(encoding='utf-8'))
        leaked = []
        for path in FIXTURES.iterdir():
            if path.is_file():
                leaked += [(path.name, u)
                           for u in find_leaks(path.read_text(encoding='utf-8'), real)]
        if leaked:
            for name, user in leaked:
                print(f'실명 노출: {name} 에 {user}', file=sys.stderr)
            return 1
        print(f'실명 {len(real)}개 모두 익명화되어 있습니다.')
        return 0

    # 원본을 아직 보관하지 않았다면 지금 옮겨둔다.
    if not ORIGINALS.exists():
        ORIGINALS.mkdir(parents=True)
        for name in TARGETS:
            shutil.copy2(FIXTURES / name, ORIGINALS / name)

    users = collect_users(read)
    me = read('assoc.txt').split('|')[0].strip() or None
    mapping = build_mapping(users, me)

    for name in TARGETS:
        text = read(name)
        (FIXTURES / name).write_text(substitute(text, mapping), encoding='utf-8')

    print(f'계정 {len(mapping)}개 익명화 완료. 본인({me}) -> user01')
    print(f'원본: {ORIGINALS.relative_to(ROOT)}/ (커밋되지 않음)')
    for user in list(mapping)[:3]:
        print(f'  {user} -> {mapping[user]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
