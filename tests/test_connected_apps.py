"""Home Page gets its own revocable, non-mutating service credential."""
from home_services.auth import PRESETS
from home_services.cli import main


def test_home_page_preset_reads_readiness_but_cannot_read_files_or_create_jobs(env):
    token, info = env.s.tokens.create("Home Page fixture", PRESETS["home-page"])
    headers = {"Authorization": f"Bearer {token}", "Home-Services-Contract": "1"}
    client = env.client()
    assert client.get("/api/v1/readiness", headers=headers).status_code == 200
    assert client.get("/api/v1/storage/volumes", headers=headers).status_code == 403
    assert client.get("/api/v1/jobs", headers=headers).status_code == 403
    assert client.post("/api/v1/jobs", headers={**headers, "Idempotency-Key": "connection-fixture"},
                       json={"kind": "cleanup", "target": {}}).status_code == 403
    env.s.tokens.revoke(info["id"])
    assert client.get("/api/v1/readiness", headers=headers).status_code == 401


def test_cli_lists_the_same_presets_as_the_permission_registry(capsys):
    import pytest
    with pytest.raises(SystemExit) as exit:
        main(["tokens", "create", "--help"])
    assert exit.value.code == 0
    help_text = capsys.readouterr().out
    for preset in PRESETS:
        assert preset in help_text
