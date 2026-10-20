$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if ($env:CONDA_DEFAULT_ENV -eq 'medocr' -and $env:CONDA_PREFIX) {
    $pythonPath = Join-Path $env:CONDA_PREFIX 'python.exe'
}
if (Test-Path -LiteralPath $pythonPath) {
    & $pythonPath -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false --server.maxUploadSize 512
} elseif (Get-Command conda -ErrorAction SilentlyContinue) {
    & conda run --no-capture-output -n medocr python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false --server.maxUploadSize 512
} else {
    Write-Host '请按 ENVIRONMENT.md 手动配置环境，或激活 medocr Conda 环境后再启动。'
    exit 1
}
exit $LASTEXITCODE
