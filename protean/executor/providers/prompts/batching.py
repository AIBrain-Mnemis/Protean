"""Shared "when to batch GUI actions" guidance for executor system prompts.

Both the native Computer Use loop (``computer_use.py``) and the MCP-based CLI surface (``claude_code.py``) want the model to batch aggressively: chain every action whose target is already knowable from what's on screen into one turn, rather than defaulting to one action per turn. *What's safe to batch* is identical between the two — only *how the model signals it* differs:

- Computer Use loop: Protean owns the whole tool-call batch (all ``tool_use`` blocks arrive in one API response) and auto-detects the last call itself (``is_last_tool`` in ``computer_use.py``) — the model never has to say anything.
- MCP surface: each tool call is a separate, stateless JSON-RPC request; Protean has no visibility into how many more calls are coming, so the model must explicitly pass ``include_screenshot=false`` on every call in the batch except the last.

Keeping the shared criteria here means the two prompts can't drift apart on *when* to batch, while each still states its own correct mechanism.
"""

BATCHING_GUIDANCE = """\
Default to batching rather than one action per turn. Before issuing any GUI tool calls, quickly plan the longest deterministic action chain you can execute from the current state, then emit that whole chain in one turn.

Use this decision rule: if the next target is already knowable from the current screenshot and known UI structure, keep chaining; if the next target is not yet knowable, stop exactly at that boundary and wait for the new screenshot before continuing.

In practice this means you should usually emit multi-call batches, not single calls. If you can confidently predict 3+ consecutive actions from the current state, execute all of them in one turn.

Example — a form with three fields you can already see in full: left_click(field1) -> type_text(value1) -> left_click(field2) -> type_text(value2) -> left_click(field3) -> type_text(value3) -> left_click(submit). All seven calls in one turn — nothing about the layout changes between them, so there is nothing new to observe until submit resolves.

Insert wait between steps only when timing is the only uncertainty and the post-wait target is still fixed regardless of transient animation or debounce. A wait() does NOT make an unknown layout knowable.

For free-text-looking controls that are actually selectors (autocomplete/typeahead/combobox chips), decide by intent precision. If the user gave an exact, unambiguous value, fill it directly (or select only if the UI enforces selection semantics); if the user intent is fuzzy or can map to multiple options, type as filter and stop at that boundary, then read refreshed options/input state before choosing the exact option.

Only stop batching at the point where the next action's target is not yet knowable from what you've already seen: content that loads asynchronously to an unknown position, a validation message that may or may not appear, a popup or menu whose concrete items/positions you have not observed yet, or a retry after an error.

Examples where the next step is genuinely unknown — stop and look instead of guessing, wait() included:
- click Search, then click a result whose position isn't known until results render
- submit a form, then click a confirmation button whose position/appearance isn't confirmed
- open a dropdown or menu, then choose an option before its contents have actually been observed — waiting first does not make the item's position knowable
- close a modal, then click an underlying element without confirming it's still there
- switch tabs, then interact with content that may differ from what you last saw
"""
