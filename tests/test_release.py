import hashlib

import pytest
from pydantic import ValidationError

from app.release import Release, validate_pair


def test_release_rejects_mutable_images_and_contract_drift():
    contract = b'{"version":"test"}'
    data = {
        "frontend_image": "example/web@sha256:" + "a" * 64,
        "backend_image": "example/server@sha256:" + "b" * 64,
        "postgres_image": "postgres@sha256:" + "c" * 64,
        "frontend_commit": "d" * 40,
        "backend_commit": "e" * 40,
        "contract_sha256": hashlib.sha256(contract).hexdigest(),
        "migration": "test",
    }
    release = Release(**data)
    assert "BACKEND_IMAGE=" in validate_pair(release, contract, contract, "test")
    with pytest.raises(ValueError, match="contract"):
        validate_pair(release, contract, b"other", "test")
    with pytest.raises(ValueError, match="migration"):
        validate_pair(release, contract, contract, "old")
    with pytest.raises(ValidationError):
        Release(**{**data, "backend_image": "example/server:latest"})
