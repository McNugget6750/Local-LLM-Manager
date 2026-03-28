import re
import pytest
from qt.highlighter import RULES

def _matches(lang: str, pattern_index: int, text: str) -> bool:
    pattern, _ = RULES[lang][pattern_index]
    return bool(re.search(pattern, text))

def test_python_keyword():
    assert _matches("python", 0, "def foo():")
    assert _matches("python", 0, "return x")
    assert _matches("python", 0, "import os")

def test_python_string_double():
    assert _matches("python", 1, '"hello world"')

def test_python_string_single():
    assert _matches("python", 1, "'hello'")

def test_python_comment():
    assert _matches("python", 2, "# this is a comment")

def test_python_number():
    assert _matches("python", 3, "x = 42")
    assert _matches("python", 3, "y = 3.14")

def test_python_decorator():
    assert _matches("python", 4, "@property")

def test_bash_keyword():
    assert _matches("bash", 0, "if [ -f foo ]; then")
    assert _matches("bash", 0, "echo hello")

def test_bash_variable():
    assert _matches("bash", 2, "$HOME")
    assert _matches("bash", 2, "${MY_VAR}")

def test_json_key():
    assert _matches("json", 0, '"name": "value"')

def test_json_number():
    assert _matches("json", 2, '"count": 42')

def test_detect_language_py(tmp_path):
    from qt.highlighter import detect_language
    assert detect_language(str(tmp_path / "foo.py")) == "python"

def test_detect_language_sh(tmp_path):
    from qt.highlighter import detect_language
    assert detect_language(str(tmp_path / "script.sh")) == "bash"

def test_detect_language_bat(tmp_path):
    from qt.highlighter import detect_language
    assert detect_language(str(tmp_path / "run.bat")) == "bash"

def test_detect_language_json(tmp_path):
    from qt.highlighter import detect_language
    assert detect_language(str(tmp_path / "data.json")) == "json"

def test_detect_language_unknown(tmp_path):
    from qt.highlighter import detect_language
    assert detect_language(str(tmp_path / "readme.txt")) == "plain"
