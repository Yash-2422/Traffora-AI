"""The example payloads in docs/traffic_mind/examples must stay valid against the schemas.

They are real outputs of the agent (surge scenario, t = 1800 s). If a schema changes and an
example no longer validates, regenerate the examples and update INPUT_OUTPUT.md.
"""

import json

import pytest

from backend.agents.traffic_mind.config import REPO_ROOT
from backend.agents.traffic_mind.schemas import (
    CongestionReport,
    PredictionReport,
    SignalRecommendationReport,
    TrafficSnapshot,
)

EXAMPLES = REPO_ROOT / "docs" / "traffic_mind" / "examples"
CASES = [
    ("traffic_status.json", TrafficSnapshot),
    ("congestion.json", CongestionReport),
    ("prediction.json", PredictionReport),
    ("signal_recommendation.json", SignalRecommendationReport),
]


@pytest.mark.parametrize("filename,model", CASES)
def test_example_is_valid_and_round_trips(filename, model):
    path = EXAMPLES / filename
    assert path.is_file(), f"missing example payload: {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    parsed = model.model_validate(data)                       # extra fields or bad values would fail here
    assert parsed.status == "ok"
    assert parsed.to_dict() == data                            # nothing is lost or renamed on the way back out


def test_examples_describe_the_same_moment():
    times = {json.loads((EXAMPLES / name).read_text(encoding="utf-8"))["timestamp_s"] for name, _ in CASES}
    assert len(times) == 1