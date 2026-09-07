# Project 11 - n8n Business Automation Pipeline
# Windows users: run these targets' commands directly, or use `make` from Git Bash / WSL.

.DEFAULT_GOAL := help
COMPOSE := docker compose

.PHONY: help up down restart logs ps smoke urls eval eval-fast test-rules reset psql tickets audit workflows check screenshots

help:  ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up:  ## start the whole stack
	$(COMPOSE) up -d
	@echo ""
	@echo "  n8n editor      http://localhost:5678"
	@echo "  console         http://localhost:8501"
	@echo "  mailpit         http://localhost:8025"
	@echo "  itsm api docs   http://localhost:8000/docs"
	@echo ""
	@echo "  First run: import workflows/ and create the two credentials -"
	@echo "  see docs/02_setup_local.md"

down:  ## stop the stack (keeps data)
	$(COMPOSE) down

restart:  ## restart every service
	$(COMPOSE) restart

logs:  ## tail logs from all services
	$(COMPOSE) logs -f --tail=100

ps:  ## show service status
	$(COMPOSE) ps

smoke:  ## send one request end to end and pretty-print the decision
	@curl -s -X POST http://localhost:5678/webhook/it-request \
	  -H 'Content-Type: application/json' \
	  -d '{"requester_email":"arun.mehta@tcs-demo.local","requester_name":"Arun Mehta","subject":"Received a suspicious email asking for my SSO credentials","description":"An email that looks like it is from IT asked me to confirm my SSO password on an external link. Two colleagues clicked it.","category":"security","requested_cost":0}' \
	  | python3 -m json.tool || echo "  no response - is wf_main_triage imported and Active?"

urls:  ## print the browser URLs and the n8n credential values
	@bash .devcontainer/welcome.sh

eval:  ## run the full evaluation suite
	python evaluation/run_eval.py

eval-fast:  ## run only the happy-path and negative cases, serially
	python evaluation/run_eval.py --kind happy_path --kind negative --concurrency 2

test-rules:  ## fast unit test of the decision layer (no Docker, no API key)
	node tools/test_rules.js

reset:  ## DESTRUCTIVE - drop volumes and start clean
	$(COMPOSE) down -v
	$(COMPOSE) up -d

psql:  ## open a psql shell on the application schema
	$(COMPOSE) exec postgres psql -U n8n -d n8n -c "SET search_path TO app,public;" -P pager=off

tickets:  ## print the 20 most recent tickets
	$(COMPOSE) exec -T postgres psql -U n8n -d n8n -P pager=off -c \
	  "SELECT ticket_id, priority, category, queue, status, approval_status, processing_ms FROM app.tickets ORDER BY created_at DESC LIMIT 20;"

audit:  ## print the 30 most recent audit rows
	$(COMPOSE) exec -T postgres psql -U n8n -d n8n -P pager=off -c \
	  "SELECT ts, level, event, node_name, left(message, 60) AS message FROM app.audit_log ORDER BY log_id DESC LIMIT 30;"

workflows:  ## regenerate the workflow exports from tools/build_workflows.py
	python tools/build_workflows.py

check:  ## validate the workflow exports without writing
	python tools/build_workflows.py --check
