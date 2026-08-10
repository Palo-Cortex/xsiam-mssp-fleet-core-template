"""Unit tests for ExampleScript.

Run with the demisto-sdk test harness (which provides demistomock and
CommonServerPython), e.g. `demisto-sdk pre-commit -i Packs/ExamplePack`.
"""

import pytest

from ExampleScript import example_script_command, format_message


def test_format_message_plain():
    assert format_message("hello fleet", uppercase=False) == {"Message": "hello fleet"}


def test_format_message_uppercase():
    assert format_message("hello fleet", uppercase=True) == {"Message": "HELLO FLEET"}


def test_command_returns_outputs():
    result = example_script_command({"message": "hello fleet"})

    assert result.outputs_prefix == "ExampleScript"
    assert result.outputs == {"Message": "hello fleet"}
    assert "hello fleet" in result.readable_output


def test_command_uppercase_arg():
    result = example_script_command({"message": "hello fleet", "uppercase": "true"})

    assert result.outputs == {"Message": "HELLO FLEET"}


def test_command_missing_message_raises():
    with pytest.raises(ValueError, match="message is required"):
        example_script_command({})
