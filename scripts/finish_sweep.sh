#!/usr/bin/env bash
# Everything that has to happen once a sweep completes, in order, once.
set -e
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
export PATH="$HOME/.local/bin:$PATH"
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="${1:-xv2}"
echo "== 1. stamp the hardware model onto rows that recorded only 'iphone' =="
./.venv/bin/python scripts/backfill_device.py "$RUN"
echo "== 2. the analysis that decides whether this ranks anything =="
./.venv/bin/python scripts/analyse_models.py "$RUN"
echo "== 3. rebuild the site (the new run claims the board only if complete) =="
./.venv/bin/python -c "import sys;sys.path.insert(0,'.');from phoneshell.bench.site import build,_board_run;print('board run:',_board_run());build('v4')"
echo "== 4. re-export the dataset =="
./.venv/bin/python scripts/export_dataset.py runtime/export
echo "== 5. publishability gate =="
./.venv/bin/python scripts/check_publishable.py
echo "ready to deploy:  npx wrangler pages deploy site --project-name=blolabel --branch=main --commit-dirty=true"
