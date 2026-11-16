#!/bin/bash
# ALIS-L4 daily chain. Snapshot BEFORE settlement; analyses after.
cd "$(dirname "$[amount]")" || exit 1
PY=/usr/bin/python3
echo "===== ALIS-L4 CHAIN $(date '+%Y-%m-%d %H:%M') ====="
echo "--- snapshot (freeze beliefs before outcomes) ---"
$PY apex_selection_snapshot.py --snapshot
echo "--- Phase 0: selection quality ---"
$PY apex_selection_quality.py --measure --write
echo "--- Phase 1: projection analysis ---"
$PY apex_projection_analysis.py --analyze --write
echo "--- Phase 2: root cause ---"
$PY apex_root_cause.py --classify --write
echo "--- Phase 3: registry / lessons / knowledge base ---"
$PY apex_error_registry.py --build --write
echo "--- Phase 4: validation loop ---"
$PY apex_validation_loop.py --report --write
echo "===== CHAIN COMPLETE ====="
