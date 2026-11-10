"""Reset the demo to its starting point.

Deletes everything the demo creates (production agent versions, bake-off candidate agents, eval runs, local
bake-off cache) so that `azd deploy contoso-te-assistant` re-creates the production agent as version 1 from
azure.yaml. Run it through demo/reset.ps1, which also redeploys the agent.

  python demo/reset.py             # delete agents + eval runs
  python demo/reset.py --keep-runs # keep the eval history
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import evalkit  # noqa: E402
from common import DEFAULT_AGENT, ROOT, load_azd_env, project_client  # noqa: E402

warnings.filterwarnings("ignore")
TEMP_PREFIXES = (evalkit.CANDIDATE_PREFIX, "te-probe-")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep-runs", action="store_true", help="keep the eval runs of te-policy-regression")
    args = ap.parse_args()

    load_azd_env()
    project = project_client()
    oa = project.get_openai_client()

    for agent in list(project.agents.list()):
        if agent.name == DEFAULT_AGENT or agent.name.startswith(TEMP_PREFIXES):
            project.agents.delete(agent.name)
            print(f"deleted agent {agent.name}")

    if not args.keep_runs:
        eval_id = evalkit.find_eval_id(oa)
        for run in list(oa.evals.runs.list(eval_id=eval_id, limit=100)):
            if run.status in ("queued", "in_progress"):
                oa.evals.runs.cancel(run.id, eval_id=eval_id)
            oa.evals.runs.delete(run.id, eval_id=eval_id)
            print(f"deleted eval run {run.name}")

    cache = ROOT / "demo" / "out" / "bakeoff-latest.json"
    if cache.exists():
        cache.unlink()
        print(f"deleted {cache.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
