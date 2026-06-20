# Convenience wrapper for src/extract_promises.py (PowerShell).
#
#   .\run.ps1 --demo                       # built-in samples
#   .\run.ps1 --source jsonl:data/sample_rows.jsonl --out output/promises.jsonl
#   .\run.ps1                              # no args -> offline smoke test
#
# Any arguments are forwarded verbatim to the extractor, so every command in the
# README works by replacing `python3 extract_promises.py` with `.\run.ps1`.
$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
$script = "src/extract_promises.py"
$py     = if ($env:PYTHON) { $env:PYTHON }
          elseif (Get-Command python -ErrorAction SilentlyContinue) { "python" }
          else { "python3" }

if ($args.Count -gt 0) {
    & $py $script @args
    exit $LASTEXITCODE
}

# --- no args: offline smoke test (no network, no optional deps) -------------
Write-Host "== 1/2  demo =="
& $py $script --demo

Write-Host "`n== 2/2  sample corpus -> output/promises_sample.jsonl =="
New-Item -ItemType Directory -Force "output" | Out-Null
& $py $script `
    --source jsonl:data/sample_rows.jsonl `
    --sections section_1 section_7 `
    --out output/promises_sample.jsonl `
    --min-score 5

Write-Host "`nSample output (first rows):"
Get-Content output/promises_sample.jsonl -TotalCount 3
