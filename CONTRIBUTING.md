# Guide to Contributing

Thank you for your interest in improving Wingmen!

If you are thinking of fixing a bug or contributing a feature, please open a Discussion first.
You will be asked for a link to the discussion when you contribute a PR.

TODO: add technical help

## Verification

Run `make verify` before opening a pull request. It runs the full unit and
headless-Textual smoke suite, compiles every source file, and checks that the
lock file is current. UI and ACP changes need a regression test that exercises
the real `WingmenApp` with `run_test`; isolated handler tests alone are not
sufficient for reactive state changes.
