"""Execution guards for the row-fenced production canary helper."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compose_prod.sh"
VIDEO_ID = "12345678-1234-4234-8234-123456789abc"
TOKEN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONTAINER_ID = "a" * 64


def _run(tmp_path: Path, mode: str = "success") -> tuple[subprocess.CompletedProcess[str], str]:
    log = tmp_path / "calls"
    docker = tmp_path / "docker"
    docker.write_text(
        f"""#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "{log}"
mode={mode}
if [[ $* == *"container inspect"* ]]; then
  name="${{@: -1}}"
  if [[ $* == *"{{{{.Id}}}}"* && $* != *"{{{{.Id}}}}|"* ]]; then printf '%s' "$name" > "{tmp_path}/name"; printf '{CONTAINER_ID}\\n'
  elif [[ $mode == replacement ]]; then printf '%s|/wrong|wrong\\n' {'b' * 64}
  else name=$(<"{tmp_path}/name"); token="${{name: -36}}"; printf '{CONTAINER_ID}|/%s|%s\\n' "$name" "$token"; fi
elif [[ $* == *"container wait"* ]]; then
  [[ $mode == wait_timeout ]] && exit 124
  [[ $mode == worker_failed ]] && printf '17\\n' || printf '0\\n'
elif [[ $* == *"container stop"* && $mode == cleanup_failed ]]; then exit 1
elif [[ $* == *"container ls"* ]]; then :
fi
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    code = f"""
source {SCRIPT}
run_preflight() {{ :; }}
check_diarization_role() {{ printf 'role-check\\n' >> "$CANARY_LOG"; }}
compose() {{
  printf 'compose %s\\n' "$*" >> "$CANARY_LOG"
  if [[ $* == *"psql"*"-f -"* ]]; then
    sql=$(cat)
    printf 'sql %s\\n' "$sql" >> "$CANARY_LOG"
    [[ $sql == *"WITH fenced"* ]] && printf 'fenced\\n'
    [[ $sql == *"WITH finalized"* ]] && {{ [[ "{mode}" == finalization_mismatch ]] && printf '0\\n' || printf '1\\n'; }}
  fi
  return 0
}}
canary_token={TOKEN}
run_diarization_canary {VIDEO_ID}
"""
    result = subprocess.run(
        ["bash", "-c", code],
        cwd=ROOT,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "HOME": os.environ.get("HOME", ""),
            "CANARY_LOG": str(log),
            "TRANSCRIPT_DEPLOY_ROOT": str(tmp_path),
        },
        text=True,
        capture_output=True,
        timeout=15,
    )
    return result, log.read_text(encoding="utf-8") if log.exists() else ""


def test_canary_success_uses_row_fence_wait_exit_cleanup_then_finalize(tmp_path: Path) -> None:
    result, calls = _run(tmp_path)
    assert result.returncode == 0, f"{result.stderr}\n{calls}"
    assert "role-check" in calls
    assert "-U hasanara_diarization" in calls
    assert "canary-lease:" in calls
    assert "container wait" in calls and "container rm --force" in calls
    assert calls.index("container rm --force") < calls.index("WITH finalized")
    assert "hasanara.canary-token=" in calls


@pytest.mark.parametrize(
    "mode", ["worker_failed", "wait_timeout", "cleanup_failed", "finalization_mismatch", "replacement"]
)
def test_canary_failure_paths_never_report_success(tmp_path: Path, mode: str) -> None:
    result, calls = _run(tmp_path, mode)
    assert result.returncode != 0
    if mode != "finalization_mismatch":
        assert "WITH fenced" in calls


def test_source_has_exact_token_recovery_and_no_legacy_lock_protocol() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "diarization-canary-recover" in source
    assert "canary-finalizing:' || :'token'" in source
    assert "canary-failed:' || :'token'" in source
    assert "pg_advisory_xact_lock" in source
    assert "pg_try_advisory_lock" not in source
    assert "lock_application_name" not in source
    assert "docker container wait" in source
    assert "[[ $worker_status == 0 ]]" in source
    assert "label=hasanara.canary-token=$canary_token" in source
    assert "SET speaker_label=NULL" in source
    assert (
        "canary-finalizing:' || :'token"
        in source.split("fence_canary_failure", 1)[1].split("finalize_canary_success", 1)[0]
    )


def test_sourceable_helper_does_not_capture_locals_in_traps() -> None:
    result = subprocess.run(
        ["bash", "-c", f"source {SCRIPT}; declare -F run_diarization_canary recover_diarization_canary"],
        cwd=ROOT,
        env={**os.environ, "TRANSCRIPT_DEPLOY_ROOT": str(ROOT)},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    source = SCRIPT.read_text(encoding="utf-8")
    assert "trap on_canary_exit EXIT" in source
    assert "trap 'on_canary_signal 130' INT" in source


def test_backlog_run_checks_the_role_and_passes_bounded_settings(tmp_path: Path) -> None:
    log = tmp_path / "calls"
    code = f"""
source {SCRIPT}
run_preflight() {{ printf 'preflight\\n' >> "{log}"; }}
check_diarization_role() {{ printf 'role-check\\n' >> "{log}"; }}
compose() {{ printf 'compose %s\\n' "$*" >> "{log}"; }}
run_diarization_backlog 7
"""
    result = subprocess.run(
        ["bash", "-c", code],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", ""), "TRANSCRIPT_DEPLOY_ROOT": str(tmp_path)},
        text=True,
        capture_output=True,
        timeout=15,
    )
    calls = log.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert calls.index("preflight") < calls.index("role-check") < calls.index("compose")
    run = calls.splitlines()[-1]
    for expected in (
        "--profile diarization run --rm --no-deps",
        "DIARIZATION_BACKLOG_MODE=true",
        "DIARIZATION_BACKLOG_BATCH_SIZE=7",
        "DIARIZATION_MAX_JOBS_PER_PROCESS=7",
        "DIARIZATION_ALLOWED_VIDEO_IDS= ",
        "DIARIZATION_CANARY_MODE=false",
        "DIARIZATION_CHUNK_SECONDS=600",
        "DIARIZATION_REFETCH_AUDIO=true",
        "DIARIZATION_STRICT=true",
        "DIARIZATION_DEVICE=cpu",
    ):
        assert expected in run
    assert run.endswith("diarization-worker")


@pytest.mark.parametrize(
    "arguments",
    [
        ("diarization-backlog", "0", "--approved"),
        ("diarization-backlog", "21", "--approved"),
        ("diarization-backlog", "5"),
        ("diarization-backlog", "5", "approved"),
        ("diarization-backlog", "05", "--approved"),
    ],
)
def test_backlog_maintenance_rejects_unbounded_or_unapproved_requests(tmp_path: Path, arguments) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "maintenance", *arguments],
        cwd=ROOT,
        env={**os.environ, "TRANSCRIPT_DEPLOY_ROOT": str(tmp_path)},
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 64
    assert "diarization-backlog requires a batch size 1..20 and --approved" in result.stderr
