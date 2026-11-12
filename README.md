# HalfProof

HalfProof is a research harness for mathematics problems, built by First Proof, that uses only
open-weight language models. This repository contains HalfProof, the ten First Proof Batch 3 problems
as a test case, and the results HalfProof obtained on them.

## What HalfProof does

Five open-weight models work on one problem at the same time and post to a shared research board:
what each is trying, partial results, and objections. The models also act as the coordinator that
chooses what to pursue next.

Every claimed result goes to model critics. One rejection refutes a claim; acceptance needs two
critics to agree, and a model may not certify its own work. The models can search and read arXiv and
OpenAlex; they have no other internet access. Each run happens in a sandbox that can see only its own
files, the mathematics software (Sage, PARI/GP, GAP) and the literature cache.

When research stops, the same models choose the main results and what they depend on, then write,
referee and revise a paper-style write-up. They are instructed to present as proved only results
that passed the checking step, and to state everything else as conditional.

## Models and providers

All model calls go through OpenRouter, pinned to one hosting provider per model with fallbacks
disabled (`contracts/profiles.json`). No closed model is called.

| Key | Model | Family | Pinned provider | Other permitted hosts |
|---|---|---|---|---|
| `dsf` | `deepseek/deepseek-v4.1-flash` | DeepSeek | together | wafer, dekallm, ionstream, digitalocean, phala, modal, fireworks, fireworks/us |
| `dsp` | `deepseek/deepseek-v4-pro` | DeepSeek | parasail/fp8 | gmicloud/fp8 |
| `qwen24t` | `qwen/qwen3.8-2.4t-a95b` | Qwen | modal | alibaba, novita, together |
| `k3` | `moonshotai/kimi-k3` | Kimi | moonshotai/mxfp4 | decart/mxfp4, deepinfra/mxfp4, modal/mxfp4 |
| `glm` | `z-ai/glm-5.3` | GLM | z-ai/fp8 | akashml, sail-research, morph, novita, siliconflow, gmicloud, baidu, parasail, atlas-cloud (fp8); baseten/fast |

Six strategies are defined: four single-model runs (`dsf-solo`, `dsp-solo`, `qwen24t-solo`,
`k3-solo`) and two teams (`dsf-swarm`, five copies of DeepSeek V4.1 Flash; and `mixed-swarm`, all five
models with Kimi K3 as coordinator). Each has a $200 budget cap and a 24-hour wall clock.

## Layout

| Path | Contents |
|---|---|
| `v2/` | Research board, run controller, sandbox, accounting and closing stage |
| `writing/` | Writing stage: plan, draft, referee, revise and select a write-up |
| `contracts/` | Model routes, budgets, roles and record schemas |
| `roles/`, `template/` | Role prompts and policies for the team strategies; research goal texts and the solution TeX template |
| `bin/` | Launchers (`package-release` to package a checkout, `v3-production` for a full run, `release-write` for the writing stage alone), the sandboxed arXiv tool and model CLI settings |
| `deployment/` | Recorded changes to the model and budget settings |
| `examples/batch-3/problems/` | The ten Batch 3 problem statements |
| `results/batch-3/` | The ten write-ups, their grades and the grader reports |

## Requirements

- A Linux cluster with SLURM. HalfProof refuses to compute outside a SLURM job.
- bubblewrap (`bwrap`) for the per-run sandbox.
- Python 3.12, Node.js installed at `~/.local/ds41-node`, and Qwen Code 0.24.6 (the agent CLI
  each model runs in) installed with npm under `~/.local/qwen-npm`.
- TeX Live with `pdflatex` (set `HARNESS_TEX_PREFIX`), and a Sage environment providing PARI/GP and GAP
  (set `DS_CAS_ENV`).
- An OpenRouter API key, passed to the launcher as a file path.
- Optional: a local arXiv metadata index (`bin/ds-build-arxiv-index.py`) and an arXiv cache.

Run state, caches and indexes live under `HARNESS_DATA` (default `~/oss-harness-data`). Problem
statements are read from `HARNESS_PROBLEMS` (default `examples/batch-3/problems`).
To keep run output off a small home quota, set `HARNESS_OUTPUT_ROOTS` to the permitted
directories (colon-separated).

## Running HalfProof on a problem

1. Package the checkout: `bin/package-release --output RELEASE`. This copies the committed
   source, records the exact Node and Qwen Code files installed on the machine, and writes
   `RELEASE/production-release.json`. Every later stage checks that it is running this package.
2. Submit one problem:

   ```bash
   bin/v3-production --release RELEASE/production-release.json \
     --problem N --statement-file problem.tex --arms mixed-swarm \
     --cohort NAME --durable-root RUN_DIR --credential-file openrouter.key \
     --node '*' --output RUN_DIR.launch.json --submit
   ```

   Research, closing, writing and delivery then proceed as chained SLURM jobs. The manuscript is
   delivered to `RUN_DIR/chain/delivery/paper.tex`, with `paper.pdf` and `manifest.json`.
3. The writing stage can also be run on its own on a finished research run:
   `bin/release-write --out DIR --contract FILE --manifest FILE --authority FILE --problem FILE`.

## Reproducing the Batch 3 run

The HalfProof Batch 3 results came from the `mixed-swarm` strategy on each of the ten problems, under the
Batch 3 rules: the problems as given to the AI systems, at most 24 hours per problem, write-ups of
at most 16 pages. Configuration, all in `contracts/profiles.json` and `roles/mixed-swarm/`:

- five models (DeepSeek V4.1 Flash, DeepSeek V4 Pro, Qwen 3.8, Kimi K3, GLM 5.3), with Kimi K3 as orchestrator;
- $200 budget cap per run and a 24-hour wall, with research closing at 76,260 seconds (21 h 11 min);
- claims checked by two critics (DeepSeek V4.1 Flash and Kimi K3), one rejection refutes, two
  acceptances certify;
- writing: 40 write-up attempts in total, 1 to 6 per problem; the latest attempt for each problem
  is the one reported.

Some problems ran more than one research run. Research used about 72,000 model calls across the ten
final runs, from about 2,500 (Problem 2) to about 9,600 (Problem 5).

Cost: about $1,440 of model usage for research on the 7 problems where it was measured, plus about
$57 for writing (provider-reported, 220 of 232 writing calls priced) — roughly $1,500 in total.

## Results on Batch 3

The write-ups are in `results/batch-3/papers/`. Each was graded blind, alongside the Humanize
(Kimi K3) and GPT-5.2 Pro solutions to the same problem, by two AI graders (OpenAI GPT-6 Astra and
Anthropic Claude Fable) on the Batch 3 referees' rubric: correctness, exposition and attribution,
each from −1 to 1. The graders saw the problem and the author's solution but not the referees'
reports. Per-problem scores are in `results/batch-3/grades.tsv` (HalfProof is `ours`) and the grader
reports in `results/batch-3/grading/`.

Mean correctness over the ten problems:

| System | Astra grader | Fable grader | Problems solved |
|---|---|---|---|
| HalfProof | −0.10 | 0.00 | 0 of 10 |
| GPT-5.2 Pro | −0.55 | −0.60 | 0 of 10 |
| Humanize (Kimi K3) | −0.85 | −0.75 | 0 of 10 |

HalfProof solved none of the ten problems. Its write-ups scored 0 ("acknowledged partial result")
in 18 of the 20 gradings: they present special cases and partial results and say so, where the
other two systems mostly claimed full solutions that were wrong. No human referee has read these
write-ups.

The Batch 3 problems and referee reports are at https://github.com/1stproof/batch-3 and in
hexagonmath.org 2610.00244.

## Licence

Code written by First Proof is licensed under the GNU Affero General Public License v3.0
(`LICENSE`). Everything else — the problem statements, the write-ups, the grades, the grader
reports and the documentation — is licensed under Creative Commons Attribution-ShareAlike 4.0
International (`LICENSE-DOCS`). See `NOTICE`.

Contact: contact@1stproof.org
