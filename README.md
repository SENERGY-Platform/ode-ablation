# ode-ablation

Artifacts of the exposure-tier ablation in the paper "Bounded Data Exposure for
LLM Coding Assistants: Validating Exposure Tiers for Energy Data Analysis". An
LLM assistant develops machine learning operators in the
[operator development environment (ODE)](https://github.com/SENERGY-Platform/operator-development-environment)
for three use cases at three exposure tiers, once with Claude Opus 5.5 and once
with Claude Haiku 5.5.

| Path | Content |
| --- | --- |
| [`protocol.md`](protocol.md) | Cases, environment, use cases, developer behavior and measures |
| [`results/errors-per-launch.csv`](results/errors-per-launch.csv) | Error of every completed evaluation, per launch (Fig. 1) |
| [`results/cases.csv`](results/cases.csv) | Lowest error, approach and termination reason per case (Table II) |
| [`results/opus/`](results/opus), [`results/haiku/`](results/haiku) | Manifest and the measures read from ODE's records, per model (Table II) |
| [`scoring/`](scoring) | The scoring script and its tests |

## Runs

One repository per case, as the run left it. The summary of the assistant's
approach is on top of each README.

| Use case | Tier | Claude Opus 5.5 | Claude Haiku 5.5 |
| --- | --- | --- | --- |
| PV generation | L0 | [`ode-ablation-pv-l0`](https://github.com/SENERGY-Platform/ode-ablation-pv-l0) | [`ode-ablation-pv-l0-haiku`](https://github.com/SENERGY-Platform/ode-ablation-pv-l0-haiku) |
| PV generation | L1 | [`ode-ablation-pv-l1`](https://github.com/SENERGY-Platform/ode-ablation-pv-l1) | [`ode-ablation-pv-l1-haiku`](https://github.com/SENERGY-Platform/ode-ablation-pv-l1-haiku) |
| PV generation | L2 | [`ode-ablation-pv-l2-rerun-1`](https://github.com/SENERGY-Platform/ode-ablation-pv-l2-rerun-1) | [`ode-ablation-pv-l2-haiku`](https://github.com/SENERGY-Platform/ode-ablation-pv-l2-haiku) |
| Individual load | L0 | [`ode-ablation-load-l0`](https://github.com/SENERGY-Platform/ode-ablation-load-l0) | [`ode-ablation-load-l0-haiku`](https://github.com/SENERGY-Platform/ode-ablation-load-l0-haiku) |
| Individual load | L1 | [`ode-ablation-load-l1`](https://github.com/SENERGY-Platform/ode-ablation-load-l1) | [`ode-ablation-load-l1-haiku`](https://github.com/SENERGY-Platform/ode-ablation-load-l1-haiku) |
| Individual load | L2 | [`ode-ablation-load-l2`](https://github.com/SENERGY-Platform/ode-ablation-load-l2) | [`ode-ablation-load-l2-haiku`](https://github.com/SENERGY-Platform/ode-ablation-load-l2-haiku) |
| Heat pump disaggregation | L0 | [`ode-ablation-nilm-l0`](https://github.com/SENERGY-Platform/ode-ablation-nilm-l0) | [`ode-ablation-nilm-l0-haiku`](https://github.com/SENERGY-Platform/ode-ablation-nilm-l0-haiku) |
| Heat pump disaggregation | L1 | [`ode-ablation-nilm-l1`](https://github.com/SENERGY-Platform/ode-ablation-nilm-l1) | [`ode-ablation-nilm-l1-haiku`](https://github.com/SENERGY-Platform/ode-ablation-nilm-l1-haiku) |
| Heat pump disaggregation | L2 | [`ode-ablation-nilm-l2`](https://github.com/SENERGY-Platform/ode-ablation-nilm-l2) | [`ode-ablation-nilm-l2-haiku`](https://github.com/SENERGY-Platform/ode-ablation-nilm-l2-haiku) |

The case of PV generation at L2 with Opus was run again after a weather import
was replaced. [`protocol.md`](protocol.md#order-of-the-runs) says why. The first
run is in
[`ode-ablation-pv-l2`](https://github.com/SENERGY-Platform/ode-ablation-pv-l2)
and does not enter the results.

## Results

`results/errors-per-launch.csv` holds one row per launch with a completed
evaluation. A launch without one, for instance a failed launch, has no row.

| Column | Meaning |
| --- | --- |
| `model`, `use_case`, `tier` | The case. `nilm` is heat pump disaggregation |
| `launch` | Position of the launch in the run, from 1 |
| `metric` | `mae` or `rmse`, as the evaluation file of the use case sets it |
| `error_w` | Error over the test window in W, as the operator library computed it |

`results/cases.csv` holds one row per case: the lowest error of the run, and
the approach of the final operator and the termination reason, both recorded by
hand.

`results/{opus,haiku}/measures.csv` holds one row per run, written by the
scoring script. `measures.json` lists what does not fit a cell: the covariates
and training topics of the final operator, prompts by kind, tool calls by tool,
token counts by kind, and the records each row was read from.

| Column | Meaning |
| --- | --- |
| `launches` | Experiment launches |
| `developer_prompts` | Prompts the developer sent, the opening problem statement and the summary request included |
| `tokens` | Input, output, cache-read and cache-write tokens of the session |
| `tool_calls` | Tool calls of the assistant |
| `confirmations` | Confirmations the assistant requested |
| `tier_raise_requests` | Assistant turns asking for a higher tier, by phrase match |
| `covariate_count` | Input series of the final operator |
| `declined_confirmations` | Confirmations the developer declined |
| `tool_calls_blocked_by_tier` | Tool calls the tier refused |
| `exchanges_aborted_at_iteration_cap` | Exchanges ODE ended at the cap of 30 tool iterations |
| `reached_operator_and_evaluation` | Whether the run reached a committed operator with a completed evaluation |
| `ended_on` | For a run without a completed evaluation, the rule that ended it |

`results/{opus,haiku}/manifest.json` names the session of each case. The Opus
manifest was proposed before the heat pump runs and the re-run of PV generation
at L2, and its rows were completed by hand afterwards. Its `discovered` block
records the original proposal.

## Scoring

The script reads ODE's database through `psql`, which is not public. It needs
Python 3 with its standard library only, tested with 3.14, plus `psql` and
`git`, and `ODE_DSN` set to the database. Per model:

```bash
python3 scoring/score_runs.py discover --series opus --out results/opus/manifest.json
python3 scoring/score_runs.py score \
    --manifest results/opus/manifest.json \
    --out-csv results/opus/measures.csv \
    --out-json results/opus/measures.json
```

With `--series haiku` and `results/haiku/` for Claude Haiku 5.5. `discover`
asks where the records leave a choice and needs a terminal. The manifest is
reviewed and committed before `score` reads it.

The tests need no database:

```bash
python3 scoring/test_score_runs.py
```

## License

Apache-2.0. See [`LICENSE`](LICENSE).
