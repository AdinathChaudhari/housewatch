# housewatch — thin wrappers over the real commands. Run `make help` (or
# just `make`) to see what's here. Nothing in this file duplicates logic:
# install/uninstall/purge call the real scripts, and everything else calls
# `python -m housewatch <cmd>` or `launchctl` directly.

SHELL := /bin/bash
.SHELLFLAGS := -e -o pipefail -c

HOUSEWATCH_HOME := $(CURDIR)
PYTHON ?= $(shell \
  if [ -n "$$HOUSEWATCH_PYTHON" ]; then echo "$$HOUSEWATCH_PYTHON"; \
  elif [ -x "$(CURDIR)/.venv/bin/python3" ]; then echo "$(CURDIR)/.venv/bin/python3"; \
  else command -v python3; fi)
LABEL := com.housewatch.collector
DOMAIN := gui/$(shell id -u)
TARGET := $(DOMAIN)/$(LABEL)
LOG_DIR := $(HOME)/Library/Logs/housewatch

# Belt-and-suspenders: `python -m housewatch` finds the package via
# WorkingDirectory when launchd runs it, but `make` may be invoked from
# elsewhere, so every recipe below gets an explicit PYTHONPATH.
export PYTHONPATH := $(HOUSEWATCH_HOME)

.DEFAULT_GOAL := help
.PHONY: help install uninstall purge start stop restart status logs test serve

help:
	@echo "housewatch — make targets:"
	@echo "  install    install + load the LaunchAgent (./install.sh)"
	@echo "  uninstall  stop + unload the LaunchAgent; config/db left alone"
	@echo "  purge      stop + unload AND delete config/db (./uninstall.sh --purge)"
	@echo "  start      load the already-installed LaunchAgent"
	@echo "  stop       unload the LaunchAgent (launchctl bootout)"
	@echo "  restart    stop then start (never kickstart -k)"
	@echo "  status     today's usage / cycle usage vs cap / last-sample age"
	@echo "  logs       tail the collector's logs"
	@echo "  test       run the pytest suite"
	@echo "  serve      run the local web dashboard in the foreground"

install:
	"$(HOUSEWATCH_HOME)/install.sh"

uninstall:
	"$(HOUSEWATCH_HOME)/uninstall.sh"

purge:
	"$(HOUSEWATCH_HOME)/uninstall.sh" --purge

start:
	@if launchctl print "$(TARGET)" >/dev/null 2>&1; then \
		echo "$(LABEL) is already loaded."; \
	else \
		launchctl bootstrap "$(DOMAIN)" "$(HOME)/Library/LaunchAgents/$(LABEL).plist"; \
		echo "started $(LABEL)."; \
	fi

stop:
	@launchctl bootout "$(TARGET)" 2>/dev/null && echo "stopped $(LABEL)." || echo "$(LABEL) was not loaded."

restart: stop start

status:
	"$(PYTHON)" -m housewatch status

logs:
	@if ls "$(LOG_DIR)"/*.log >/dev/null 2>&1; then \
		tail -n 100 -f "$(LOG_DIR)"/*.log; \
	else \
		echo "no logs yet at $(LOG_DIR) — run 'make install' and let it poll first."; \
	fi

test:
	"$(PYTHON)" -m pytest "$(HOUSEWATCH_HOME)/tests" -q

serve:
	"$(PYTHON)" -m housewatch serve
