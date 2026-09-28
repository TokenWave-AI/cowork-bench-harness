# Offline condition checks

From this harness package:

```bash
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

These checks use no model APIs or Docker. A loopback HTTP test verifies that
Oracle requests carrying CoWork tools are rejected before an upstream request.
Other checks cover exact native registries, the three Goal tools, broker
`task_instruction` on rows 039/093, binding to the durable run condition, and
Oracle Goal instructions. Temporary files stay under `tests/.tmp` and are
removed after each test.

Startup regressions verify that a precisely bound empty/pending MCP initialization
fails its attempt and can resume the same session under the original deadline.
Wrong identities, extra servers, early tool activity, native results, and expired
or stopped runs remain rejected. The native CLI streams are simulated; these
tests do not launch the CLI. The same checks cover CoWork and Oracle conditions.

Live CLI/provider compatibility and native Docker/broker admission still need
the documented condition-specific smoke checks.
