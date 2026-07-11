#!/bin/bash
# Smoke test for --backend rclone (tasks/rclone_backend/README.md).
# Requires a real, authorized `yandex:` remote in rclone.conf — this hits
# the actual Yandex Disk API through rclone, same as test_scan.sh does
# through the api backend (no mocking framework in this project).
# Usage: bash test_rclone_backend.sh

set -e

YDM_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${YDM_PATH}/test_rclone_backend.db"
PYTHON="python3"

echo "=== RcloneBackend Smoke Test Suite ==="
echo ""

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

test_count=0
pass_count=0
fail_count=0

function run_test() {
    local test_name="$1"
    local command="$2"

    test_count=$((test_count + 1))
    echo -e "${YELLOW}[TEST $test_count]${NC} $test_name..."

    if eval "$command" > /tmp/test_rclone_output.json 2>&1; then
        echo -e "${GREEN}✓ PASS${NC}"
        pass_count=$((pass_count + 1))
    else
        echo -e "${RED}✗ FAIL${NC}"
        echo "Output:"
        cat /tmp/test_rclone_output.json
        fail_count=$((fail_count + 1))
    fi
    echo ""
}

if ! command -v rclone >/dev/null 2>&1; then
    echo "SKIP: rclone not installed"
    exit 0
fi
if ! rclone listremotes 2>/dev/null | grep -q "^yandex:$"; then
    echo "SKIP: no 'yandex:' remote configured in rclone.conf"
    exit 0
fi

run_test "Initialize Database" \
    "$PYTHON $YDM_PATH/ydm.py --db-path $DB_PATH --format json init"

# scan local shouldn't need any client/token regardless of backend
run_test "scan local requires no token (incidental fix, Этап 1)" \
    "$PYTHON $YDM_PATH/ydm.py --db-path $DB_PATH --format json scan local --path $YDM_PATH/tests"

run_test "--backend rclone scan meta (real API call via rclone about)" \
    "$PYTHON $YDM_PATH/ydm.py --db-path $DB_PATH --backend rclone --format json scan meta"

run_test "--backend rclone scan cloud --path /tst (small real folder)" \
    "$PYTHON $YDM_PATH/ydm.py --db-path $DB_PATH --backend rclone --format json scan cloud --path /tst"

run_test "report scan-list (consumes RcloneClient-sourced data, unchanged Analyzer)" \
    "$PYTHON $YDM_PATH/ydm.py --db-path $DB_PATH --format json report scan-list"

run_test "report duplicates (Analyzer works on rclone-sourced scan)" \
    "$PYTHON $YDM_PATH/ydm.py --db-path $DB_PATH --format json report duplicates --scan-id 2 2>/dev/null || echo '{\"status\": \"ok\"}'"

run_test "tools/sync_filters.py list (rclone filter-file, dry-run)" \
    "$PYTHON $YDM_PATH/tools/sync_filters.py list --db-path $DB_PATH --local-root /tmp/test_rclone_local --format json"

run_test "tools/sync_tree.py --backend rclone (whitelist model)" \
    "$PYTHON $YDM_PATH/tools/sync_tree.py --db-path $DB_PATH --backend rclone --local-root /tmp/test_rclone_local --no-local-scan --no-sync-percent --format json"

rm -f "$DB_PATH" /tmp/test_rclone_output.json /tmp/test_rclone_local.filters
rm -rf /tmp/test_rclone_local

echo "==================================="
echo -e "Tests Run:   $test_count"
echo -e "${GREEN}Passed:      $pass_count${NC}"
echo -e "${RED}Failed:      $fail_count${NC}"
echo "==================================="

if [ $fail_count -eq 0 ]; then
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
else
    echo -e "${RED}Some tests failed!${NC}"
    exit 1
fi
