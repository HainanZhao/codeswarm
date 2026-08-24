
run := .venv/bin/wingmen
python := PYTHONPATH=src .venv/bin/python

.PHONY: run
run:
	$(run)

.PHONY: test
test:
	$(python) -m unittest discover -s tests -v

.PHONY: compile
compile:
	$(python) -m compileall -q src tests

.PHONY: lock
lock:
	uv lock --check --python .venv/bin/python

.PHONY: typecheck
typecheck:
	.venv/bin/mypy src/wingmen/acp/relay.py src/wingmen/session.py src/wingmen/mode_policy.py src/wingmen/settings.py src/wingmen/settings_schema.py src/wingmen/agents.py --follow-imports=skip --ignore-missing-imports

.PHONY: package
package:
	.venv/bin/wingmen --version
	$(python) scripts/verify_package.py

.PHONY: verify
verify: diff-check package test compile lock typecheck

.PHONY: diff-check
diff-check:
	git diff --check HEAD

.PHONY: gemini-acp
gemini-acp:
	$(run) acp "gemini --experimental-acp" --project-dir ~/sandbox --title "Google Gemini"

.PHONY: claude-acp
claude-acp:
	$(run) acp "claude-code-acp" --project-dir ~/sandbox --title "Claude"


.PHONY: codex-acp
codex-acp:
	$(run) acp "codex-acp"  --project-dir ~/sandbox --title="OpenAI Codex"

.PHONY: replay
replay:
	ACP_INITIALIZE=0 $(run) acp "$(run) replay $(realpath replay.jsonl)" --project-dir ~/sandbox

.PHONY: echo
echo:
	$(run) acp "uv run echo_client.py"
