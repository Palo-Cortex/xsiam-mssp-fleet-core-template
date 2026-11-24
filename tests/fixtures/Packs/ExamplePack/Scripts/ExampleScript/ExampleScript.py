"""ExampleScript — minimal exemplar of an MSSP-authored fleet script.

Echoes a message back as structured context output. It exists so the
template's release → pin → converge pipeline has a real pack to exercise end
to end; replace this script (and its pack) with your own MSSP-authored
content when you adopt the template.
"""

from typing import Any, Dict

import demistomock as demisto
from CommonServerPython import *
from CommonServerUserPython import *


def format_message(message: str, uppercase: bool) -> Dict[str, str]:
    """Build the structured output for the given message.

    Args:
        message: Text to echo back.
        uppercase: When True, the echoed text is upper-cased.

    Returns:
        Dict shaped like {"Message": <text>}.
    """
    return {"Message": message.upper() if uppercase else message}


def example_script_command(args: Dict[str, Any]) -> CommandResults:
    """Parse XSOAR args, run the formatter, and build the command result.

    Args:
        args: The ``demisto.args()`` mapping. Supported keys are message
            (required) and uppercase (optional boolean, default false).

    Returns:
        CommandResults with the echoed message under the ExampleScript prefix.

    Raises:
        ValueError: If the required message argument is missing.
    """
    message = args.get("message")
    if not message:
        raise ValueError("message is required.")

    uppercase = argToBoolean(args.get("uppercase", "false"))
    result = format_message(str(message), uppercase)

    return CommandResults(
        outputs_prefix="ExampleScript",
        outputs_key_field="Message",
        outputs=result,
        readable_output=tableToMarkdown("ExampleScript", result),
        raw_response=result,
    )


def main():
    """Script entry point. Dispatches to the command function with error wrapping."""
    try:
        return_results(example_script_command(demisto.args()))
    except Exception as ex:
        return_error(f"Failed to execute ExampleScript. Error: {str(ex)}")


if __name__ in ("__main__", "__builtin__", "builtins"):  # pragma: no cover
    main()
