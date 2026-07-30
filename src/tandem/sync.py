"""Sync engine: translate each new active-side entry and append it to the
shadow session file.

Guarantees:
- Append-only; the shadow is never rewritten or truncated.
- Appends are whole-line + fsync (util.append_jsonl_fsync), so a torn write
  cannot leave a half entry that was recorded as synced.
- Exactly-once across crashes via a write-ahead intent in the sync cursor:
  before appending the output of source line N we persist {line: N,
  pre_size}; after the append we persist the advanced cursor + translation
  context. On restart, an unresolved intent tells us whether the append
  happened (shadow grew past pre_size) — if it did, the line is re-translated
  for its context side effects but not re-appended.
- Translation failures are localized to a single entry/turn: the raw entry is
  quarantined under ~/.tandem/quarantine/, a plain-text placeholder goes into
  the shadow (at most one per turn), and sync continues.
"""

from __future__ import annotations

from pathlib import Path

from . import paths
from .constants import PLACEHOLDER
from .converter import ReferenceConverter, TraceConverter, TranslationError
from .events import SessionContext
from .harness import get_adapter, other
from .runner import ctx_to_cursor
from .state import PairedSession, StateStore, SyncCursor
from .tailer import TailedLine
from .util import append_jsonl_fsync, write_file_atomic


class SyncSetupError(RuntimeError):
    pass


# sentinel line index for a dangle-flush write-ahead intent (real source line
# indices are >= 0, so it can never collide with a line append)
_FLUSH_LINE = -1


class SyncEngine:
    """EventSink translating from `source` into the other harness's file."""

    def __init__(
        self,
        store: StateStore,
        session: PairedSession,
        source: str,
        converter: TraceConverter | None = None,
    ):
        self.store = store
        self.session = session
        self.source = source
        self.target_id = other(source)
        self.direction = f"{source}->{self.target_id}"
        self.converter: TraceConverter = converter or ReferenceConverter()
        self.target = get_adapter(self.target_id)

        if self.target_id == "claude":
            if not session.claude_session_id:
                raise SyncSetupError("no claude session id recorded")
            self.shadow_path = paths.claude_transcript_path(
                session.cwd, session.claude_session_id
            )
        else:
            if not session.codex_session_id:
                raise SyncSetupError("no codex session id recorded yet")
            found = paths.find_codex_rollout(session.codex_session_id)
            if found is None:
                raise SyncSetupError(
                    f"codex rollout for {session.codex_session_id} not found"
                )
            self.shadow_path = found
        if not self.shadow_path.exists():
            raise SyncSetupError(f"shadow transcript missing: {self.shadow_path}")

        # Resolved lazily on the first handle() call because the cursor is
        # owned by the tail loop.
        self._prepared = False
        self._skip_append_line: int | None = None

    # -- crash recovery ------------------------------------------------------

    def _prepare(self, ctx: SessionContext, cursor: SyncCursor) -> None:
        self._prepared = True
        intent = cursor.pending.get("intent")
        if intent:
            try:
                grew = self.shadow_path.stat().st_size > int(intent["pre_size"])
            except OSError:
                grew = False
            if int(intent["line"]) == _FLUSH_LINE:
                if grew:
                    # the flush landed before the crash; its calls are closed
                    ctx.pending_calls.clear()
                cursor.pending.pop("intent", None)
                ctx_to_cursor(ctx, cursor)
                self.store.save_cursor(cursor)
            elif grew:
                # The append for this line landed before the crash; skip the
                # re-append but still re-run translation for ctx effects.
                self._skip_append_line = int(intent["line"])
        if self.target_id == "claude":
            # The claude file may have grown since we last wrote (its own CLI
            # appends while claude is active); chain onto the real leaf and
            # pick up the model claude last used for rendered entries.
            adapter = get_adapter("claude")
            leaf = adapter.derive_leaf_uuid(self.shadow_path)
            if leaf:
                ctx.claude_leaf_uuid = leaf
            model = adapter.derive_last_model(self.shadow_path)
            if model:
                ctx.claude_model = model

    # -- sink interface ------------------------------------------------------

    def handle(self, line: TailedLine, ctx: SessionContext, cursor: SyncCursor) -> None:
        if not self._prepared:
            self._prepare(ctx, cursor)

        if line.raw is None:
            result: list[dict] | TranslationError = TranslationError(
                reason="source line is not valid JSON",
                entry_summary=line.text[:120],
            )
        else:
            result = self.converter.translate_entry(line.raw, self.direction, ctx)

        if isinstance(result, TranslationError):
            self._handle_failure(line, result, ctx, cursor)
            return
        if result:
            self._append_once(result, line, ctx, cursor)

    def close(self) -> None:
        pass

    # -- source handoff ------------------------------------------------------

    def flush_dangling(self, ctx: SessionContext, cursor: SyncCursor) -> int:
        """Close every pending tool call with a placeholder result. Called
        after a drain when the source is being handed off (role switch,
        one-off). Exactly-once via the same write-ahead intent as line
        appends, keyed on the sentinel line index _FLUSH_LINE."""
        if not self._prepared:
            self._prepare(ctx, cursor)
        fn = getattr(self.converter, "flush_dangling", None)
        if fn is None or not ctx.pending_calls:
            return 0
        events = fn(ctx)
        entries = self.target.render_events(events, ctx) if events else []
        if not entries:
            ctx_to_cursor(ctx, cursor)
            self.store.save_cursor(cursor)
            return 0
        pre_size = self.shadow_path.stat().st_size
        cursor.pending["intent"] = {"line": _FLUSH_LINE, "pre_size": pre_size}
        self.store.save_cursor(cursor)
        append_jsonl_fsync(self.shadow_path, entries)
        cursor.pending.pop("intent", None)
        ctx_to_cursor(ctx, cursor)
        self.store.save_cursor(cursor)
        return len(entries)

    # -- internals -----------------------------------------------------------

    def _append_once(
        self, entries: list[dict], line: TailedLine, ctx: SessionContext, cursor: SyncCursor
    ) -> None:
        skip_append = self._skip_append_line == line.line_index
        if skip_append:
            self._skip_append_line = None
            if self.target_id == "claude":
                # the crashed run appended entries with uuids we no longer
                # know; re-derive the chain tip from the file
                leaf = get_adapter("claude").derive_leaf_uuid(self.shadow_path)
                if leaf:
                    ctx.claude_leaf_uuid = leaf
        else:
            pre_size = self.shadow_path.stat().st_size
            cursor.pending["intent"] = {"line": line.line_index, "pre_size": pre_size}
            self.store.save_cursor(cursor)
            append_jsonl_fsync(self.shadow_path, entries)

        cursor.pending.pop("intent", None)
        cursor.byte_offset = line.end_offset
        cursor.line_index = line.line_index + 1
        ctx_to_cursor(ctx, cursor)
        self.store.save_cursor(cursor)

    def _handle_failure(
        self, line: TailedLine, err: TranslationError, ctx: SessionContext, cursor: SyncCursor
    ) -> None:
        qdir = paths.quarantine_dir(self.session.tandem_id)
        qpath = qdir / f"{self.source}-line-{line.line_index:06d}.json"
        write_file_atomic(qpath, line.text + "\n")

        # At most one placeholder per source turn; further failures in the
        # same turn only quarantine.
        if cursor.pending.get("last_placeholder_turn") == ctx.turn_index:
            cursor.byte_offset = line.end_offset
            cursor.line_index = line.line_index + 1
            ctx_to_cursor(ctx, cursor)
            self.store.save_cursor(cursor)
            return

        text = self._placeholder_text(ctx, err, qpath)
        entries = self.target.render_placeholder(text, ctx)
        cursor.pending["last_placeholder_turn"] = ctx.turn_index
        cursor.failed_turns += 1
        self._append_once(entries, line, ctx, cursor)

    def _placeholder_text(self, ctx: SessionContext, err: TranslationError, qpath: Path) -> str:
        return PLACEHOLDER.format(
            turn=ctx.turn_index,
            source=get_adapter(self.source).display_name,
            reason=err.reason,
            quarantine=qpath,
        )
