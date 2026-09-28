#!/bin/sh

# Collect reproducible checks without printing .env or Docker environment values.
# A failed check is reported while later checks continue, so one run can expose
# multiple problems. The script exits non-zero if any required check fails.

set -u

PYTHON=${PYTHON:-python3}
DIAG_LOG_LINES=${DIAG_LOG_LINES:-100}
status=0

heading() {
    printf '\n== %s ==\n' "$1"
}

run_check() {
    description=$1
    shift
    printf '\n-- %s\n' "$description"
    if "$@"; then
        printf 'PASS: %s\n' "$description"
    else
        result=$?
        printf 'FAIL (%s): %s\n' "$result" "$description"
        status=1
    fi
}

heading "Runtime"
run_check "Python is available" "$PYTHON" --version
run_check "Bridge sources compile" "$PYTHON" -m py_compile bridge.py conversation.py
run_check "Unit tests" "$PYTHON" -m unittest discover -s tests -v

heading "Configuration"
if [ -f .env ]; then
    printf 'PASS: .env exists (contents hidden)\n'
else
    printf 'FAIL: .env is missing; copy .env.example to .env and configure it\n'
    status=1
fi

if command -v docker >/dev/null 2>&1; then
    run_check "Docker daemon is reachable" sh -c 'docker info >/dev/null'
    run_check "Docker Compose configuration is valid" docker compose config --quiet

    heading "Compose service status"
    docker compose ps || status=1

    heading "Recent bridge logs (last ${DIAG_LOG_LINES} lines)"
    printf 'Review ERROR/FAIL/timeout/ACK messages; logs can contain message text.\n'
    docker compose logs --no-color --tail="$DIAG_LOG_LINES" meshcore-openwebui-bridge || status=1
else
    printf 'FAIL: docker is not installed or is not on PATH\n'
    status=1
fi

heading "Result"
if [ "$status" -eq 0 ]; then
    printf 'All diagnostic checks passed.\n'
else
    printf 'One or more checks failed. Review the first failure and nearby logs above.\n'
fi

exit "$status"
