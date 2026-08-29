.PHONY: run test verify rust-test rust-tmux diff-check

run:
	cargo run -p codeswarm-cli -- $(ARGS)

# Cargo is the canonical test runner for this Rust-only repository.
test:
	cargo test --workspace

rust-test:
	cargo fmt --all -- --check
	cargo test --workspace
	cargo clippy --workspace --all-targets -- -D warnings

rust-tmux:
	bash tests/tmux/smoke.sh
	bash tests/tmux/store.sh
	bash tests/tmux/config.sh
	bash tests/tmux/shell.sh
	bash tests/tmux/performance.sh

diff-check:
	git diff --check HEAD

verify: diff-check rust-test rust-tmux
