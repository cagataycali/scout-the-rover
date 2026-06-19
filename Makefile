# earth-rover-mini — canonical entrypoint. Run `make help` for menu.

SHELL       := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

VENV ?= .venv
PY   ?= $(VENV)/bin/python
PIP  ?= $(VENV)/bin/pip

SDK_DIR ?= $(CURDIR)/earth-rovers-sdk
SDK_URL ?= https://github.com/cagataycali/earth-rovers-sdk.git
SDK_PORT ?= 8001

.PHONY: help
help: ## show this menu
	@awk -F':|##' '/^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$NF}' $(MAKEFILE_LIST)

.PHONY: run
run: venv ## start the rover agent REPL
	@ROVER_AGENT_ID=main $(PY) agent.py

.PHONY: voice
voice: venv-voice ## start the voice agent (laptop mic+speakers)
	@ROVER_AGENT_ID=voice $(PY) voice_agent.py

.PHONY: voice-rover
voice-rover: venv-voice ## start the voice agent THROUGH the rover (mic+speaker over SDK)
	@curl -s -o /dev/null -w "" http://localhost:$(SDK_PORT)/data 2>/dev/null || \
		{ echo "⚠️  SDK not reachable on :$(SDK_PORT). Run 'make sdk-up' first."; exit 1; }
	@$(PY) voice_agent.py --audio rover

.PHONY: dashboard
dashboard: venv ## start the web dashboard (drive scout from a browser)
	@echo "🛞 dashboard → http://localhost:$${DASH_PORT:-8080}  (needs 'make sdk-up' for camera/telemetry)"
	@$(PY) dashboard_server.py

.PHONY: sdk
sdk: $(SDK_DIR)/.cloned ## clone + setup earth-rovers-sdk (our fork)

$(SDK_DIR)/.cloned:
	@test -d $(SDK_DIR) || git clone $(SDK_URL) $(SDK_DIR)
	@cd $(SDK_DIR) && python3.13 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
	@touch $@
	@echo "✅ SDK ready. Configure $(SDK_DIR)/.env then: make sdk-up"

.PHONY: sdk-up
sdk-up: ## start earth-rovers-sdk server on $(SDK_PORT)
	@cd $(SDK_DIR) && SDK_PORT=$(SDK_PORT) nohup .venv/bin/hypercorn main:app --bind 0.0.0.0:$(SDK_PORT) > /tmp/earth-rover-sdk.log 2>&1 & \
	sleep 5 && curl -s -o /dev/null -w "SDK HTTP %{http_code}\n" http://localhost:$(SDK_PORT)

.PHONY: sdk-down
sdk-down: ## stop earth-rovers-sdk server
	@pkill -f "hypercorn main:app" || true

.PHONY: venv
venv: $(VENV)/.installed ## create .venv + install requirements

$(VENV)/.installed: requirements.txt
	@test -d $(VENV) || $(shell command -v python3.13 || command -v python3) -m venv $(VENV)
	@$(PIP) install --quiet --upgrade pip
	@$(PIP) install --quiet -r requirements.txt
	@touch $@
	@echo "✅ venv ready"

.PHONY: venv-voice
venv-voice: venv ## install voice extras (pyaudio)
	@$(PIP) install --quiet -r requirements-voice.txt

.PHONY: test
test: venv ## run tests
	@$(PIP) install --quiet pytest
	@$(PY) -m pytest tests/ -v

.PHONY: shell
shell: venv ## python shell with tools loaded
	@$(PY) -ic "from tools import *; print('loaded ROVER_ALL_TOOLS (%d tools)' % len(ROVER_ALL_TOOLS))"

.PHONY: clean
clean: ## remove venv + caches
	@rm -rf $(VENV) __pycache__ tools/__pycache__ tests/__pycache__ .pytest_cache

.PHONY: telegram
telegram: venv ## start telegram listener (chat with scout from anywhere)
	@ROVER_AGENT_ID=telegram $(PY) telegram_listener.py

.PHONY: thinker
thinker: venv ## start slow-thinker background loop (every 60s by default)
	@ROVER_AGENT_ID=thinker $(PY) thinker_loop.py

.PHONY: merge
merge: venv ## merge per-agent datasets → one unified timeline (DATASET=<dir> or today)
	@DS="$(DATASET)"; \
	if [ -z "$$DS" ]; then DS="datasets/scout__earth-rover-mini-$$(date +%Y%m%d)"; fi; \
	echo "🔗 merging $$DS ..."; \
	$(PY) -m tools.merge_datasets "$$DS"

.PHONY: live
live: venv ## start agent + telegram + thinker concurrently (Ctrl+C to stop all)
	@trap 'kill 0' INT TERM EXIT; \
	$(PY) telegram_listener.py & \
	$(PY) thinker_loop.py & \
	$(PY) agent.py; \
	wait

# PERSISTENCE — run scout's telegram listener + thinker as durable OS services.
# Cross-platform: launchd (macOS) / systemd user units (Linux).
# Generates unit files from THIS dir + venv, so paths are always correct.

UNAME_S    := $(shell uname -s)
ABS_PY     := $(CURDIR)/$(PY)
ABS_ENV    := $(CURDIR)/.env
LABEL_PFX  ?= com.cagatay.scout

# macOS paths
LA_DIR     := $(HOME)/Library/LaunchAgents
LOG_DIR    := $(HOME)/Library/Logs/earth-rover-mini

# Linux paths
SD_DIR     := $(HOME)/.config/systemd/user

.PHONY: persist
persist: persist-telegram persist-thinker ## install BOTH services as durable (launchd/systemd)
	@echo "✅ scout persisted (telegram + thinker)."

.PHONY: persist-telegram
persist-telegram: venv ## install telegram listener as a durable service
	@$(MAKE) --no-print-directory _persist NAME=telegram SCRIPT=telegram_listener.py DESC="Telegram listener" EXTRA_ENV=""

.PHONY: persist-thinker
persist-thinker: venv ## install slow-thinker loop as a durable service
	@$(MAKE) --no-print-directory _persist NAME=thinker SCRIPT=thinker_loop.py DESC="slow-thinker loop" EXTRA_ENV="THINKER_INTERVAL=60"

# internal: generate + install one unit. params: NAME SCRIPT DESC EXTRA_ENV
.PHONY: _persist
_persist:
	@mkdir -p "$(LOG_DIR)"
ifeq ($(UNAME_S),Darwin)
	@mkdir -p "$(LA_DIR)"; \
	PLIST="$(LA_DIR)/$(LABEL_PFX)-$(NAME).plist"; \
	{ \
	  echo '<?xml version="1.0" encoding="UTF-8"?>'; \
	  echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'; \
	  echo '<plist version="1.0">'; \
	  echo '<dict>'; \
	  echo '    <key>Label</key>'; \
	  echo "    <string>$(LABEL_PFX)-$(NAME)</string>"; \
	  echo '    <key>WorkingDirectory</key>'; \
	  echo "    <string>$(CURDIR)</string>"; \
	  echo '    <key>ProgramArguments</key>'; \
	  echo '    <array>'; \
	  echo "        <string>$(ABS_PY)</string>"; \
	  echo "        <string>$(CURDIR)/$(SCRIPT)</string>"; \
	  echo '    </array>'; \
	  echo '    <key>EnvironmentVariables</key>'; \
	  echo '    <dict>'; \
	  echo '        <key>PATH</key>'; \
	  echo "        <string>$(CURDIR)/$(VENV)/bin:/usr/local/bin:/usr/bin:/bin</string>"; \
	  if [ -n "$(EXTRA_ENV)" ]; then \
	    K=$$(echo "$(EXTRA_ENV)" | cut -d= -f1); V=$$(echo "$(EXTRA_ENV)" | cut -d= -f2-); \
	    echo "        <key>$$K</key>"; echo "        <string>$$V</string>"; \
	  fi; \
	  echo '    </dict>'; \
	  echo '    <key>RunAtLoad</key>'; \
	  echo '    <true/>'; \
	  echo '    <key>KeepAlive</key>'; \
	  echo '    <dict>'; \
	  echo '        <key>SuccessfulExit</key>'; echo '        <false/>'; \
	  echo '        <key>Crashed</key>'; echo '        <true/>'; \
	  echo '    </dict>'; \
	  echo '    <key>ThrottleInterval</key>'; \
	  echo '    <integer>10</integer>'; \
	  echo '    <key>StandardOutPath</key>'; \
	  echo "    <string>$(LOG_DIR)/scout-$(NAME).log</string>"; \
	  echo '    <key>StandardErrorPath</key>'; \
	  echo "    <string>$(LOG_DIR)/scout-$(NAME).err</string>"; \
	  echo '</dict>'; \
	  echo '</plist>'; \
	} > "$$PLIST"; \
	echo "📝 wrote $$PLIST"; \
	printf "   start scout-$(NAME) now & keep durable across restarts? (y/n) "; \
	read ans; \
	if [ "$$ans" = "y" ] || [ "$$ans" = "Y" ]; then \
	  launchctl unload "$$PLIST" 2>/dev/null || true; \
	  launchctl load -w "$$PLIST" && echo "🚀 scout-$(NAME) loaded (RunAtLoad=durable)."; \
	else \
	  echo "💤 plist written but not loaded. Load later: launchctl load -w $$PLIST"; \
	fi
else
	@mkdir -p "$(SD_DIR)"; \
	UNIT="$(SD_DIR)/scout-$(NAME).service"; \
	{ \
	  echo '[Unit]'; \
	  echo "Description=scout — $(DESC) (Earth Rover Mini)"; \
	  echo 'After=network-online.target'; \
	  echo 'Wants=network-online.target'; \
	  echo ''; \
	  echo '[Service]'; \
	  echo 'Type=simple'; \
	  echo "WorkingDirectory=$(CURDIR)"; \
	  echo "EnvironmentFile=-$(ABS_ENV)"; \
	  if [ -n "$(EXTRA_ENV)" ]; then echo "Environment=$(EXTRA_ENV)"; fi; \
	  echo "ExecStart=$(ABS_PY) $(CURDIR)/$(SCRIPT)"; \
	  echo 'Restart=on-failure'; \
	  echo 'RestartSec=10'; \
	  echo 'StandardOutput=journal'; \
	  echo 'StandardError=journal'; \
	  echo ''; \
	  echo '[Install]'; \
	  echo 'WantedBy=default.target'; \
	} > "$$UNIT"; \
	echo "📝 wrote $$UNIT"; \
	systemctl --user daemon-reload; \
	printf "   start scout-$(NAME) now & keep durable across restarts? (y/n) "; \
	read ans; \
	if [ "$$ans" = "y" ] || [ "$$ans" = "Y" ]; then \
	  systemctl --user enable --now scout-$(NAME).service && \
	  echo "🚀 scout-$(NAME) enabled+started. (run 'loginctl enable-linger $$USER' for boot w/o login)"; \
	else \
	  echo "💤 unit written but not enabled. Enable later: systemctl --user enable --now scout-$(NAME)"; \
	fi
endif

.PHONY: unpersist
unpersist: ## stop + remove BOTH durable services
ifeq ($(UNAME_S),Darwin)
	@for n in telegram thinker; do \
	  P="$(LA_DIR)/$(LABEL_PFX)-$$n.plist"; \
	  launchctl unload "$$P" 2>/dev/null || true; \
	  rm -f "$$P" && echo "🗑  removed $$P"; \
	done
else
	@for n in telegram thinker; do \
	  systemctl --user disable --now scout-$$n.service 2>/dev/null || true; \
	  rm -f "$(SD_DIR)/scout-$$n.service" && echo "🗑  removed scout-$$n.service"; \
	done; \
	systemctl --user daemon-reload
endif
	@echo "✅ scout unpersisted."

.PHONY: persist-status
persist-status: ## show status of durable services
ifeq ($(UNAME_S),Darwin)
	@launchctl list | grep "$(LABEL_PFX)" || echo "no scout services loaded"
else
	@systemctl --user status scout-telegram scout-thinker --no-pager || true
endif

.PHONY: persist-logs
persist-logs: ## tail logs of durable services
ifeq ($(UNAME_S),Darwin)
	@tail -n 40 -f "$(LOG_DIR)"/scout-*.log "$(LOG_DIR)"/scout-*.err 2>/dev/null || echo "no logs yet at $(LOG_DIR)"
else
	@journalctl --user -u scout-telegram -u scout-thinker -n 40 -f
endif
