# sample-target (FIXTURE)

**This is a test fixture, not the product.** It is a tiny FastAPI "notes" service that PRediction Market gates during
development, in its own tests and in CI, and that the first league runs against. The factory (`prm/`) knows nothing about it:
everything the factory needs comes from `.prm.toml`.

- `notes_app/`: the service (`from notes_app import create_app`).
- `docs/SPEC.md`: the public spec. Traders may read it, and the hidden holdouts are written from it alone.
- `tests/`: the service's own (visible, deliberately partial) unit tests: `pytest examples/sample-target`.
- `.prm.toml`: the target contract (settle command, holdouts location, context globs).
- `../sample-target-holdouts/`: fixture holdouts, public on purpose so CI can run end to end. Real targets keep them private.
- `../patches/`: demo PR patches, relative to this directory (`git apply --directory=examples/sample-target <patch>` from the repo root).
