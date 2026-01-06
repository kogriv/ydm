# Issue: Graceful Shutdown Hang During Initialization

## Problem
SIGTERM sent during scan initialization phase causes process to hang indefinitely.

**Observed behavior:**
```
Lock acquired (PID: 568177)
Starting new cloud scan 42...
Will scan from path: /Books

Received SIGTERM. Graceful shutdown requested...
<process hangs, never exits>
```

## Root Cause
Scan initialization (API calls, CloudScanner setup) happens BEFORE entering main scan loop.

**Current signal handling:**
- Signal sets `_terminate_requested = True`
- Main scan loop checks this flag between iterations
- **BUT:** If scan is in initialization phase, loop hasn't started yet → flag never checked

**Code location:**
```python
# ydm.py:1554-1556
scanner = CloudScanner(client, ...)  # May block here
files_count = scanner.scan(...)       # Or here
```

## Impact
- Medium: Only affects scans interrupted during first 2-3 minutes
- Workaround: Use SIGKILL (`kill -9`) instead of SIGTERM during init phase
- Lock file prevents concurrent scans, so issue is contained

## Solution Options

### Option 1: Timeout on initialization
```python
# Wrap CloudScanner creation with timeout
signal.alarm(30)  # 30 sec timeout
scanner = CloudScanner(...)
signal.alarm(0)   # Clear timeout
```

### Option 2: Check _terminate_requested in CloudScanner.__init__
```python
class CloudScanner:
    def __init__(self, ...):
        global _terminate_requested
        if _terminate_requested:
            raise TerminationRequested()
        # ... rest of init
```

### Option 3: Make API calls non-blocking
Use async/await or threading for initial API calls.

## Status
⚠️ Known issue, documented
- Core fix (concurrent scan protection) is working ✓
- This is a separate edge case for future improvement

## Testing
```bash
python3 ydm.py --config-profile test scan cloud --path /Books &
PID=$!
sleep 5  # During init phase
kill -TERM $PID  # Hangs
kill -9 $PID     # Works
```
