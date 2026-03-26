import pytest
from qt.tool_call_checker import check_and_fix

def test_valid_json_returns_dict():
    raw = '{"name": "read_file", "parameters": {"path": "foo.py"}}'
    result, err = check_and_fix(raw)
    assert result == {"name": "read_file", "parameters": {"path": "foo.py"}}
    assert err is None

def test_trailing_comma_fixed():
    raw = '{"name": "run", "parameters": {"cmd": "ls",}}'
    result, err = check_and_fix(raw)
    assert result is not None
    assert err is None

def test_unclosed_brace_fixed():
    raw = '{"name": "run", "parameters": {"cmd": "ls"}'
    result, err = check_and_fix(raw)
    assert result is not None
    assert err is None

def test_tool_key_renamed_to_name():
    raw = '{"tool": "read_file", "parameters": {}}'
    result, err = check_and_fix(raw)
    assert result is not None
    assert "name" in result
    assert "tool" not in result
    assert err is None

def test_input_key_renamed_to_parameters():
    raw = '{"name": "read_file", "input": {"path": "x"}}'
    result, err = check_and_fix(raw)
    assert result is not None
    assert "parameters" in result
    assert "input" not in result
    assert err is None

def test_single_quotes_fixed():
    raw = "{'name': 'run', 'parameters': {}}"
    result, err = check_and_fix(raw)
    assert result is not None
    assert err is None

def test_truncated_string_fixed():
    raw = '{"name": "run", "parameters": {"cmd": "ls'
    result, err = check_and_fix(raw)
    assert result is not None
    assert err is None

def test_unrecoverable_returns_error():
    raw = "not json at all @@##"
    result, err = check_and_fix(raw)
    assert result is None
    assert err is not None
    assert len(err) > 0
