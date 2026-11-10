"""Contoso T&E Assistant console: edit the Foundry agent, try it out and evaluate every version.

Runs behind Container Apps built-in authentication (Entra ID), which forwards the signed-in user in the
X-MS-CLIENT-PRINCIPAL* headers. Talks to Foundry with the app's managed identity (AZURE_CLIENT_ID).

Local run (uses your `azd auth login` identity and the azd environment):
    uvicorn main:app --app-dir console/app --port 8000
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "demo")]

import evalkit  # noqa: E402
from common import DEFAULT_AGENT, VARIANTS, current_vector_store_id, load_azd_env, project_client, read_text  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
AGENT = os.environ.get("AGENT_NAME", DEFAULT_AGENT)
EASY_AUTH = os.environ.get("AUTH_MODE", "local") == "easyauth"
PRESETS = {
    "v1": "Baseline: professional, cites the handbook",
    "v2": "Friendlier & shorter",
    "v3": "Friendly and safe",
}
GROUNDING_MARKER = "\n\nGrounding\n- The complete T&E handbook is included below"

log = logging.getLogger("console")
log.setLevel(logging.INFO)
if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(logger_name="console")

load_azd_env()  # local development; a no-op in Azure where FOUNDRY_PROJECT_ENDPOINT is set

app = FastAPI(title="Contoso T&E Assistant console")
_lock = threading.Lock()
_clients: dict = {}
_run_details: dict[str, dict] = {}


def clients():
    with _lock:
        if not _clients:
            project = project_client()
            oa = project.get_openai_client()
            _clients.update(project=project, oa=oa, eval_id=evalkit.find_eval_id(oa))
        return _clients["project"], _clients["oa"], _clients["eval_id"]


# ---- identity ---------------------------------------------------------------------------------

def current_user(request: Request) -> dict:
    """Signed-in user from Container Apps authentication (Entra ID)."""
    upn = request.headers.get("x-ms-client-principal-name")
    if not upn:
        if EASY_AUTH:
            raise HTTPException(401, "Not signed in")
        return {"upn": os.environ.get("USERNAME", "presenter") + " (local)", "name": "Local presenter"}
    name = upn
    try:
        principal = json.loads(base64.b64decode(request.headers.get("x-ms-client-principal", "")))
        name = next((c["val"] for c in principal.get("claims", []) if c.get("typ") == "name"), upn)
    except Exception:  # noqa: BLE001 - display name is cosmetic
        pass
    return {"upn": upn, "name": name}


# ---- agent versions ---------------------------------------------------------------------------

def split_instructions(instructions: str) -> tuple[str, str]:
    """(editable instructions, knowledge mode). Inline-grounded versions carry the handbook in the prompt."""
    if GROUNDING_MARKER in instructions:
        return instructions.split(GROUNDING_MARKER, 1)[0].rstrip(), "inline"
    return instructions, "file_search"


def version_view(v) -> dict:
    d = v.as_dict() if hasattr(v, "as_dict") else dict(v)
    definition = d.get("definition") or {}
    meta = d.get("metadata") or {}
    instructions, knowledge = split_instructions(definition.get("instructions") or "")
    if not any(t.get("type") == "file_search" for t in definition.get("tools") or []) and knowledge == "file_search":
        knowledge = "none"
    return {
        "version": str(d.get("version")),
        "created_at": d.get("created_at"),
        "model": definition.get("model"),
        "instructions": instructions,
        "knowledge": knowledge,
        "updated_by": meta.get("updated_by") or ("azd deploy" if not meta else ""),
        "note": meta.get("change_note") or meta.get("note") or "",
        "source": meta.get("source") or "",
    }


def agent_versions(project) -> list:
    return list(project.agents.list_versions(AGENT, limit=50, order="desc"))


@app.get("/api/me")
def me(request: Request):
    return current_user(request)


@app.get("/api/config")
def config(request: Request):
    project, _, eval_id = clients()
    models = [
        {"name": d.name, "model": d.as_dict().get("modelName"), "publisher": d.as_dict().get("modelPublisher")}
        for d in project.deployments.list()
        if d.name != os.environ.get("JUDGE_MODEL", "gpt-5.4")
    ]
    presets = [{"key": k, "title": t, "instructions": read_text(VARIANTS[k])} for k, t in PRESETS.items()]
    return {
        "agent": AGENT,
        "user": current_user(request),
        "gate": evalkit.GATE_PASS_RATE,
        "eval_name": evalkit.EVAL_NAME,
        "eval_id": eval_id,
        "models": models,
        "presets": presets,
    }


@app.get("/api/agent")
def agent():
    project, _, _ = clients()
    return {"name": AGENT, "versions": [version_view(v) for v in agent_versions(project)]}


class NewVersion(BaseModel):
    instructions: str = Field(min_length=20, max_length=20000)
    model: str
    knowledge: str = "file_search"
    note: str = Field(default="", max_length=300)


@app.post("/api/agent/versions")
def save_version(body: NewVersion, request: Request):
    from azure.ai.projects.models import FileSearchTool, PromptAgentDefinition

    user = current_user(request)
    project, _, _ = clients()
    latest = agent_versions(project)[0].as_dict()
    definition = dict(latest.get("definition") or {})
    definition.pop("kind", None)

    instructions = body.instructions.strip()
    if body.knowledge == "inline":
        definition["instructions"] = evalkit.inline_grounding(instructions)
        definition["tools"] = []
    else:
        tools = [t for t in definition.get("tools") or [] if t.get("type") == "file_search"]
        if not tools:
            store = current_vector_store_id(project)
            tools = [FileSearchTool(vector_store_ids=[store]).as_dict()] if store else []
        definition["instructions"] = instructions
        definition["tools"] = tools
    evalkit.switch_model(definition, body.model)

    metadata = {"updated_by": user["upn"][:256], "source": "console"}
    if body.note.strip():
        metadata["change_note"] = body.note.strip()
    created = project.agents.create_version(
        AGENT, definition=PromptAgentDefinition(**definition), description=latest.get("description"), metadata=metadata
    )
    log.info("agent_version_saved", extra={
        "agent_name": AGENT, "agent_version": str(created.version), "model": body.model,
        "knowledge": body.knowledge, "updated_by": user["upn"], "change_note": body.note,
    })
    return version_view(created)


# ---- chat -------------------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    version: str | None = None


@app.post("/api/chat")
def chat(body: ChatRequest, request: Request):
    user = current_user(request)
    project, oa, _ = clients()
    version = body.version or str(agent_versions(project)[0].version)
    started = time.perf_counter()
    try:
        response = oa.responses.create(
            input=body.message,
            extra_body={"agent_reference": {"name": AGENT, "version": version, "type": "agent_reference"}},
        )
    except Exception as exc:  # noqa: BLE001 - surface model/agent errors in the UI
        raise HTTPException(502, f"Agent call failed: {exc}") from exc
    elapsed = (time.perf_counter() - started) * 1000
    usage = response.usage.model_dump() if response.usage else {}
    log.info("agent_chat", extra={"agent_name": AGENT, "agent_version": version, "user": user["upn"],
                                  "latency_ms": round(elapsed)})
    return {
        "text": response.output_text,
        "version": version,
        "latency_ms": round(elapsed),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


# ---- evaluations ------------------------------------------------------------------------------

class EvalRequest(BaseModel):
    version: str | None = None


@app.post("/api/evals/runs")
def start_eval(body: EvalRequest, request: Request):
    user = current_user(request)
    project, oa, eval_id = clients()
    version = body.version or str(agent_versions(project)[0].version)
    stamp = datetime.now(timezone.utc).strftime("%H%M")
    run_id = evalkit.start_run(
        oa, eval_id, run_name=f"console-v{version}-{stamp}", agent=AGENT, version=version,
        dataset=evalkit.dataset_ref(project),
        metadata={"source": "console", "triggered_by": user["upn"][:256]}, concurrency=4,
    )
    log.info("eval_run_started", extra={"agent_name": AGENT, "agent_version": version, "run_id": run_id,
                                        "triggered_by": user["upn"]})
    return {"run_id": run_id, "version": version}


def infer_version(created_at: int, versions: list[dict]) -> str | None:
    """Runs started with `azd ai eval run start` target the latest version at that time."""
    for v in versions:  # newest first
        if (v["created_at"] or 0) <= created_at:
            return v["version"]
    return None


@app.get("/api/evals/runs")
def list_runs(limit: int = 20):
    project, oa, eval_id = clients()
    versions = [version_view(v) for v in agent_versions(project)]
    runs = []
    for r in oa.evals.runs.list(eval_id, limit=min(limit, 50), order="desc"):
        raw = r.model_dump()
        meta = r.metadata or {}
        counts = raw.get("result_counts") or {}
        total, passed = counts.get("total") or 0, counts.get("passed") or 0
        agent_name = meta.get("agent_name") or AGENT
        version = meta.get("agent_version") or (infer_version(r.created_at, versions) if agent_name == AGENT else None)
        rate = passed / total if total and r.status == "completed" else None
        runs.append({
            "id": r.id, "name": r.name, "status": r.status, "created_at": r.created_at,
            "agent_name": agent_name, "agent_version": version,
            "source": meta.get("source") or ("bake-off" if meta.get("bakeoff_candidate") else "azd"),
            "triggered_by": meta.get("triggered_by"),
            "passed": passed, "total": total, "errored": counts.get("errored") or 0,
            "pass_rate": rate, "gate": None if rate is None else rate >= evalkit.GATE_PASS_RATE,
            "report_url": raw.get("report_url"),
        })
    return {"gate": evalkit.GATE_PASS_RATE, "runs": runs}


def _text(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)[:2000] if value else ""


@app.get("/api/evals/runs/{run_id}")
def run_detail(run_id: str):
    if run_id in _run_details:
        return _run_details[run_id]
    _, oa, eval_id = clients()
    run = oa.evals.runs.retrieve(run_id, eval_id=eval_id)
    raw = run.model_dump()
    detail = {"id": run.id, "name": run.name, "status": run.status, "result_counts": raw.get("result_counts"),
              "report_url": raw.get("report_url"), "items": [], "criteria": {}}
    if run.status != "completed":
        return detail
    criteria: dict[str, dict] = {}
    for item in oa.evals.runs.output_items.list(run_id, eval_id=eval_id, limit=100):
        d = item.model_dump()
        src = d.get("datasource_item") or {}
        results = []
        for res in d.get("results") or []:
            name = res.get("name")
            c = criteria.setdefault(name, {"passed": 0, "total": 0, "scores": []})
            c["total"] += 1
            c["passed"] += 1 if res.get("passed") else 0
            if isinstance(res.get("score"), (int, float)):
                c["scores"].append(float(res["score"]))
            dims = [
                {"id": x.get("id"), "score": x.get("score"), "reason": x.get("reason")}
                for x in (res.get("properties") or {}).get("dimension_scores") or []
                if x.get("applicable", True) and (x.get("score") or 5) < 4
            ]
            results.append({"name": name, "passed": bool(res.get("passed")), "score": res.get("score"),
                            "reason": _text(res.get("reason") or (res.get("sample") or {}).get("reason")),
                            "dimensions": dims})
        detail["items"].append({
            "query": src.get("query"), "category": src.get("category"), "expected": src.get("ground_truth"),
            "answer": _text(src.get("sample.output_text")), "passed": all(r["passed"] for r in results) and bool(results),
            "results": results,
        })
    detail["items"].sort(key=lambda i: i["passed"])
    detail["criteria"] = {
        k: {"passed": v["passed"], "total": v["total"],
            "mean": round(sum(v["scores"]) / len(v["scores"]), 2) if v["scores"] else None}
        for k, v in criteria.items()
    }
    _run_details[run_id] = detail
    return detail


# ---- static UI --------------------------------------------------------------------------------

@app.get("/healthz")
def healthz(deep: bool = False):
    """Liveness; `?deep=1` also checks that the managed identity can reach the agent (no details exposed)."""
    if not deep:
        return {"ok": True}
    try:
        project, _, _ = clients()
        project.agents.get(AGENT)
        return {"ok": True, "foundry": True}
    except Exception:  # noqa: BLE001
        log.exception("deep health check failed")
        return {"ok": False, "foundry": False}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_cache_api(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response
