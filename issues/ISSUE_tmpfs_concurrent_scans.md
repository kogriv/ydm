# Issue: tmpfs DB Schema Loss During Scan

## Problem
Scan crashed at 6550 files with error:
```
sqlite3.OperationalError: no such table: scan_progress
```

## Root Cause
Second instance of `ydm.py scan` deleted shared tmpfs DB (`/dev/shm/ydm_scan.db`) via `reset_temp_db()` during active scan.

**Sequence:**
1. First scan running → tmpfs DB exists with schema
2. Second scan starts → calls `reset_temp_db()` → **deletes shared tmpfs DB**
3. First scan calls `save_checkpoint()` → `get_connection()` → creates **empty DB without schema**
4. INSERT fails → crash

## Solution (3 Levels)

### Level 1: Lock File (Prevention)
- Lock file `/tmp/ydm_cloud_scan.lock` with PID
- Check for running process before scan
- Auto-cleanup stale locks
- **Location:** ydm.py:1445-1480, 1604-1610

### Level 2: Auto-Recovery (Resilience)
- Detect missing schema in `get_connection()`
- Restore from disk checkpoint if active scan
- Continue from last saved state
- **Location:** ydm.py:125-151

### Level 3: Process Isolation (Defense in Depth)
- Process-specific tmpfs path: `/dev/shm/ydm_scan_{PID}.db`
- Eliminates conflicts even if lock fails
- **Location:** ydm.py:107-108, 24-25

## Testing
```bash
# Test concurrent scan prevention
python3 ydm.py scan cloud --progress &
sleep 5
python3 ydm.py scan cloud  # Should fail with lock error

# Test recovery (simulate DB deletion during scan)
python3 ydm.py scan cloud --progress &
SCAN_PID=$!
sleep 30
rm /dev/shm/ydm_scan_${SCAN_PID}.db  # Should auto-recover
```

## Status
✅ Fixed in commit (2026-01-06)
