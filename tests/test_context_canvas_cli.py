import json
import subprocess
import sys
from pathlib import Path


def run_cli(tmp_path, *args):
    script = Path(__file__).resolve().parents[1] / "packages" / "context-canvas" / "context_canvas" / "cli.py"
    cmd = [sys.executable, str(script), "--root", str(tmp_path), *args]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=True)
    return json.loads(proc.stdout)


def test_cli_start_ref_node_read_closeout(tmp_path):
    started = run_cli(tmp_path, "start", "--session-id", "cli-s1", "--goal", "CLI smoke")
    assert started["ok"] is True

    ref = run_cli(tmp_path, "add-ref", "cli-s1", "--content", "raw evidence", "--label", "smoke")
    assert ref["ref"] == "refs/tc_001.md"

    node = run_cli(
        tmp_path,
        "upsert-node",
        "cli-s1",
        "--kind",
        "verification",
        "--status",
        "done",
        "--summary",
        "CLI can maintain evidence-backed canvas",
        "--ref",
        ref["ref"],
    )
    assert node["node"]["id"] == "N001"

    read = run_cli(tmp_path, "read", "cli-s1")
    assert read["canvas"]["nodes"][0]["refs"] == ["refs/tc_001.md"]

    closeout = run_cli(tmp_path, "closeout", "cli-s1")
    assert "MemPalace-ready" in closeout["closeout"]
