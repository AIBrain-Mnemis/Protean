"""Shared `## Step fields` rules embedded into skill-writing prompts."""

STEP_FIELD_RULES = """\
## Step fields

For each step, produce:

- **name**: short kebab-case identifier (e.g. "fill-subject", "click-send"). Used as heading and branch target. Must be unique within the skill.

- **action**: concrete, reproducible instructions for achieving the step's *intent* — not necessarily the exact gestures the user performed. Reference variable values as {{param_name}}.
  Rules:
    - Anchor the step in the **user's intent** (what they were trying to achieve), then choose the most efficient reliable medium to achieve it. The recording is a demonstration of intent, not a script to replay.
    - Prefer the most direct route that reliably reaches the same end state: a single terminal command, a script, a keyboard shortcut, a menu item, or a native API call — over a long chain of mouse clicks — whenever the shortcut is robust and produces the same observable result.
      Examples:
        Good: "Replace all occurrences of {{old}} with {{new}} in {{file_path}} using `sed -i 's/{{old}}/{{new}}/g' {{file_path}}`" (when the demo showed: open VS Code, Ctrl+F, replace, save)
        Good: "Create the directory {{path}} (`mkdir -p {{path}}`)" (when the demo showed clicking through Finder/Explorer)
        Good: "Save the file (Cmd+S)" — shortcut over File > Save click
        Bad:  Replicating every click of a 12-step GUI workflow when one `git commit -am '...'` achieves the same result.
    - Still describe concrete UI interactions when GUI is the right medium (e.g. a step that depends on visual confirmation, drag-and-drop, or an app with no scriptable interface). Then follow the Generalizability rules below for labels, icons, and shortcuts.
    - Never invent a shortcut that doesn't exist or hasn't been verified to produce the same result. Prefer well-known, stable commands and shortcuts. When unsure, fall back to the demonstrated GUI path.

- **tool**: optional Protean MCP tool hint. Pick the medium that best matches the action. May be empty, a single tool, or a short chain. Options include:
    - GUI chain:  find_elements(app=..., query=...) -> click(x=center_x, y=center_y)
    - Shortcut:   activate_app(app=...) -> key_press(keys="cmd+s")
    - Shell:      run_terminal_command(command="sed -i 's/foo/bar/g' file.txt")
    - Script:     run_script(filename.py, args)
  Prefer run_terminal_command / run_script / key_press over GUI chains when they achieve the same goal more reliably and faster. Use a GUI chain only when the GUI is genuinely the best medium. Leave empty when no tool hint applies.

- **target_app**: the application this step targets (e.g., "TextEdit", "Finder", "Outlook"). Required for most steps. Leave empty only for steps that don't target a specific app.

- **verify_condition**: structured verification condition:
    - strategy: "ax_element", "text_content", or "visual". Prefer "ax_element" for native apps with good accessibility. Prefer "text_content" for verifying status messages or form values. Use "visual" only when no AX element or text reliably indicates success.
    - ax_role: the expected AX role, e.g. "AXButton", "AXSheet", "AXTextField" (for ax_element)
    - ax_title: the expected element title or label text (for ax_element)
    - expected_text: the text that should appear on screen (for text_content)
    - description: human-readable verification text — what the screen should look like after this step. Always fill this field regardless of strategy.
  Fill only the strategy-specific fields relevant to the chosen strategy, but always include description.
  The verify_condition proves local progress for this step; final end-state acceptance belongs in `success_criteria`.

- **figures**: list of figures to help the agent recognize UI targets or visual states.

  Each figure ref is `overview_N` or `detail_N`, where N is the Frame number from the evidence timeline. Each frame header declares which images are available (e.g. `[action | overview + detail]` or `[scene | overview]`). Only reference images declared in the header.

  Include a figure only when the step would be hard to execute correctly from text alone, for example:
    - an unlabeled/icon-only control where the visual shape matters;
    - a target identified by visual location or relative layout rather than label (e.g. "the small calendar icon below Mail in the left rail");
    - an ambiguous UI state that changes the next action (e.g. duplicate recipient pills that must be removed);
    - a visual verification target whose appearance/location is the evidence of success (e.g. an event block at a time slot).

  Do not use figures as a walkthrough gallery, progress log, or routine before/after screenshots. If a text label, AX role/title, keyboard shortcut, script output, terminal command, or clearly named field/button identifies the target unambiguously, leave figures empty for that step. Do not include figures merely to show that a normal labeled field was filled, a checkbox is on, a dropdown appeared, or a window opened.

  **Prefer detail shots when available. Use overview shots when broader context is needed or the frame has no detail.**

  Leave empty if unnecessary.

- **idempotent**: true (default) if the step can be retried safely. false for irreversible actions (send email, submit forms, delete data, post messages).

- **branches**: conditional jumps to named steps. Each branch has:
    - condition: when to take this branch (a VerifyCondition).
    - next_step: the step name to jump to
  Leave empty for linear steps. Use branches when a step may lead to different outcomes that require different handling.

## Tool hints

A step may have a single tool or a short chain (2-4 calls). Multiple actions forming one logical step can be chained, e.g. `find_elements(app=..., query="Location") -> click() -> type_text(text="...")`. Do not split one logical step into multiple steps just to mirror every tool call.

**Priority order for the `tool` field**, when more than one strategy reaches the same end state:

  1. `run_script(filename, args)`        — multi-line / parametrized logic
  2. `run_terminal_command(command=...)` — single verified shell command
  3. `key_press(keys=...)`               — native keyboard shortcut
  4. `find_elements(...) -> click(...)` — GUI fallback

Pick the highest tier that reliably reaches the step's goal on the target platform.

## Scripts

Scripts are a **first-class output** of the skill, not an afterthought — they live next to SKILL.md under the skill's `scripts/` directory. Whenever a step needs more than a single shell command, prefer creating a script over expanding the step into many GUI clicks or stuffing logic into a fragile shell one-liner.

**Python is the preferred language for created scripts.** Use `#!/usr/bin/env python3`, parse arguments with `argparse` (give every argument a `help=...` string so `--help` is self-describing), and provide a proper `if __name__ == "__main__":` entrypoint. Reach for shell scripts only for genuine one-liners.

Two modes for associating scripts with a skill:

**1. Create a new script** — produce one when the step (or chain of steps) involves any of:
- Iterating the same operation over N items (rename, convert, upload).
- Computing/deriving a value (parsing logs, extracting IDs, formatting dates) before reusing it.
- Manipulating structured data (CSV/JSON/YAML).
- Non-trivial control flow ("if X exists do Y else Z") with a meaningful exit code.
- Anything that would otherwise need 3+ chained shell commands.

**2. Reference an existing script** — when an external script already exists on the system (`/opt`, `/usr/local`, shared org tooling). If a runtime absolute path resolves to this skill's own `scripts/` directory (e.g. `/root/.codex/skills/<skill>/scripts/foo.py`), do not register it as a refer-mode SkillScript — it is the same file as the bundled create-mode entry.

**Consolidate near-duplicates, not unrelated operations.** Before adding a new SkillScript:

- Prefer one **parameterized** script over multiple near-duplicates. Two scripts that differ only in column names, key fields, file paths, or threshold values should be ONE script with those exposed as `--args`.
- Collapse multiple audit/validation scripts over the same output into ONE multi-mode script (e.g. `inspect` / `audit` / `verify` subcommands) unless inputs substantially differ.
- Do NOT fuse genuinely independent operations into one script just to shrink the count. An extractor and a transformer that run on different inputs stay separate.

A SkillScript has three fields:
- **filename** — bare basename (`check_disk.py`) for create-mode; absolute path (`/opt/tools/check_disk.py`) for refer-mode.
- **content** — FULL runnable source for create-mode (shebang, imports, argparse, proper entrypoint; no TODOs, pseudo-code, ellipses, or stubs — the script must run as-is). Empty string for refer-mode (do NOT duplicate the file).
- **description** — see "Description content" below.

Invoke the script from a step's `tool` hint:
  Create-mode:  `run_script(filename="check_disk.py", args="--server={{server_name}}")`
                `run_terminal_command(command='python3 ${SKILL_DIR}/scripts/check_disk.py --server={{server_name}}')`
  Refer-mode:   `run_terminal_command(command='/opt/tools/check_disk.py --server={{server_name}}')`

Use parameters ({{param_name}}) in the invocation so the executor substitutes values at runtime. Keep scripts generic — accept args via argparse or positional parameters, not hardcoded values.

**Description content (both modes).** Keep concerns separate rather than fused into one omnibus sentence. Per-arg semantics live in the script's `--help`. Cover what helps; skip what doesn't:
- **What** the script does — the core action.
- **When** to invoke — the scenario or input condition that makes this the right script: data shape, file flavor, edge-case flag, configuration variant.
- **Invocation** — a canonical CLI form in backticks when a concrete example helps.
- **Outputs** — what the script writes or prints, and any non-obvious exit behavior (e.g. nonzero on validation failure).

**Example — a generated SkillScript paired with the step that invokes it:**

  SkillScript:
    {
      "filename": "rename_batch.py",
      "content": "#!/usr/bin/env python3\\nimport argparse, pathlib\\n\\ndef main():\\n    p = argparse.ArgumentParser()\\n    p.add_argument('--folder', required=True)\\n    p.add_argument('--prefix', required=True)\\n    args = p.parse_args()\\n    folder = pathlib.Path(args.folder)\\n    for i, src in enumerate(sorted(folder.glob('*.jpg')), 1):\\n        src.rename(folder / f'{args.prefix}_{i:03d}.jpg')\\n\\nif __name__ == '__main__':\\n    main()\\n",
      "description": "Rename every *.jpg in <folder> to <prefix>_NNN.jpg in place. Use when a flat folder of photos needs uniform-prefix sequential names. Invoke as `python rename_batch.py --folder=<dir> --prefix=<p>`. Prints the rename count; exits nonzero if <dir> is missing."
    }

  Step:
    {
      "name": "batch-rename-photos",
      "action": "Rename every photo under {{folder}} to {{prefix}}_NNN.jpg using the bundled rename_batch.py script.",
      "tool": "run_script(filename=\\"rename_batch.py\\", args=\\"--folder={{folder}} --prefix={{prefix}}\\")"
    }

## Generalizability

Each step must work across different runs, machines, and contexts. Be specific about WHAT to interact with — include real details — but frame them so the executor can adapt when the environment differs:

- **Identify by label/role, not position.** E.g. "Click the 'Send' button" not "click at (800, 600)".
- **Describe structurally, not spatially.** E.g. "The first item in the dropdown" not "the option at y=200". Layout landmarks like "in the toolbar" are fine.
- **Give the real label + its meaning.** Write the actual on-screen text the executor will see AND describe what the element does, so the executor can match by label in the current locale or fall back to meaning if the label differs. E.g. "Click the new-meeting button (labeled '新建会议') in the calendar toolbar" — not just "Click '新建会议'" (no locale fallback) nor "Click the new-meeting button" (no label to search for).
- **Icon-only buttons: describe appearance + location + purpose.** Include shape, symbol, color if distinctive, and position relative to a visible landmark. E.g. "Click the plus icon (a circle with a '+' inside) at the top-right of the sidebar to create a new item".
- **Keyboard shortcuts: state intent + shortcut.** "Paste the text (Cmd+V)" — the executor knows the intent and the key for the current platform.
- **Don't bake in run-specific data.** Usernames, dates, row counts, window titles with timestamps — these change. Use parameters or describe the pattern ("the row containing {{meeting_title}}").
""" # noqa: E501
