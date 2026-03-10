#!/usr/bin/env fish
# run_sweep.fish - adapted for lab04 school database agents

set -x LD_PRELOAD (pwd)/libsqlite3.so

set -l rates 10 100 500 1000
set -l duration 10
set -l writers 4
set -l db_base /tmp/sqlite_lock_test
set -l trace_dir ./traces

set -l i 1
while test $i -le (count $argv)
    switch $argv[$i]
        case --rates
            set i (math $i + 1)
            set rates (string split " " $argv[$i])
        case --duration
            set i (math $i + 1)
            set duration $argv[$i]
        case --writers
            set i (math $i + 1)
            set writers $argv[$i]
    end
    set i (math $i + 1)
end

mkdir -p $trace_dir


# CHANGED: pre-create empty log files so dashboard can discover them
for rate in $rates
    touch "$trace_dir/rate_$rate.log"
end



# CHANGED: launch dashboard in background BEFORE sweep so it streams live
echo "=== Starting dashboard at http://localhost:8050 ==="
uv run dashboard.py $trace_dir > $trace_dir/dashboard.log 2>&1 &
set dash_pid $last_pid
sleep 2
cat $trace_dir/dashboard.log  # show startup output
set dash_pid $last_pid
sleep 1   # give dash a moment to bind



# CHANGED: initialize db once before sweep
set init_db "$db_base"_init.db
uv run dbimpl_lab04.py --dbname $init_db 2>/dev/null

for rate in $rates
    set db      "$db_base"_r"$rate".db
    set db      "$trace_dir/rate_$rate.db"
    set logfile "$trace_dir/rate_$rate.log"

    
    rm -f $db $db-wal $db-shm
    truncate -s 0 $logfile

    set avg_wait (math "1.0 / $rate")
    set numloops (math --scale=0 "$duration * $rate / $writers")
    if test $numloops -gt 200
        set numloops 200
    end

    echo "=== Rate: $rate w/s | avg_wait: $avg_wait | numloops: $numloops | writers: $writers ==="

    # stderr (lock traces) -> logfile; stdout (lab04 log()) -> terminal
    uv run dbimpl_lab04.py \
        --dbname $db \
        --admit  $numloops $avg_wait \
        --enroll $numloops $avg_wait \
        --offer  $numloops $avg_wait \
        --check  $numloops $avg_wait \
        --report $numloops $avg_wait \
        2>$logfile

    set lines (wc -l < $logfile | string trim)
    echo "  -> $logfile ($lines trace lines)"
end

echo ""
echo "=== Sweep complete. Dashboard still running (pid $dash_pid) ==="
echo "    Press Ctrl-C to stop."
wait $dash_pid