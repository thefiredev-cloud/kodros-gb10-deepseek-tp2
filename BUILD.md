# Build guide: kodros-gb10-deepseek-tp2

This repository does not build a program. It holds the settings, one patch, the watchdogs and the units that run the [MiaAI-Lab recipe](https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks) on two GB10 machines. These steps rebuild the deployment. They were not run again on 2026-11-18 because they need the computers. The measured results are dated 2026-10-27 in [docs/evidence/](docs/evidence/).

## You need

- Two GB10 machines: a head (rank 0) and a worker (rank 1). Ours are an NVIDIA DGX Spark and an ASUS Ascent GX10 with 128 GB unified memory each.
- A direct ConnectX-7 link between them, used as two RoCE v2 rails at 200 Gb/s, MTU 9000.
- Key-based SSH from the head to the worker, Docker with the NVIDIA runtime, an NFS export of the checkpoint folder, and about 400 GB of free disk for the first download.
- A Hugging Face read token from an account that has access to the gated overlay, if you want `ABLIT=1`.

The upstream recipe's Requirements section is the full checklist.

## Build

Run on the head. The recipe is pinned to one commit.

```bash
git clone https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks.git ~/work/mia-dsv41/repo
cd ~/work/mia-dsv41/repo
git checkout 27741e176a9e5b98c10c9c7d012fd3e4fcdc19b6

OURS=~/src/kodros-gb10-deepseek-tp2    # this repository
git apply "$OURS/recipe/patches/0001-start-port-check-bound-host.patch"
cp "$OURS/recipe/local.sh.example" scripts/local.sh   # set your addresses, users and paths
cp "$OURS/recipe/env" .env                            # fix the home paths
```

Published files use `192.0.2.1` for the head and `192.0.2.2` for the worker. Replace both with your link addresses.

## Start

```bash
hf auth login            # read token for the gated overlay
DRY_RUN=1 ./start.sh     # prints both ranks' commands and starts nothing
./start.sh               # first run builds the image and downloads about 400 GB
```

Later starts take about a minute. The API listens on the head address at port 8000 with model id `DeepSeek-V4.1-Flash-1017-EXL3`.

## Install the watchdogs

The scripts are in `ops/bin/` and `ops/sbin/`, the units in `ops/systemd/`. Install order, paths and checks are in [docs/OPERATIONS.md](docs/OPERATIONS.md).

| Piece | What it does |
| --- | --- |
| `spark-thermal-guard` | Stops both ranks at 89 C for 3 rows, on low memory or on stale telemetry |
| `spark-memguard` | Kills the local rank below 1 GiB free |
| `dsv41-keepalive` | Restarts the pair when both nodes are cool |
| `cluster-swap.py` | Head proxy on :8010 with a model gate and token-gated profile swaps |
| `omp-cluster-proxy` | Workstation gateway on `127.0.0.1:8080` that adds the bearer |
| `cluster-bench` | c1, c4 and c8 throughput check against a saved baseline |

## Check it

```bash
ops/bin/cluster-bench
```

Compare the result with `docs/evidence/cluster-bench-baseline-2026-10-27.json`. On 2026-10-27 one stream decoded at 96.1 tok/s and 8 streams at 327.9 tok/s in aggregate.

## Limits

- Output quality of the abliterated overlay against the stock checkpoint has not been measured.
- The thermal guard stopped the pair 9 times between 2026-10-22 and 2026-10-27. Multi-day uptime is not proven.
- Why each setting differs from upstream: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and [docs/PERFORMANCE.md](docs/PERFORMANCE.md).
