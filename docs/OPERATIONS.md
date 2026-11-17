# Operations

How the pair is kept serving, how to check it, and how to change it without losing it. Paths assume the upstream
recipe at `~/work/mia-dsv41/repo` on the head and this repository's `ops/bin` copied to `~/ops/bin` on each node.

## What runs where

| Unit | Node | Kind | Does |
| --- | --- | --- | --- |
| `spark-thermal-blackbox.service` | both | user, always | writes a CSV row every 2 s to `~/ops/thermal-log/<UTC date>.csv` |
| `spark-thermal-guard.service` | both | user, always | stops both ranks on heat, low memory or stale telemetry |
| `spark-memguard.service` | both | user, always | kills the local rank below 1 GiB `MemAvailable` |
| `dsv41-keepalive.timer` | head | user, every 2 min | restarts both ranks when it is safe |
| `cluster-swap.service` | head | user, always | proxy and model gate on `:8010` |
| `gb10-gpu-clock.service` | both | system, oneshot | `nvidia-smi -lgc 300,2100` |
| `gb10-cpu-policy.service` | both | system, oneshot | schedutil on all cores, X925 cores capped at 2.808 GHz |
| `omp-cluster-proxy.service` | workstation | user, always | client gateway on `127.0.0.1:8080` |
| `scripts/memguard.sh` | both | started by `start.sh` | the recipe's own memory watchdog |

User units need lingering (`loginctl enable-linger $USER`) so they run without a login session.

## Install

On both nodes:

```bash
mkdir -p ~/ops/bin ~/.config/systemd/user
cp ops/bin/spark-thermal-blackbox ops/bin/spark-thermal-guard ops/bin/spark-memguard ~/ops/bin/
cp ops/systemd/user/spark-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now spark-thermal-blackbox spark-thermal-guard spark-memguard

sudo install -m 755 ops/sbin/gb10-cpu-policy /usr/local/sbin/
sudo cp ops/systemd/system/gb10-*.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now gb10-gpu-clock gb10-cpu-policy
```

On the head only:

```bash
cp ops/bin/dsv41-keepalive ops/bin/cluster-swap.py ~/ops/bin/
cp -r ops/systemd/user/dsv41-keepalive.* ops/systemd/user/cluster-swap.service* ~/.config/systemd/user/
cp ops/state/cluster-profiles.example.json ~/ops/state/cluster-profiles.json
install -m 600 /dev/null ~/.config/cluster-swap/token   # then write a random token into it
systemctl --user daemon-reload
systemctl --user enable --now cluster-swap dsv41-keepalive.timer
```

Before enabling anything, edit the placeholders: worker address and user (`user@192.0.2.2`), the head's link address
(`192.0.2.1`), and in `spark-thermal-guard` the `PEERS` map from each node's hostname to the other node's ssh target.
The guard on each node must be able to ssh to the other with `StrictHostKeyChecking=yes`.

## Thermal black box and guard

The black box records `utc, gpu_c, gpu_w, gpu_util, sm_mhz, clk_reasons, memavail_mib, swap_mib, load1` and then one
column per `/sys/class/thermal/thermal_zone*`. Each row is fsynced, and the folder keeps 14 daily files.

The guard reads the newest row every 2 s and acts on:

| Condition | Action |
| --- | --- |
| hottest of GPU and all zones >= 86 C | log a warning |
| hottest >= 89 C for 3 consecutive rows (6 s) | stop the engine here and on the peer, append to `TRIPPED` |
| `MemAvailable` < 1,024 MiB for 2 consecutive rows | same |
| newest row missing, malformed or older than 10 s while an engine runs | same |

It never starts anything. After a successful stop it sleeps 60 s. Change the thresholds with `TRIP_C`, `WARN_C`,
`HOLD` and `MEM_FLOOR_MIB` in the unit's environment; we have not changed them to buy uptime, and do not recommend it.

On GB10 the hottest reading is an ACPI zone (`acpitz`), not the GPU: under load the gap was a median 5-6 C and a
95th percentile 15-17 C on 2026-10-27 ([M11](evidence/2026-10-27-measurements.md)).
Between 2026-10-22 and 2026-10-27 19:40Z the guard stopped the pair 9 times on heat, 7 times from the head and twice
from the worker ([evidence](evidence/2026-10-27-thermal-trips.txt)). Since the rack change of 2026-10-27 09:20Z there
have been two trips, both on the worker, at 18:52Z and 19:16Z. After the second, the keepalive restarted the pair at
19:16:59Z and it was serving again at 19:17:50Z.

## Memory guards

Three things watch `MemAvailable`, because GB10 does not fail cleanly when a rank takes all of memory: the machine
stops answering and needs a power cycle (upstream README).

1. The recipe's `scripts/memguard.sh`, started by `start.sh` on both nodes: kills the rank below 2.5 GiB held for 4 s,
   or below 1 GiB in two samples, sampling every 0.25 s. A dip that recovers is logged, not killed.
2. `spark-memguard`: kills the local rank below 1 GiB, sampling every second, and logs the top RSS processes every 10 s
   while below 4 GiB. Log: `~/ops/memguard/memguard.log`, kills in `~/ops/memguard/TRIPPED`.
3. The thermal guard's memory rule above, which stops both ranks.

The last two are a backstop in case the recipe's guard is not running, for example while `start.sh` is between steps.

## Keepalive and the restart budget

`dsv41-keepalive` runs every 2 minutes from its timer and exits at once when the engine answers. When the engine is
down it restarts both ranks with `./start.sh restart`, but only if all of these hold:

| Gate | Default | Override |
| --- | --- | --- |
| hold files absent | `~/ops/state/OWNER_AWAY_HOLD`, `~/ops/state/THERMAL_QUALIFICATION` | remove the file |
| no `start.sh` or `prepare.sh` already running | | |
| a container started less than 15 min ago is still loading, not down | 900 s | |
| worker reachable over ssh | | |
| head zones 0 and 2 and worker zone 0 below | 70 C | `KEEPALIVE_COOL_C` |
| last guard trip at least | 900 s ago | `KEEPALIVE_TRIP_QUIET_S` |
| restarts in the window below | 4 per 21,600 s | `KEEPALIVE_MAX_RESTARTS`, `KEEPALIVE_WINDOW_S` |

When the budget runs out it logs `needs a human` and stops trying. Its log is `~/ops/logs/dsv41-keepalive.log`, and
each restart's full output goes to `~/.cache/dsv41-tensorfold/logs/keepalive-<time>.log`.

Pause it for planned work and always start it again afterwards:

```bash
systemctl --user stop dsv41-keepalive.timer
# ... tuning or maintenance ...
systemctl --user start dsv41-keepalive.timer
```

`THERMAL_QUALIFICATION` also makes `cluster-swap` answer 503 to normal traffic, so it is not a quiet hold.

## Health checks

From the workstation:

```bash
curl -s http://127.0.0.1:8080/health
# {"proxy":"ok","vllm_up":true,"loaded":"DeepSeek-V4.1-Flash-1017-EXL3","profile":"dsv41-1017-tf","swap_state":"idle",...}
curl -s http://127.0.0.1:8080/v1/models
```

Trust `vllm_up` and `loaded`. `swap_state: failed` can remain from an old swap record while the engine serves normally.

Useful `/metrics` keys (TensorFold names, prefix `tensorfold:`): `requests_running`, `requests_waiting`,
`preemptions_total`, `prompt_tokens_total`, `generation_tokens_total`, `request_decode_seconds_sum`,
`mtp_drafted_total`, `mtp_accepted_total`. Average decode speed per request is
`generation_tokens_total / request_decode_seconds_sum`. Counters reset when the engine restarts.

On the head:

```bash
docker logs --since 10m dsv41-1017-tf | grep "prompt cache"   # hits, misses, evictions, MiB used of 512
docker inspect dsv41-1017-tf -f '{{json .Args}}'             # expect --parallel 8 --context 1048576 --spill-gib 64
docker inspect dsv41-1017-tf -f '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'RDMA_NAP|STREAMS'
tail -n 1 ~/ops/thermal-log/$(date -u +%F).csv
tail -n 5 ~/ops/thermal-log/TRIPPED ~/ops/logs/dsv41-keepalive.log
```

Throughput check, from the workstation: `ops/bin/cluster-bench` runs c1 x3, c4 and c8 on a fixed prompt and compares
with the saved baseline, flagging drift past 3 %. It adds load, so skip it when either SoC is within about 3 C of 83 C or
when live sessions are running.

## Changing settings

Settings live in the recipe's `.env` (see [`recipe/env`](../recipe/env)). To test one:

1. Stop the keepalive timer.
2. Run `cluster-bench --json` for a baseline, cold: put a nonce in any long prompt, or the prefix cache answers it.
3. `cd ~/work/mia-dsv41/repo && ./start.sh restart`, optionally with `KEY=value` before it to try without editing
   `.env`. `start.sh` checks rank 0's flags against the image before it stops anything, so a bad flag costs nothing.
4. Bench again. Keep the change only if c4 or c8 moves more than 3 %, or a temperature you care about moves.
5. Start the keepalive timer.

Rollback is deleting the line from `.env` and restarting. Both nodes must run the same image ID; `start.sh` refuses
otherwise, and a tag rebuilt locally on the worker does not count. Copy images with
`docker save IMAGE | ssh WORKER docker load`.

## Spill v2

Enabled 2026-10-27 19:02Z with `SPILL_GIB=64`. Both ranks check that they were started from the same image, model,
kit, Engram files and spill settings, and refuse to serve if not. On this pair the first attempt failed with
`rank 1's configuration differs from rank 0's in engram`: rank 0 read Engram from the Hugging Face snapshot, whose files
are symlinks, and the fingerprint counts only regular files. The fix is a folder of hardlinks to the blobs:

```bash
SNAP=$HF_CACHE/hub/models--deepseek-ai--DeepSeek-V4.1-Flash-1017/snapshots/1bae0e1088993b9b82de08f59de5be5a34e42170
DEST=~/.cache/dsv41-tensorfold/engram-1bae0e10
mkdir -p "$DEST"
for f in config.json model.safetensors.index.json model-00047-of-00048.safetensors model-00048-of-00048.safetensors; do
  ln "$(readlink -f "$SNAP/$f")" "$DEST/$f"
done
```

Then set `HEAD_ENGRAM_DIR` to that folder. Hardlinks cost no space; the folder and the cache must be on one file
system. Rollback: remove the `SPILL_*` and `HEAD_ENGRAM_DIR` lines and restart.

## Habits that keep throughput

- Keep CPU-heavy jobs off both nodes while serving. A `zstd` at 214 % CPU plus `tar` on the worker cut c8 throughput
  to 180-226 tok/s (from 315-337) and added about 12 C. Backups and archive jobs belong elsewhere or under
  `nice`/`ionice`.
- A busy CPU on either node looks like a thermal regression. Check `ps -eo pcpu,comm --sort=-pcpu | head` on both
  before blaming airflow.
- Leave the GPU clock cap and the engine's 12.2 GiB memory reserve alone.

## Known loose ends on this pair

- The profile registry still lists `slots: 4` for this profile. `cluster-swap` only reports that field; the engine runs
  8 streams.
- The engine has no API key. On this pair a small TCP forwarder also exposes the head's `:8000` and `:8010` on its
  Tailscale address, so anything on the tailnet can reach the engine without the `:8010` gate. Restrict it with
  Tailscale ACLs, or drop the `:8000` forwarder.
- The engine container's cpuset is empty, so it runs on all 20 cores. Earlier notes describe pinning to the A725
  cores; that pin is not in force.
- The live registry wraps `start.sh` in a local launch-guard script that is not published here.
- The keepalive reads only the head's `TRIPPED` file, so a trip on the worker does not start its 15-minute quiet
  period. On 2026-10-27 the worker tripped at 18:52:10Z and the keepalive tried a restart at 18:52:44Z, once all
  three zones read below 70 C.
