# Deployment reference: DeepSeek-V4.1-Flash-1017 on a DGX Spark and an ASUS Ascent GX10

This repository documents one running deployment: DeepSeek-V4.1-Flash-1017, quantized to 2.9 bits per weight in EXL3
and patched with an abliteration overlay, served by TensorFold's native engine with tensor parallelism across two GB10
machines. It holds the settings we run with, one patch to the upstream recipe, the watchdogs and service units that keep
the pair serving, and the measurements behind each setting.

The recipe that does the real work is
[MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks](https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks).
This repository pins it, adds our deltas, and does not copy it. Model weights are not here.

| | |
| --- | --- |
| Model | [deepseek-ai/DeepSeek-V4.1-Flash-1017](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017) (MIT) |
| Quantization | [Mia-AiLab/DeepSeek-V4.1-Flash-1017-EXL3-EAQ-v1-2.9-bpw](https://huggingface.co/Mia-AiLab/DeepSeek-V4.1-Flash-1017-EXL3-EAQ-v1-2.9-bpw) @ `45ee5d0f` (MIT, 196 GiB) |
| Overlay | [drowzeys/keys-DeepSeek-V4.1-Flash-1017-Abliterated](https://huggingface.co/drowzeys/keys-DeepSeek-V4.1-Flash-1017-Abliterated) @ `0bdc2036` (gated; get your own access) |
| Engine | [TensorFold](https://github.com/ashhart/TensorFold) native (Zig), BertholomusAI's `dsv41` family @ `629c524`, TP=2 |
| Recipe | MiaAI-Lab recipe @ [`27741e1`](https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks/commit/27741e176a9e5b98c10c9c7d012fd3e4fcdc19b6), image `tensorfold-dsv41:exl3-tp2-928adbd0e57d` |
| API | OpenAI-compatible, model id `DeepSeek-V4.1-Flash-1017-EXL3`, 1,048,576-token window, 8 concurrent streams |
| Model card | [thefiredev/DeepSeek-V4.1-Flash-1017-Ablit-2xSpark](https://huggingface.co/thefiredev/DeepSeek-V4.1-Flash-1017-Ablit-2xSpark) (no weights) |

## Compute

| | Head (rank 0) | Worker (rank 1) |
| --- | --- | --- |
| Machine | NVIDIA DGX Spark | ASUS Ascent GX10 |
| SoC | GB10, 128 GB unified memory, 10x Cortex-X925 + 10x Cortex-A725 | same |
| Kernel | 6.17.0-1029-nvidia | 6.17.0-1026-nvidia |
| Runs | `tensorfold-native serve` (API, vision tower), NFS export of the checkpoint | `tf-dsv41-lanes` |

The two machines share one ConnectX-7 link, used as two RoCE v2 rails (`rocep1s0f0`, `roceP2p1s0f0`), each negotiated at
200,000 Mb/s with MTU 9000 and GID index 3. ConnectX-7 firmware is 28.50.1002. The GPU clock is held to 300-2100 MHz and
the X925 cores to 2.808 GHz on both nodes (see [docs/OPERATIONS.md](OPERATIONS.md)).

## Measured performance

All numbers come from this pair on 2026-10-27, through the full client path, with the settings in
[`recipe/env`](../recipe/env). The ID column points at the entry in
[docs/evidence/2026-10-27-measurements.md](evidence/2026-10-27-measurements.md).

| Measurement | Result | ID |
| --- | ---: | --- |
| Decode, one stream (fixed code prompt, thinking off) | 96.1 tok/s | M3 |
| Decode, 4 concurrent streams, aggregate | 197.4 tok/s | M3 |
| Decode, 8 concurrent streams, aggregate | 327.9 tok/s | M3 |
| Decode, 8 streams sustained for 60 s | 329-337 tok/s | M1, M2 |
| Cold prefill at 11.3K / 22.7K / 45.3K prompt tokens | 2,152 / 3,914 / 4,322 tok/s | M1 |
| Cold prefill, upstream `tools/bench.py`, 2K / 8K / 30K | 1,526 / 1,924 / 2,069 tok/s | M5 |
| Repeat of a 45K-token prompt (prefix reuse) | 0.44 s, against 10.5 s cold | M1 |
| Shared system prompt, new conversation, 65.5K tokens (63,488 reused) | 4.8 s, against 35.0 s cold | M8, M13 |
| Same prompt after an engine restart, restored from spill | 3.9 s, against 35.0 s cold | M12 |
| Agent traffic, 8.7 h: prompt tokens computed rather than reused | 4.15 % of 76.6M | M7 |
| DSpark draft acceptance on that traffic | 67.1 % | M7 |
| Hottest SoC sensor, 60 s at 8 streams (head / worker) | 70.5 / 68.9 C | M2 |

Two settings account for the change from the recipe defaults: `PARALLEL=8` raised 4-stream throughput 73 % and
8-stream throughput 74 %, and `RDMA_NAP_US=2000` lowered the sustained SoC peak by 6 C at the same throughput.
[docs/PERFORMANCE.md](PERFORMANCE.md) lists those, the levers that did nothing, and where the remaining time goes.

## Quick start

You need two GB10 machines prepared as the upstream README's Requirements section describes (link, key-based ssh,
Docker with the NVIDIA runtime, disk, NFS), and your own access to the gated overlay repository if you want `ABLIT=1`.

```bash
# On the head
git clone https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks.git ~/work/mia-dsv41/repo
cd ~/work/mia-dsv41/repo
git checkout 27741e176a9e5b98c10c9c7d012fd3e4fcdc19b6

# Our deltas (this repository checked out at ~/src/kodros-gb10-deepseek-tp2)
OURS=~/src/kodros-gb10-deepseek-tp2
git apply "$OURS/recipe/patches/0001-start-port-check-bound-host.patch"
cp "$OURS/recipe/local.sh.example" scripts/local.sh   # then set your addresses, users and paths
cp "$OURS/recipe/env" .env                            # then fix the home paths

hf auth login            # a read token from an account that has the overlay's gated access
DRY_RUN=1 ./start.sh     # prints both ranks' commands; starts nothing
./start.sh               # first run builds the image and downloads ~400 GB; later starts take about a minute
```

What our deltas change against upstream defaults:

| Setting | Upstream | Here | Why |
| --- | --- | --- | --- |
| `PARALLEL` | 4 | 8 | +73 % at c4, +74 % at c8, c1 unchanged (M1) |
| `RDMA_NAP_US` | 500 | 2000 | -6 C sustained SoC peak, no throughput change (M1) |
| `ABLIT` | 0 | 1 | serve the overlay |
| `SPILL_GIB` | 0 | 64 | keep prompt states on local NVMe (M8) |
| `HEAD_ENGRAM_DIR` | empty | hardlink folder | spill v2 refused to start while rank 0 read Engram through symlinks (M8) |
| `HOST`, `PORT` | 0.0.0.0, 8888 | head's link address, 8000 | match existing clients; keep the open API off other networks |
| `SERVED_NAME` | `DeepSeek-V4.1-Flash-1017` | `DeepSeek-V4.1-Flash-1017-EXL3` | match existing clients |
| `start.sh` port check | any listener on the port | listener on the bound address only | another proxy holds :8000 on a different address |

Then install the watchdogs and units from [`ops/`](../ops/) as described in [docs/OPERATIONS.md](OPERATIONS.md).

## Repository layout

```
recipe/env                         the .env we run (PARALLEL, RDMA_NAP_US, spill v2)
recipe/local.sh.example            scripts/local.sh with placeholders
recipe/patches/                    our one change to upstream start.sh
ops/bin/dsv41-keepalive            restarts the pair after a guard stop or power loss, when both nodes are cool
ops/bin/spark-thermal-blackbox     2 s fsynced CSV of GPU, memory and every thermal zone
ops/bin/spark-thermal-guard        stops both ranks at >= 89 C for 3 rows, low memory, or stale telemetry
ops/bin/spark-memguard             kills the local rank below 1 GiB MemAvailable
ops/bin/cluster-swap.py            head proxy on :8010: model gate, health, token-gated profile swaps
ops/bin/omp-cluster-proxy          workstation gateway on 127.0.0.1:8080, adds the bearer
ops/bin/cluster-bench              c1/c4/c8 throughput and SoC check against a saved baseline
ops/sbin/gb10-cpu-policy           schedutil and the X925 frequency cap
ops/systemd/                       user and system units for the above
ops/state/                         example profile registry for cluster-swap
docs/ARCHITECTURE.md               request path, weight layout, control loops, memory budget
docs/OPERATIONS.md                 running, restarting, checking health
docs/PERFORMANCE.md                what helped, what did not, prompt cache behavior
docs/evidence/                     dated measurements and log extracts the docs cite
huggingface/                       the Hugging Face model card and deployment.json (every pin and engine argument)
```

Published copies replace our addresses with `192.0.2.1` (head) and `192.0.2.2` (worker), users with `user`, and host
names with `head-host` and `worker-host`.

## What is not verified

- Output quality of the abliterated overlay against the stock checkpoint. We have not run a comparison, and the
  recipe's README notes that the quantization's model card has no perplexity or KL measurement either.
- Restoring a kept prompt state from spill after an eviction while the engine keeps running. Restore after an engine
  restart is measured (M12).
- Image prompts and the prompt cache on the current image (upstream patch 0006 is in it; see PERFORMANCE.md).
- Days of uptime under the current settings. The thermal guard stopped the pair 9 times between 2026-10-22 and
  2026-10-27 19:40Z ([docs/evidence/2026-10-27-thermal-trips.txt](evidence/2026-10-27-thermal-trips.txt)).

## License and credits

Our scripts, units and documentation are under the Apache License 2.0 ([LICENSE](../LICENSE)). [NOTICE](../NOTICE) names the
upstream work this depends on and the licenses it keeps. The weights are not distributed here: DeepSeek's model and Mia's
AI Lab's quantization are MIT, and the overlay is gated by its author.

This deployment stands on other people's work:

- [DeepSeek](https://huggingface.co/deepseek-ai) for DeepSeek-V4.1-Flash-1017, its DSpark draft blocks and Engram tables.
- [Mia's AI Lab](https://github.com/MiaAI-Lab) for the EXL3 EAQ quantization and the two-Spark recipe this repository pins.
- [BertholomusAI (Albert Lee)](https://github.com/bertholomus) for the `dsv41` family on TensorFold and its kernel kit.
- [Ash Hart and the TensorFold contributors](https://github.com/ashhart/TensorFold) for the engine.
- [Robert Wojciechowski](https://github.com/wojo) for the spill v2 and KV-sharding patches the recipe ported.
- [drowzeys](https://huggingface.co/drowzeys) for the Keys abliteration overlay.
- [turboderp](https://github.com/turboderp-org/exllamav3) for the EXL3 format.

The upstream recipe's [CREDITS.md](https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks/blob/27741e176a9e5b98c10c9c7d012fd3e4fcdc19b6/CREDITS.md)
lists every contributor by commit.
