from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "delegation_bayes.py"
SPEC = importlib.util.spec_from_file_location("delegation_bayes", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bayes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bayes)


def make_task(**overrides):
    raw = {
        "schema": bayes.TASK_SCHEMA,
        "task_family": "implementation",
        "runtime_label": "public-runtime",
        "dimensions": {
            "scope": 3,
            "coupling": 0,
            "ambiguity": 0,
            "consequence": 0,
            "context_load": 0,
            "platform_specificity": 1,
            "repeatability": 4,
            "verification_clarity": 4,
        },
        "stable_contract": True,
        "exclusive_ownership": True,
        "external_side_effects": False,
        "secrets_or_private_data": False,
        "destructive": False,
        "final_judgment": False,
        "independent_workstreams": 2,
        "shared_context_ratio": 0.2,
        "estimated_direct_minutes": 20,
    }
    raw.update(overrides)
    return bayes.validate_task(raw)


def make_outcome(**overrides):
    raw = {
        "route": "luna_max",
        "acceptance_met": True,
        "independently_verified": True,
        "output_contract_complete": True,
        "safety_violation": False,
        "scope_drift": False,
        "primary_attempt": True,
        "quality_5": 4.5,
        "elapsed_minutes": 10,
        "coordination_minutes": 2,
        "rework_minutes": 0,
        "actual_cost_units": 1,
        "budget_cost_units": 1,
        "tags": ["focused", "verified"],
    }
    raw.update(overrides)
    return bayes.validate_outcome(raw)


def test_hard_blocker_keeps_main_agent_as_owner():
    task = make_task(external_side_effects=True)

    result = bayes.recommend(task, [])

    assert result["route"] == "direct"
    assert result["workers"] == 0
    assert "hard_gate:external_side_effects" in result["reasons"]


def test_low_risk_sparse_bucket_allows_bounded_exploration():
    result = bayes.recommend(make_task(), [])

    assert result["route"] == "luna_max"
    assert result["workers"] == 1
    assert result["exploration"] is True
    assert result["reasons"] == ["bounded_low_risk_exploration"]


def test_posterior_respects_runtime_and_policy_boundaries():
    task = make_task()
    primary = bayes.build_observation(task, make_outcome())
    other_runtime = {**primary, "runtime_label": "another-runtime"}
    old_policy = {**primary, "policy_version": "older-policy"}

    posterior = bayes.posterior(
        "luna_max",
        [primary, other_runtime, old_policy],
        task,
        bayes.score_task(task)["bucket"],
    )

    assert posterior["alpha"] == 3.0
    assert posterior["beta"] == 2.0
    assert posterior["effective_n"] == 1.0
    assert posterior["exact_n"] == 1


def test_report_keeps_runtime_results_separate():
    task = make_task()
    primary = bayes.build_observation(task, make_outcome())
    other_task = make_task(runtime_label="another-runtime")
    other = bayes.build_observation(other_task, make_outcome())

    report = bayes.global_report([primary, other])

    assert report["routes"] is None
    assert sorted(report["runtimes"]) == ["another-runtime", "public-runtime"]
    assert report["runtimes"]["another-runtime"]["routes"]["luna_max"]["update_eligible_count"] == 1


def test_rescue_attempt_is_visible_but_does_not_update_posterior():
    task = make_task()
    primary = bayes.build_observation(task, make_outcome())
    rescue = bayes.build_observation(
        task,
        make_outcome(
            acceptance_met=False,
            independently_verified=False,
            output_contract_complete=False,
            quality_5=2,
            primary_attempt=False,
        ),
    )

    assert rescue["primary_attempt"] is False
    assert rescue["update_eligible"] is False
    posterior = bayes.posterior(
        "luna_max",
        [primary, rescue],
        task,
        bayes.score_task(task)["bucket"],
    )
    assert posterior["effective_n"] == 1.0
    assert posterior["alpha"] == 3.0
    assert posterior["beta"] == 2.0


def test_direct_route_requires_zero_coordination_minutes():
    valid = make_outcome(route="direct", coordination_minutes=0)
    observation = bayes.build_observation(make_task(), valid)

    assert observation["route"] == "direct"
    assert observation["coordination_minutes"] == 0.0

    with pytest.raises(bayes.ContractError, match="coordination_minutes must be 0"):
        bayes.validate_outcome(
            make_outcome(route="direct", coordination_minutes=1)
        )


def test_store_override_and_default_are_portable(monkeypatch, tmp_path):
    monkeypatch.delenv("BATON_DELEGATION_STORE", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert "hermes" not in str(bayes.default_store()).lower()

    override = tmp_path / "reviewed" / "observations.jsonl"
    monkeypatch.setenv("BATON_DELEGATION_STORE", str(override))
    assert bayes.resolve_store(None) == override

    monkeypatch.delenv("BATON_DELEGATION_STORE", raising=False)
    xdg_root = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_root))
    assert bayes.default_store() == xdg_root / "baton-fanout-skill" / "delegation-observations.jsonl"


def test_dimension_values_are_observable_and_bounded():
    task = make_task()
    assert set(task["dimensions"]) == set(bayes.DIMENSIONS)
    assert all(0 <= value <= 4 for value in task["dimensions"].values())

    invalid = make_task()
    invalid["dimensions"] = {**invalid["dimensions"], "scope": 5}
    with pytest.raises(bayes.ContractError, match="dimensions.scope"):
        bayes.validate_task(invalid)


def test_cli_score_recommend_record_report_round_trip(tmp_path):
    task_path = tmp_path / "task.json"
    outcome_path = tmp_path / "outcome.json"
    store_path = tmp_path / "state" / "observations.jsonl"
    task_path.write_text(
        json.dumps(
            {
                "schema": bayes.TASK_SCHEMA,
                "task_family": "implementation",
                "runtime_label": "public-runtime",
                "dimensions": {
                    "scope": 1,
                    "coupling": 0,
                    "ambiguity": 0,
                    "consequence": 0,
                    "context_load": 0,
                    "platform_specificity": 0,
                    "repeatability": 4,
                    "verification_clarity": 4,
                },
                "stable_contract": True,
                "exclusive_ownership": True,
                "external_side_effects": False,
                "secrets_or_private_data": False,
                "destructive": False,
                "final_judgment": False,
                "independent_workstreams": 1,
                "shared_context_ratio": 0,
                "estimated_direct_minutes": 20,
            }
        ),
        encoding="utf-8",
    )
    outcome_path.write_text(
        json.dumps(
            {
                "route": "luna_max",
                "acceptance_met": True,
                "independently_verified": True,
                "output_contract_complete": True,
                "safety_violation": False,
                "scope_drift": False,
                "primary_attempt": True,
                "quality_5": 4.5,
                "elapsed_minutes": 8,
                "coordination_minutes": 2,
                "rework_minutes": 0,
                "actual_cost_units": 1,
                "budget_cost_units": 1,
                "tags": ["cli", "verified"],
            }
        ),
        encoding="utf-8",
    )
    environment = {**os.environ, "BATON_DELEGATION_STORE": str(store_path)}

    def run(*arguments):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        return json.loads(completed.stdout)

    assert run("score", "--task", str(task_path))["complexity"] >= 0
    assert run("recommend", "--task", str(task_path))["route"] == "luna_max"
    assert run(
        "record",
        "--task",
        str(task_path),
        "--outcome",
        str(outcome_path),
    )["qualified_success"] is True
    report = run("report")
    assert report["observations"] == 1
    assert report["runtimes"]["public-runtime"]["routes"]["luna_max"][
        "qualified_successes"
    ] == 1
