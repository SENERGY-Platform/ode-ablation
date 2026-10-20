# Ablation protocol

The values and rules of the exposure-tier ablation in the paper "Bounded Data
Exposure for LLM Coding Assistants: Validating Exposure Tiers for Energy Data
Analysis". Section IV of the paper explains the design. This file holds what a
run needs.

## Cases

A case combines one exposure tier (L0, L1, L2), one use case and one model.
Nine cases per model, eighteen in total.

| Model | Identifier | Repository of a case |
| --- | --- | --- |
| Claude Opus 5.5 | `claude-opus-5-5` | `ode-ablation-{use case}-{tier}` |
| Claude Haiku 5.5 | `claude-haiku-5-5` | `ode-ablation-{use case}-{tier}-haiku` |

In repository names, the use cases are `pv` (PV generation), `load` (individual
load) and `nilm` (heat pump disaggregation), and the tier is lower case.

Each case is run once, in a fresh repository with its own conversation and
experiment history. The MLflow experiment is named after the repository, so no
run can build on the results of another.

## Environment

| Item | PV generation and individual load | Heat pump disaggregation |
| --- | --- | --- |
| ODE commit | `3b245c11880df68b90417d448d476abcf146e4b3` | `1212b62a661771048add28c54bcbb7dbe056cf38` |
| Operator library | 1.8.1 for PV generation, 1.8.2 for individual load | 1.9.0 |
| JupyterHub session image | `2026-10-07-0c29381` | `2026-10-14-1212b62` |
| Ray | 2.55.0 | 2.55.0 |
| MLflow | 3.11.1 | 3.11.1 |

The versions are the same for both models. Heat pump disaggregation runs on
later versions of ODE and the operator library, which were extended to support
this use case.

| Model setting | Value |
| --- | --- |
| Provider | Anthropic API, native tool use |
| Reasoning effort | high |
| Adaptive thinking | on |
| Tokens per response | at most 64,000 |
| Tool iterations per exchange | 30 |

## Data split

| Use case | Training end | Test end |
| --- | --- | --- |
| PV generation, individual load | `2026-09-01T00:00:00Z` | `2026-10-01T00:00:00Z` |
| Heat pump disaggregation | `2026-02-01T00:00:00Z` | `2026-03-01T00:00:00Z` |

The environment enforces the split. No tool returns values from the test window
at any tier. The test window of heat pump disaggregation lies earlier, because
the sub-meter recorded nothing from 2026-03-06 to 2026-06-05 and the heat pump
runs mainly in winter.

## Use cases

| Use case | Target device | Target series | Task | Metric | Threshold |
| --- | --- | --- | --- | --- | --- |
| PV generation | `Wechselrichter` | `root.powerTotal` | Hourly mean power, 24 h ahead | MAE | 30 W, about 5% of the inverter's maximum power |
| Individual load | `Stromzähler` | `sensor.MT175.P` | Hourly mean power, 24 h ahead | RMSE | 6.1 W, about 5% of the mean power at the meter |
| Heat pump disaggregation | `Emonio Wärmepumpe Kompressor` | `root.egy_watt` | 15-minute mean power of the compressor, from the main meter | MAE | 200 W, about 5% of the compressor's maximum power in the test window |

Every run is scored against the target series, in watts.

### Evaluation file

Each repository's `evaluation.yaml` is ODE's scaffold with these values set. The
assistant cannot change it. Each run repository holds the file as the
assistant read it.

| Use case | `metric` | `goal` | `threshold` | `target_series` | `prediction_field` | `resolution` |
| --- | --- | --- | --- | --- | --- | --- |
| PV generation | `mae` | `minimise` | `30.0` | `root.powerTotal` | `prediction` | `1h` |
| Individual load | `rmse` | `minimise` | `6.1` | `sensor.MT175.P` | `prediction` | `1h` |
| Heat pump disaggregation | `mae` | `minimise` | `200.0` | `root.egy_watt` | `prediction` | `15min` |

### Problem statements

The opening message of every run, the same at every tier and for both models.
Reproduced as sent.

PV generation:

```
Forecast the photovoltaic generation 24 hours ahead, as hourly mean power in
watts. The evaluation criteria are mine and they are in `evaluation.yaml`.

The session is done when you tell me the operator is complete and the most
recent completed evaluation run meets the threshold in that file. I will end it
early after four hours, after ten experiment launches, or after three turns in which
nothing changes in the repository, the selection or the experiment state.
```

Individual load:

```
Forecast the net electrical consumption from the grid 24 hours ahead, as hourly mean power in
watts. The evaluation criteria are mine and they are in `evaluation.yaml`.

The session is done when you tell me the operator is complete and the most
recent completed evaluation run meets the threshold in that file. I will end it
early after four hours, after ten experiment launches, or after three turns in which
nothing changes in the repository, the selection or the experiment state.
```

Heat pump disaggregation:

```
Estimate the electrical power the heat pumps compressor draws, from the site's main meter,
as 15-minute mean power in watts. The main meter is transformer-rated, with a
factor of 50. The heat pumps compressor has a sub-meter of its own: use it to train and to
evaluate, but the operator will not receive it when it runs. Beyond the main
meter, the operator may use any covariate. The evaluation criteria are mine and
they are in `evaluation.yaml`.

The session is done when you tell me the operator is complete and the most
recent completed evaluation run meets the threshold in that file. I will end it
early after four hours, after ten experiment launches, or after three turns in which
nothing changes in the repository, the selection or the experiment state.
```

## Developer behavior

### Before the first message

1. Set the session's data split from [Data split](#data-split).
2. Set the session's exposure tier to the case's tier.
3. Check that the session runs on the model of the case.
4. Write the evaluation file from [Evaluation file](#evaluation-file).
5. Send the problem statement of the use case.

The tier and the data split do not change during a run. No profile override is
written during the study.

### Answers

Questions from the assistant are answered from the decision sheet below,
verbatim from the [facts about the use case](#facts-about-the-use-cases), or
with "not known". Beyond that, the developer only performs actions the
assistant cannot perform itself, and otherwise replies with one of these
fixed phrases:

| Phrase | When |
| --- | --- |
| `committed` | After committing the repository at the assistant's request |
| `proceed with your proposal` | When the assistant proposes a change and asks before proceeding |
| `experiment done` | When the assistant has not picked up a finished experiment within five minutes |
| `Are you done?` | When the assistant stops without saying whether the operator is complete |

### Decision sheet

| Situation | L0 and L1 | L2 |
| --- | --- | --- |
| Executed code reads values of a series the operator uses as input, target or covariate | Decline | Confirm |
| Executed code trains a model on such a series | Decline, since ODE launches training as an experiment | Confirm |
| Any other executed code | Confirm | Confirm |
| A proposed data selection | Confirm | Confirm |
| A launch | Assign the target to the target device of the use case | Same |
| A launch in heat pump disaggregation | Also set the role of the target and of every other device except the main meter to training, and leave the rest of the proposal unchanged | Same |

An assistant may choose either series that measures the target quantity, for
instance the inverter or the smart plug in PV generation. The developer assigns
the target at launch, so every run is scored against the target device.

A declined call carries no reason. The assistant receives the environment's
fixed rejection: a tool result marking the call rejected, and the turn "The
developer declined the `<tool>` call. Do not retry it; ask what they would
prefer."

A series with the training role is read in training and scored against, but it
is never delivered to the operator in the test window.

### End of a run

A run ends on the first of these:

| Condition | Rule |
| --- | --- |
| Success | The assistant reports the operator complete and the latest evaluation meets the threshold |
| No further improvement | The assistant finds no further improvement |
| Abandonment | The assistant states that no further improvement is possible without a higher tier, or three consecutive assistant turns pass without a change to the repository, the data selection, or the experiment state |
| Limits | Four hours, or ten experiment launches |

The environment enforces neither limit. The developer keeps both.

After the end, the developer sends this request, verbatim:

```
You are done! This is now outside of any scoring. Change the readme and add a section on top, summarizing your approach to handling the problem stated in the first prompt.
```

The summary it produces is on top of each run repository's README. Despite its
wording, the request and its answer count toward the prompts and tokens of the
run.

## Measures

| Measure | Question | Source |
| --- | --- | --- |
| Error over the test window of every completed evaluation, MAE for PV generation and heat pump disaggregation, RMSE for individual load | RQ1 | Operator library, per launch: [`results/errors-per-launch.csv`](results/errors-per-launch.csv) |
| Experiment launches | RQ2 | Scoring script: `launches` |
| Prompts the developer sends | RQ2 | Scoring script: `developer_prompts` |
| Tokens of the session, the sum of input, output, cache-read and cache-write tokens | RQ2 | Scoring script: `tokens` |
| Tool calls, confirmations requested, requests for a higher tier, covariates of the final operator | Explanation | Scoring script |
| Approach and termination reason | Explanation | Recorded by hand: [`results/cases.csv`](results/cases.csv) |

The naive baseline of a use case is whichever of two estimates without
covariates reaches the lower error over the test window: the measured value 24
hours earlier, or the mean of the measured values over the last month before the
test window. In heat pump disaggregation only the mean applies, since the
operator never receives the sub-meter.

| Use case | Naive baseline |
| --- | --- |
| PV generation | 82 W |
| Individual load | 236 W |
| Heat pump disaggregation | 1398 W |

## Order of the runs

The Opus runs of PV generation and individual load came first. Heat pump
disaggregation was added after them, on the later versions above. The Haiku
runs followed, each use case on the versions of its Opus runs.

The first Opus run of PV generation at L2 read the OpenMeteo weather history
import `urn:infai:ses:import:06e7cfe6-0477-eaf1-3f9e-c9a357f5ffcc`. That import
mixed reanalysis archive values with forecast values for the days before each
import run, and no field named the source. It was replaced by
`urn:infai:ses:import:57ebf2a0-f019-6426-bdf4-b12a1e7363f0`, which carries
archive values only, and the case was run again on the same versions. The first
run is in `ode-ablation-pv-l2` and does not enter the results. The run in the
results is `ode-ablation-pv-l2-rerun-1`.

## Facts about the use cases

The developer answers questions from these facts.

### PV generation

| Device | What it is | Path | Carries |
| --- | --- | --- | --- |
| `Wechselrichter` | APSystems DS3-S micro-inverter, read with the ESP-ECU firmware. The target | `root.powerTotal` | Power, W |
| | | `root.energyTotal` | Energy, kWh, the day's yield, reset every day |
| `Leiste PV` | Nous A5T smart plug the inverter feeds in through. Measures the same output | `sensor.ENERGY.Power` | Power, W |
| | | `sensor.ENERGY.Total` | Energy, kWh, cumulative |

| Import | Instance |
| --- | --- |
| Irradiance, for many locations. An operator filters to the plant's location | `urn:infai:ses:import:b0d917d5-2d54-b75c-bcaf-7650d6b4b714` |
| yr.no weather forecast | `urn:infai:ses:import:a50aa583-282e-56c2-b101-7aaf68ebd2b9` |
| OpenMeteo weather history | `urn:infai:ses:import:57ebf2a0-f019-6426-bdf4-b12a1e7363f0` |
| OpenMeteo historic weather forecasts | `urn:infai:ses:import:6fd7bbee-c1dd-d165-562d-d43c7d300055` |

Approximate location of the plant: 51°30'N 10°00'E.

### Individual load

| Device | What it is | Path | Carries |
| --- | --- | --- | --- |
| `Stromzähler` | Iskra MT 175 smart meter, read with Tasmota. The target | `sensor.MT175.P` | Power, W |
| | | `sensor.MT175.E_in` | Energy, kWh, consumption |
| `Qubino` | Qubino three-phase meter directly upstream, read over Z-Wave. Measures the same load | `power.value` | Power, W |
| | | `energy.value` | Energy, kWh |

Both devices hold more than a year of history. Sub-meters exist for some
downstream consumers, but their values are not known in advance of a forecast.

### Heat pump disaggregation

The site is an industrial site. The main meter sits at its grid connection.

| Device | What it is | Path | Carries |
| --- | --- | --- | --- |
| `Hauptzähler` | Transformer-rated main meter, read through its pulse output with an ESP32 running Tasmota | `sensor.data.obis_1_8_0` | Energy, kWh, cumulative register. Multiply by 50 for the energy at the grid connection. Moves in steps of 1/256 kWh, 195 Wh after the factor |
| | | `sensor.data.obis_1_8_0_f` | `obis_1_8_0` already multiplied by 50, from 2025-01-17. No unit declared. Moves in steps of 0.25 kWh |
| | | `sensor.data.factor` | 50, constant |
| | | `sensor.data.obis_16_7` | Always empty |
| `Emonio Wärmepumpe Kompressor` | Emonio P3 meter of the heat pump's compressor, reports every 15 s. The target, with the training role | `root.egy_watt` | Power, W, the sum of three phases. No unit declared. A few watts below zero while idle |
| | | `root.egy_watt_a`, `root.egy_watt_b`, `root.egy_watt_c` | Power per phase, W. Phase c from 2025-11-03 |
| | | `root.egy_kwh` | Energy, kWh, a running total that is reset at times |

The operator may use the main meter and the platform's imports, but no other
device on the site.

| Import | Instance |
| --- | --- |
| OpenMeteo weather history for the site's coordinates | `urn:infai:ses:import:aac42d68-bc8e-cefe-de8e-be1ac252541c` |

Approximate location of the site: 51°12'N 12°42'E. Both devices hold more than a
year of history before the training end.
