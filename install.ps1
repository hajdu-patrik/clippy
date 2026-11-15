# Clippy installer for Windows: run it from the cloned folder with
#   powershell -ExecutionPolicy Bypass -File install.ps1
# It sets up the Python environment, the local LLM and the clippy command. Safe to run again.
param([switch]$NoGpu, [switch]$Studio)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }

Step "Python 3.12"
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { throw "Python 3.12 is missing: winget install Python.Python.3.12" }
if (-not (Test-Path .venv\Scripts\python.exe)) { py -3.12 -m venv .venv }
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $python -m pip install --upgrade pip --quiet
& $python -m pip install -r requirements.txt

$nvidia = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($nvidia -and -not $NoGpu) {
    Step "NVIDIA GPU found: Whisper on the GPU"
    & $python -m pip install -r requirements-gpu.txt
}
if ($Studio) {
    Step "Studio extras (voice isolation, word alignment, about 3 GB)"
    & $python -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
    & $python -m pip install -r requirements-ml.txt
}

if (-not (Get-Command node -ErrorAction SilentlyContinue) -and -not (Get-Command deno -ErrorAction SilentlyContinue)) {
    Write-Host "Node.js or Deno helps YouTube downloads: winget install OpenJS.NodeJS.LTS" -ForegroundColor Yellow
}
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

Step "Local LLM (Ollama)"
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    ollama pull hf.co/Qwen/Qwen3-14B-GGUF:Q6_K
    ollama create clippy-qwen3:14b-q6k -f models\Modelfile.qwen3-14b-q6k
} else {
    Write-Host "Ollama is missing: winget install Ollama.Ollama, then run this installer again." -ForegroundColor Yellow
}

Step "The clippy command and tab completion"
& bin\clippy.cmd install
Write-Host "`nDone. Open a new terminal and type: clippy start" -ForegroundColor Green
