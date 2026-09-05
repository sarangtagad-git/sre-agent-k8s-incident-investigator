# Bring up Docker Desktop (if needed), then the k3d cluster, Opik, the Boutique
# app port-forward, and the Streamlit dashboard - the one command to run before
# a demo/recording session.
#
# Usage (from PowerShell, anywhere):
#   & "D:\Claude Code\SRE Agent - K8S Incident Investigator\scripts\start-env.ps1"
#
# Docker Desktop must be launched from Windows, so this script owns that part;
# everything else happens in scripts/start-env.sh inside WSL, which this calls.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads a .ps1 with no
# BOM using the system ANSI codepage, not UTF-8 - a stray em-dash or curly
# quote gets mangled into multi-byte mojibake that breaks the parser outright
# (hit this once already: "Missing closing ')' in expression" from a single
# em-dash inside a string literal).

$ErrorActionPreference = "Stop"
$repoWinPath = "D:\Claude Code\SRE Agent - K8S Incident Investigator"
$repoWslPath = "/mnt/d/Claude Code/SRE Agent - K8S Incident Investigator"

Write-Host "== Docker Desktop ==" -ForegroundColor Cyan
$dockerUp = $false
try {
    wsl.exe -e bash -lc "docker info" *> $null
    if ($LASTEXITCODE -eq 0) { $dockerUp = $true }
} catch {}

if (-not $dockerUp) {
    Write-Host "  not reachable from WSL - launching Docker Desktop..."
    Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    Write-Host "  waiting for it to come up (this can take a couple minutes)..."
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 5
        wsl.exe -e bash -lc "docker info" *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Write-Host "." -NoNewline
    }
    Write-Host ""
    if (-not $ready) {
        Write-Host "  Docker still not reachable after 5 min. Check Docker Desktop manually (this laptop has a known issue where WSL integration silently disables itself after a restart - Settings > Resources > WSL Integration)." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "  Docker is up." -ForegroundColor Green
} else {
    Write-Host "  already up." -ForegroundColor Green
}

Write-Host "== Handing off to WSL for the rest (k3d, Opik, port-forward, dashboard) ==" -ForegroundColor Cyan
wsl.exe -e bash -lc "cd '$repoWslPath' && bash scripts/start-env.sh"
