#!/usr/bin/env python3
"""Helper script: opens a macOS dialog via osascript asking for a secret.

If the user types "protean", prints SUCCESS. Otherwise prints FAIL.
Used by smoke_terminal_cua.py to test that run_terminal_command's
screenshot lets the LLM see and interact with visual popups.
"""
import subprocess
import sys

result = subprocess.run(
    [
        "osascript", "-e",
        'display dialog "Enter the secret code:" '
        'default answer "" '
        'buttons {"OK"} default button "OK" '
        'with title "Protean Test"',
    ],
    capture_output=True,
    text=True,
)

# osascript returns "button returned:OK, text returned:XXX"
output = result.stdout.strip()
if "text returned:" in output:
    answer = output.split("text returned:")[-1].strip()
    if answer == "protean":
        print("SUCCESS: correct answer")
        sys.exit(0)
    else:
        print(f"FAIL: got '{answer}', expected 'protean'")
        sys.exit(1)
else:
    print(f"FAIL: dialog cancelled or unexpected output: {output}")
    sys.exit(1)
