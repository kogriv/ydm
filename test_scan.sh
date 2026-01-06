#!/bin/bash

echo "=== TEST: Cloud scan with checkpoint monitoring ==="
echo ""

# Start scan in background
echo "Starting scan of / (root directory)..."
timeout 30 python3 ydm.py scan cloud --progress 2>&1 > /tmp/full_scan.log &
SCAN_PID=$!
echo "Scan PID: $SCAN_PID"

# Monitor checkpoint activity
echo ""
echo "Monitoring tmpfs DB size..."
for i in {1..30}; do
  if [ -f /dev/shm/ydm_scan.db ]; then
    SIZE=$(ls -lh /dev/shm/ydm_scan.db 2>/dev/null | awk '{print $5}')
    echo "[$i] tmpfs DB: $SIZE"
    sleep 1
  else
    echo "[$i] tmpfs DB not found (scan finished or not started)"
    sleep 1
  fi
done

echo ""
echo "Scan completed. Checking results..."
wait $SCAN_PID 2>/dev/null

# Show last lines of scan
echo ""
echo "=== SCAN OUTPUT (last 20 lines) ==="
tail -20 /tmp/full_scan.log

