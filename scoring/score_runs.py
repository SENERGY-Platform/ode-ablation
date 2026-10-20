#!/usr/bin/env python3
"""The measures of the tier ablation that ODE's records hold, for the runs of one
model. It runs after all runs with a model and is never shown to an assistant.

Two commands
------------
`discover` proposes a manifest from what ODE recorded. The session carries the
tier, the experiment carries the repository, and the repository name carries
the case. Where no record decides, it asks the developer. The manifest is
reviewed and committed before `score` reads it, so the assignment of a session
to a case stays a recorded decision.

`score` reads a reviewed manifest, asks nothing, and writes one CSV row and one
JSON entry per run.

Determinism
-----------
No language model call and no fuzzy matching. Every measure over conversation
content is read from a structured field or by exact substring matching against a
list this file carries as a constant. Every collection is sorted before it is
written. Every source is read once per run and never retried. A missing or
ambiguous input raises with the run id and the field, so no partial row is
written.

What it reads, per run
----------------------
From ODE's database, through `psql`: the persisted conversation, the
confirmations, the exchange aborts, the tool-call audit, the token usage, the
experiment records and the run summaries ODE kept.

The error of each launch is not read here. The operator library computes it in
the experiment, and `results/errors-per-launch.csv` lists it. The approach and
the termination reason of a run are recorded by hand in `results/cases.csv`.

ODE's records are read from its tables rather than its routes. The routes need a
developer token, `GET /chat/sessions/{id}` returns only unresolved
confirmations, and an exchange abort has no route at all. A run summary is a
cache ODE fills when a summary is first built, and a launch whose summary was
never kept fails the run rather than being rebuilt here.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# --- constants --------------------------------------------------------------
#
# Every list below is matched literally. None of them is a regular expression,
# none is a stem, and none is extended at runtime.

# A turn matching one of these phrases is counted as a request for a higher
# tier. Matched as an exact substring of the assistant's text, case-folded,
# nothing else. The wording follows ODE's own prompt block 4, which is what the
# assistant is instructed with ("say which, say what it would tell you, and ask
# them to raise the tier"), and its tier refusal.
TIER_RAISE_PHRASES = (
    "a higher exposure tier",
    "a higher tier",
    "exposure tier to l1",
    "exposure tier to l2",
    "raise the exposure tier",
    "raise the session's tier",
    "raise the tier",
    "raising the tier",
    "tier to be raised",
    "tier would have to be raised",
)

# A developer prompt is a `user` message the developer typed. ODE stores three
# other kinds of `user` message: tool results, which carry no text block; its own
# injected turns, which carry origin 'ode'; and the outcome of every resolved
# confirmation, which carries the developer's origin like a typed message and is
# told apart only by its opening words. ODE writes those in `confirmationOutcome`,
# pkg/chat/engine.go, and every variant opens with one of these. Matched as a
# case-sensitive prefix of the message text.
ORIGIN_DEVELOPER = ""
CONFIRMATION_OUTCOME_PREFIXES = (
    "The developer approved the ",
    "The developer declined the ",
)

# The fixed phrases of `../protocol.md` and the summary request after a run, by
# the label the JSON counts them under. A developer prompt counts under one when
# its whole text, stripped and case-folded, equals the reply case-folded.
# Anything else, the opening problem statement aside, counts as other: an answer
# from the decision sheet or the facts about the use case, or "not known".
SCRIPTED_REPLIES = {
    "are you done": "Are you done?",
    "committed": "committed",
    "experiment done": "experiment done",
    "proceed with your proposal": "proceed with your proposal",
    "readme summary": (
        "You are done! This is now outside of any scoring. Change the readme and add"
        " a section on top, summarizing your approach to handling the problem stated"
        " in the first prompt."
    ),
}
PROMPT_OPENING = "opening problem statement"
PROMPT_OTHER = "other"

# The target series per use case, as `../protocol.md` lists them. Discovery
# reads them to tell which use case a session that never launched belongs to.
# `nilm` is heat pump disaggregation.
TARGET_SERIES = {
    "pv": {
        "service_id": "urn:infai:ses:service:9ddd93b2-0988-407f-931d-a2ebcc8f6d94",
        "column": "root.powerTotal",
    },
    "load": {
        "service_id": "urn:infai:ses:service:61447a7c-5e75-48dd-b2db-bcfcd23fa33d",
        "column": "sensor.MT175.P",
    },
    "nilm": {
        "service_id": "urn:infai:ses:service:b99a5d0d-9221-4d63-9071-1e70e36344a7",
        "column": "root.egy_watt",
    },
}

# The model of each series. ODE records it on the session, and a session on
# another model is not a run of the series.
SERIES_MODELS = {"opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5"}

# The repository of a run: `ode-ablation-{usecase}-{tier}` with Opus, with
# `-haiku` appended with Haiku. A re-run carries `-rerun-{n}` before that. The
# experiment record carries the repository as `owner/name`, which is what makes a
# launch attributable to a case.
REPOSITORY_PATTERN = re.compile(r"^ode-ablation-(pv|load|nilm)-(l0|l1|l2)(?:-rerun-\d+)?(-haiku)?$")

# The tiers, as the session records them, and the order the use cases ran in.
# The order decides nothing about a result, since every output is sorted by run
# id, but discovery asks its questions in it, so the developer is asked about
# the runs in the order they happened.
TIERS = ("L0", "L1", "L2")
USE_CASE_ORDER = ("pv", "load", "nilm")

# The test window per use case, from `../protocol.md`, Data split. `start` is
# the training end. The script carries them rather than reading them from a run,
# because a run's own recorded bounds are what it is checked against.
TEST_WINDOWS = {
    "pv": {"start": "2026-09-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"},
    "load": {"start": "2026-09-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"},
    "nilm": {"start": "2026-02-01T00:00:00Z", "end": "2026-03-01T00:00:00Z"},
}

# A launch topic's role (`InputTopic.Role`, pkg/experiments/deployment.go in ODE,
# from 1212b62). Empty is every topic recorded before roles existed, and means
# input. A training topic is read in training and scored against, and never
# reaches the operator, so it is not a covariate. NILM's target is one.
ROLE_INPUT = "input"
ROLE_TRAINING = "training"

# The stages a run passes through, for one that stopped before a completed
# evaluation.
STAGES = ("no selection", "no commit", "no launch", "no completed evaluation")

# How a run without a completed evaluation ended, per `../protocol.md`, End of a
# run. The environment enforces neither limit, so these two numbers never
# answer anything here: discovery uses them to suggest, and the developer
# decides.
END_REASONS = ("abandonment", "wall clock", "launch cap")
LAUNCH_CAP = 10
WALL_CLOCK_HOURS = 4

# What ODE's experiment status means here, from the enum in
# `pkg/experiments/experiments.go`.
STATUS_SUCCEEDED = "SUCCEEDED"
SPLIT_CONFIRMED = "confirmed"

# The tool-call outcome ODE records when the exposure tier refuses a tool
# (`OutcomeBlockedByTier`, pkg/tools/dispatch.go). The environment's refusal,
# counted apart from the developer's declines.
OUTCOME_BLOCKED_BY_TIER = "blocked_by_tier"


class ScoringError(RuntimeError):
    """Raised with the run id and the field whenever an input is missing or
    ambiguous. Every failure path in this script raises this, so a partial row
    is never written."""

    def __init__(self, run_id: str, field_name: str, detail: str) -> None:
        super().__init__(f"run {run_id}: {field_name}: {detail}")
        self.run_id = run_id
        self.field_name = field_name
        self.detail = detail


def fail(run_id: str, field_name: str, detail: str) -> "ScoringError":
    return ScoringError(run_id, field_name, detail)


# --- inputs ---------------------------------------------------------------


@dataclass(frozen=True)
class RunSpec:
    """One case of the ablation, as the manifest names it.

    Only what a human decided: which session is which case, and of which series.
    """

    run_id: str
    use_case: str
    tier: str
    session_id: str
    series: str


@dataclass
class Endpoints:
    ode_dsn: str


def read_manifest(path: str) -> tuple[list[RunSpec], dict[str, str]]:
    """The runs, with what no ODE record carries: which case a session is,
    and how a run that did not finish ended.

    Read once, and returned sorted by run id, so the output row order is the
    manifest's content and not its formatting.
    """
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)

    if not isinstance(document, dict) or "runs" not in document:
        raise ScoringError("<manifest>", "runs", f"{path} has no 'runs' key")
    series = document.get("series")
    if series not in SERIES_MODELS:
        raise ScoringError(
            "<manifest>", "series", f"{series!r} is not one of {list(SERIES_MODELS)}"
        )

    required = (
        "run_id",
        "use_case",
        "tier",
        "session_id",
    )
    specs: list[RunSpec] = []
    ended_on: dict[str, str] = {}
    for entry in document["runs"]:
        run_id = entry.get("run_id", "<unnamed>")
        for key in required:
            if not entry.get(key):
                raise fail(run_id, key, "missing from the manifest")
        if entry["use_case"] not in USE_CASE_ORDER:
            raise fail(
                run_id,
                "use_case",
                f"{entry['use_case']!r} is not one of {sorted(USE_CASE_ORDER)}",
            )
        if entry["tier"] not in TIERS:
            raise fail(run_id, "tier", f"{entry['tier']!r} is not one of {list(TIERS)}")
        expected_id = f"{entry['use_case']}-{entry['tier'].lower()}"
        if run_id != expected_id:
            raise fail(
                run_id,
                "run_id",
                f"a {entry['use_case']} run at {entry['tier']} is {expected_id!r}",
            )
        specs.append(RunSpec(**{key: entry[key] for key in required}, series=series))
        ended_on[run_id] = entry.get("ended_on", "")

    ids = [spec.run_id for spec in specs]
    if len(set(ids)) != len(ids):
        raise ScoringError("<manifest>", "run_id", "two runs share an id")
    sessions = [spec.session_id for spec in specs]
    if len(set(sessions)) != len(sessions):
        raise ScoringError(
            "<manifest>", "session_id", "two runs name the same session, and a session is one case"
        )
    return sorted(specs, key=lambda spec: spec.run_id), ended_on


# Both commands read ODE's database and nothing else.
ENVIRONMENT = ("ODE_DSN",)


def endpoints_from_environment() -> Endpoints:
    """The database DSN, from the environment."""
    missing = sorted(name for name in ENVIRONMENT if not os.environ.get(name))
    if missing:
        raise ScoringError("<environment>", ", ".join(missing), "not set")
    return Endpoints(ode_dsn=os.environ["ODE_DSN"])


# --- the sources ----------------------------------------------------------


def query_rows(
    ends: Endpoints, statement: str, parameters: dict[str, str], run_id: str, field_name: str
) -> list[dict]:
    """The rows of one statement against ODE's database, as JSON objects.

    Through `psql`, so the script needs no database driver. Every parameter is a
    psql variable that psql quotes as a literal (`:'name'`), never text formatted
    into the statement. Timestamps come back in UTC. Row order is not taken from
    the database: every caller sorts what it reads.
    """
    command = ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]
    for name, value in sorted(parameters.items()):
        command += ["-v", f"{name}={value}"]
    command.append(ends.ode_dsn)
    script = (
        "SET TimeZone = 'UTC';\n"
        "SELECT coalesce(json_agg(row_to_json(found)), '[]'::json) FROM (\n"
        f"{statement}\n"
        ") AS found;\n"
    )
    try:
        result = subprocess.run(command, input=script, capture_output=True, text=True, check=False)
    except FileNotFoundError as error:
        raise fail(run_id, field_name, "psql is not installed") from error
    if result.returncode != 0:
        raise fail(run_id, field_name, f"psql failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


# A session as the session route spells it, so discovery and scoring read the
# same keys whichever of them asks.
SESSION_ROWS = """
    SELECT s.id, s.title, s.tier AS exposure_tier, s.model, s.selection, s.created_at, s.updated_at,
           (SELECT count(*) FROM ode_chat_messages m WHERE m.session_id = s.id) AS message_count
    FROM ode_chat_sessions s
"""

# A launch as the experiments route spells it. The split is the pair of bounds
# the launch ran under; whether the run confirmed them is the summary's, not this
# record's.
EXPERIMENT_ROWS = """
    SELECT id AS experiment_id, session_id, repository, commit_sha, status,
           submitted_at, input_topics,
           CASE WHEN split_training_end IS NULL THEN NULL
                ELSE json_build_object('training_end', split_training_end,
                                       'test_end', split_test_end)
           END AS data_split
    FROM ode_experiments
"""


def read_conversation(spec: RunSpec, ends: Endpoints) -> dict:
    """The persisted conversation: the session and every message in `seq`
    order, in the shape `GET /chat/sessions/{id}` serves."""
    parameters = {"session_id": spec.session_id}
    sessions = query_rows(
        ends, SESSION_ROWS + " WHERE s.id = :'session_id'", parameters, spec.run_id, "conversation"
    )
    if len(sessions) != 1:
        raise fail(spec.run_id, "conversation", f"ODE has no session {spec.session_id}")
    messages = query_rows(
        ends,
        "SELECT seq, role, content, origin FROM ode_chat_messages WHERE session_id = :'session_id'",
        parameters,
        spec.run_id,
        "conversation",
    )
    document = {
        "session": sessions[0],
        "messages": sorted(messages, key=lambda message: message["seq"]),
    }

    tier = document["session"].get("exposure_tier")
    if tier != spec.tier:
        raise fail(
            spec.run_id,
            "exposure_tier",
            f"the session is at {tier!r}, the manifest says {spec.tier!r}",
        )
    model = document["session"].get("model")
    if model != SERIES_MODELS[spec.series]:
        raise fail(
            spec.run_id,
            "model",
            f"the session ran on {model!r}, the {spec.series} series on {SERIES_MODELS[spec.series]!r}",
        )
    return document


def read_confirmations(spec: RunSpec, ends: Endpoints) -> list[dict]:
    """Every confirmation of the session, resolved ones included, from
    `ode_confirmations`. The columns are ODE's own."""
    rows = query_rows(
        ends,
        """
        SELECT id, session_id, tool, input, tier, created_at, resolved_at,
               decision, applied_input
        FROM ode_confirmations
        WHERE session_id = :'session_id'
        """,
        {"session_id": spec.session_id},
        spec.run_id,
        "confirmations",
    )
    rows.sort(key=lambda row: (row["created_at"], row["id"]))

    for row in rows:
        if row["resolved_at"] is None:
            raise fail(
                spec.run_id,
                "confirmations",
                f"confirmation {row['id']} is still unresolved, so the run was not finished",
            )
        if row["decision"] not in ("approved", "rejected"):
            raise fail(
                spec.run_id,
                "confirmations",
                f"confirmation {row['id']} carries decision {row['decision']!r}",
            )
    return rows


def read_exchange_aborts(spec: RunSpec, ends: Endpoints) -> list[dict]:
    """Every exchange ODE stopped itself, from `ode_exchange_aborts`.

    The stop reason is otherwise an Exchange field, and an Exchange is memory.
    """
    rows = query_rows(
        ends,
        "SELECT id, session_id, reason, at FROM ode_exchange_aborts WHERE session_id = :'session_id'",
        {"session_id": spec.session_id},
        spec.run_id,
        "exchange_aborts",
    )
    return sorted(rows, key=lambda row: (row["at"], row["id"]))


def read_tool_call_outcomes(spec: RunSpec, ends: Endpoints) -> dict[str, int]:
    """The session's dispatch events in `ode_tool_calls`, by outcome.

    An event, not a call: a call that needs the developer's confirmation writes
    `awaiting_confirmation` when it is held and a second row with its outcome
    once approved, so these do not sum to the calls made. A tier refusal comes
    before the hold, so a call has at most one `blocked_by_tier` row."""
    rows = query_rows(
        ends,
        "SELECT outcome, count(*) AS calls FROM ode_tool_calls "
        "WHERE session_id = :'session_id' GROUP BY outcome",
        {"session_id": spec.session_id},
        spec.run_id,
        "tool_calls",
    )
    return {row["outcome"]: row["calls"] for row in sorted(rows, key=lambda row: row["outcome"])}


# The token counts ODE records per model call in `ode_usage`, by its column
# names. Their sum is a run's token count.
USAGE_COLUMNS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens")


def read_usage(spec: RunSpec, ends: Endpoints) -> dict[str, int]:
    """The session's token usage from `ode_usage`, summed per column over every
    model call, with the number of calls it was recorded for. A call on another
    model than the series' raises, and so does a session with no usage at all:
    every run talks to the model, so a session without a record is not one ODE
    accounted."""
    rows = query_rows(
        ends,
        "SELECT model, " + ", ".join(USAGE_COLUMNS) + " FROM ode_usage WHERE session_id = :'session_id'",
        {"session_id": spec.session_id},
        spec.run_id,
        "usage",
    )
    if not rows:
        raise fail(spec.run_id, "usage", "ODE recorded no token usage for the session")
    expected = SERIES_MODELS[spec.series]
    models = sorted({row["model"] for row in rows} - {expected})
    if models:
        raise fail(spec.run_id, "usage", f"usage recorded on {models}, the {spec.series} series runs on {expected!r}")
    totals = {column: sum(row[column] for row in rows) for column in USAGE_COLUMNS}
    totals["model_calls"] = len(rows)
    return totals


def all_experiments(ends: Endpoints, run_id: str) -> list[dict]:
    """Every launch ODE recorded. Discovery and scoring share this, so the two
    cannot disagree about which launches exist."""
    return query_rows(ends, EXPERIMENT_ROWS, {}, run_id, "experiments")


def read_experiments(spec: RunSpec, ends: Endpoints) -> list[dict]:
    """The run's launches, oldest first."""
    mine = query_rows(
        ends,
        EXPERIMENT_ROWS + " WHERE session_id = :'session_id'",
        {"session_id": spec.session_id},
        spec.run_id,
        "experiments",
    )
    for record in mine:
        for key in ("experiment_id", "status", "submitted_at", "commit_sha"):
            if key not in record:
                raise fail(spec.run_id, f"experiment.{key}", "missing from the record")
    return sorted(mine, key=lambda record: (record["submitted_at"], record["experiment_id"]))


def all_summaries(ends: Endpoints, run_id: str) -> dict[str, dict]:
    """Every run summary ODE kept, by experiment id."""
    rows = query_rows(
        ends, "SELECT experiment_id, record FROM ode_experiment_summaries", {}, run_id, "summaries"
    )
    return {row["experiment_id"]: row["record"] for row in rows}


def read_summaries(spec: RunSpec, ends: Endpoints, launches: list[dict]) -> tuple[dict[str, dict], list[str]]:
    """The summaries ODE kept for the run's launches, as
    `GET /experiments/{id}/results` serves them, and the launches it kept none
    for.

    ODE keeps a summary once it is built and settled, and a summary the poller
    built without the developer's token is never kept. A launch without one is
    tolerated only where its summary could not change the termination: one
    whose own record says it succeeded may be the run's completed evaluation,
    which only its summary can confirm, so it fails the run instead.
    """
    kept: dict[str, dict] = {}
    missing: list[str] = []
    for launch in launches:
        experiment_id = launch["experiment_id"]
        field_name = f"summary[{experiment_id}]"
        rows = query_rows(
            ends,
            "SELECT record FROM ode_experiment_summaries WHERE experiment_id = :'experiment_id'",
            {"experiment_id": experiment_id},
            spec.run_id,
            field_name,
        )
        if rows:
            kept[experiment_id] = rows[0]["record"]
            continue
        if launch.get("status") == STATUS_SUCCEEDED:
            raise fail(
                spec.run_id,
                field_name,
                "the launch succeeded and ODE kept no summary for it; open its results in ODE "
                "as the developer to build one",
            )
        missing.append(experiment_id)
    return kept, sorted(missing)


# --- time -----------------------------------------------------------------


def parse_instant(value: str, run_id: str, field_name: str) -> datetime:
    """One instant, in UTC. Accepts ISO 8601 with an offset or a trailing Z,
    and refuses anything else rather than guessing an offset."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise fail(run_id, field_name, f"{value!r} is not an ISO 8601 instant") from error
    if moment.tzinfo is None:
        raise fail(run_id, field_name, f"{value!r} carries no time zone")
    return moment.astimezone(timezone.utc)


# --- the variables --------------------------------------------------------


def covariates(spec: RunSpec, launch: dict) -> list[dict[str, str]]:
    """The covariates of the final operator: the input topics of the final
    launch, each mapping as the filter it reads (a device or an import
    instance), the service id for a device, and the variable path. Listed,
    never scored and never compared against an expected set. A training topic
    is not one; `training_topics` lists those.

    An import's topic is its instance id with the colons replaced, and carries
    no service (`imports.NodeInput` in ODE), so its service id is empty."""
    return topic_mappings(spec, launch, ROLE_INPUT)


def training_topics(spec: RunSpec, launch: dict) -> list[dict[str, str]]:
    """The training topics of the final launch, listed like the covariates.
    Recorded beside them, never counted: they are what the launch was scored
    against, not what the operator receives."""
    return topic_mappings(spec, launch, ROLE_TRAINING)


def topic_mappings(spec: RunSpec, launch: dict, role: str) -> list[dict[str, str]]:
    """The mappings of the final launch's topics of one role. A role other
    than input, training or none raises rather than being read as either."""
    topics = launch.get("input_topics")
    if not topics:
        raise fail(spec.run_id, "input_topics", "the final launch recorded none")

    listed: list[dict[str, str]] = []
    for index, topic in enumerate(topics):
        topic_role = topic.get("role") or ROLE_INPUT
        if topic_role not in (ROLE_INPUT, ROLE_TRAINING):
            raise fail(
                spec.run_id,
                f"input_topics[{index}].role",
                f"{topic_role!r} is neither {ROLE_INPUT!r} nor {ROLE_TRAINING!r}",
            )
        if topic_role != role:
            continue
        filter_type = topic.get("filterType")
        if filter_type == FILTER_DEVICE:
            service_id = service_id_of(topic.get("name", ""))
            if not service_id:
                raise fail(
                    spec.run_id,
                    f"input_topics[{index}].name",
                    f"{topic.get('name')!r} carries no service id",
                )
        elif filter_type == FILTER_IMPORT:
            service_id = ""
        else:
            raise fail(
                spec.run_id,
                f"input_topics[{index}].filterType",
                f"{filter_type!r} is neither a device nor an import, so the topic names no series",
            )
        for mapping in topic.get("mappings", []):
            listed.append(
                {
                    "filter_type": filter_type,
                    "filter_value": topic["filterValue"],
                    "service_id": service_id,
                    "variable_path": mapping.get("source", ""),
                }
            )
    return sorted(
        listed,
        key=lambda topic: (
            topic["filter_type"],
            topic["filter_value"],
            topic["service_id"],
            topic["variable_path"],
        ),
    )


# The two filters an operator input reads through: a device, or an import
# instance (`FilterTypeImport`, pkg/imports/wiring.go in ODE).
FILTER_DEVICE = "DeviceId"
FILTER_IMPORT = "ImportId"

SERVICE_URN = "urn:infai:ses:service:"
SERVICE_TOPIC = SERVICE_URN.replace(":", "_")


def service_id_of(topic_name: str) -> str:
    """The service id a topic name carries.

    ODE writes the Kafka topic as the service id with every colon replaced by an
    underscore (`serviceTopicName`, pkg/experiments/retarget.go), so the name is
    converted back rather than parsed. A name in neither spelling returns empty
    and the caller fails the run.
    """
    if topic_name.startswith(SERVICE_TOPIC):
        return SERVICE_URN + topic_name[len(SERVICE_TOPIC) :]
    if topic_name.startswith(SERVICE_URN):
        return topic_name
    return ""


def interaction_cost(
    spec: RunSpec,
    confirmations: list[dict],
    aborts: list[dict],
    launches: list[dict],
) -> dict[str, int]:
    """The launches, the confirmations the assistant requested, the declined
    ones among them, and the exchanges ODE ended at the tool-iteration cap.
    Counts only, each from one structured field. Confirmations are every tool
    call the developer was asked to approve, declined ones included."""
    declined = sum(1 for row in confirmations if row["decision"] == "rejected")
    capped = sum(1 for row in aborts if row["reason"] == "max_iterations")
    return {
        "confirmations": len(confirmations),
        "declined_confirmations": declined,
        "launches": len(launches),
        "exchanges_aborted_at_iteration_cap": capped,
    }


def developer_prompts(spec: RunSpec, conversation: dict) -> dict[str, int]:
    """The prompts the developer sent, by kind: the first is the opening problem statement, the rest count under the
    scripted reply they equal or as other. See CONFIRMATION_OUTCOME_PREFIXES for
    what is not a prompt although it is stored like one."""
    scripted = {reply.casefold(): label for label, reply in SCRIPTED_REPLIES.items()}
    counted = {label: 0 for label in (PROMPT_OPENING, PROMPT_OTHER, *SCRIPTED_REPLIES)}
    opened = False
    for message in conversation["messages"]:
        if message.get("role") != "user":
            continue
        texts = [block.get("text") or "" for block in message.get("content", []) if block.get("type") == "text"]
        if not texts:
            continue
        origin = message.get("origin")
        if origin is None:
            raise fail(spec.run_id, "developer_prompts", f"message {message['seq']} carries no origin")
        text = "\n".join(texts)
        if origin != ORIGIN_DEVELOPER or text.startswith(CONFIRMATION_OUTCOME_PREFIXES):
            continue
        if not opened:
            counted[PROMPT_OPENING] += 1
            opened = True
            continue
        counted[scripted.get(text.strip().casefold(), PROMPT_OTHER)] += 1
    return dict(sorted(counted.items()))


def tool_calls(spec: RunSpec, conversation: dict) -> dict[str, int]:
    """Every tool call the assistant made, by tool: the `tool_use`
    blocks of its messages, counted per tool name. A call the developer declined
    or the tier refused is a call made, and is counted here too."""
    counted: dict[str, int] = {}
    for message in conversation["messages"]:
        if message.get("role") != "assistant":
            continue
        for block in message.get("content", []):
            if block.get("type") != "tool_use":
                continue
            name = block.get("tool_name")
            if not name:
                raise fail(spec.run_id, "tool_calls", f"message {message['seq']} has a tool call with no name")
            counted[name] = counted.get(name, 0) + 1
    return dict(sorted(counted.items()))


def tier_raise_requests(spec: RunSpec, conversation: dict) -> int:
    """The requests for a higher tier: turns matching TIER_RAISE_PHRASES,
    case-folded exact substring matching and nothing else. A turn counts once however many
    phrases it matches."""
    count = 0
    for message in conversation["messages"]:
        if message.get("role") != "assistant":
            continue
        text = "\n".join(
            block.get("text") or ""
            for block in message.get("content", [])
            if block.get("type") == "text"
        ).casefold()
        if any(phrase in text for phrase in TIER_RAISE_PHRASES):
            count += 1
    return count


def termination(
    spec: RunSpec,
    conversation: dict,
    launches: list[dict],
    summaries: dict[str, dict],
    ended_on: str,
) -> dict[str, Any]:
    """Whether the run reached a committed operator and a completed evaluation, and if not, the stage it stopped at.

    A criterion the summary reports as `not_computed` is not a criterion that
    failed, so nothing here reads `evaluation_criteria`.
    """
    selection = conversation["session"].get("selection")
    committed = [launch for launch in launches if launch.get("commit_sha")]
    completed = [
        launch
        for launch in launches
        if summaries.get(launch["experiment_id"], {}).get("status") == STATUS_SUCCEEDED
        and (summaries[launch["experiment_id"]].get("data_split") or {}).get("confirmed")
        == SPLIT_CONFIRMED
    ]

    if completed:
        stage = ""
        reached = True
    elif not selection:
        stage, reached = STAGES[0], False
    elif not committed:
        stage, reached = STAGES[1], False
    elif not launches:
        stage, reached = STAGES[2], False
    else:
        stage, reached = STAGES[3], False

    if not reached and ended_on not in ("abandonment", "wall clock", "launch cap"):
        raise fail(
            spec.run_id,
            "ended_on",
            f"{ended_on!r} is not one of 'abandonment', 'wall clock', 'launch cap'",
        )

    return {
        "reached_operator_and_evaluation": reached,
        "stopped_at": stage,
        "ended_on": ended_on if not reached else "",
        "launches": len(launches),
        "committed_launches": len(committed),
        "completed_evaluations": len(completed),
    }


# --- one run --------------------------------------------------------------


def score_run(spec: RunSpec, ends: Endpoints, ended_on: str) -> tuple[dict, dict]:
    """Every variable of one run. Nothing here reads another run, so no figure
    depends on which runs have already been scored."""
    conversation = read_conversation(spec, ends)
    confirmations = read_confirmations(spec, ends)
    aborts = read_exchange_aborts(spec, ends)
    outcomes = read_tool_call_outcomes(spec, ends)
    usage = read_usage(spec, ends)
    launches = read_experiments(spec, ends)
    summaries, unsummarised = read_summaries(spec, ends, launches)

    successful = [
        launch
        for launch in launches
        if summaries.get(launch["experiment_id"], {}).get("status") == STATUS_SUCCEEDED
        and (summaries[launch["experiment_id"]].get("data_split") or {}).get("confirmed")
        == SPLIT_CONFIRMED
    ]
    window = TEST_WINDOWS[spec.use_case]
    if successful:
        recorded_split = successful[-1].get("data_split") or {}
        for field_name, expected in (
            ("training_end", window["start"]),
            ("test_end", window["end"]),
        ):
            recorded = recorded_split.get(field_name)
            if not recorded:
                raise fail(spec.run_id, f"data_split.{field_name}", "the launch recorded none")
            if parse_instant(recorded, spec.run_id, f"data_split.{field_name}") != parse_instant(
                expected, spec.run_id, f"data_split.{field_name}"
            ):
                raise fail(
                    spec.run_id,
                    f"data_split.{field_name}",
                    f"the run recorded {recorded}, the protocol sets {expected}",
                )

    final_launch = launches[-1] if launches else {}
    covariate_list = covariates(spec, final_launch) if launches else []
    training_list = training_topics(spec, final_launch) if launches else []
    counts = interaction_cost(spec, confirmations, aborts, launches)
    calls = tool_calls(spec, conversation)
    prompts = developer_prompts(spec, conversation)
    detail = termination(spec, conversation, launches, summaries, ended_on)

    row = {
        "run_id": spec.run_id,
        "use_case": spec.use_case,
        "tier": spec.tier,
        "model": conversation["session"]["model"],
        "launches": counts["launches"],
        "developer_prompts": sum(prompts.values()),
        "tokens": sum(usage[column] for column in USAGE_COLUMNS),
        "tool_calls": sum(calls.values()),
        "confirmations": counts["confirmations"],
        "tier_raise_requests": tier_raise_requests(spec, conversation),
        "covariate_count": len(covariate_list),
        "declined_confirmations": counts["declined_confirmations"],
        "tool_calls_blocked_by_tier": outcomes.get(OUTCOME_BLOCKED_BY_TIER, 0),
        "exchanges_aborted_at_iteration_cap": counts["exchanges_aborted_at_iteration_cap"],
        "reached_operator_and_evaluation": detail["reached_operator_and_evaluation"],
        "ended_on": detail["ended_on"],
    }

    side = {
        "covariates": covariate_list,
        "training_topics": training_list,
        "termination": detail,
        "developer_prompts": {"by_kind": prompts},
        "tool_calls": {"by_tool": calls, "dispatch_events_by_outcome": outcomes},
        "tokens": usage,
        "sources": {
            "session_id": spec.session_id,
            "final_launch": final_launch.get("experiment_id", ""),
            "final_successful_launch": successful[-1]["experiment_id"] if successful else "",
            "launches_without_a_kept_summary": unsummarised,
            "commit_sha": final_launch.get("commit_sha", ""),
            "test_window": dict(window),
        },
    }
    return row, side


# --- output ---------------------------------------------------------------

# The measures of RQ2 first, then those that explain differences between runs,
# then how the run went.
CSV_COLUMNS = (
    "run_id",
    "use_case",
    "tier",
    "model",
    "launches",
    "developer_prompts",
    "tokens",
    "tool_calls",
    "confirmations",
    "tier_raise_requests",
    "covariate_count",
    "declined_confirmations",
    "tool_calls_blocked_by_tier",
    "exchanges_aborted_at_iteration_cap",
    "reached_operator_and_evaluation",
    "ended_on",
)


def write_csv(path: str, rows: list[dict]) -> None:
    """One row per run, one column per variable, runs in run-id order and
    columns in the order above. Newlines are written explicitly so the
    file is byte-identical on every platform."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
        writer.writeheader()
        for row in sorted(rows, key=lambda row: row["run_id"]):
            writer.writerow(row)


def write_json(path: str, sides: dict[str, dict], ends: Endpoints, manifest_path: str) -> None:
    """The lists that do not fit a cell, and what produced them.

    `sort_keys` throughout, so the file is a function of its content and not of
    the order this script happened to build it.
    """
    document = {
        "runs": sides,
        "inputs": {
            "manifest": manifest_path,
            "ode_database": database_label(ends.ode_dsn),
            "test_windows": TEST_WINDOWS,
        },
        "repositories": {
            "scoring_script": repository_version(os.path.dirname(os.path.abspath(__file__))),
            "operators": {
                run_id: side["sources"]["commit_sha"] for run_id, side in sorted(sides.items())
            },
        },
        "constants": {
            "tier_raise_phrases": list(TIER_RAISE_PHRASES),
            "confirmation_outcome_prefixes": list(CONFIRMATION_OUTCOME_PREFIXES),
            "scripted_replies": SCRIPTED_REPLIES,
        },
        "notes": NOTES,
    }
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")


# What a reader of the output needs to know about how the measures are read.
NOTES = [
    "ODE's records are read from its database tables, not from its routes: the "
    "session route returns only unresolved confirmations, an exchange abort has "
    "no route at all, and the routes need a developer token the tables do not.",
    "The error of each launch is computed by the operator library in the "
    "experiment and is not read here. The approach and the termination reason of "
    "a run are recorded by hand.",
    "Developer prompts are the developer's user messages with a text block, less "
    "the confirmation outcomes ODE stores in the same shape, which are told apart "
    "by their opening words. The opening problem statement and the summary "
    "request after the run are counted.",
    "A topic with the training role, which the target of heat pump "
    "disaggregation has, is listed under training_topics and not counted as a "
    "covariate.",
    "Tokens are the sum of input, output, cache-read and cache-write tokens ODE "
    "recorded for the session in `ode_usage`.",
]


def database_label(dsn: str) -> str:
    """Where ODE's database is, without the credential the DSN carries. A URI
    and a key=value string are both read."""
    if "://" in dsn:
        parts = urllib.parse.urlsplit(dsn)
        return f"{parts.netloc.rsplit('@', 1)[-1]}{parts.path}"
    fields = dict(item.split("=", 1) for item in dsn.split() if "=" in item)
    return f"{fields.get('host', '')}:{fields.get('port', '5432')}/{fields.get('dbname', '')}"


def repository_version(path: str) -> str:
    """The commit the script at `path` is, with `-dirty` appended when the
    directory differs from it, so an output never names a commit its script
    was not."""
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return "not a git repository"
    changes = subprocess.run(
        ["git", "-C", path, "status", "--porcelain", "--", "."],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() + ("-dirty" if changes.stdout.strip() else "")


# --- discovery ------------------------------------------------------------
#
# Not reached by anything above. `discover` proposes a manifest from what ODE
# recorded and asks wherever the records leave a choice. `score` reads the
# manifest a human reviewed and committed, and asks nothing. Both live in this file because they share the
# constants above: a discovery carrying its own copy of the repository pattern
# or the target series could drift from the scoring that reads them.
#
# Almost every manifest field follows from a record. The session carries the
# tier, the experiment carries the repository, and the repository name carries
# the case. Three things do not follow from any record, and they are what the
# manifest is for: which of several sessions of a case is the run, and which of
# the developer's three stop rules ended a run that did not finish. Every
# answer is written into the manifest under `discovered.answers`, so the file
# records not only the assignment but also which part of it no record supplied.

DISCOVERY = "<discovery>"

def list_sessions(ends: Endpoints) -> list[dict]:
    """Every session ODE recorded, archived ones included: a discarded pilot is
    still a candidate the developer has to rule out."""
    return query_rows(ends, SESSION_ROWS, {}, DISCOVERY, "sessions")


def case_of_repository(repository: str) -> tuple[str, str, str] | None:
    """The series and case a repository name is, or None for a repository that
    is not one of the study's. `owner/name` and a bare name are both accepted,
    and the tier comes back as the session spells it."""
    name = repository.rsplit("/", 1)[-1]
    match = REPOSITORY_PATTERN.match(name.strip())
    if not match:
        return None
    series = "haiku" if match.group(3) else "opus"
    return series, match.group(1), match.group(2).upper()


def run_id_of(use_case: str, tier: str) -> str:
    return f"{use_case}-{tier.lower()}"


def attribute(
    sessions: list[dict], experiments: list[dict], summaries: dict[str, dict], series: str
) -> tuple[dict, list[dict]]:
    """Every session of the series, sorted into the case its launches name.

    A session on another model than the series' is not looked at. A session is
    attributed by the repository its launches were built from, because that is
    the only record tying a session to a case. A session whose launches name
    two repositories breaks `one repository per run` and raises rather than
    being assigned to either, and so does one that launched from another
    series' repository. A session with no launch cannot be attributed at all and
    is returned separately, to be offered for a case that has no candidate.
    """
    launches: dict[str, list[dict]] = {}
    for record in experiments:
        session_id = record.get("session_id")
        if session_id:
            launches.setdefault(session_id, []).append(record)

    found: dict[str, list[dict]] = {}
    unattributed: list[dict] = []
    for session in sorted(sessions, key=lambda item: (item.get("created_at", ""), item.get("id", ""))):
        if session.get("model") != SERIES_MODELS[series]:
            continue
        mine = sorted(
            launches.get(session.get("id", ""), []),
            key=lambda record: (record.get("submitted_at", ""), record.get("experiment_id", "")),
        )
        if not mine:
            unattributed.append(candidate(session, [], summaries))
            continue

        cases = {case_of_repository(record.get("repository", "")) for record in mine}
        cases.discard(None)
        if len(cases) > 1:
            raise fail(
                session.get("id", "<unnamed>"),
                "repository",
                f"the session launched from {sorted(cases)}, and a run has one repository",
            )
        if not cases:
            unattributed.append(candidate(session, mine, summaries))
            continue

        repository_series, use_case, tier = cases.pop()
        if repository_series != series:
            raise fail(
                session.get("id", "<unnamed>"),
                "repository",
                f"the session ran on {SERIES_MODELS[series]} and launched from a "
                f"{repository_series} series repository",
            )
        found.setdefault(run_id_of(use_case, tier), []).append(candidate(session, mine, summaries))
    return found, unattributed


def candidate(session: dict, launches: list[dict], summaries: dict[str, dict]) -> dict:
    """One session, with what tells it apart from another session of the same
    case: when it ran, how long, how much was said, and how far it got.

    How far it got is read off the kept summaries, with the rule `termination`
    applies: the launch record carries the split it ran under, and only the
    summary says whether the run confirmed it."""
    reached = any(
        summaries.get(record.get("experiment_id", ""), {}).get("status") == STATUS_SUCCEEDED
        and (summaries[record["experiment_id"]].get("data_split") or {}).get("confirmed")
        == SPLIT_CONFIRMED
        for record in launches
    )
    return {
        "session_id": session.get("id", ""),
        "title": session.get("title", ""),
        "tier": session.get("exposure_tier", ""),
        "selection": session.get("selection"),
        "created_at": session.get("created_at", ""),
        "updated_at": session.get("updated_at", ""),
        "message_count": session.get("message_count", 0),
        "repository": launches[-1].get("repository", "") if launches else "",
        "launches": len(launches),
        "reached": reached,
    }


def span_hours(entry: dict) -> float:
    """How long a session was open, from its own two timestamps. Used only to
    suggest a stop rule, never to decide one."""
    try:
        first = parse_instant(entry["created_at"], DISCOVERY, "created_at")
        last = parse_instant(entry["updated_at"], DISCOVERY, "updated_at")
    except (ScoringError, KeyError):
        return 0.0
    return (last - first).total_seconds() / 3600.0


def suggest_ended_on(entry: dict) -> str:
    """Which of the developer's three rules the records look like. A
    suggestion: none of the three is enforced, two can hold at once, and which
    one the developer applied is theirs to say."""
    if entry["launches"] >= LAUNCH_CAP:
        return "launch cap"
    if span_hours(entry) >= WALL_CLOCK_HOURS:
        return "wall clock"
    return "abandonment"


def device_of_selection(selection: Any, use_case: str) -> str:
    """The device the confirmed selection reads the target series from, or
    empty. Matched on the target's service id and variable path, so a selection
    that carries several series still yields the target's device."""
    if not isinstance(selection, dict):
        return ""
    target = TARGET_SERIES[use_case]
    for entry in selection.get("series") or []:
        if (
            entry.get("service_id") == target["service_id"]
            and entry.get("variable_path") == target["column"]
        ):
            return entry.get("device_id", "")
    return ""


class Asker:
    """Every question discovery asks, and every answer it got.

    An answer no ODE record could have supplied is exactly what the manifest
    exists for, so it is written into the file rather than left in a terminal.
    A question with nobody to answer it raises: a manifest guessed in a
    non-interactive shell would carry the guess with no sign that it was one.
    """

    def __init__(self) -> None:
        self.answers: list[dict[str, str]] = []

    def _read(self, run_id: str, field_name: str, question: str) -> str:
        if not sys.stdin.isatty():
            raise fail(
                run_id,
                field_name,
                f"{question}, and stdin is not a terminal, so there is nobody to ask",
            )
        return input(f"{question}\n> ").strip()

    def choose(self, run_id: str, field_name: str, question: str, options: list[str], default: str = "") -> str:
        lines = [f"[{run_id}] {question}"]
        for index, option in enumerate(options, start=1):
            lines.append(f"  {index}) {option}")
        if default:
            lines.append(f"  [enter] {default}")
        answer = self._read(run_id, field_name, "\n".join(lines))
        if not answer and default:
            chosen = default
        else:
            try:
                chosen = options[int(answer) - 1]
            except (ValueError, IndexError) as error:
                raise fail(run_id, field_name, f"{answer!r} is not one of the offered answers") from error
        self.answers.append({"run_id": run_id, "field": field_name, "value": chosen})
        return chosen


def describe(entry: dict) -> str:
    state = "reached an evaluation" if entry["reached"] else "did not finish"
    return (
        f"{entry['session_id']}  {entry['created_at']}  tier {entry['tier']}  "
        f"{entry['message_count']} messages  {entry['launches']} launches  "
        f"{entry['title'] or 'untitled'}  ({state})"
    )


def could_be(entry: dict, use_case: str, tier: str) -> bool:
    """Whether a session that never launched could still be this case.

    A session without a launch carries no repository, so the only records left
    are its tier and the series the developer confirmed in it. A session at
    another tier is not this case. A session whose selection names another use
    case's target is not either. A session with no selection at all cannot be
    ruled out, and is offered rather than dropped.
    """
    if entry["tier"] != tier:
        return False
    selection = entry["selection"]
    if not isinstance(selection, dict) or not selection.get("series"):
        return True
    return bool(device_of_selection(selection, use_case))


def pick_session(
    asker: Asker, use_case: str, tier: str, options: list[dict], unattributed: list[dict]
) -> dict:
    """Which session is this case.

    A session is offered when it launched from this case's repository, and also
    when it never launched and nothing rules it out. Both belong in the same
    list: a run that ended before its first launch is a documented outcome
    (`STAGES[2]`), and a discarded pilot of the same case may well have
    launched, so taking the one session with a launch would take the pilot and
    drop the run without anything saying so.

    One candidate is taken without a question, since nothing is left to decide. Two
    or more is the case the manifest exists for, and no record says which is
    which.
    """
    run_id = run_id_of(use_case, tier)
    offered = options + [entry for entry in unattributed if could_be(entry, use_case, tier)]

    if not offered:
        raise fail(
            run_id,
            "session_id",
            "no session launched from this case's repository, and no session without a launch "
            "fits its tier and target series",
        )
    if len(offered) == 1:
        return offered[0]

    described = [describe(entry) for entry in offered]
    chosen = asker.choose(
        run_id,
        "session_id",
        f"{len(offered)} sessions could be this case. "
        f"{len(options)} launched from its repository, {len(offered) - len(options)} never launched. "
        "Which one is the run?",
        described,
    )
    return offered[described.index(chosen)]


def manifest_row(
    asker: Asker,
    use_case: str,
    tier: str,
    entry: dict,
) -> dict[str, str]:
    """One row, from the records where they carry it and from an answer where
    they do not."""
    run_id = run_id_of(use_case, tier)

    if entry["tier"] != tier:
        raise fail(
            run_id,
            "exposure_tier",
            f"the session is at {entry['tier']!r} and launched from a {tier} repository",
        )

    ended_on = ""
    if not entry["reached"]:
        ended_on = asker.choose(
            run_id,
            "ended_on",
            f"No launch of this run completed an evaluation, after {entry['launches']} launches "
            f"over {span_hours(entry):.1f} hours. Which rule ended it?",
            list(END_REASONS),
            default=suggest_ended_on(entry),
        )

    return {
        "run_id": run_id,
        "use_case": use_case,
        "tier": tier,
        "session_id": entry["session_id"],
        "ended_on": ended_on,
    }


def discover(ends: Endpoints, series: str) -> dict:
    """The rows of one series, asked for in the order the runs were made and written
    sorted by run id, so the file is a function of its content and the
    questions follow the order the runs were made in."""
    found, unattributed = attribute(
        list_sessions(ends), all_experiments(ends, DISCOVERY), all_summaries(ends, DISCOVERY), series
    )
    asker = Asker()
    rows: list[dict[str, str]] = []
    taken: dict[str, str] = {}
    for use_case in USE_CASE_ORDER:
        for tier in TIERS:
            run_id = run_id_of(use_case, tier)
            entry = pick_session(asker, use_case, tier, found.get(run_id, []), unattributed)
            if entry["session_id"] in taken:
                raise fail(
                    run_id,
                    "session_id",
                    f"{entry['session_id']} is already the session of {taken[entry['session_id']]}",
                )
            taken[entry["session_id"]] = run_id
            rows.append(manifest_row(asker, use_case, tier, entry))
    return {
        "discovered": {
            "at": datetime.now(timezone.utc).isoformat(),
            "ode_database": database_label(ends.ode_dsn),
            "scoring_script": repository_version(os.path.dirname(os.path.abspath(__file__))),
            "answers": asker.answers,
        },
        "series": series,
        "runs": sorted(rows, key=lambda row: row["run_id"]),
    }


def write_manifest(path: str, document: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")


# --- entry point ----------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    proposal = commands.add_parser(
        "discover",
        help="propose a manifest from what ODE recorded, asking where no record decides",
    )
    proposal.add_argument("--series", required=True, choices=list(SERIES_MODELS), help="the series to propose")
    proposal.add_argument("--out", required=True, help="the manifest to write, for review and commit")

    scoring = commands.add_parser("score", help="the dependent variables, from a reviewed manifest")
    scoring.add_argument("--manifest", required=True, help="the runs of one series, as JSON")
    scoring.add_argument("--out-csv", required=True, help="one row per run")
    scoring.add_argument("--out-json", required=True, help="the lists that do not fit a cell")

    arguments = parser.parse_args(argv)
    if arguments.command == "discover":
        ends = endpoints_from_environment()
        write_manifest(arguments.out, discover(ends, arguments.series))
        print(f"wrote {arguments.out}; review it and commit it before scoring", file=sys.stderr)
        return 0

    specs, ended_on = read_manifest(arguments.manifest)
    ends = endpoints_from_environment()

    rows: list[dict] = []
    sides: dict[str, dict] = {}
    for spec in specs:
        row, side = score_run(spec, ends, ended_on[spec.run_id])
        rows.append(row)
        sides[spec.run_id] = side

    write_csv(arguments.out_csv, rows)
    write_json(arguments.out_json, sides, ends, arguments.manifest)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ScoringError as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
