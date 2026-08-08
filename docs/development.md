# Developing tandem

Dev setup and the extension surface. (Back to the
[README](../README.md).)

## Extending tandem

The sync engine talks to a small adapter interface
(`tandem.converter.TraceConverter`):

```python
class TraceConverter(Protocol):
    def translate_entry(entry, direction, ctx) -> list[TargetEntry] | TranslationError
```

`ReferenceConverter` implements it via a normalized event model
(`tandem/events.py`) derived from the observed formats. Pass your own
converter to `SyncEngine(store, session, source, converter=...)`.

## Development

```bash
uv sync && uv run pytest
pipx install .        # or: uv tool install .
```

Dependencies are deliberately small: `click` (CLI), `pydantic` v2 (event
schema), `watchdog` (transcript tailing), `pexpect`/ptyprocess (PTY
passthrough); state is stdlib `sqlite3`.
