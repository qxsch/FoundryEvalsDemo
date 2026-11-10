"""Model bake-off: evaluate cheaper models for the T&E assistant and show what a switch takes.

Every candidate is a *shadow agent* (te-candidate-<key>): a copy of the production agent with only the model
(and, if needed, the grounding) changed. Production is never touched. All candidates are evaluated by the same
Foundry eval (te-policy-regression), so the runs sit side by side in the portal.

  python demo/model_bakeoff.py                                   # current vs gpt-5.4-nano vs DeepSeek-V4-Flash (drop-in + adapted)
  python demo/model_bakeoff.py --candidates deepseek-v4-flash-inline
  python demo/model_bakeoff.py --promote deepseek-v4-flash-inline  # make it the production version
  python demo/model_bakeoff.py --cleanup                         # delete the shadow agents
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import evalkit  # noqa: E402
import pricing  # noqa: E402
from common import DEFAULT_AGENT, ROOT, load_azd_env, project_client  # noqa: E402

OUT = ROOT / "demo" / "out"


@dataclass(frozen=True)
class Candidate:
    key: str
    model: str | None  # None = production agent as-is
    grounding: str = "file_search"  # or "inline": handbook in the instructions, no tool
    temperature: float | None = None
    note: str = ""


CANDIDATES = {
    "current": Candidate("current", None, note="production agent, unchanged"),
    "gpt-5.4-nano": Candidate("gpt-5.4-nano", "gpt-5.4-nano", note="same family, smaller"),
    "deepseek-v4-flash": Candidate("deepseek-v4-flash", "DeepSeek-V4-Flash", note="open weights, EU data zone, drop-in swap"),
    "deepseek-v4-flash-inline": Candidate(
        "deepseek-v4-flash-inline", "DeepSeek-V4-Flash", "inline",
        temperature=evalkit.MODEL_SETTINGS["DeepSeek-V4-Flash"]["temperature"],
        note="adapted: handbook in context, no file_search, temperature 0"
    ),
}
DEFAULT_SET = ["current", "gpt-5.4-nano", "deepseek-v4-flash", "deepseek-v4-flash-inline"]


def candidate_definition(c: Candidate, prod_def: dict) -> dict:
    d = {k: prod_def.get(k) for k in ("model", "instructions", "tools", "temperature", "top_p", "tool_choice")}
    if c.model and c.model != d["model"]:
        d["model"] = c.model
        d["temperature"] = d["top_p"] = None  # sampling settings are model-specific
    if c.temperature is not None:
        d["temperature"] = c.temperature
    if c.grounding == "inline" and "<handbook>" not in (d["instructions"] or ""):
        d["instructions"] = evalkit.inline_grounding(d["instructions"])
        d["tools"] = []
        d["tool_choice"] = None
    return d


def resolve_targets(project, keys: list[str]) -> list[dict]:
    prod_version, prod_def = evalkit.production(project)
    targets = []
    for key in keys:
        c = CANDIDATES[key]
        if c.model is None:
            agent, version, model = DEFAULT_AGENT, prod_version, prod_def["model"]
        else:
            agent = evalkit.shadow_name(key)
            definition = candidate_definition(c, prod_def)
            version = evalkit.ensure_agent_version(
                project, agent, definition,
                description=f"Shadow candidate for {DEFAULT_AGENT}: {c.note}",
                metadata={"base_agent": DEFAULT_AGENT, "base_version": prod_version, "candidate": key},
            )
            model = definition["model"]
        created_at = int(project.agents.get(agent).versions["latest"].get("created_at") or 0)
        targets.append({"key": key, "agent": agent, "version": version, "model": model, "note": c.note,
                        "grounding": c.grounding, "base_version": prod_version, "created_at": created_at})
    return targets


def run_bakeoff(args) -> None:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table

    console = Console()
    keys = [k.strip() for k in args.candidates.split(",") if k.strip()]
    unknown = [k for k in keys if k not in CANDIDATES]
    if unknown:
        raise SystemExit(f"Unknown candidate(s) {unknown}. Choose from: {', '.join(CANDIDATES)}")

    load_azd_env()
    project = project_client()
    oa = project.get_openai_client()
    eval_id = evalkit.find_eval_id(oa)
    dataset = evalkit.dataset_ref(project)

    with console.status("Preparing shadow candidates..."):
        targets = resolve_targets(project, keys)
    for t in targets:
        console.print(f"  [bold]{t['key']:<26}[/] {t['agent']}:{t['version']}  model={t['model']}  ({t['note']})")

    run_ids: dict[str, str] = {}
    stamp = time.strftime("%H%M")
    for t in targets:
        # Production runs started from the console count too; candidates only reuse bake-off runs.
        match = {"agent_name": t["agent"], "agent_version": t["version"], "azd_dataset_version": dataset[1]}
        if t["key"] != "current":
            match["bakeoff_candidate"] = t["key"]
        existing = None if args.fresh else evalkit.find_run(oa, eval_id, not_before=t["created_at"], **match)
        if existing:
            run_ids[t["key"]] = existing.id
            console.print(f"  reusing run [cyan]{existing.name}[/] for {t['key']} (use --fresh to re-run)")
        else:
            run_ids[t["key"]] = evalkit.start_run(
                oa, eval_id, run_name=f"bakeoff-{t['key']}-v{t['version']}-{stamp}", agent=t["agent"],
                version=t["version"], dataset=dataset, metadata={"bakeoff_candidate": t["key"]},
                concurrency=args.concurrency)

    started = time.time()

    def progress(runs) -> Table:
        tbl = Table(title=f"Foundry eval runs ({int(time.time() - started)}s)", show_header=True)
        for col in ("candidate", "status", "rows done"):
            tbl.add_column(col)
        for t in targets:
            r = runs.get(run_ids[t["key"]])
            rc = (r.model_dump().get("result_counts") or {}) if r else {}
            done = sum((rc.get(k) or 0) for k in ("passed", "failed", "errored"))
            tbl.add_row(t["key"], r.status if r else "queued", f"{done}/{rc.get('total') or '?'}")
        return tbl

    with Live(progress({}), console=console, refresh_per_second=1) as live:
        runs = evalkit.wait(oa, eval_id, list(run_ids.values()), on_poll=lambda rs: live.update(progress(rs)))

    with console.status("Collecting results and live prices..."):
        summaries = {t["key"]: evalkit.summarize(oa, eval_id, runs[run_ids[t["key"]]]) for t in targets}
        prices = pricing.get_prices(sorted({t["model"] for t in targets}), args.region)
        deployments = {d.name for d in project.deployments.list()}

    rows = build_rows(targets, summaries, prices, args.questions_per_month)
    render_scorecard(console, rows, args.questions_per_month)
    pick = choose(rows, args.target)
    render_checklist(console, pick, rows, deployments)

    OUT.mkdir(parents=True, exist_ok=True)
    report = {"eval_id": eval_id, "gate_pass_rate": evalkit.GATE_PASS_RATE, "rows": rows,
              "summaries": {k: asdict(v) for k, v in summaries.items()}}
    (OUT / "bakeoff-latest.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    console.print(f"[dim]Saved {OUT / 'bakeoff-latest.json'}. Compare the runs in the portal: "
                  f"{(summaries[targets[0]['key']].report_url or '').split('/run/')[0]}[/]")


def build_rows(targets, summaries, prices, per_month: int) -> list[dict]:
    rows = []
    for t in targets:
        s = summaries[t["key"]]
        p = prices.get(t["model"])
        n = s.total or 1
        cost_q = pricing.cost_usd(p, s.prompt_tokens, s.cached_tokens, s.completion_tokens) / n if p else None
        crit = s.criteria
        rows.append({
            **t,
            "run_id": s.run_id, "status": s.status, "total": s.total, "passed": s.passed, "pass_rate": s.pass_rate,
            "groundedness": crit.get("groundedness", {}).get("mean"),
            "completeness": crit.get("response_completeness", {}).get("mean"),
            "rubric": crit.get("te-policy-rubric", {}).get("mean"),
            "clean_replies": s.clean_replies,
            "p50_ms": s.p50_ms, "p95_ms": s.p95_ms,
            "in_per_q": s.prompt_tokens / n, "out_per_q": s.completion_tokens / n,
            "price": asdict(p) if p else None,
            "cost_per_1k": cost_q * 1000 if cost_q is not None else None,
            "cost_per_month": cost_q * per_month if cost_q is not None else None,
            "gate": s.pass_rate >= evalkit.GATE_PASS_RATE and s.status == "completed",
            "failures": s.failures,
        })
    base = next((r for r in rows if r["key"] == "current"), None)
    for r in rows:
        if base and base["cost_per_1k"] and r["cost_per_1k"]:
            r["cost_vs_current"] = r["cost_per_1k"] / base["cost_per_1k"]
            base_value = base["pass_rate"] / base["cost_per_1k"]
            r["value_vs_current"] = (r["pass_rate"] / r["cost_per_1k"]) / base_value if base_value else None
    return rows


def _f(v, fmt, dash="-"):
    return dash if v is None else format(v, fmt)


def render_scorecard(console, rows, per_month: int) -> None:
    from rich.table import Table

    tbl = Table(title=f"Model bake-off: quality vs cost (gate: pass rate >= {evalkit.GATE_PASS_RATE:.0%})",
                show_lines=True)
    for col, justify in [("Candidate", "left"), ("Model / grounding", "left"), ("Pass rate", "right"),
                         ("Grounded\n(1-5)", "right"), ("Complete\n(1-5)", "right"), ("Policy\nrubric", "right"),
                         ("Clean\nreplies", "right"), ("Latency\np50", "right"), ("Tokens/q\nin / out", "right"),
                         ("$ per 1K\nquestions", "right"), (f"$ / month\n@{per_month:,} q", "right"),
                         ("Cost vs\ncurrent", "right"), ("Quality\nper $", "right"), ("Gate", "center")]:
        tbl.add_column(col, justify=justify)
    for r in rows:
        gate = "[bold green]PASS[/]" if r["gate"] else "[bold red]FAIL[/]"
        rate_color = "green" if r["gate"] else "red"
        clean_color = "green" if r["clean_replies"] == r["total"] else "red"
        cost_vs = r.get("cost_vs_current")
        value = r.get("value_vs_current")
        tbl.add_row(
            r["key"], f"{r['model']}\n[dim]{r['grounding']}[/]",
            f"[{rate_color}]{r['pass_rate']:.0%}[/] ({r['passed']}/{r['total']})",
            _f(r["groundedness"], ".2f"), _f(r["completeness"], ".2f"), _f(r["rubric"], ".2f"),
            f"[{clean_color}]{r['clean_replies']}/{r['total']}[/]",
            _f(r["p50_ms"] and r["p50_ms"] / 1000, ".1f") + "s",
            f"{r['in_per_q']:,.0f} / {r['out_per_q']:,.0f}",
            _f(r["cost_per_1k"], ",.2f"), _f(r["cost_per_month"], ",.0f"),
            "-" if cost_vs is None else f"{cost_vs:.0%}",
            "-" if value is None else f"{value:.1f}x",
            gate,
        )
    console.print(tbl)
    srcs = {f"{r['model']}: {r['price']['sku']}, {r['price']['source']}" for r in rows if r["price"]}
    console.print("[dim]Prices (USD per 1M tokens) from the Azure Retail Prices API: " + "; ".join(sorted(srcs)) + "[/]")


def choose(rows, target: str | None) -> dict:
    if target:
        return next(r for r in rows if r["key"] == target)
    others = [r for r in rows if r["key"] != "current"]
    passing = sorted((r for r in others if r["gate"]), key=lambda r: r["cost_per_1k"] or 1e9)
    if passing:
        return passing[0]
    return sorted(others or rows, key=lambda r: r["cost_per_1k"] or 1e9)[0]


def render_checklist(console, r: dict, rows: list[dict], deployments: set[str]) -> None:
    from rich.panel import Panel

    ok, no, info = "[green]✔[/]", "[red]✖[/]", "[cyan]•[/]"
    base = next((x for x in rows if x["key"] == "current"), None)
    lines = []
    lines.append(f"{ok if r['model'] in deployments else no} Model deployment [bold]{r['model']}[/] exists in the "
                 "Foundry project" + ("" if r["model"] in deployments else
                 " → add it to infra/main.parameters.json (and azure.yaml), then `azd provision`"))
    if r["price"]:
        lines.append(f"{info} SKU {r['price']['sku']}: input ${r['price']['input']:.3f} · cached "
                     f"${r['price']['cached']:.3f} · output ${r['price']['output']:.2f} per 1M tokens")
    leaks = r["total"] - r["clean_replies"]
    if leaks:
        lines.append(f"{no} Agent runtime compatibility: {leaks}/{r['total']} replies leak internal text "
                     "(reasoning, tool prompts) → adapt the agent for this model, e.g. candidate "
                     "[bold]deepseek-v4-flash-inline[/] (handbook in context, no file_search)")
    else:
        lines.append(f"{ok} Agent runtime compatibility: all replies clean"
                     + (" (handbook in context; trade-off: handbook updates now need an agent redeploy)"
                        if r["grounding"] == "inline" else ""))
    lines.append(f"{ok if r['gate'] else no} Quality gate: pass rate {r['pass_rate']:.0%} vs required "
                 f"{evalkit.GATE_PASS_RATE:.0%}" + (f" (current: {base['pass_rate']:.0%})" if base else ""))
    if base and r.get("cost_per_month") is not None and base.get("cost_per_month"):
        saving = base["cost_per_month"] - r["cost_per_month"]
        lines.append(f"{info} Cost: ${r['cost_per_month']:,.0f} vs ${base['cost_per_month']:,.0f} per month "
                     f"→ saves ${saving:,.0f}/month ({1 - r['cost_per_1k'] / base['cost_per_1k']:.0%})")
    if r["key"] != "current":
        lines.append(f"{info} Promote: `python demo/model_bakeoff.py --promote {r['key']}` creates a new production "
                     "version from exactly the definition that was evaluated (the console shows who/what changed it)")
        lines.append(f"{info} Make it permanent in config-as-code: azure.yaml still describes the old agent, so "
                     "`azd deploy contoso-te-assistant` would roll back; update model"
                     + (", instructions (handbook in context) and drop file_search" if r["grounding"] == "inline"
                        else "") + " there")
    lines.append(f"{info} Re-gate production: `azd ai eval run start --eval {evalkit.EVAL_NAME} "
                 f"--fail-on pass-rate={evalkit.GATE_PASS_RATE}`")
    lines.append(f"{info} Watch tokens & latency in App Insights; rollback = promote the previous version again")
    verdict = ("[bold green]Ready to switch[/]" if r["gate"] and not leaks
               else "[bold red]Not ready to switch[/]")
    console.print(Panel("\n".join(lines), title=f"Switch checklist: {r['key']} → {verdict}", expand=False))


def promote(key: str) -> None:
    from azure.ai.projects.models import PromptAgentDefinition
    from rich.console import Console

    console = Console()
    load_azd_env()
    project = project_client()
    name = evalkit.shadow_name(key)
    latest = project.agents.get(name).versions["latest"]
    d = dict(latest["definition"])
    body = {k: d.get(k) for k in ("model", "instructions", "tools", "temperature", "top_p", "tool_choice")
            if d.get(k) is not None}
    v = project.agents.create_version(
        DEFAULT_AGENT, definition=PromptAgentDefinition(**body),
        description=f"Promoted from model bake-off candidate {name}:{latest['version']}",
        metadata={"updated_by": "model-bakeoff", "promoted_from": f"{name}:{latest['version']}",
                  "change_note": f"Model switch to {body['model']} (promoted from bake-off candidate {key})"},
    )
    console.print(f"[green]Promoted[/] {name}:{latest['version']} → [bold]{DEFAULT_AGENT}:{v.version}[/] "
                  f"(model {body['model']})")
    console.print(f"Next: azd ai eval run start --eval {evalkit.EVAL_NAME} --name after-switch "
                  f"--fail-on pass-rate={evalkit.GATE_PASS_RATE}")


def cleanup() -> None:
    load_azd_env()
    project = project_client()
    for agent in project.agents.list():
        if agent.name.startswith(evalkit.CANDIDATE_PREFIX):
            project.agents.delete(agent.name)
            print(f"deleted shadow agent {agent.name}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidates", default=",".join(DEFAULT_SET),
                    help=f"comma-separated, from: {', '.join(CANDIDATES)}")
    ap.add_argument("--fresh", action="store_true", help="always start new eval runs (default: reuse matching runs)")
    ap.add_argument("--questions-per-month", type=int, default=20_000)
    ap.add_argument("--concurrency", type=int, default=4, help="parallel agent calls per eval run")
    ap.add_argument("--region", default="swedencentral", help="region for list prices")
    ap.add_argument("--target", help="candidate to build the switch checklist for (default: cheapest that passes)")
    ap.add_argument("--promote", metavar="CANDIDATE", help="make the evaluated candidate the production version")
    ap.add_argument("--cleanup", action="store_true", help="delete all shadow candidate agents")
    args = ap.parse_args()
    if args.cleanup:
        cleanup()
    elif args.promote:
        promote(args.promote)
    else:
        run_bakeoff(args)


if __name__ == "__main__":
    main()
