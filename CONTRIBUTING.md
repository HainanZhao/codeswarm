# Guide to Contributing

Thank you for your interest in improving CodeSwarm!

If you are thinking of fixing a bug or contributing a feature, please open a Discussion first.
You will be asked for a link to the discussion when you contribute a PR.

TODO: add technical help

## Verification

Run `make verify` before opening a pull request. It runs the full unit and
headless-Textual smoke suite, compiles every source file, and checks that the
lock file is current. UI and ACP changes need a regression test that exercises
the real `CodeSwarmApp` with `run_test`; isolated handler tests alone are not
sufficient for reactive state changes.

### Textual layout and virtualization tests

Textual lays out widgets asynchronously. A transcript must not be virtualized
until its initially mounted message blocks have non-zero layout heights;
otherwise spacer rows are based on provisional measurements and the scroll
range can change when an older message is remounted. The virtualizer also
keeps its current guard window mounted during small scrolls, so late Markdown
measurement does not trigger an unnecessary widget-tree rebuild.

When adding or changing transcript tests:

- settle the app with `await pilot.pause(...)` before asserting mounted
  windows or scroll ranges;
- use `CodeSwarmApp.run_test` at a fixed terminal size;
- assert logical message retention and bounded mounting, rather than relying
  on a specific platform's exact row count;
- run the virtualization regressions repeatedly when investigating CI-only
  failures, then run `make verify` before pushing.
