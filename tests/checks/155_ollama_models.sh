#!/bin/bash
# Tests-paths: libs/ollama_common.py scripts/install_ollama scripts/install_apertus scripts/install_codellama scripts/install_deepseek scripts/install_mistral scripts/install_qwen scripts/install_starcoder2
# libs/ollama_common.py and the add-ons that use it: one Ollama release per kcluster that pulls every model its
# add-ons ask for, whichever of them runs. SSH and Helm are faked.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/155_ollama_models_test.py
