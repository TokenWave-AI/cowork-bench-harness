# Offline acceptance checks

From this source package, run:

```bash
HARNESS_TEST_RELEASE=/absolute/path/to/unpacked-release \
  python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

The checks make no model API or Docker calls. They execute the shipped Goal
driver against a small JSON-RPC stdio peer and use the release's real dispatcher
and registry with harmless two-row fixture engines. No live release files are
edited. `HARNESS_TEST_RELEASE` is required for dispatcher and prepare checks;
without it those checks are explicitly skipped.

The suite covers tool discovery before the first paid turn, Goal completion,
deadline interruption using the authoritative turn ID, thread continuation,
provider denial, Oracle's minimal tool inventory, independent per-row runtime
selection, MCP forwarding, immutable runtime hashes, and offline preparation
from two different source paths. Prepare checks verify the eight-hour budget,
selected rows and conditions, source relocation, refusal to overwrite an
existing output, and absence of copied credential contents.

Temporary fixtures are removed after each test and default to `tests/.tmp`.
Set `HARNESS_TEST_TMPDIR` to a writable data-disk directory if needed.
`HARNESS_TEST_PACKAGE` can select a different copy of this source package.

These checks do not certify a provider connection, pinned binary compatibility,
live Docker/MCP behavior, broker host admission, or task quality. Run the
documented doctor and condition-specific smoke commands before an evaluation.
