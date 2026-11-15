#!/bin/sh
# Clippy installer for macOS and Linux: run it from the cloned folder with  sh install.sh
# It sets up the Python environment, the local LLM and the clippy command. Safe to run again.
set -e
cd "$(dirname "$0")"

step() { printf '\n== %s\n' "$1"; }

step "Python 3.12"
PYTHON="$(command -v python3.12 || command -v python3 || true)"
[ -n "$PYTHON" ] || { echo "Python 3.12 is missing (macOS: brew install python@3.12, Linux: your package manager)"; exit 1; }
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' ||
  { echo "Python 3.12 or newer is needed, found: $("$PYTHON" --version)"; exit 1; }
[ -x .venv/bin/python ] || "$PYTHON" -m venv .venv
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -r requirements.txt
if command -v nvidia-smi >/dev/null 2>&1; then
  step "NVIDIA GPU found: Whisper on the GPU"
  .venv/bin/python -m pip install -r requirements-gpu.txt
fi

command -v node >/dev/null 2>&1 || command -v deno >/dev/null 2>&1 ||
  echo "Node.js or Deno helps YouTube downloads (brew install node / your package manager)."
[ -f .env ] || cp .env.example .env

step "Local LLM (Ollama)"
if command -v ollama >/dev/null 2>&1; then
  ollama pull hf.co/Qwen/Qwen3-14B-GGUF:Q6_K
  ollama create clippy-qwen3:14b-q6k -f models/Modelfile.qwen3-14b-q6k
else
  echo "Ollama is missing: https://ollama.com/download, then run this installer again."
fi

step "The clippy command and tab completion"
chmod +x bin/clippy
bin/clippy install
printf '\nDone. Open a new terminal and type: clippy start\n'
