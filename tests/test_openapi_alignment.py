"""The served routes must cover the frozen contract (openapi.yaml v1.0).

A6 hook: this is the Foundation-level contract conformance check; full
schema-level contract tests belong to QA's suite.
"""

import pathlib

import yaml
from fastapi.testclient import TestClient

from app.main import API_PREFIX, create_app

CONTRACT = pathlib.Path(__file__).resolve().parents[1] / "openapi.yaml"


def test_every_contract_route_is_served():
    spec = yaml.safe_load(CONTRACT.read_text())
    app = create_app()
    with TestClient(app) as client:
        served = client.get("/openapi.json").json()["paths"]

    served_normalized = {
        path.removeprefix(API_PREFIX): {m for m in ops if m in
                                        ("get", "post", "put", "delete", "patch")}
        for path, ops in served.items()
    }

    missing = []
    for path, ops in spec["paths"].items():
        # Contract {ticker}/{date} param names must match exactly.
        norm = path.replace("{date}", "{rec_date}")
        for method in ops:
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            if norm not in served_normalized or method not in served_normalized[norm]:
                missing.append(f"{method.upper()} {path}")
    assert not missing, f"contract routes not served: {missing}"
