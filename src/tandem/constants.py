"""Shared literals: attribution markers and placeholder wording."""

ATTRIBUTION = {
    "claude": "[via claude-code]",
    "codex": "[via codex]",
    "tandem": "[tandem]",
    "user": "",
}

SEED_NOTE = (
    "[tandem] This session is one half of tandem paired session {tandem_id}. "
    "It was created by tandem (no model has run here yet). Turns executed in "
    "{other} are synced below as plain-text context; entries are tagged "
    "[via claude-code] or [via codex] by the agent that produced them. Tool "
    "calls from the other agent appear as action summaries, not replayable "
    "calls."
)

# Untranslatable-entry placeholder (decision for the spec's open question).
PLACEHOLDER = (
    "[tandem: turn {turn} could not be translated from {source} — {reason}; "
    "raw entry quarantined at {quarantine}]"
)
