#!/usr/bin/env bash
# Prints the live browser URLs, the n8n credential values you need to type in,
# and what to do next.  Run any time:   bash tools/show_urls.sh   (or: make urls)
set -uo pipefail
cd "$(dirname "$0")/.."

B=$'\033[1m'; D=$'\033[2m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; O=$'\033[0m'

if [[ -f .runtime/urls ]]; then
  source .runtime/urls
elif [[ -f .devcontainer/.urls ]]; then
  source .devcontainer/.urls          # layout used by earlier versions
fi

# Fall back to deriving them, in case .env exists but the urls file does not.
if [[ -z "${N8N_URL:-}" && -f .env ]]; then
  N8N_URL="$(grep -E '^WEBHOOK_URL=' .env | head -1 | cut -d= -f2- | sed 's:/*$::')"
  MAIL_URL="$(grep -E '^MAILPIT_URL=' .env | head -1 | cut -d= -f2-)"
  PG_PW="$(grep -E '^POSTGRES_PASSWORD=' .env | head -1 | cut -d= -f2-)"
  UI_URL="${N8N_URL/-5678./-8501.}"
  [[ "$UI_URL" == "$N8N_URL" ]] && UI_URL="http://localhost:8501"
fi

N8N_URL="${N8N_URL:-http://localhost:5678}"
UI_URL="${UI_URL:-http://localhost:8501}"
MAIL_URL="${MAIL_URL:-http://localhost:8025}"
PG_PW="${PG_PW:-see POSTGRES_PASSWORD in .env}"

running=$(docker compose ps --services --filter status=running 2>/dev/null | wc -l | tr -d ' ')

printf '\n%s' "$B"
printf '===============================================================================\n'
printf '  Project 11 - n8n Business Automation Pipeline   (TCS / Enterprise IT)\n'
printf '===============================================================================%s\n' "$O"

if [[ "$running" -ge 5 ]]; then
  printf '\n  %sStack is running (%s services)%s\n' "$G" "$running" "$O"
else
  printf '\n  %sStack not running%s  -  start it:  %sdocker compose up -d%s\n' "$Y" "$O" "$C" "$O"
fi

printf '\n  %sOpen in a browser tab%s  (Ctrl/Cmd-click, or use the PORTS panel)\n' "$B" "$O"
printf '    n8n editor      %s%s%s\n' "$C" "$N8N_URL" "$O"
printf '    Ops console     %s%s%s\n' "$C" "$UI_URL" "$O"
printf '    Mailpit inbox   %s%s%s   %s<- every notification lands here%s\n' "$C" "$MAIL_URL" "$O" "$D" "$O"

if grep -q '^OPENAI_API_KEY=sk-replace-me' .env 2>/dev/null; then
  printf '\n  %s! OPENAI_API_KEY is not set%s\n' "$Y" "$O"
  printf '    Repo -> Settings -> Secrets and variables -> Codespaces -> New secret\n'
  printf '    Name OPENAI_API_KEY, then:  bash tools/make_env.sh && docker compose up -d\n'
fi

printf '\n  %sn8n credentials to create%s  (names must match EXACTLY)\n' "$B" "$O"
printf '    %sPostgres - Project 11%s\n' "$C" "$O"
printf '      host %spostgres%s   port 5432   database n8n   user n8n   schema %sapp%s   SSL off\n' "$G" "$O" "$G" "$O"
printf '      password  %s%s%s\n' "$G" "$PG_PW" "$O"
printf '    %sSMTP - Mailpit (local)%s\n' "$C" "$O"
printf '      host %smailpit%s    port 1025   no user, no password, TLS off\n' "$G" "$O"

printf '\n  %sNext%s\n' "$B" "$O"
if [[ "$running" -lt 5 ]]; then
  printf '    1.  docker compose up -d\n'
  printf '    2.  bash tools/show_urls.sh\n'
else
  printf '    1.  Open the n8n editor, create the owner account\n'
  printf '    2.  Import all four files from workflows/  (wf_error_handler FIRST)\n'
  printf '    3.  Create the two credentials above\n'
  printf '    4.  Activate wf_main_triage, wf_approval, wf_sla_sweep\n'
  printf '    5.  make smoke     # one request end to end\n'
  printf '    6.  make eval      # the 30-case evaluation\n'
fi

printf '\n  %sFull walkthrough: docs/08_codespaces.md%s\n\n' "$D" "$O"
