#!/usr/bin/env bash
# trader-auto: one trading day of ticks. Run it from the project folder (the one
# with trader/ and .env). The Kite login (python3 trader-kite/login.py) is a human
# step before this starts: it needs your TOTP, and the session dies at 06:00 IST.
#
#   bash day-trader-skill/trader-auto/run-day.sh            # ticks until 15:20 IST or the state says done
#   bash day-trader-skill/trader-auto/run-day.sh --dry-run  # the same loop, never an order
#
# Every tick is `python3 <this folder>/auto.py tick`, then a pause of
# auto.tick_seconds from trader/config.json (default 60). Output goes to
# trader/auto/<date>.log. The loop ends at 15:20 IST, or earlier when the day's
# state file says phase "done" with the summary written, or "closed" (not a
# trading day).
set -u
export TZ=Asia/Kolkata
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
CONFIG="${CONFIG:-trader/config.json}"
END_HHMM="${END_HHMM:-1520}"
EXTRA=("$@")

if [ ! -f "$CONFIG" ]; then
  echo "run-day.sh: no $CONFIG here. Run this from the project folder (cp day-trader-skill/trader-auto/config.auto.example.json trader/config.json first)." >&2
  exit 1
fi
mkdir -p trader/auto
DATE="$(date +%Y-%m-%d)"
LOG="trader/auto/$DATE.log"
STATE="trader/auto/$DATE.json"
TICK="$("$PYTHON" - "$CONFIG" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        cfg = json.load(fh)
    print(int((cfg.get("auto") or {}).get("tick_seconds") or 60))
except Exception:
    print(60)
PY
)"
[ "$TICK" -ge 15 ] 2>/dev/null || TICK=60

echo "$(date '+%Y-%m-%d %H:%M:%S') IST  run-day.sh start: tick every ${TICK}s until ${END_HHMM} IST, log $LOG, args: ${EXTRA[*]:-none}" | tee -a "$LOG"
while true; do
  NOW="$(date +%H%M)"
  if [ "$NOW" -ge "$END_HHMM" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') IST  past ${END_HHMM}: stopping" | tee -a "$LOG"
    break
  fi
  echo "--- $(date '+%Y-%m-%d %H:%M:%S') IST tick" >> "$LOG"
  "$PYTHON" "$HERE/auto.py" tick "${EXTRA[@]}" >> "$LOG" 2>&1
  CODE=$?
  if [ -f "$STATE" ]; then
    PHASE="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("phase",""))' "$STATE" 2>/dev/null)"
    if [ "$PHASE" = "closed" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') IST  not a trading day: stopping" | tee -a "$LOG"
      break
    fi
    if [ "$PHASE" = "done" ] && "$PYTHON" -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("summary") else 1)' "$STATE" 2>/dev/null; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') IST  day summarised: stopping" | tee -a "$LOG"
      break
    fi
  fi
  if [ "$CODE" -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') IST  tick exit $CODE (login needed, closed day, or a refusal: see above)" >> "$LOG"
  fi
  sleep "$TICK"
done
echo "$(date '+%Y-%m-%d %H:%M:%S') IST  run-day.sh end. Review: python3 $HERE/auto.py report" | tee -a "$LOG"
