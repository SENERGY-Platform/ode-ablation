"""Checks on the pure parts of score_runs.py: the counts, the manifest and the
attribution of a session to a case. Nothing here touches the database."""

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import score_runs as s  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"ok   {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} {detail}")


def spec(use_case="pv", run_id="pv-l0"):
    return s.RunSpec(
        run_id=run_id,
        use_case=use_case,
        tier="L0",
        session_id="sess-1",
        series="opus",
    )


# --- covariates ---

launch = {
    "input_topics": [
        {
            "name": "urn_infai_ses_service_bbbb",
            "filterType": "DeviceId",
            "filterValue": "dev-2",
            "mappings": [{"dest": "value", "source": "root.powerTotal"}],
        },
        {
            "name": "urn_infai_ses_service_aaaa",
            "filterType": "DeviceId",
            "filterValue": "dev-1",
            "mappings": [{"dest": "value", "source": "sensor.ENERGY.Power"}],
        },
        {
            "name": "urn_infai_ses_import_cccc",
            "filterType": "ImportId",
            "filterValue": "urn:infai:ses:import:cccc",
            "mappings": [{"dest": "cloud", "source": "value.cloud_cover"}, {"dest": "temp", "source": "value.temperature"}],
        },
    ]
}
listed = s.covariates(spec(), launch)
check("topics are sorted, devices before imports", [t["filter_value"] for t in listed] == [
    "dev-1", "dev-2", "urn:infai:ses:import:cccc", "urn:infai:ses:import:cccc"
], listed)
check(
    "an import is listed per mapping, with no service",
    [(t["filter_type"], t["service_id"], t["variable_path"]) for t in listed[2:]]
    == [("ImportId", "", "value.cloud_cover"), ("ImportId", "", "value.temperature")],
)
check(
    "service id comes back as a urn",
    listed[0]["service_id"] == "urn:infai:ses:service:aaaa",
    listed[0],
)
check("variable path is the mapping source", listed[0]["variable_path"] == "sensor.ENERGY.Power")

try:
    s.covariates(spec(), {"input_topics": [{"name": "nonsense", "filterType": "DeviceId", "filterValue": "d", "mappings": []}]})
    check("an unreadable topic name raises", False)
except s.ScoringError as error:
    check("an unreadable topic name raises", "service id" in str(error))

try:
    s.covariates(spec(), {"input_topics": [{"name": "t", "filterType": "OperatorId", "filterValue": "o", "mappings": []}]})
    check("an operator input raises", False)
except s.ScoringError as error:
    check("an operator input raises", "neither a device nor an import" in str(error))

# A NILM launch: the main meter is the input, the heat pump's sub-meter the label.
NILM = s.TARGET_SERIES["nilm"]
labelled = {
    "input_topics": [
        {
            "name": "urn_infai_ses_service_9dc0f355",
            "filterType": "DeviceId",
            "filterValue": "dev-meter",
            "role": "input",
            "mappings": [{"dest": "energy", "source": "sensor.data.obis_1_8_0_f"}],
        },
        {
            "name": NILM["service_id"].replace(":", "_"),
            "filterType": "DeviceId",
            "filterValue": "dev-heatpump",
            "role": "training",
            "mappings": [{"dest": "power", "source": NILM["column"]}],
        },
    ]
}
check(
    "a training topic is not a covariate",
    [t["filter_value"] for t in s.covariates(spec("nilm", "nilm-l0"), labelled)] == ["dev-meter"],
)
check(
    "a training topic is listed apart",
    [(t["filter_value"], t["variable_path"]) for t in s.training_topics(spec("nilm", "nilm-l0"), labelled)]
    == [("dev-heatpump", NILM["column"])],
)
check("a topic with no role has no training topic", s.training_topics(spec(), launch) == [])

try:
    s.covariates(spec(), {"input_topics": [dict(launch["input_topics"][0], role="label")]})
    check("an unknown role raises", False)
except s.ScoringError as error:
    check("an unknown role raises", "input_topics[0].role" in str(error))

# --- the conversation counts ---

conversation = {
    "session": {"exposure_tier": "L0", "selection": {"device_id": "dev-1"}},
    "messages": [
        {"role": "assistant", "content": [{"type": "text", "text": "Which meter did you mean?"}]},
        {"role": "user", "content": [{"type": "text", "text": "The house meter."}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I would need values here. Please RAISE THE TIER to L1."},
                {"type": "tool_use", "tool_name": "read_values", "tool_input": {}},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Asking you to raise the tier, and raising the tier again."},
                {"type": "tool_use", "tool_name": "run_code", "tool_input": {}},
                {"type": "tool_use", "tool_name": "read_values", "tool_input": {}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_name": "read_values", "tool_result": "{}"}]},
    ],
}
confirmations = [
    {"id": "c1", "decision": "rejected", "resolved_at": "now", "tool": "run_code"},
    {"id": "c2", "decision": "approved", "resolved_at": "now", "tool": "run_code"},
    {"id": "c3", "decision": "rejected", "resolved_at": "now", "tool": "launch_experiment"},
]
aborts = [{"reason": "max_iterations"}, {"reason": "max_iterations"}]
counts = s.interaction_cost(spec(), confirmations, aborts, [{"experiment_id": "e1"}])
check("launches counted from the records", counts["launches"] == 1, counts)
check("declines counted from the decision", counts["declined_confirmations"] == 2)
check("aborts counted from the audit", counts["exchanges_aborted_at_iteration_cap"] == 2)
check("every confirmation counted", counts["confirmations"] == 3)

calls = s.tool_calls(spec(), conversation)
check("tool calls counted per name, assistant turns only", calls == {"read_values": 2, "run_code": 1}, calls)
try:
    s.tool_calls(spec(), {"messages": [{"seq": 4, "role": "assistant", "content": [{"type": "tool_use"}]}]})
    check("a tool call with no name raises", False)
except s.ScoringError as error:
    check("a tool call with no name raises", "message 4" in str(error))

raises = s.tier_raise_requests(spec(), conversation)
check("tier raises are case-folded and per turn", raises == 2, f"got {raises}")


def typed(seq, text, origin=""):
    return {"seq": seq, "role": "user", "origin": origin, "content": [{"type": "text", "text": text}]}


prompted = {
    "messages": [
        typed(1, "Forecast the meter."),
        {"seq": 2, "role": "assistant", "content": [{"type": "text", "text": "Which meter?"}]},
        typed(3, "Committed"),
        typed(4, "  ARE YOU DONE?\n"),
        typed(5, "proceed with your proposal"),
        typed(6, "Not known."),
        typed(7, "The developer declined the run_code call. Do not retry it; ask what they would prefer."),
        typed(8, "The developer approved the launch_experiment call after changing it. Changed: a → b."),
        typed(9, "The run finished.", origin="ode"),
        {"seq": 10, "role": "user", "origin": "", "content": [{"type": "tool_result", "tool_name": "run_code"}]},
        typed(11, "committed, and pushed"),
        typed(12, s.SCRIPTED_REPLIES["readme summary"]),
    ]
}
prompts = s.developer_prompts(spec(), prompted)
check(
    "prompts are the developer's typed turns, by kind",
    prompts == {
        "are you done": 1,
        "committed": 1,
        "experiment done": 0,
        "opening problem statement": 1,
        "other": 2,
        "proceed with your proposal": 1,
        "readme summary": 1,
    },
    prompts,
)
try:
    s.developer_prompts(spec(), {"messages": [{"seq": 3, "role": "user", "content": [{"type": "text", "text": "x"}]}]})
    check("a typed turn with no origin raises", False)
except s.ScoringError as error:
    check("a typed turn with no origin raises", "message 3" in str(error))

# --- termination ---

summaries = {
    "e1": {"status": "SUCCEEDED", "data_split": {"confirmed": "confirmed"}},
}
detail = s.termination(spec(), conversation, [{"experiment_id": "e1", "commit_sha": "abc"}], summaries, "")
check("a completed evaluation terminates", detail["reached_operator_and_evaluation"] and detail["stopped_at"] == "")

detail = s.termination(
    spec(),
    conversation,
    [{"experiment_id": "e1", "commit_sha": "abc"}],
    {"e1": {"status": "FAILED", "data_split": {"confirmed": "pending"}}},
    "abandonment",
)
check("a failed evaluation stops at the last stage", detail["stopped_at"] == "no completed evaluation")

detail = s.termination(
    spec(),
    {"session": {"selection": None}, "messages": []},
    [{"experiment_id": "e1", "commit_sha": "abc"}],
    {"e1": {"status": "FAILED"}},
    "wall clock",
)
check("no selection is the first stage", detail["stopped_at"] == "no selection")

try:
    s.termination(spec(), conversation, [{"experiment_id": "e1", "commit_sha": "abc"}], {"e1": {"status": "FAILED"}}, "gave up")
    check("an unknown end reason raises", False)
except s.ScoringError as error:
    check("an unknown end reason raises", "ended_on" in str(error))

# --- the manifest ---

PV = s.TARGET_SERIES["pv"]
selection = {
    "series": [
        {"device_id": "dev-9", "service_id": "urn:infai:ses:service:other", "variable_path": "x"},
        {"device_id": "dev-1", "service_id": PV["service_id"], "variable_path": PV["column"]},
    ]
}


def manifest_entry(**changes):
    entry = {
        "run_id": "pv-l0",
        "use_case": "pv",
        "tier": "L0",
        "session_id": "sess-1",
        "ended_on": "",
    }
    entry.update(changes)
    return entry


def read_entries(*entries, series="opus"):
    with tempfile.TemporaryDirectory() as directory:
        path = pathlib.Path(directory) / "runs.json"
        document = {"runs": list(entries)}
        if series is not None:
            document["series"] = series
        path.write_text(json.dumps(document))
        return s.read_manifest(str(path))


specs, ended = read_entries(
    manifest_entry(),
    manifest_entry(run_id="load-l2", use_case="load", tier="L2", session_id="sess-2"),
    manifest_entry(run_id="nilm-l1", use_case="nilm", tier="L1", session_id="sess-3"),
)
check("the target series are not in the manifest", not hasattr(specs[0], "target_service_id"))
check("the manifest reads back sorted", [spec.run_id for spec in specs] == ["load-l2", "nilm-l1", "pv-l0"])
check("ended_on is carried beside the spec", ended == {"pv-l0": "", "load-l2": "", "nilm-l1": ""})
check("every spec carries the manifest's series", {spec.series for spec in specs} == {"opus"})

try:
    read_entries(manifest_entry(run_id="heat-l0", use_case="heat"))
    check("a use case outside the three raises", False)
except s.ScoringError as error:
    check("a use case outside the three raises", "use_case" in str(error))

for missing_or_unknown in (None, "sonnet"):
    try:
        read_entries(manifest_entry(), series=missing_or_unknown)
        check(f"a manifest with series {missing_or_unknown!r} raises", False)
    except s.ScoringError as error:
        check(f"a manifest with series {missing_or_unknown!r} raises", error.field_name == "series")

try:
    read_entries(manifest_entry(run_id="pv-lowest"))
    check("a run id that is not its case raises", False)
except s.ScoringError as error:
    check("a run id that is not its case raises", "pv-l0" in str(error))

try:
    read_entries(manifest_entry(tier="l0"))
    check("a tier outside the three raises", False)
except s.ScoringError as error:
    check("a tier outside the three raises", "tier" in str(error))

try:
    read_entries(manifest_entry(), manifest_entry(run_id="pv-l1", tier="L1"))
    check("one session for two cases raises", False)
except s.ScoringError as error:
    check("one session for two cases raises", "session is one case" in str(error))

# --- discovery: the case a repository names ---

check("an owner-qualified repository names its case", s.case_of_repository("study/ode-ablation-pv-l0") == ("opus", "pv", "L0"))
check("a bare name names its case", s.case_of_repository("ode-ablation-load-l2") == ("opus", "load", "L2"))
check("a NILM repository names its case", s.case_of_repository("study/ode-ablation-nilm-l1") == ("opus", "nilm", "L1"))
check("the haiku suffix names the haiku series", s.case_of_repository("study/ode-ablation-pv-l1-haiku") == ("haiku", "pv", "L1"))
check("a re-run names its case", s.case_of_repository("study/ode-ablation-pv-l2-rerun-1") == ("opus", "pv", "L2"))
check("a haiku re-run names its case", s.case_of_repository("ode-ablation-load-l0-rerun-2-haiku") == ("haiku", "load", "L0"))
check("any other suffix names none", s.case_of_repository("study/ode-ablation-pv-l2-pilot") is None)
check("an unrelated repository names none", s.case_of_repository("study/operator-playground") is None)
check("a tier outside the three names none", s.case_of_repository("ode-ablation-pv-l3") is None)

# --- discovery: sessions sorted into cases ---

def session(id_, tier, created="2026-09-20T08:00:00Z", updated="2026-09-20T09:00:00Z", model="claude-opus-5-5"):
    return {
        "id": id_,
        "title": "",
        "exposure_tier": tier,
        "model": model,
        "created_at": created,
        "updated_at": updated,
        "message_count": 12,
        "selection": selection,
    }


# The summaries ODE kept, by experiment id. `launch` files one for every record
# it makes, because a launch record carries the split it ran under and only its
# summary says whether the run confirmed it.
SUMMARIES = {}


def launch(session_id, repository, status="SUCCEEDED", confirmed="confirmed", at="2026-09-20T08:30:00Z"):
    record = {
        "experiment_id": f"e-{session_id}-{at}",
        "session_id": session_id,
        "repository": repository,
        "status": status,
        "data_split": {"training_end": s.TEST_WINDOWS["pv"]["start"], "test_end": s.TEST_WINDOWS["pv"]["end"]},
        "submitted_at": at,
    }
    SUMMARIES[record["experiment_id"]] = {"status": status, "data_split": {"confirmed": confirmed}}
    return record


found, unattributed = s.attribute(
    [session("s1", "L0"), session("s2", "L0"), session("s3", "L1")],
    [
        launch("s1", "study/ode-ablation-pv-l0"),
        launch("s2", "study/ode-ablation-pv-l0", status="FAILED", confirmed="pending"),
    ],
    SUMMARIES,
    "opus",
)
check("both sessions of a case are candidates", [entry["session_id"] for entry in found["pv-l0"]] == ["s1", "s2"])
check("a completed evaluation is seen", found["pv-l0"][0]["reached"] and not found["pv-l0"][1]["reached"])

unkept = launch("s4", "study/ode-ablation-pv-l1")
check(
    "a launch without a kept summary is not seen as completed",
    not s.candidate(session("s4", "L1"), [unkept], {})["reached"],
)
check("a session without a launch is unattributed", [entry["session_id"] for entry in unattributed] == ["s3"])

try:
    s.attribute(
        [session("s1", "L0")],
        [launch("s1", "study/ode-ablation-pv-l0"), launch("s1", "study/ode-ablation-load-l0", at="2026-09-20T08:40:00Z")],
        SUMMARIES,
        "opus",
    )
    check("two repositories in one session raise", False)
except s.ScoringError as error:
    check("two repositories in one session raise", "one repository" in str(error))

found, unattributed = s.attribute(
    [session("h1", "L1", model="claude-haiku-5-5"), session("o1", "L1"), session("h2", "L2", model="claude-haiku-5-5")],
    [launch("h1", "study/ode-ablation-pv-l1-haiku"), launch("o1", "study/ode-ablation-pv-l1")],
    SUMMARIES,
    "haiku",
)
check("a series sees its own sessions only", list(found) == ["pv-l1"] and found["pv-l1"][0]["session_id"] == "h1", found)
check("an unlaunched session of the series stays a candidate", [entry["session_id"] for entry in unattributed] == ["h2"])

try:
    s.attribute([session("o2", "L0")], [launch("o2", "study/ode-ablation-pv-l0-haiku")], SUMMARIES, "opus")
    check("a launch from another series' repository raises", False)
except s.ScoringError as error:
    check("a launch from another series' repository raises", "haiku series repository" in str(error))

# --- discovery: what the records suggest, and never decide ---

check(
    "the launch cap is suggested at the cap",
    s.suggest_ended_on({"launches": 10, "created_at": "2026-09-20T08:00:00Z", "updated_at": "2026-09-20T08:10:00Z"})
    == "launch cap",
)
check(
    "the wall clock is suggested past four hours",
    s.suggest_ended_on({"launches": 2, "created_at": "2026-09-20T08:00:00Z", "updated_at": "2026-09-20T12:30:00Z"})
    == "wall clock",
)
check(
    "neither cap suggests abandonment",
    s.suggest_ended_on({"launches": 2, "created_at": "2026-09-20T08:00:00Z", "updated_at": "2026-09-20T09:00:00Z"})
    == "abandonment",
)
check("every suggestion is an accepted answer", set(s.END_REASONS) == {"abandonment", "wall clock", "launch cap"})
check("the run order is pv, load, nilm", s.USE_CASE_ORDER == ("pv", "load", "nilm"))
check("every use case has a target and a test window", set(s.TARGET_SERIES) == set(s.TEST_WINDOWS) == set(s.USE_CASE_ORDER))

check("the target device comes off the selection", s.device_of_selection(selection, "pv") == "dev-1")
check("a selection without the target yields none", s.device_of_selection({"series": []}, "pv") == "")
check("a use case reads its own series", s.device_of_selection(selection, "load") == "")

# --- discovery never guesses an answer it cannot ask for ---

try:
    s.Asker()._read("pv-l0", "ended_on", "which rule ended it?")
    check("a question with nobody to ask raises", False)
except s.ScoringError as error:
    check("a question with nobody to ask raises", "not a terminal" in str(error))

# --- discovery: which session is the case ---
#
# The case that matters is the one the manifest exists for: a discarded pilot of
# a case launched, the run itself never did. Taking the only session with a
# launch would take the pilot and drop the run, and nothing in the output would
# say so.


class RecordingAsker(s.Asker):
    """Answers with the first option and remembers having been asked, so a
    question that should have been asked cannot pass unnoticed."""

    def __init__(self):
        super().__init__()
        self.asked = []

    def choose(self, run_id, field_name, question, options, default=""):
        self.asked.append((run_id, field_name, len(options)))
        self.answers.append({"run_id": run_id, "field": field_name, "value": options[0]})
        return options[0]

    def text(self, run_id, field_name, question):
        raise AssertionError(f"asked for text: {question}")


def unlaunched(id_, tier, use_case="pv", has_selection=True):
    entry = session(id_, tier)
    target = s.TARGET_SERIES[use_case]
    entry["selection"] = (
        {"series": [{"device_id": f"dev-{use_case}", "service_id": target["service_id"], "variable_path": target["column"]}]}
        if has_selection
        else None
    )
    return s.candidate(entry, [], SUMMARIES)


pilot = s.candidate(
    session("pilot", "L0"),
    [launch("pilot", "study/ode-ablation-pv-l0", status="FAILED", confirmed="pending")],
    SUMMARIES,
)
real = unlaunched("real", "L0")

asker = RecordingAsker()
picked = s.pick_session(asker, "pv", "L0", [pilot], [real])
check("a pilot that launched is not taken over a run that did not", asker.asked == [("pv-l0", "session_id", 2)], asker.asked)
check("the answer is recorded", [answer["field"] for answer in asker.answers] == ["session_id"])

asker = RecordingAsker()
only = s.pick_session(asker, "pv", "L0", [pilot], [])
check("one candidate is taken without a question", only is pilot and asker.asked == [])

asker = RecordingAsker()
s.pick_session(
    asker, "pv", "L0", [pilot, s.candidate(session("other", "L0"), [launch("other", "study/ode-ablation-pv-l0")], SUMMARIES)], []
)
check("two launched candidates are asked about", asker.asked == [("pv-l0", "session_id", 2)], asker.asked)

try:
    s.pick_session(RecordingAsker(), "pv", "L0", [], [unlaunched("elsewhere", "L2")])
    check("a case with no candidate raises", False)
except s.ScoringError as error:
    check("a case with no candidate raises", "session_id" in str(error))

# what a session that never launched can still be ruled out by
check("another tier is ruled out", not s.could_be(unlaunched("x", "L1"), "pv", "L0"))
check("another use case's target is ruled out", not s.could_be(unlaunched("x", "L0", use_case="load"), "pv", "L0"))
check("its own use case's target fits", s.could_be(unlaunched("x", "L0", use_case="load"), "load", "L0"))
check("the heat pump's series is NILM's", s.could_be(unlaunched("x", "L0", use_case="nilm"), "nilm", "L0"))
check("the heat pump's series is not load's", not s.could_be(unlaunched("x", "L0", use_case="nilm"), "load", "L0"))
check("no selection cannot be ruled out", s.could_be(unlaunched("x", "L0", has_selection=False), "pv", "L0"))

# --- the database is named without its credential ---

check(
    "a URI DSN loses its password",
    s.database_label("postgresql://ode:secret@localhost:5432/ode?sslmode=disable") == "localhost:5432/ode",
)
check(
    "a multi-host URI is named, not fatal",
    s.database_label("postgresql://ode:secret@a:5432,b:5432/ode") == "a:5432,b:5432/ode",
)

# --- a launch without a kept summary ---

kept_rows = {"e-ok": [{"record": {"status": "SUCCEEDED"}}]}
real_query_rows = s.query_rows
s.query_rows = lambda ends, statement, parameters, run_id, field_name: kept_rows.get(parameters["experiment_id"], [])
try:
    kept, missing = s.read_summaries(
        spec(), None, [{"experiment_id": "e-ok", "status": "SUCCEEDED"}, {"experiment_id": "e-failed", "status": "FAILED"}]
    )
    check("a failed launch without a summary is listed", missing == ["e-failed"] and list(kept) == ["e-ok"], (kept, missing))
    try:
        s.read_summaries(spec(), None, [{"experiment_id": "e-gone", "status": "SUCCEEDED"}])
        check("a succeeded launch without a summary raises", False)
    except s.ScoringError as error:
        check("a succeeded launch without a summary raises", "e-gone" in str(error))
finally:
    s.query_rows = real_query_rows
check(
    "a key=value DSN loses its password",
    s.database_label("host=localhost port=5433 dbname=ode user=ode password=secret") == "localhost:5433/ode",
)

# --- discovery end to end, with the database reads stubbed ---
#
# Nine cases, one session each, every one reached: a proposal nobody has to be
# asked about. It proves the wiring between the three tables, the attribution and
# the manifest, and it proves that a complete set of records produces a manifest
# with no answer in it.

with tempfile.TemporaryDirectory() as directory:
    sessions = []
    launches = []
    for index, (use_case, tier) in enumerate(
        [(u, t) for u in ("load", "nilm", "pv") for t in ("L0", "L1", "L2")], start=1
    ):
        name = f"ode-ablation-{use_case}-{tier.lower()}"
        target = s.TARGET_SERIES[use_case]
        entry = session(f"s-{index}", tier)
        entry["selection"] = {
            "series": [
                {
                    "device_id": f"device-{use_case}",
                    "service_id": target["service_id"],
                    "variable_path": target["column"],
                }
            ]
        }
        sessions.append(entry)
        launches.append(launch(f"s-{index}", f"study/{name}"))

    answered = {
        "FROM ode_chat_sessions": sessions,
        "FROM ode_experiments": launches,
        "FROM ode_experiment_summaries": [
            {"experiment_id": record["experiment_id"], "record": SUMMARIES[record["experiment_id"]]}
            for record in launches
        ],
    }

    def stub(ends, statement, parameters, run_id, field_name):
        for table, rows in answered.items():
            if table in statement:
                return rows
        raise AssertionError(statement)

    real_query_rows = s.query_rows
    s.query_rows = stub
    try:
        proposal = s.discover(s.Endpoints("postgresql://ode:secret@localhost/ode"), "opus")
    finally:
        s.query_rows = real_query_rows

    check("every case is proposed", [row["run_id"] for row in proposal["runs"]] == [
        "load-l0", "load-l1", "load-l2", "nilm-l0", "nilm-l1", "nilm-l2", "pv-l0", "pv-l1", "pv-l2"
    ])
    check("complete records need no answer", proposal["discovered"]["answers"] == [])
    check("the proposal names its series", proposal["series"] == "opus")
    check("the manifest carries no password", "secret" not in json.dumps(proposal))
    check(
        "a reached run carries no end reason",
        {row["ended_on"] for row in proposal["runs"]} == {""},
    )

    written = pathlib.Path(directory) / "runs.json"
    s.write_manifest(str(written), proposal)
    specs, _ = s.read_manifest(str(written))
    check("the proposal is a manifest the scoring half reads", len(specs) == 9)

# --- one run end to end, with the database reads stubbed ---

run_tables = {
    "FROM ode_chat_sessions": [{"id": "sess-1", "exposure_tier": "L0", "model": "claude-opus-5-5", "selection": selection}],
    "FROM ode_chat_messages": [
        {"seq": 2, "role": "assistant", "content": [{"type": "tool_use", "tool_name": "launch_experiment"}]},
        {"seq": 1, "role": "assistant", "content": [{"type": "text", "text": "Shall I launch?"}]},
        {"seq": 0, "role": "user", "origin": "", "content": [{"type": "text", "text": "Forecast the inverter."}]},
        {"seq": 3, "role": "user", "origin": "", "content": [{"type": "text", "text": "The developer approved the launch_experiment call. It ran and returned:\n{}"}]},
    ],
    "FROM ode_confirmations": [
        {"id": "c1", "created_at": "2026-10-08T10:00:00+00:00", "resolved_at": "2026-10-08T10:01:00+00:00", "decision": "approved"},
    ],
    "FROM ode_exchange_aborts": [],
    "FROM ode_tool_calls": [{"outcome": "ok", "calls": 1}, {"outcome": "blocked_by_tier", "calls": 2}],
    "FROM ode_usage": [
        {"model": "claude-opus-5-5", "input_tokens": 3, "output_tokens": 40, "cached_input_tokens": 500, "cache_write_tokens": 6000},
        {"model": "claude-opus-5-5", "input_tokens": 1, "output_tokens": 20, "cached_input_tokens": 700, "cache_write_tokens": 0},
    ],
    "FROM ode_experiments": [
        {
            "experiment_id": "e1",
            "session_id": "sess-1",
            "repository": "SENERGY-Platform/ode-ablation-pv-l0",
            "commit_sha": "abc",
            "status": "SUCCEEDED",
            "submitted_at": "2026-10-08T10:02:00+00:00",
            "data_split": {"training_end": "2026-09-01T00:00:00+00:00", "test_end": "2026-10-01T00:00:00+00:00"},
            "input_topics": [
                {
                    "filterType": "DeviceId",
                    "filterValue": "dev-1",
                    "name": "urn_infai_ses_service_x",
                    "mappings": [{"dest": "value", "source": "root.powerTotal"}],
                }
            ],
        }
    ],
    "FROM ode_experiment_summaries": [{"record": {"status": "SUCCEEDED", "data_split": {"confirmed": "confirmed"}}}],
}


def run_stub(ends, statement, parameters, run_id, field_name):
    # The table a statement reads is its last FROM: the session statement counts
    # messages in a subselect before its own FROM.
    found = [(statement.rindex(table), table) for table in run_tables if table in statement]
    if not found:
        raise AssertionError(statement)
    return run_tables[max(found)[1]]


real_query_rows = s.query_rows
s.query_rows = run_stub
try:
    row, side = s.score_run(spec(), s.Endpoints("postgresql://localhost/ode"), "")
finally:
    s.query_rows = real_query_rows

check("a scored row has every CSV column and no other", set(row) == set(s.CSV_COLUMNS), sorted(set(row) ^ set(s.CSV_COLUMNS)))
check("the run reached its evaluation", row["reached_operator_and_evaluation"] is True and row["ended_on"] == "")
check("the row carries the session's model", row["model"] == "claude-opus-5-5")
check("tokens are every usage column summed over the calls", row["tokens"] == 7264, row["tokens"])
check(
    "the usage is listed per column",
    side["tokens"] == {"input_tokens": 4, "output_tokens": 60, "cached_input_tokens": 1200, "cache_write_tokens": 6000, "model_calls": 2},
    side["tokens"],
)

recorded_usage = run_tables["FROM ode_usage"]
s.query_rows = run_stub
try:
    for label, rows in (
        ("usage on another model raises", [dict(recorded_usage[0], model="claude-haiku-5-5")] + recorded_usage[1:]),
        ("a session without usage raises", []),
    ):
        run_tables["FROM ode_usage"] = rows
        try:
            s.score_run(spec(), s.Endpoints("postgresql://localhost/ode"), "")
            check(label, False)
        except s.ScoringError as error:
            check(label, error.field_name == "usage", str(error))
finally:
    s.query_rows = real_query_rows
    run_tables["FROM ode_usage"] = recorded_usage
check("tool calls come from the messages", row["tool_calls"] == 1 and side["tool_calls"]["by_tool"] == {"launch_experiment": 1})
check("tier refusals come from the audit", row["tool_calls_blocked_by_tier"] == 2)
check("the covariate is listed", row["covariate_count"] == 1 and side["covariates"][0]["filter_value"] == "dev-1")
check("the confirmation is counted", row["confirmations"] == 1)
check(
    "the opening is the one prompt, the approval is none",
    row["developer_prompts"] == 1 and side["developer_prompts"]["by_kind"]["opening problem statement"] == 1,
    (row["developer_prompts"], side["developer_prompts"]),
)

s.query_rows = run_stub
try:
    s.score_run(
        s.RunSpec(run_id="pv-l0", use_case="pv", tier="L0", session_id="sess-1", series="haiku"),
        s.Endpoints("postgresql://localhost/ode"),
        "",
    )
    check("a session on another model than the series' raises", False)
except s.ScoringError as error:
    check("a session on another model than the series' raises", error.field_name == "model")
finally:
    s.query_rows = real_query_rows

# A run that never launched, and one whose only launch failed, still get a row.
completed = (run_tables["FROM ode_experiments"], run_tables["FROM ode_experiment_summaries"])
s.query_rows = run_stub
try:
    run_tables["FROM ode_experiments"] = []
    row, side = s.score_run(spec(), s.Endpoints("postgresql://localhost/ode"), "abandonment")
    # A commit is only seen through a launch, so a run without one stops at
    # "no commit" whether or not it committed.
    check(
        "a run without a launch is scored at its stage",
        side["termination"]["stopped_at"] == "no commit" and row["ended_on"] == "abandonment" and row["covariate_count"] == 0
        and side["sources"]["final_launch"] == "",
        (row, side["sources"]),
    )

    run_tables["FROM ode_experiments"] = [dict(completed[0][0], status="FAILED")]
    run_tables["FROM ode_experiment_summaries"] = []
    row, side = s.score_run(spec(), s.Endpoints("postgresql://localhost/ode"), "launch cap")
    check(
        "a run whose launch failed stops at the evaluation",
        side["termination"]["stopped_at"] == "no completed evaluation" and row["ended_on"] == "launch cap"
        and side["sources"]["launches_without_a_kept_summary"] == ["e1"],
        (row, side["sources"]),
    )
finally:
    s.query_rows = real_query_rows
    run_tables["FROM ode_experiments"], run_tables["FROM ode_experiment_summaries"] = completed

# A NILM run is checked against NILM's window, and its label is not a covariate.
nilm_launch = dict(
    completed[0][0],
    repository="SENERGY-Platform/ode-ablation-nilm-l0",
    data_split={"training_end": "2026-02-01T00:00:00+00:00", "test_end": "2026-03-01T00:00:00+00:00"},
    input_topics=labelled["input_topics"],
)
s.query_rows = run_stub
try:
    run_tables["FROM ode_experiments"] = [nilm_launch]
    row, side = s.score_run(spec("nilm", "nilm-l0"), s.Endpoints("postgresql://localhost/ode"), "")
    check(
        "a NILM run counts the main meter, not the label",
        row["covariate_count"] == 1 and [t["filter_value"] for t in side["training_topics"]] == ["dev-heatpump"],
        (row["covariate_count"], side["training_topics"]),
    )
    check("a NILM run records its own window", side["sources"]["test_window"] == s.TEST_WINDOWS["nilm"])

    try:
        s.score_run(spec(), s.Endpoints("postgresql://localhost/ode"), "")
        check("a PV run on NILM's split raises", False)
    except s.ScoringError as error:
        check("a PV run on NILM's split raises", "data_split.training_end" in str(error))
finally:
    s.query_rows = real_query_rows
    run_tables["FROM ode_experiments"] = completed[0]

# --- the output is byte-identical over two writes ---

with tempfile.TemporaryDirectory() as directory:
    first = pathlib.Path(directory) / "a.csv"
    second = pathlib.Path(directory) / "b.csv"
    made = [
        {column: "" for column in s.CSV_COLUMNS} | {"run_id": "pv-l2"},
        {column: "" for column in s.CSV_COLUMNS} | {"run_id": "pv-l0"},
    ]
    s.write_csv(str(first), made)
    s.write_csv(str(second), list(reversed(made)))
    check("the CSV is sorted and stable", first.read_bytes() == second.read_bytes())
    check("run order is the run id", first.read_text().splitlines()[1].startswith("pv-l0"))

print()
if FAILURES:
    print(f"{len(FAILURES)} failed: {FAILURES}")
    sys.exit(1)
print("all checks passed")
