"""Validate an immutable release pair before emitting Compose image variables."""

import argparse
import hashlib
import json
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import BaseModel, ConfigDict, Field

IMAGE = r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64}$"


class Release(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frontend_image: str = Field(pattern=IMAGE)
    backend_image: str = Field(pattern=IMAGE)
    postgres_image: str = Field(pattern=IMAGE)
    frontend_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    backend_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    migration: str


def validate_pair(release, backend_contract, frontend_contract, migration):
    hashes = {hashlib.sha256(value).hexdigest() for value in (backend_contract, frontend_contract)}
    if hashes != {release.contract_sha256}:
        raise ValueError("Frontend/backend API contract mismatch")
    if release.migration != migration:
        raise ValueError("Release migration mismatch")
    return (
        "\n".join(
            f"{name.upper()}={getattr(release, name)}"
            for name in ("frontend_image", "backend_image", "postgres_image")
        )
        + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="JSON-subset YAML release manifest")
    parser.add_argument("--frontend-contract", required=True)
    parser.add_argument(
        "--output-env", required=True, help="New Compose image env file (no secrets)"
    )
    args = parser.parse_args()
    try:
        release = Release.model_validate_json(Path(args.manifest).read_bytes())
        variables = validate_pair(
            release,
            Path("contracts/openapi.json").read_bytes(),
            Path(args.frontend_contract).read_bytes(),
            ScriptDirectory.from_config(Config("alembic.ini")).get_current_head(),
        )
        with Path(args.output_env).open("x") as output:
            output.write(variables)
        print(json.dumps({"status": "ok"}))
        return 0
    except Exception:
        print(
            json.dumps({"status": "failed", "check": "manifest, contract, migration, output path"})
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
