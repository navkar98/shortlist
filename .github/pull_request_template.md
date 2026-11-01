## What and why

<!-- What this changes and why. Link the issue if there is one ("Closes #12"). -->

## Checked for real

<!-- What you ran against the real thing: which endpoint, page, CLI version, or command, and what came back.
     Say what you could not check. -->

## Checklist

- [ ] `python -m pytest -q` passes
- [ ] New parsing is backed by trimmed real responses in `tests/fixtures/`
- [ ] Every request goes through `polite.Polite`; nothing works around a 403, a 429, or a bot challenge
- [ ] No secrets, personal paths, or personal preferences in the diff
- [ ] README or CONTRIBUTING updated if behavior changed
