"""Shared helpers for the demo scripts: azd environment, credential, Foundry clients."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTRUCTIONS_DIR = ROOT / "agent" / "instructions"
HANDBOOK = ROOT / "agent" / "knowledge" / "contoso-te-policy.md"

# Instruction variants used in the demo story.
VARIANTS = {
    "v1": INSTRUCTIONS_DIR / "v1-baseline.md",
    "v2": INSTRUCTIONS_DIR / "v2-friendlier-shorter.md",
    "v3": INSTRUCTIONS_DIR / "v3-friendly-and-safe.md",
}

DEFAULT_AGENT = "contoso-te-assistant"
DEFAULT_MODEL = "gpt-5.4-mini"
VECTOR_STORE_NAME = "contoso-te-handbook"


def azd_path() -> str | None:
    local = ROOT / ".tools" / "azd" / ("azd.exe" if os.name == "nt" else "azd")
    return str(local) if local.exists() else shutil.which("azd")


def load_azd_env() -> None:
    """Copy the azd environment into os.environ unless we already run inside azd / the container."""
    if os.environ.get("FOUNDRY_PROJECT_ENDPOINT"):
        return
    azd = azd_path()
    if not azd:
        raise SystemExit("FOUNDRY_PROJECT_ENDPOINT is not set and azd was not found.")
    out = subprocess.run(
        [azd, "env", "get-values", "--output", "json"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    for key, value in json.loads(out).items():
        os.environ.setdefault(key, str(value))


def credential():
    from azure.identity import AzureDeveloperCliCredential, DefaultAzureCredential

    if os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT"):
        return DefaultAzureCredential()  # managed identity in Azure (AZURE_CLIENT_ID selects the UAMI)
    # Locally, use the identity the presenter signed in with via `azd auth login`.
    return AzureDeveloperCliCredential(tenant_id=os.environ.get("AZURE_TENANT_ID") or None)


def project_client():
    from azure.ai.projects import AIProjectClient

    return AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=credential())


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def agent_definition(instructions: str, model: str, vector_store_id: str | None, temperature: float | None = None):
    from azure.ai.projects.models import FileSearchTool, PromptAgentDefinition

    tools = [FileSearchTool(vector_store_ids=[vector_store_id])] if vector_store_id else []
    kwargs = {"model": model, "instructions": instructions, "tools": tools}
    if temperature is not None:
        kwargs["temperature"] = temperature
    return PromptAgentDefinition(**kwargs)


def current_vector_store_id(project=None) -> str | None:
    """VECTOR_STORE_ID from the environment, otherwise the vector store with the well-known name."""
    if os.environ.get("VECTOR_STORE_ID"):
        return os.environ["VECTOR_STORE_ID"]
    project = project or project_client()
    openai = project.get_openai_client()
    for store in openai.vector_stores.list():
        if store.name == VECTOR_STORE_NAME:
            return store.id
    return None
