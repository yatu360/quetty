from queue_load_test.harness.phase7_patchright_identity import run_backend_identity_workflow
from queue_load_test.models import BrowserBackendName


async def test_patchright_production_shaped_identity_workflow() -> None:
    result = await run_backend_identity_workflow(
        BrowserBackendName.PATCHRIGHT,
        cycles=3,
        include_failure_matrix=True,
    )

    assert result["fresh_context_creation"] == "PASS"
    assert result["identity_acquisition"] == "PASS"
    assert result["explicit_transfer_restore"] == "PASS"
    assert result["explicit_storage_state_restore"] == "PASS"
    assert result["hybrid_transfer_first_fallback"] == "PASS"
    assert result["process_restart"] == "PASS"
    assert result["application_repository_restart"] == "PASS"
    assert result["persisted_identity_changes"] == 0
    assert result["replacement_identities_created"] == 0
    assert result["context_cleanup"]["failures"] == 0  # type: ignore[index]
    assert result["resource_cleanup"] == {
        "active_contexts_after_shutdown": 0,
        "managed_processes_after_shutdown": 0,
        "new_browser_processes_after_shutdown": 0,
    }
