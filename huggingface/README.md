---
license: apache-2.0
base_model:
- deepseek-ai/DeepSeek-V4.1-Flash-1017
- Mia-AiLab/DeepSeek-V4.1-Flash-1017-EXL3-EAQ-v1-2.9-bpw
pipeline_tag: text-generation
tags:
- deepseek
- exl3
- tensorfold
- dgx-spark
- gb10
- tensor-parallel
- abliterated
- deployment
---

# DeepSeek-V4.1-Flash-1017, abliterated overlay, on a DGX Spark and an ASUS Ascent GX10

This repository holds no weights. It records how one pair of GB10 machines serves DeepSeek-V4.1-Flash-1017 at 2.9 bits
per weight with an abliteration overlay, and what that deployment measured. The license above covers the files in this
repository (this card, `deployment.json` and the recipe files). The weights keep their own terms, listed below.

Full documentation, service units and watchdog scripts are in the GitHub repository
[thefiredev-cloud/kodros-gb10-deepseek-tp2](https://github.com/thefiredev-cloud/kodros-gb10-deepseek-tp2).

## What is served

| | |
| --- | --- |
| Base model | [deepseek-ai/DeepSeek-V4.1-Flash-1017](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017), MIT |
| Weights served | [Mia-AiLab/DeepSeek-V4.1-Flash-1017-EXL3-EAQ-v1-2.9-bpw](https://huggingface.co/Mia-AiLab/DeepSeek-V4.1-Flash-1017-EXL3-EAQ-v1-2.9-bpw) @ `45ee5d0f`, MIT, 196 GiB |
| Overlay | [drowzeys/keys-DeepSeek-V4.1-Flash-1017-Abliterated](https://huggingface.co/drowzeys/keys-DeepSeek-V4.1-Flash-1017-Abliterated) @ `0bdc2036`, file `exl3_wo_b_l10_35_mia29.safetensors`. Gated by its author. Request access there; it is not redistributed here. |
| Engram tables | shards 47 and 48 of the base model @ `1bae0e10`, unquantized |
| Engine | [TensorFold](https://github.com/ashhart/TensorFold) native engine with BertholomusAI's `dsv41` family ([fork](https://github.com/bertholomus/TensorFold/tree/deepseek-v41-zig) @ `629c524`), TP=2 |
| Recipe | [MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks](https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks) @ `27741e1`, plus one local patch |
| API | OpenAI-compatible, id `DeepSeek-V4.1-Flash-1017-EXL3`, 1,048,576-token window, 8 concurrent streams, thinking on by default, DSpark drafting on |

The overlay replaces 104 attention tensors in layers 10-35 of the Mia EXL3 pack, according to the recipe's README. We
have not measured output quality against the stock checkpoint. The recipe's README notes that the quantization's model
card has no perplexity or KL measurement against the unquantized model.

## Hardware

Rank 0: NVIDIA DGX Spark. Rank 1: ASUS Ascent GX10. Both are GB10 systems with 128 GB unified memory, joined by their
ConnectX-7 ports as two RoCE v2 rails (200,000 Mb/s each, MTU 9000). GPU clock held to 300-2100 MHz on both.

## Measured on 2026-10-27

Through the full client path, fixed code prompt, temperature 0, thinking off.

| | |
| --- | ---: |
| Decode, 1 stream | 96.1 tok/s |
| Decode, 4 streams, aggregate | 197.4 tok/s |
| Decode, 8 streams, aggregate | 327.9 tok/s |
| Cold prefill, 11.3K / 22.7K / 45.3K tokens | 2,152 / 3,914 / 4,322 tok/s |
| Agent traffic over 8.7 h: prompt tokens computed rather than reused from the prompt cache | 4.15 % of 76.6M |
| DSpark draft acceptance on that traffic | 67.1 % |
| 65.5K-token prompt sharing a system prompt with an earlier conversation | 4.8 s, against 35.0 s cold |
| Same prompt after an engine restart, restored from the spill v2 disk tier | 3.9 s, against 35.0 s cold |

`PARALLEL=8` (recipe default 4) gave +73 % at 4 streams and +74 % at 8. `RDMA_NAP_US=2000` (default 500) lowered the
sustained SoC peak by 6 C at the same throughput. GPU clocks above 2100 MHz, a faster CPU cap, a larger prefill chunk
and a smaller context pool changed nothing measurable. The GitHub repository has each measurement with its time.

## Files

- `deployment.json`: every pin, engine argument and environment value the pair runs with.
- `recipe/env`: the recipe `.env` (stream count, RDMA nap, spill v2).
- `recipe/local.sh.example`: the recipe's `scripts/local.sh` with placeholder addresses.
- `recipe/patches/0001-start-port-check-bound-host.patch`: our one change to the recipe's `start.sh`.

## Reproduce

```bash
git clone https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks.git && cd DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks
git checkout 27741e176a9e5b98c10c9c7d012fd3e4fcdc19b6
git apply /path/to/recipe/patches/0001-start-port-check-bound-host.patch
cp /path/to/recipe/local.sh.example scripts/local.sh && cp /path/to/recipe/env .env   # then edit addresses and paths
hf auth login     # an account with access to the gated overlay
./start.sh
```

## Credits

DeepSeek for the model, its draft blocks and Engram. Mia's AI Lab for the EXL3 EAQ quantization and the two-Spark recipe.
BertholomusAI (Albert Lee) for the `dsv41` family on TensorFold and its kernel kit. Ash Hart and the TensorFold
contributors for the engine. Robert Wojciechowski for the spill v2 patches the recipe ported. drowzeys for the Keys
overlay. turboderp for the EXL3 format.
