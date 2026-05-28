"""Toolkit registry — pre-defined action functions for skill steps.

Each toolkit function maps to a Platform layer capability.
Steps can reference these tools via a natural-language tool string in their `tool` field.

Usage in SKILL.md:
    ### 3. Send the meeting
    **Tool:**
    find_elements(app="Microsoft Outlook", query="Send") -> click_at(center_x, center_y)
    Send the meeting from the Outlook compose window.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolkitParam:
    """A parameter for a toolkit function."""

    name: str
    description: str
    required: bool = True
    default: str = ""


@dataclass(frozen=True)
class ToolkitFunction:
    """A pre-defined action template."""

    name: str
    description: str
    kind: str  # default route kind: "gui", "command", "api"
    params: list[ToolkitParam] = field(default_factory=list)


# ── Toolkit function definitions ─────────────────────────

TOOLKIT_FUNCTIONS: dict[str, ToolkitFunction] = {}


def _register(func: ToolkitFunction) -> ToolkitFunction:
    TOOLKIT_FUNCTIONS[func.name] = func
    return func


# -- GUI interaction via Protean MCP --

_register(ToolkitFunction(
    name="find_elements",
    description="Find visible UI elements by text and return exact center coordinates.",
    kind="gui",
    params=[
        ToolkitParam("app", "Target application name"),
        ToolkitParam("query", "Visible text to search for"),
    ],
))

_register(ToolkitFunction(
    name="click_at",
    description="Click at verified coordinates, typically after find_elements or move.",
    kind="gui",
    params=[
        ToolkitParam("x", "X coordinate"),
        ToolkitParam("y", "Y coordinate"),
        ToolkitParam("coordinate_mode", "Coordinate mode such as global or window", required=False),
    ],
))

_register(ToolkitFunction(
    name="screenshot",
    description="Capture the current display when visual inspection is needed.",
    kind="gui",
    params=[
        ToolkitParam("display", "Display index", required=False),
    ],
))

_register(ToolkitFunction(
    name="move",
    description="Move the cursor to an estimated target and inspect the action-centered crop.",
    kind="gui",
    params=[
        ToolkitParam("x", "X coordinate"),
        ToolkitParam("y", "Y coordinate"),
        ToolkitParam("coordinate_mode", "Coordinate mode such as global or window", required=False),
        ToolkitParam(
            "include_action_view",
            "Whether to include the action-centered crop",
            required=False,
        ),
    ],
))

_register(ToolkitFunction(
    name="type_text",
    description="Find an input field by label and type text into it.",
    kind="gui",
    params=[
        ToolkitParam("app", "Target application name"),
        ToolkitParam("label", "Accessibility label of the input field"),
        ToolkitParam("text", "Text to type"),
    ],
))

_register(ToolkitFunction(
    name="menu_click",
    description="Click a menu item by path (e.g. 'Edit > Remove Background').",
    kind="gui",
    params=[
        ToolkitParam("app", "Target application name"),
        ToolkitParam("path", "Menu path separated by ' > '"),
    ],
))

_register(ToolkitFunction(
    name="select_option",
    description="Select an option from a dropdown/popup by label.",
    kind="gui",
    params=[
        ToolkitParam("app", "Target application name"),
        ToolkitParam("label", "Accessibility label of the dropdown"),
        ToolkitParam("value", "Option to select"),
    ],
))

# -- Tier 1: System-level actions --

_register(ToolkitFunction(
    name="activate_app",
    description="Bring an application to the foreground.",
    kind="command",
    params=[
        ToolkitParam("app", "Application name or bundle ID"),
    ],
))

_register(ToolkitFunction(
    name="key_press",
    description="Press a keyboard shortcut.",
    kind="command",
    params=[
        ToolkitParam("keys", "Key combination (e.g. 'cmd+s', 'cmd+shift+e')"),
    ],
))

# -- Tier 0: Headless / scripted actions (prefer when they reach the same goal) --

_register(ToolkitFunction(
    name="run_terminal_command",
    description=(
        "Run a one-shot shell command in the user's terminal. Prefer this over "
        "long GUI chains when a verified command produces the same end state "
        "(e.g. `sed -i 's/foo/bar/g' file`, `mkdir -p ...`, `git commit -am ...`, "
        "`curl -O ...`). Pick a shell that exists on the target platform."
    ),
    kind="command",
    params=[
        ToolkitParam("command", "Full shell command to execute"),
        ToolkitParam("shell", "Shell to use (e.g. 'bash', 'pwsh')", required=False),
        ToolkitParam("timeout", "Timeout in seconds", required=False),
    ],
))

_register(ToolkitFunction(
    name="run_script",
    description=(
        "Run a script bundled with the skill (under scripts/). Prefer this for "
        "multi-step transformations (e.g. iterating over many files) that would "
        "be tedious or fragile to perform as GUI clicks."
    ),
    kind="command",
    params=[
        ToolkitParam("filename", "Script file under the skill's scripts/ directory"),
        ToolkitParam("args", "Arguments to pass to the script", required=False),
    ],
))


def get_toolkit_prompt() -> str:
    """Generate a description of available toolkit functions for the LLM prompt."""
    lines = ["## Available Toolkit Functions", ""]
    lines.append(
        'The "tool" field is an optional tool string. Write it as either a single '
        'Protean MCP tool or a short chain of Protean MCP tools that captures the '
        'preferred strategy for the logical step. Keep the "action" field human-readable.'
    )
    lines.append("")
    for func in TOOLKIT_FUNCTIONS.values():
        params_str = ", ".join(
            f'{p.name}{"?" if not p.required else ""}' for p in func.params
        )
        lines.append(f"- **{func.name}**({params_str}): {func.description}")
    lines.append("")
    lines.append("Tool chains: combine tools with -> when one logical step needs multiple actions.")
    lines.append("find_elements locates targets precisely — prefer it over guessing coordinates.")
    lines.append(
        "Prefer run_terminal_command / run_script / key_press over long GUI chains "
        "whenever a verified command or shortcut achieves the same end state "
        "(e.g. `sed`/`mkdir`/`git` instead of clicking through an editor or file manager)."
    )
    lines.append("")
    lines.append("Examples:")
    lines.append(
        '  find_elements(app="Microsoft Outlook", query="Send") -> click_at(center_x, center_y)'
    )
    lines.append(
        '  find_elements(app="Microsoft Outlook", query="Location")'
        ' -> click_at(center_x, center_y) -> type_text(text="Room 101")'
    )
    lines.append(
        '  activate_app(app="Microsoft Outlook") -> key_press(keys="cmd+n")'
    )
    lines.append(
        '  run_terminal_command(command="sed -i \'s/{{old}}/{{new}}/g\' {{file_path}}")'
        '   # replaces a VS Code find/replace + save GUI sequence'
    )
    lines.append(
        '  run_script(filename="rename_batch.py", args="{{folder}}")'
        '   # replaces N manual renames'
    )
    lines.append("")
    lines.append("Example step with toolkit hint:")
    lines.append(
        '  {"name": "open-meeting-composer", '
        '"action": "In Microsoft Outlook, locate the visible New Event button '
        'and open the meeting composer", '
        '"tool": "find_elements(app=\"Microsoft Outlook\", query=\"New Event\") '
        '-> click_at(center_x, center_y)"}'
    )
    lines.append("")
    lines.append("Example step without toolkit:")
    lines.append('  {"name": "navigate-to-settings", '
                 '"action": "Open the app preferences from the menu bar", '
                 '"tool": ""}')
    return "\n".join(lines)
