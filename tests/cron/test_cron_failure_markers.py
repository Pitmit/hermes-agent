import pytest

from cron.scheduler import _final_response_from_result


class _Agent:
    @staticmethod
    def _format_turn_completion_explanation(*_args):
        return ""


@pytest.mark.parametrize("text", [
    "NO-GO: source unavailable",
    "NO GO: source unavailable",
    "FEHLGESCHLAGEN: report missing",
    "FAIL: verifier failed",
    "FAILED: verifier failed",
    '{"status":"NO-GO","report_saved":false}',
    '{"status":"ERROR"}',
])
def test_explicit_task_failure_marker_fails_cron_run(text):
    with pytest.raises(RuntimeError, match="agent reported task failure"):
        _final_response_from_result(
            {"final_response": text, "completed": True, "failed": False},
            "job", "name", _Agent,
        )


@pytest.mark.parametrize("text", ["[SILENT]", "All checks passed", "No goals are due."])
def test_non_failure_responses_unchanged(text):
    assert _final_response_from_result(
        {"final_response": text, "completed": True, "failed": False},
        "job", "name", _Agent,
    ) == text
