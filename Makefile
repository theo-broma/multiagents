.DEFAULT_GOAL := help
.PHONY: help install check test build run init clean uninstall

UV ?= uv

help:  ## show this help
	@echo "multiagents"
	@echo
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "} {printf "  \033[1m%-10s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Start with 'make install', then 'make check'."

install:  ## install the package and prepare this machine to launch the MCP
	@command -v $(UV) >/dev/null || { \
		echo "uv is not installed — see https://docs.astral.sh/uv/"; exit 1; }
	$(UV) sync
	@# `uv sync` only populates .venv, so the command would exist nowhere but
	@# this directory. A tool install puts it on PATH; --editable so it tracks
	@# the source, and --force so re-running install is idempotent.
	$(UV) tool install --editable --force . >/dev/null
	@# Seeding writes the editable defaults to ~/.config/multiagents/ and the
	@# MCP registration the launcher points at. Both are idempotent.
	@$(UV) run python -c "from multiagents.config import seed_global; print('config      ' + str(seed_global()))"
	@$(UV) run multiagents mcp-config >/dev/null
	@$(UV) run python -c "from multiagents.paths import global_config_dir; \
		print('mcp         ' + str(global_config_dir() / 'mcp.json'))"
	@printf 'command     '; command -v multiagents \
		|| { echo "installed, but NOT on your PATH"; \
		     echo '             add uv'"'"'s bin directory to PATH, then reopen your shell:'; \
		     echo "               $(UV) tool update-shell"; }
	@echo
	@echo "Installed. Next:"
	@echo "  make check                     # are the agent CLIs present and authenticated?"
	@echo "  cd <your project> && multiagents init"

check:  ## report CLIs, agents, authentication, budget and git readiness
	@$(UV) run multiagents doctor

test:  ## run the test suite
	$(UV) run pytest tests/ -q

init:  ## initialise the CURRENT directory as a multiagents project
	@$(UV) run multiagents init

build:  ## build the container environment and authenticate every provider
	@$(UV) run multiagents build

run:  ## launch the orchestrator for the current project
	@$(UV) run multiagents run

clean:  ## remove build and test caches (leaves project state alone)
	@rm -rf .pytest_cache build dist *.egg-info
	@find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
	@echo "caches removed"

uninstall:  ## remove this machine's global config and agent state
	@# Delegated rather than rm -rf'd here: the command checks for uncommitted
	@# work in agent worktrees first and prunes the stale registrations after,
	@# neither of which a Makefile recipe should be trying to do. It runs
	@# before the tool is removed, because removing the tool removes it.
	@$(UV) run multiagents uninstall $(if $(FORCE),--force,)
	@-$(UV) tool uninstall multiagents 2>/dev/null || true
	@# Single quotes: backticks in a recipe are command substitution, and this
	@# line previously ran `make install` in the middle of uninstalling.
	@echo 'the multiagents command is gone too. `make install` puts it back.'
