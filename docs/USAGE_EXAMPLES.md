#!/bin/bash
# Usage Examples for YDM Bug Fixes
# Run these commands to test the fixed functionality

YDM="python3 ydm.py"
DB="monitor.db"

echo "========================================"
echo "YDM Bug Fix Examples & Usage"
echo "========================================"
echo ""

echo "Sync Manager (simple_sync) examples"
echo "----------------------------------"
echo "Show sync tree (JSON default):"
echo "   $ python3 tools/sync_tree.py --path /DAO --depth 2"
echo ""
echo "Show full tree with branches (text):"
echo "   $ python3 tools/sync_tree.py --path /DAO --depth 2 --format text --text-tree --show-all"
echo ""
echo "Disable local scan and sync_percent:"
echo "   $ python3 tools/sync_tree.py --no-local-scan --no-sync-percent"
echo ""
echo "Exclude management (dry-run):"
echo "   $ python3 tools/sync_exclude.py add --path /DAO/2"
echo ""
echo "Apply exclude change:"
echo "   $ python3 tools/sync_exclude.py remove --path /DAO/2 --apply"
echo ""
echo "Text output without header:"
echo "   $ python3 tools/sync_exclude.py add --path /DAO/2 --format text --no-text-header"
echo ""
echo "Disable daemon restart and local scan:"
echo "   $ python3 tools/sync_exclude.py add --path /DAO/2 --apply --no-restart-daemon --no-local-scan"
echo ""

# Example 1: Initialize database
echo "1. Initialize database (if not already done)"
echo "   $ $YDM --db-path $DB init"
echo ""

# Example 2: Check for crashed scans on startup
echo "2. When you run a scan, crashed scans are auto-detected:"
echo "   $ $YDM scan cloud --progress"
echo "   Output:"
echo "   ⚠️  Warning: Found 1 crashed scan(s) from previous runs:"
echo "     - Scan 8 (cloud) at 2025-01-05 19:30:15"
echo ""

# Example 3: Use analyze-scan to check data integrity
echo "3. Check if a scan has data integrity issues:"
echo "   $ $YDM --format json report analyze-scan --scan-id 8"
echo ""
echo "   This will show:"
echo "   - File count (should be > 0 if successful)"
echo "   - Folder count"
echo "   - Progress breakdown (completed/in_progress/pending)"
echo "   - ⚠️ Warnings if something looks wrong"
echo ""

# Example 4: Find long paths
echo "4. Find files with paths exceeding 240 characters:"
echo "   $ $YDM --format json report long-paths --scan-id 8 --limit-chars 240"
echo ""
echo "   Useful for debugging filesystem issues with long filenames"
echo ""

# Example 5: Find duplicates by hash
echo "5. Find duplicate files by MD5 hash:"
echo "   $ $YDM --format json report duplicates --scan-id 8"
echo ""
echo "   Returns:"
echo "   - Hash of duplicate files"
echo "   - Number of occurrences"
echo "   - Total size that could be freed"
echo ""

# Example 6: Find duplicates by name+size
echo "6. Find potential duplicates by size and filename:"
echo "   $ $YDM --format json report duplicates --scan-id 8 --by-name"
echo ""
echo "   Useful when MD5 not available or for quick search"
echo ""

# Example 7: Signal handling demo
echo "7. Test signal handling (graceful shutdown):"
echo "   $ $YDM scan cloud --progress &"
echo "   [1] 12345"
echo ""
echo "   # Gracefully stop (data saved):"
echo "   $ kill 12345"
echo ""
echo "   # Or force checkpoint without stopping:"
echo "   $ kill -USR1 12345"
echo ""

# Example 8: Resume interrupted scan
echo "8. Resume a crashed or interrupted scan:"
echo "   $ $YDM scan cloud --resume --scan-id 8 --progress"
echo ""
echo "   This will:"
echo "   - Restore scan data from disk"
echo "   - Continue from last checkpoint"
echo "   - Resume folders that were in_progress"
echo ""

# Example 9: Diagnostic workflow
echo "9. Full diagnostic workflow:"
echo ""
echo "   Step 1: List all scans"
echo "   $ $YDM --format json report scan-list --limit 10"
echo ""
echo "   Step 2: Check latest scan for issues"
echo "   $ $YDM --format json report analyze-scan --scan-id 8"
echo ""
echo "   Step 3: If issues found, check long paths"
echo "   $ $YDM --format json report long-paths --scan-id 8"
echo ""
echo "   Step 4: If incomplete, check progress"
echo "   $ $YDM --format json report scan-progress --scan-id 8"
echo ""
echo "   Step 5: Resume if needed"
echo "   $ $YDM scan cloud --resume --scan-id 8 --progress"
echo ""

# Example 10: Watch long-running scan
echo "10. Monitor scan progress in real-time:"
echo "   Terminal 1 (run scan):"
echo "   $ $YDM scan cloud --progress"
echo ""
echo "   Terminal 2 (monitor in another window):"
echo "   $ watch -n 5 \"$YDM report scan-progress --scan-id 8 | jq '.scan.progress'\""
echo ""

# Example 11: Cleanup duplicate files (example)
echo "11. Use duplicates report to find cleanup candidates:"
echo "   $ $YDM --format json report duplicates --scan-id 8 | jq '.duplicates | sort_by(-.total_size) | .[0:5]'"
echo ""
echo "   This shows top 5 duplicate groups by total size"
echo "   You could then safely delete duplicates from these groups"
echo ""

# Example 12: JSON output for automation
echo "12. All reports support JSON output for scripting:"
echo "   $ $YDM --format json report scan-list | jq '.data.scans | map(select(.status==\"crashed\"))'"
echo ""
echo "   This finds all crashed scans in JSON output"
echo ""

echo "========================================"
echo "For more details, see:"
echo "- FIX_SCAN_DATA_LOSS_IMPLEMENTATION.md"
echo "- FIX_SUMMARY.md"
echo "- ISSUE_SCAN_DATA_LOSS.md"
echo "========================================"
