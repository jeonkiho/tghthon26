#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

# 로컬 비밀(SERAPH_SLACK_TOKEN 등)을 담는 파일. 깃에 올리지 않는다(.gitignore).
# 있으면 자동으로 불러오므로 매번 export 하지 않아도 된다.
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install -r requirements-api.txt
exec .venv/bin/python run_gui.py
