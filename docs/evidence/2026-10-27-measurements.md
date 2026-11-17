# Measurement log, 2026-10-27

Transcribed from the operators' dated notes and logs for this pair. All times UTC. All requests went through the
workstation gateway (127.0.0.1:8080 -> head :8010 -> engine :8000). Each section ID is cited from the other docs.

## M1. Stream count and RDMA nap (09:01-09:13Z)

Fixed code prompt, temperature 0, thinking off, `max_tokens` 600. "Before" is the recipe default (`PARALLEL=4`,
`RDMA_NAP_US=500`).

| Metric | Before | After | Delta |
| --- | ---: | ---: | ---: |
| c1 decode | 94.8 tok/s | 95.6 tok/s | flat |
| c4 aggregate | 113.4 tok/s | 196.4 tok/s | +73 % |
| c8 aggregate | 187.7 tok/s | 327.4 tok/s | +74 % |
| Sustained 50 s c8 | not run | 337.2 tok/s | |
| SoC peak, sustained c8 | 83.8 C | 77.8 C | -6 C |
| Idle SoC / GPU afterwards | 71 / 60 C | 55.7 / 53 C | |
| MemAvailable on the head after load | 7.1 GiB | 7.0 GiB | flat |

Also measured in M1: cold prefill 2,152 tok/s at 11.3K tokens, 3,914 at 22.7K, 4,322 at 45.3K. A repeated prompt
cost 0.04-0.15x of its first pass (45K prompt: 10.5 s first, 0.44 s repeat). DSpark acceptance on this prompt was
77-78 % at c1 and 88-92 % at c4/c8, about 4.3 drafted tokens per round.

Tried and reverted in M1:

| Setting | Result |
| --- | --- |
| `--mtp-drafts N` | refused by the image: "--mtp-drafts is not served by this tensorfold-native yet" |
| `TF_DS_PREFILL_CHUNK=4096` (default 2048) | 11.3K: 2,152 -> 2,078 tok/s; 45.3K: 4,301 -> 4,322 tok/s |
| `ROUND_ROWS=16` | c8 329 -> 323 tok/s |

## M2. Rack order (09:20Z)

Order changed to head at the bottom, worker in the middle, a small desktop machine on top.

| Metric | Before | After |
| --- | ---: | ---: |
| Sustained 60 s c8 | 337.2 tok/s | 329.1 tok/s |
| SoC peak, head | 77.8 C | 70.5 C |
| SoC peak, worker | 77.6 C | 68.9 C |
| Idle SoC head / worker | 55.7 / not recorded | 48.8 / 50.8 C |
| c1 / c4 / c8 | 95.6 / 196.4 / 327.4 | 96.6 / 197.9 / 314.8 |

During this session a `zstd` (214 % CPU) plus `tar` job on the worker gave 180-226 tok/s at c8 and an 83 C worker
SoC. Without it the numbers above returned. The operators' note puts the job's cost at 30-45 % of cluster throughput
and about 12 C of SoC heat.

## M3. Context pool and idle wake (between 09:00 and 10:10Z)

| Lever | Result |
| --- | --- |
| Pool 262,144 vs 1,048,576 | c1 94.7, c4 196.8, c8 329.6 tok/s at 262K: same as 1M |
| `RDMA_IDLE_S` wake cost | first request after 20 s and 60 s idle: 1.21-1.28 s; follow-up 1.21-1.23 s |
| RoCE rails | both rails at 200,000 Mb/s, MTU 9000, GID index 3 on both ranks |
| mlx5 IRQs | already one queue per core across all 20 CPUs |

Saved baseline from this session: c1 96.1, c4 197.4, c8 327.9 tok/s, SoC peak 70.5 C
([cluster-bench-baseline-2026-10-27.json](cluster-bench-baseline-2026-10-27.json)).

## M4. Clocks (10:40-10:48Z), applied as root, then reverted

| Lever | Result |
| --- | --- |
| GPU cap 2100 -> 2418 MHz (`nvidia-smi -lgc 300,2418`, both nodes) | c1 96.1 -> 97.9, c4 197.4 -> 200.5, c8 327.9 -> 337.0 (+2.8 %); sustained worker SoC 68.9 -> 83.5 C |
| GPU cap 3003 MHz | same as 2418 (c8 337.0); the board settles near 2392 MHz |
| GPU cap 2200 MHz | same as 2100 (c8 329.7) |
| X925 CPU cap 2.808 -> 3.9 GHz | cold prefill 2,211 / 4,132 / 4,090 vs 2,202 / 3,984 / 4,396 tok/s; decode unchanged |

Decode round time was about 47 ms against a 10.6 ms estimate for reading the active weights once per round.

## M5. Recipe tool comparison (after 08:52Z migration)

Upstream `tools/bench.py` (random-word prompts, thinking off): prefill 2K / 8K / 30K = 1,526 / 1,924 / 2,069 tok/s;
decode 58.8 tok/s at one stream; 124.6 tok/s for four streams together. Real prompts at c1: prose 55.7, code
82.3 tok/s; code c4 195.5 tok/s. GPU capped at 2100 MHz.

## M6. Image requests and the prompt cache (06:40Z, previous image)

Engine image in use before the 08:27Z restart (the earlier recipe, without upstream patch 0006). An identical
9.2K-token request with one small PNG, sent twice, logged `resumed at 0` and `cached=0` both times, and the miss counter
did not move. The same request without the image resumed at 8,192 (kept states sit on 8,192-token boundaries). An agent
session holding one screenshot therefore re-prefilled 79K-90K tokens every turn at about 600 tok/s on that image.

## M7. Live run counters (10:10-18:52Z)

See [2026-10-27-prompt-cache-run.txt](2026-10-27-prompt-cache-run.txt).

## M8. Spill v2 (18:55Z and 19:02Z)

18:55Z: `SPILL_GIB=64` refused to start: `dsv41: rank 1's configuration differs from rank 0's in engram`
(FingerprintMismatch). Rank 0 read Engram from the Hugging Face snapshot folder, whose files are symlinks; the spill
fingerprint lists only regular files there, so rank 0's listing was empty while rank 1's local copy had two shards.

19:02Z: started with `HEAD_ENGRAM_DIR` pointing at a folder of hardlinks to the same blobs. `start.sh restart` rc=0,
log line `dsv41 spill: rank 0's store under /spill (64.0 GiB, staging 128 MiB); the ranks agreed`. Right after: a cold
65,915-token agent system prompt prefilled in 35.0 s (about 1,880 tok/s). A second request with the same system prompt
and a different user turn reused 63,488 tokens and returned in 4.8 s.
Restoring kept states from spill after a restart: see M12.

## M9. Memory after load

MemAvailable read from `/proc/meminfo` on a lightly loaded pair. It moves by about a GiB within minutes as prompts
come and go, so treat single readings as approximate.

| When | Head MemAvailable | Worker MemAvailable | Config |
| --- | ---: | ---: | --- |
| 09:13Z | 7.0 GiB | not recorded | 8 streams, spill off |
| 19:05Z | 5.58 GiB | not read | 8 streams, spill 64 GiB, 3 min after start |
| 19:06Z | 5.99 GiB | 7.90 GiB | 8 streams, spill 64 GiB, 5 min after start |

## M10. Eight streams on the previous image (before 08:15Z)

On the image the pair ran before the recipe migration (an earlier dsv41 recipe, image ID `224c6fca43b8`), `TF_DS_STREAMS=8` with
`--parallel 8` stalled the engine at 5 or more concurrent requests: `requests_running 5`, `generation_tokens_running 0`,
no error and no progress until a restart. Reproduced at c8 and c5, then reverted to 4 streams. The current image
(MiaAI-Lab recipe at 27741e1) runs 8 streams; M1 and M7 were taken on it.

## M11. GPU power and SoC-to-GPU gap (black box, 2026-10-27 00:00-19:10Z)

From each node's `~/ops/thermal-log/2026-10-27.csv`, rows with GPU utilization of 80 % or more (head 7,771 of 34,530
rows, worker 6,202 of 34,231). "Gap" is the hottest thermal zone minus the GPU temperature in the same row.

| | GPU power p50 / p95 / max | Gap p50 / p95 |
| --- | --- | --- |
| Head | 41.6 / 53.3 / 79.3 W | 6 / 15 C |
| Worker | 40.9 / 51.6 / 77.2 W | 5 / 17 C |

## M12. Spill v2 restore after an engine restart (19:16-19:37Z)

At 19:16:31Z the worker read 89 C for 3 rows and the thermal guard stopped both ranks (trip logged 19:16:38Z). The
keepalive restarted the pair at 19:16:59Z (head zones 63 / 62 C, worker 68 C) and logged `ok: serving` at 19:17:50Z,
with spill v2 still on (container label `spill=64/50/0/8192`).

At 19:37:15Z a 65,499-token request carrying the same agent system prompt as before the restart logged
`prompt cache: restored 63488 tokens from disk`, then `65499 tokens, resumed at 63488`, and finished at 19:37:18Z
(`prompt=65499 cached=63488`). The request took 3.9 s, against 35.0 s for the cold 65,915-token prompt in M8.

## M13. When a shared prefix stops matching (2026-10-27)

Two requests with an identical system prompt and a different last user turn shared 63,488 of 65,499 prompt tokens
(4.8 s against 35.0 s cold, M8). Between agent sessions the system prompt is not always byte-identical: one rewrite of
a tool description changed the text at character 1,836, so the new session matched only the tokens before that point
and prefilled the rest cold.
