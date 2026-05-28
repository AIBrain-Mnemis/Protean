#!/usr/bin/env python3
"""Helper script for smoke_terminal_v2.py.

Prompts for input. If the answer is "protean", prints SUCCESS and exits.
Otherwise waits 35s then prints FAIL — long enough for idle detection (~30s)
to fire before the script exits.
"""
import sys
import time

answer = input("Enter the secret: ")
if answer.strip() == "protean":
    print("SUCCESS: correct answer")
    sys.exit(0)
else:
    time.sleep(90)
    print("FAIL: wrong answer after wait")
    sys.exit(1)
