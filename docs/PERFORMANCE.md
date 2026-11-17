# Performance

Every number here was measured on this pair on 2026-10-27 through the full client path (workstation gateway, head
proxy, engine). IDs such as M1 point at [evidence/2026-10-27-measurements.md](evidence/2026-10-27-measurements.md).
Benchmarks used a fixed code prompt, temperature 0, thinking off and `max_tokens` 600 unless the entry says otherwise.
Short runs vary by a few percent between identical tries, so we keep a change only when c4 or c8 moves more than 3 %.

## Where we ended up

| | Recipe defaults | Now | ID |
| --- | ---: | ---: | --- |
| c1 decode | 94.8 tok/s | 96.1 tok/s | M1, M3 |
| c4 aggregate | 113.4 tok/s | 197.4 tok/s | M1, M3 |
| c8 aggregate | 187.7 tok/s | 327.9 tok/s | M1, M3 |
| SoC peak, sustained c8 | 83.8 C | 70.5 C head, 68.9 C worker | M1, M2 |

## Levers that worked

| Lever | Effect | ID |
| --- | --- | --- |
| `PARALLEL=8` (recipe default 4) | c4 +73 %, c8 +74 %, c1 flat; `MemAvailable` after load unchanged (7.1 -> 7.0 GiB) | M1 |
| `RDMA_NAP_US=2000` (recipe default 500) | sustained c8 SoC peak 83.8 -> 77.8 C, throughput unchanged | M1 |
| Rack order: head at the bottom, worker in the middle | sustained SoC peak 77.8 -> 70.5 C (head) and 77.6 -> 68.9 C (worker); c8 within noise | M2 |
| No CPU-heavy jobs on the nodes | one `zstd` at 214 % CPU plus `tar` on the worker cost 30-45 % of c8 throughput and about 12 C | M2 |

Why the nap helps: each rank's RDMA proxy polls its completion queues on a CPU core. After 2 s without traffic
(`RDMA_IDLE_S`) it sleeps `RDMA_NAP_US` between looks instead of spinning; upstream measured the proxy at about 2 % of a
core per rank with a 500 µs nap, against about 100 % without one. A decode round never sees a nap, and the first
request after 20 s or 60 s of idle took 1.21-1.28 s against 1.21-1.23 s for the follow-up (M3), so the longer nap
cost nothing we could measure. It gave the 6 C lower peak in M1.

Why 8 streams help while one stream does not move: each decode round reads the active weights and crosses the ring
twice per layer whether it carries one stream or eight, so more streams share a cost that one stream pays alone. No
setting we tried changed single-stream speed; with DSpark drafting it already returns about 4.3 tokens per round on
code text (M1).

## Levers that did nothing

| Lever | Result | ID |
| --- | --- | --- |
| GPU clock cap 2100 -> 2418 MHz | c8 +2.8 % (inside run-to-run variation); worker SoC 68.9 -> 83.5 C. Reverted. | M4 |
| GPU clock cap 3003 MHz | same as 2418; the board settles near 2392 MHz on its own | M4 |
| GPU clock cap 2200 MHz | same as 2100 | M4 |
| X925 CPU cap 2.808 -> 3.9 GHz | cold prefill and decode unchanged | M4 |
| `TF_DS_PREFILL_CHUNK=4096` | 45.3K prefill 4,301 -> 4,322 tok/s; 11.3K 2,152 -> 2,078 | M1 |
| `ROUND_ROWS=16` | c8 329 -> 323 tok/s | M1 |
| Pool 262,144 instead of 1,048,576 positions | c1 94.7, c4 196.8, c8 329.6: same as 1M, so we keep 1M | M3 |
| Shorter `RDMA_IDLE_S` | no idle-wake penalty exists to remove | M3 |
| mlx5 IRQ affinity | already one queue per core on all 20 CPUs | M3 |
| `--mtp-drafts N` | refused by this image; draft depth comes from the checkpoint's draft blocks | M1 |

We keep the GPU at 300-2100 MHz, the clock range NVIDIA staff suggested on the developer forum (thread t/377478) to
avoid power transients. Upstream measured that a GPU locked at 1,800 MHz on one node costs 3-6 % of prefill; matching
caps on both nodes matter more than the cap itself.

## Where the remaining time goes

A decode round takes about 47 ms. Reading the active weights once per round at the pair's memory bandwidth would take
about 10.6 ms (M4). The gap is in the engine's EXL3 dequantization path, which no host setting reaches. The options left
are upstream kernel work, or a second pair of machines (TP=4) for more aggregate throughput.

## Prefill and the prompt cache

Cold prefill on this pair, by tool:

| Prompt | Speed | ID |
| --- | ---: | --- |
| Fixed text, 11.3K / 22.7K / 45.3K tokens | 2,152 / 3,914 / 4,322 tok/s | M1 |
| Upstream `tools/bench.py` random words, 2K / 8K / 30K | 1,526 / 1,924 / 2,069 tok/s | M5 |
| An agent's 65,915-token system prompt, spill on | 35.0 s, about 1,880 tok/s | M8 |

Prefill speed depends on the prompt as much as its length, so compare like with like.

The engine keeps prompt states (512 MiB on rank 0) and resumes a new request from the longest kept prefix, in steps of
8,192 tokens. Agent traffic is where this pays. Over one 8.7-hour run of real agent sessions (M7):

- 968 requests carried 76.6M prompt tokens and produced 0.46M tokens, a 165:1 ratio. The median prompt was 94,397
  tokens.
- 95.85 % of prompt tokens came from the cache; 3.2M (4.15 %) were computed.
- 277 requests had no cache hit at all. At the end of the run the cache held 79 states in 503 of 512 MiB and had
  evicted 183.
- DSpark accepted 67.1 % of drafted tokens. On the fixed code prompt acceptance was 77-78 % at c1 and 88-92 % at c4/c8
  (M1), so agent text drafts worse than code.

Three things broke reuse on this pair.

1. Images, on the previous engine image. A request that carried an image never resumed: an identical 9.2K-token
   request with one PNG logged `resumed at 0` and `cached=0` twice, while the same text without the image resumed at
   8,192 (M6). An agent session with one screenshot in its history re-prefilled 79K-90K tokens every turn at about
   600 tok/s, roughly 2.5 minutes per tool call. Our client-side fix was to declare the model text-only to the agents,
   which then send a separate vision model's text description, so each turn's prompt extends the last one byte for
   byte. The current image includes the recipe's patch 0006, which keys an image's positions by its content so image
   prompts can resume (upstream measured turn 2 of a 27K-token image prompt resuming 26,624 tokens in 0.8 s). We have
   not re-measured image prompts on the current image.
2. A prompt that changes early. Reuse stops at the first token that differs from a kept state, so everything after it
   is computed again. Two conversations with the same system prompt shared 63,488 of 65,499 tokens (4.8 s against
   35.0 s cold), but one rewrite of a tool description changed the agent system prompt at character 1,836, and the
   next session matched only the tokens before it (M13).
3. Eviction. Eight streams of 90K-token sessions fill 512 MiB, and the oldest states go first (183 evictions in M7).

## Spill v2

Spill v2 writes evicted prompt states to each node's NVMe and reads them back instead of recomputing. We enabled it at
2026-10-27 19:02Z with a 64 GiB cap per node (M8).

- The first start failed with `rank 1's configuration differs from rank 0's in engram`. The spill fingerprint lists the
  regular files in each rank's Engram folder. Rank 0 read Engram from the Hugging Face snapshot, whose entries are
  symlinks, so its list was empty. A folder of hardlinks to the same blobs (`HEAD_ENGRAM_DIR`) fixed it. See
  [OPERATIONS.md](OPERATIONS.md#spill-v2).
- After the start: a cold 65,915-token system prompt took 35.0 s. A second request with the same system prompt and a
  different user turn reused 63,488 tokens and answered in 4.8 s.
- Head `MemAvailable` a few minutes after the load read 5.6-6.0 GiB, against 7.0 GiB without spill earlier in the day
  (M9). The readings move by about a GiB as prompts come and go, so this is not a precise cost.
- After an engine restart the kept states come back from disk. At 19:16Z the thermal guard stopped the pair and the
  keepalive restarted it at 19:17Z. At 19:37Z a 65,499-token request with the same system prompt logged `restored 63488
  tokens from disk`, resumed at 63,488 and finished in 3.9 s, against 35.0 s cold (M12). Restore after an eviction
  without a restart has not been measured separately.

## Previous image

On the image the pair ran before the recipe migration, 8 streams stalled the engine once 5 or more requests ran at
once, with no error and no progress until a restart (M10). The current image has run 8 streams since 2026-10-27; the
8.7-hour run in M7 used it.

## Thermal limits

The SoC's ACPI zones, not the GPU, set the limit. With the GPU at 80 % utilization or more, the hottest zone read a
median 5-6 C and a 95th percentile 15-17 C above the GPU, and the GPU drew a median of about 41 W (M11). The guard
stops both ranks at 89 C held for 6 s. Between 2026-10-22 and 2026-10-27 19:40Z that happened 9 times (see
[evidence/2026-10-27-thermal-trips.txt](evidence/2026-10-27-thermal-trips.txt)), 4 of them on 2026-10-22 and the
last two on the worker. Airflow around both nodes is the remaining physical lever.
