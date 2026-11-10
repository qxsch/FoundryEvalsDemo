"""Create (or reuse) the vector store that backs the agent's file_search tool.

Uploads agent/knowledge/contoso-te-policy.md. Idempotent: an existing, fully indexed
store with the same name is reused. Prints the vector store id; with --set-azd-env it
also stores it as VECTOR_STORE_ID in the current azd environment.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

from common import HANDBOOK, ROOT, VECTOR_STORE_NAME, azd_path, load_azd_env, project_client


def ensure_vector_store(openai) -> str:
    for store in openai.vector_stores.list():
        if store.name == VECTOR_STORE_NAME and store.status == "completed" and store.file_counts.completed >= 1:
            print(f"Reusing vector store {store.id}")
            return store.id

    store = openai.vector_stores.create(name=VECTOR_STORE_NAME)
    print(f"Created vector store {store.id}; uploading {HANDBOOK.name} ...")
    with HANDBOOK.open("rb") as handle:
        uploaded = openai.vector_stores.files.upload_and_poll(vector_store_id=store.id, file=handle)
    if uploaded.status != "completed":
        raise SystemExit(f"Handbook indexing ended with status {uploaded.status}: {uploaded.last_error}")
    return store.id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-azd-env", action="store_true", help="store VECTOR_STORE_ID in the azd environment")
    args = parser.parse_args()

    load_azd_env()
    openai = project_client().get_openai_client()

    # Role assignments can take a few minutes to propagate right after provisioning.
    for attempt in range(1, 13):
        try:
            store_id = ensure_vector_store(openai)
            break
        except Exception as exc:  # noqa: BLE001 - retry on authorization propagation only
            if "403" in str(exc) or "401" in str(exc) or "PermissionDenied" in str(exc):
                print(f"Waiting for role assignment to propagate ({attempt}/12): {str(exc)[:120]}")
                time.sleep(20)
                continue
            raise
    else:
        raise SystemExit("Could not reach the Foundry project: access was not granted in time.")

    print(f"VECTOR_STORE_ID={store_id}")
    if args.set_azd_env:
        subprocess.run([azd_path(), "env", "set", "VECTOR_STORE_ID", store_id], cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
