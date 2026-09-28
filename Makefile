.PHONY: help test test-verbose test-file diagnostics

PYTHON ?= python3
TEST ?= tests

help:
	@printf '%s\n' \
		'make test          Run the complete unit-test suite' \
		'make test-verbose  Run every test and print each test name' \
		'make test-file TEST=tests.test_bridge.RoutingTests.test_temp_is_one_shot_route' \
		'make diagnostics   Run local checks and inspect the Docker Compose service'

test:
	$(PYTHON) -m unittest discover -s tests -v

test-verbose: test

test-file:
	$(PYTHON) -m unittest -v $(TEST)

diagnostics:
	PYTHON="$(PYTHON)" ./scripts/diagnostics.sh
