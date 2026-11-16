"""A solver that stops without a flag must not be reported as a solve."""

import pytest

from backend.output_types import is_placeholder_flag, reported_flag, solver_output_json_schema


@pytest.mark.parametrize("flag", ["NOT_FOUND", "not found", "Not-Found", "", "  ", None, "N/A", "none", "UNKNOWN", "FLAG_NOT_FOUND"])
def test_placeholders_are_not_flags(flag):
    assert is_placeholder_flag(flag)
    assert reported_flag({"type": "flag_found", "flag": flag, "method": "I did not get the flag"}) is None


@pytest.mark.parametrize("flag", ["ESNA{pl4g_1t_1n}", "FCSC{not_found}", "flag{none}", "d41d8cd98f00b204e9800998ecf8427e"])
def test_real_flags_are_reported(flag):
    assert not is_placeholder_flag(flag)
    assert reported_flag({"type": "flag_found", "flag": f" {flag}\n", "method": "cat /flag"}) == flag


def test_other_output_reports_no_flag():
    assert reported_flag({"type": "gave_up", "flag": "ESNA{guess}", "method": "stuck"}) is None
    assert reported_flag(None) is None


def test_schema_has_no_give_up_option_and_stays_strict():
    schema = solver_output_json_schema()
    assert schema["properties"]["type"]["enum"] == ["flag_found"]
    # OpenAI strict structured outputs: every property required, nothing extra
    assert set(schema["required"]) == set(schema["properties"]) and schema["additionalProperties"] is False
