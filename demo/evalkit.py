"""Helpers to evaluate pinned agent versions (including shadow candidates) under the regression eval."""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

# The OpenAI SDK has no types for Foundry's agent data sources; its serializer warnings are noise.
warnings.filterwarnings("ignore", message="Pydantic serializer warnings")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from common import DEFAULT_AGENT, HANDBOOK, read_text  # noqa: E402

EVAL_NAME = "te-policy-regression"
DATASET_NAME = "golden"
# Same threshold the CLI gate uses: azd ai eval run start --fail-on pass-rate=<GATE_PASS_RATE>
GATE_PASS_RATE = float(os.environ.get("EVAL_GATE_PASS_RATE", "0.9"))
CANDIDATE_PREFIX = "te-candidate-"
TERMINAL = {"completed", "failed", "canceled", "cancelled"}
# Sampling settings a model needed to pass the gate in the model bake-off; applied when an agent switches to it.
MODEL_SETTINGS = {"DeepSeek-V4-Flash": {"temperature": 0.0}}


def switch_model(definition: dict, model: str) -> dict:
    """Point an agent definition at another model. Sampling settings are model-specific, so they are reset."""
    if definition.get("model") != model:
        for key in ("temperature", "top_p"):
            definition.pop(key, None)
        definition.update(MODEL_SETTINGS.get(model, {}))
    definition["model"] = model
    return definition


def shadow_name(key: str) -> str:
    """Agent names allow only alphanumerics and hyphens (max 63)."""
    return (CANDIDATE_PREFIX + re.sub(r"[^A-Za-z0-9]+", "-", key)).strip("-")[:63]


def find_eval_id(oa, name: str = EVAL_NAME) -> str:
    for ev in oa.evals.list(limit=100):
        if ev.name == name:
            return ev.id
    raise SystemExit(f"Eval '{name}' not found. Run: azd ai eval create {name}")


def dataset_ref(project) -> tuple[str, str]:
    """(id, version) of the newest version of the golden dataset, the one `azd ai eval run start` uses."""
    versions = list(project.datasets.list_versions(name=DATASET_NAME))
    if not versions:
        raise SystemExit(f"Dataset '{DATASET_NAME}' not found. Run: azd ai eval create {EVAL_NAME}")
    latest = max(versions, key=lambda d: tuple(int(p) for p in re.findall(r"\d+", d.version)))
    return latest.id, latest.version


def production(project, agent: str = DEFAULT_AGENT) -> tuple[str, dict]:
    """(version, definition) of the agent's latest version."""
    latest = project.agents.get(agent).versions["latest"]
    return str(latest["version"]), dict(latest["definition"])


def inline_grounding(instructions: str) -> str:
    """Adapt instructions for models that should get the handbook in context instead of calling file_search."""
    text = re.sub(r"\s*\(file_search\)", "", instructions).rstrip()
    return (
        f"{text}\n\nGrounding\n"
        "- The complete T&E handbook is included below between <handbook> tags. It is your only source: "
        "answer from it and cite its section numbers. You have no search tool.\n"
        "- Every amount, tier, limit, approval and form you mention must be written in the handbook. Look each city, "
        "tier and amount up in the handbook before you write it; never rely on memory. Never add requirements, "
        "reassurances or inferences that the handbook does not state (for example that no receipt or no approval is "
        "needed, or that nothing else is covered).\n"
        "- Lists in the handbook are exhaustive: never put a city or item into a list that does not name it; "
        "use the catch-all rule instead (for example 'every other location').\n"
        "- If the handbook allows something subject to conditions or an approval, say it is allowed under those "
        "conditions and list all of them. You explain the policy; never phrase an answer as if you granted approval.\n"
        "- Even for a plain yes, state the full rule of the section you cite, and only what it says. Completeness "
        "beats the word limit: never drop a number, condition, deadline or route just to stay short.\n"
        "- Form TE-22 is only for exceptions. When the handbook prescribes a regular route for the situation (an "
        "approval, a booking channel or another form), give that route instead of TE-22. Never say both that no "
        "exception is possible and how to request one.\n"
        "- When you point to an exception route, include who grants it and the timing rules the handbook sets for it.\n"
        "- Your reply goes straight to the employee: never include your reasoning, notes about tools or files, "
        "or tags such as </think>.\n\n"
        f"<handbook>\n{read_text(HANDBOOK)}\n</handbook>\n"
    )


def _norm(defn: dict) -> str:
    keep = {k: defn.get(k) for k in ("kind", "model", "instructions", "tools", "tool_choice", "temperature", "reasoning")}
    return json.dumps(keep, sort_keys=True, default=str)


def ensure_agent_version(project, name: str, definition: dict, description: str, metadata: dict[str, str]) -> str:
    """Return the latest version of `name` if it already has this definition, otherwise create a new version."""
    from azure.ai.projects.models import PromptAgentDefinition

    definition = {"kind": "prompt", **definition}
    try:
        latest = project.agents.get(name).versions["latest"]
        if _norm(dict(latest["definition"])) == _norm(definition):
            return str(latest["version"])
    except Exception:  # noqa: BLE001 - agent does not exist yet
        pass
    body = {k: v for k, v in definition.items() if k != "kind" and v is not None}
    created = project.agents.create_version(
        name, definition=PromptAgentDefinition(**body), description=description, metadata=metadata
    )
    return str(created.version)


def start_run(oa, eval_id: str, *, run_name: str, agent: str, version: str, dataset: tuple[str, str],
              metadata: dict[str, str], concurrency: int = 4) -> str:
    dataset_id, dataset_version = dataset
    data_source = {
        "type": "azure_ai_target_completions",
        "input_messages": {
            "type": "template",
            "template": [{"role": "user", "content": "{{item.query}}", "type": "message"}],
        },
        "source": {"type": "file_id", "id": dataset_id},
        "target": {"type": "azure_ai_agent", "name": agent, "version": version, "tool_descriptions": []},
        "item_generation_params": {"type": "target_completions", "max_concurrency": concurrency},
    }
    meta = {
        "azd_eval": EVAL_NAME,
        "azd_dataset": DATASET_NAME,
        "azd_dataset_version": dataset_version,
        "evaluation_level": "turn",
        "agent_name": agent,
        "agent_version": version,
        **metadata,
    }
    run = oa.evals.runs.create(
        eval_id, name=run_name, data_source=data_source, metadata=meta, extra_body={"evaluation_level": "turn"}
    )
    return run.id


def find_run(oa, eval_id: str, not_before: int = 0, **match: str):
    """Most recent completed or still running run whose metadata contains all key/values in `match`.

    Runs created before `not_before` (epoch seconds, e.g. the agent version's creation time) are ignored, so a run
    of an earlier agent that had the same name and version number (before a reset) is never reused.
    """
    for run in oa.evals.runs.list(eval_id, limit=100, order="desc"):
        meta = run.metadata or {}
        if (run.status in ("completed", "queued", "in_progress") and (run.created_at or 0) >= not_before
                and all(meta.get(k) == v for k, v in match.items())):
            return run
    return None


def wait(oa, eval_id: str, run_ids: list[str], on_poll=None, poll_s: int = 15) -> dict:
    runs: dict = {}
    while True:
        for rid in run_ids:
            if rid not in runs or runs[rid].status not in TERMINAL:
                runs[rid] = oa.evals.runs.retrieve(rid, eval_id=eval_id)
        if on_poll:
            on_poll(runs)
        if all(r.status in TERMINAL for r in runs.values()):
            return runs
        time.sleep(poll_s)


@dataclass
class RunSummary:
    run_id: str
    name: str
    status: str
    total: int = 0
    passed: int = 0
    criteria: dict[str, dict] = field(default_factory=dict)  # name -> {passed, total, mean}
    clean_replies: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    p50_ms: float | None = None
    p95_ms: float | None = None
    report_url: str | None = None
    failures: list[dict] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def summarize(oa, eval_id: str, run) -> RunSummary:
    raw = run.model_dump()
    counts = raw.get("result_counts") or {}
    s = RunSummary(run.id, run.name or "", run.status, counts.get("total", 0), counts.get("passed", 0),
                   report_url=raw.get("report_url"))
    for usage in raw.get("per_model_usage") or []:
        if usage.get("model_name") == "azure_ai_agent_target":
            s.prompt_tokens = usage.get("prompt_tokens", 0)
            s.cached_tokens = usage.get("cached_tokens", 0)
            s.completion_tokens = usage.get("completion_tokens", 0)
    latency = (raw.get("latency") or {}).get("target") or {}
    s.p50_ms, s.p95_ms = latency.get("p50_ms"), latency.get("p95_ms")

    scores: dict[str, list[float]] = {}
    passes: dict[str, int] = {}
    for item in oa.evals.runs.output_items.list(run.id, eval_id=eval_id, limit=100):
        d = item.model_dump()
        failed = []
        for res in d.get("results") or []:
            name = res.get("name")
            if isinstance(res.get("score"), (int, float)):
                scores.setdefault(name, []).append(float(res["score"]))
            passes[name] = passes.get(name, 0) + (1 if res.get("passed") else 0)
            if not res.get("passed"):
                failed.append(name)
            for dim in (res.get("properties") or {}).get("dimension_scores") or []:
                if dim.get("id") == "clean_reply" and (dim.get("score") or 0) >= 4:
                    s.clean_replies += 1
        if failed:
            src = d.get("datasource_item") or {}
            s.failures.append({"query": src.get("query"), "category": src.get("category"), "failed": failed})
    for name, vals in scores.items():
        s.criteria[name] = {"passed": passes.get(name, 0), "total": len(vals), "mean": statistics.fmean(vals)}
    return s
