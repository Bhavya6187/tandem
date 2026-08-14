"""Shared literals: attribution markers and placeholder wording."""

ATTRIBUTION = {
    "claude": "[via claude-code]",
    "codex": "[via codex]",
    "opencode": "[via opencode]",
    "tandem": "[tandem]",
    "user": "",
}

SEED_NOTE = (
    "[tandem] This session is one half of tandem paired session {tandem_id}. "
    "It was created by tandem (no model has run here yet). Turns executed in "
    "{other} are synced below as context; text messages are tagged "
    "[via claude-code] or [via codex] by the agent that produced them. Tool "
    "calls from the other agent are mirrored as native tool-call records in "
    "this session's own tool vocabulary."
)

# Seed line of the minimal rollout a cold `tandem sub` worker resumes. Cold
# workers get no shared history; the note exists so the rollout has model
# context at all (codex resume and tandem's own validate_transcript both
# reject a rollout with no response_item).
SUB_SEED_NOTE = "[tandem] Delegated subagent worker session (no shared history)."

# Untranslatable-entry placeholder (decision for the spec's open question).
PLACEHOLDER = (
    "[tandem: turn {turn} could not be translated from {source} — {reason}; "
    "raw entry quarantined at {quarantine}]"
)
