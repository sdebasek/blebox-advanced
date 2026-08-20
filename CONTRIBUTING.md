# Contributing

Thanks for taking a look. Bug reports with a diagnostics dump attached are as
valuable as code.

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-test.txt
pytest
```

The test suite pins Home Assistant, needs **Python 3.14**, and runs entirely
offline against mocked devices. No hardware required.

Linting and formatting use [ruff](https://docs.astral.sh/ruff/):

```bash
ruff check custom_components tests
ruff format custom_components tests
```

CI runs `hassfest`, HACS validation and the test suite on every push.

## Commit messages

This project uses [Conventional Commits](https://www.conventionalcommits.org/).
The changelog and version bumps are generated from them by `release-please`, so
the prefix decides what gets released.

```
<type>[optional scope]: <description>

[optional body]

[optional footer]
```

| Type | Effect | Use for |
| --- | --- | --- |
| `feat` | minor bump | a new entity, option or capability |
| `fix` | patch bump | a bug fix |
| `docs` | no release | README and docs only |
| `refactor` | no release | no behaviour change |
| `test` | no release | tests only |
| `chore` | no release | tooling, CI, dependencies |
| `perf` | patch bump | performance |

A breaking change gets a `!` after the type, or a `BREAKING CHANGE:` footer, and
triggers a major bump:

```
feat!: rename the domain to blebox_advanced

BREAKING CHANGE: config entries cannot migrate across domains, so the
integration must be deleted and re-added.
```

Examples from this repository:

```
feat: add callback delivery sensor and repair issues
fix: apply a changed report interval to the device
docs: split setup and troubleshooting out of the README
```

Write the body to explain **why**, not what. The diff already says what.

## Style

- **No em dashes or en dashes.** Use a plain hyphen, a comma, or restructure the
  sentence. This applies to code comments, docstrings and documentation.
- Docstrings on every public function and class; `ruff` enforces this.
- Comments explain the non-obvious. A comment restating the code is noise; a
  comment explaining why a device needs its own fields echoed back is not.

## Design rules

These are the invariants that make the integration safe to point at hardware
someone depends on. Tests pin all of them, so breaking one fails CI.

1. **Never modify a device action this integration did not create.** Ownership
   comes from the callback URL. The one exception is the opt-in button behaviour
   control, which writes only slots holding a native relay action, never an HTTP
   action or an action type we do not understand.

2. **Check capacity before the first write.** A provisioning run either fits
   entirely or changes nothing. Half-provisioned hardware is worse than none.

3. **Never infer a button press by polling.** Polling cannot distinguish a wall
   press from a Home Assistant command, breaks when the input is detached from
   the relay, and can never detect a long press. Events are pushed.

4. **All device I/O lives in `blebox_actions.py`.** Nothing else opens a
   connection. A firmware change must be fixable in one file, and the event
   receiver must keep working if the undocumented API disappears entirely.

5. **Detect capabilities, do not check model names.** Entities appear because
   the device reported the underlying setting, and value ranges come from the
   device's own constraint metadata.

6. **Never log or expose a callback token.** Diagnostics redact them.

7. **Trust the device's answer over an assumption.** Both `set` endpoints return
   the resulting state; use it rather than assuming the write worked.

## Tests

Every bug fix needs a regression test that fails against the old code. Several
existing tests exist purely because something went wrong once, and their
docstrings say so, which is the point.

Prefer testing observable behaviour through Home Assistant over calling internal
functions. Fixtures in `tests/test_integration.py` are real payloads captured
from hardware, so use them rather than inventing shapes.

## Pull requests

- One logical change per PR.
- Say what hardware you tested on, if any. "Not tested on hardware" is a
  perfectly acceptable statement and much better than silence.
- If you touched anything under the undocumented API, say how you established
  the behaviour. "The wBox bundle sends this" or "I watched `lastCall` cycle" is
  the kind of evidence that makes a change reviewable.
