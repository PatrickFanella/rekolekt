"""Contracts for selective application release artifact selection."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".gitea" / "workflows" / "backend-role-release.yaml"


def test_selective_release_accepts_only_supported_application_roles():
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "- api" in source
    assert "- ingest-cuda" in source
    assert '[[ "$ROLE" == api || "$ROLE" == ingest-cuda || "$ROLE" == clips ]]' in source
    assert "Dockerfile.api" in source
    assert "Dockerfile.ingest.cuda" in source
    assert "- clips" in source
    assert "Dockerfile.clips" in source
    assert "frontend/Dockerfile" not in source
    assert "- frontend" not in source
    assert "hasanara-frontend" not in source
    assert "Dockerfile.cuda" not in source
    assert "Dockerfile.postgres-walg" not in source


def test_selective_release_revalidates_every_unchanged_first_party_digest():
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "base_manifest_json" in source
    assert "reused-images.txt" in source
    assert 'cosign verify --key cosign.pub "$image"' in source
    assert 'cosign verify-attestation --key cosign.pub --type slsaprovenance "$image"' in source
    assert 'cosign verify-attestation --key cosign.pub --type spdxjson "$image"' in source
    assert 'manifest["images"][os.environ["ROLE"]]' in source
    assert '"reused_artifacts_reverified": True' in source


def test_selected_digest_is_scanned_signed_attested_and_uploaded():
    source = WORKFLOW.read_text(encoding="utf-8")

    workflow = yaml.safe_load(source)
    assert workflow["jobs"]["backend-image"]["runs-on"] == "switchyard-production"
    assert "--severity CRITICAL,HIGH --pkg-types library --exit-code 1" in source
    assert 'docker push "$IMAGE:$TAG"' not in source
    assert "quay.io/skopeo/stable@sha256:47853bb9fb24202af9110531ebd6e43c5f97701254ca290596640290d17942f4" in source
    assert 'destination="docker://${IMAGE}:${TAG}"' in source
    assert "destination_tls_verify=true" in source
    assert "if docker container inspect gitea >/dev/null 2>&1; then" in source
    assert 'docker network create --internal "$network"' in source
    assert 'docker network connect --alias registry-origin "$network" gitea' in source
    assert 'destination="docker://registry-origin:3000/${repository}:${TAG}"' in source
    assert '"docker-daemon:${IMAGE}:${TAG}" "$DESTINATION"' in source
    assert '--dest-tls-verify="$DESTINATION_TLS_VERIFY"' in source
    assert '--dest-registry-token "$REGISTRY_BEARER_TOKEN"' in source
    assert '[[ "$canonical_digest" == "$pushed_digest" ]]' in source
    assert "trap cleanup EXIT" in source
    assert '"builder": {"id":' in source
    assert 'cosign sign --yes --key env://COSIGN_PRIVATE_KEY "$IMAGE_REF"' in source
    assert "--type slsaprovenance --predicate selected.provenance.json" in source
    assert "--type spdxjson --predicate selected.spdx.json" in source
    assert "release-images.json" in source


def test_selective_release_drops_a_legacy_frontend_role_from_the_base_manifest():
    source = WORKFLOW.read_text(encoding="utf-8")

    assert 'if role not in {os.environ["ROLE"], "redis", "frontend"}:' in source
    assert 'manifest["images"].pop("frontend", None)' in source
    assert 'manifest["services"].pop("frontend", None)' in source


def test_clip_worker_release_has_a_separate_manifest_without_changing_core_roles(tmp_path, monkeypatch):
    import json
    import subprocess
    import sys
    import textwrap

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        item
        for item in workflow["jobs"]["backend-image"]["steps"]
        if item["name"] == "Compose selective immutable manifest"
    )
    script = textwrap.dedent(step["run"].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0])
    commit = "a" * 40
    digest = "sha256:" + "b" * 64
    image = "git.subcult.tv/subculture-collective/hasanara-clips"
    monkeypatch.setenv("ROLE", "clips")
    monkeypatch.setenv("SOURCE_COMMIT", commit)
    monkeypatch.setenv("IMAGE", image)
    monkeypatch.setenv("DIGEST", digest)
    subprocess.run([sys.executable, "-c", script], cwd=tmp_path, check=True)
    assert json.loads((tmp_path / "clip-worker-image.json").read_text()) == {
        "schema_version": 1,
        "source_commit": commit,
        "image": f"{image}@{digest}",
        "role": "clips",
        "service": "clip-worker",
    }
    assert not (tmp_path / "release-images.json").exists()
