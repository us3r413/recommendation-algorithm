<#
.SYNOPSIS
    Deploy the recommendation API to an EC2 instance via SCP + SSH.

.DESCRIPTION
    This script:
    1. Uploads source code (src/, app.py, requirements.txt, .env) to the EC2 instance
    2. Uploads dataset CSV files
    3. Runs the remote setup script (setup-ec2.sh) to install deps and start the server

.PARAMETER KeyFile
    Path to your .pem SSH key file.

.PARAMETER Host
    Public IP or DNS of the EC2 instance.

.PARAMETER User
    SSH username (default: ec2-user for Amazon Linux).

.PARAMETER RemoteDir
    Remote directory to deploy into (default: ~/recommendation-algorithm).

.PARAMETER SkipDataset
    Skip uploading dataset files (useful for redeployment of code-only changes).

.EXAMPLE
    .\deploy-to-ec2.ps1 -KeyFile "C:\keys\my-key.pem" -Host "54.123.45.67"
    .\deploy-to-ec2.ps1 -KeyFile "C:\keys\my-key.pem" -Host "54.123.45.67" -SkipDataset
#>

param(
    [Parameter(Mandatory=$true)]
    [string]$KeyFile,

    [Parameter(Mandatory=$true)]
    [string]$RemoteHost,

    [string]$User = "ec2-user",

    [string]$RemoteDir = "~/recommendation-algorithm",

    [switch]$SkipDataset
)

$ErrorActionPreference = "Stop"

# Resolve project root (one level up from this script)
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path "$ProjectRoot\app.py")) {
    # Fallback: script might be run from project root
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
    if (-not (Test-Path "$ProjectRoot\app.py")) {
        $ProjectRoot = Get-Location
    }
}

Write-Host "============================================" -ForegroundColor Cyan
Write-Host " Job Recommendation API - EC2 Deployment" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Project root : $ProjectRoot"
Write-Host "Target       : $User@$RemoteHost"
Write-Host "Remote dir   : $RemoteDir"
Write-Host "Skip dataset : $SkipDataset"
Write-Host ""

# Verify key file exists
if (-not (Test-Path $KeyFile)) {
    Write-Error "Key file not found: $KeyFile"
    exit 1
}

# SSH/SCP options
$SshOpts = @("-i", $KeyFile, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null")
$Target = "$User@$RemoteHost"

function Invoke-SSH {
    param([string]$Command)
    Write-Host "  [SSH] $Command" -ForegroundColor DarkGray
    ssh @SshOpts $Target $Command
    if ($LASTEXITCODE -ne 0) { Write-Error "SSH command failed: $Command"; exit 1 }
}

function Invoke-SCP {
    param([string]$Local, [string]$Remote)
    Write-Host "  [SCP] $Local -> $Remote" -ForegroundColor DarkGray
    scp @SshOpts -r $Local "${Target}:${Remote}"
    if ($LASTEXITCODE -ne 0) { Write-Error "SCP failed: $Local"; exit 1 }
}

# --- Step 1: Create remote directory structure ---
Write-Host "`n[1/4] Creating remote directories..." -ForegroundColor Yellow
Invoke-SSH "mkdir -p $RemoteDir/src $RemoteDir/dataset $RemoteDir/infra"

# --- Step 2: Upload source code ---
Write-Host "`n[2/4] Uploading source code..." -ForegroundColor Yellow

# Core files
Invoke-SCP "$ProjectRoot\app.py" "$RemoteDir/app.py"
Invoke-SCP "$ProjectRoot\requirements.txt" "$RemoteDir/requirements.txt"
Invoke-SCP "$ProjectRoot\.env" "$RemoteDir/.env"

# src/ directory
Invoke-SCP "$ProjectRoot\src" "$RemoteDir/"

# Remote setup script
Invoke-SCP "$ProjectRoot\infra\setup-ec2.sh" "$RemoteDir/infra/setup-ec2.sh"

# --- Step 3: Upload dataset (unless skipped) ---
if (-not $SkipDataset) {
    Write-Host "`n[3/4] Uploading dataset files..." -ForegroundColor Yellow

    $DatasetFiles = @(
        "職缺.csv",
        "職務對照表.csv",
        "城市對照表.csv",
        "瀏覽次數.csv",
        "userBehaviorFeature.csv",
        "userBehaviorEvents.csv"
    )

    foreach ($file in $DatasetFiles) {
        $localPath = "$ProjectRoot\dataset\$file"
        if (Test-Path $localPath) {
            Invoke-SCP $localPath "$RemoteDir/dataset/$file"
        } else {
            Write-Warning "Dataset file not found (skipping): $localPath"
        }
    }

    # Optional: graph cache
    $graphCache = "$ProjectRoot\dataset\graph_cache.pkl"
    if (Test-Path $graphCache) {
        Write-Host "  Uploading graph_cache.pkl..." -ForegroundColor DarkGray
        Invoke-SCP $graphCache "$RemoteDir/dataset/graph_cache.pkl"
    }
} else {
    Write-Host "`n[3/4] Skipping dataset upload (--SkipDataset)" -ForegroundColor DarkYellow
}

# --- Step 4: Run remote setup ---
Write-Host "`n[4/4] Running remote setup script..." -ForegroundColor Yellow
Invoke-SSH "chmod +x $RemoteDir/infra/setup-ec2.sh && bash $RemoteDir/infra/setup-ec2.sh"

# --- Done ---
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host " Deployment complete!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "API endpoint:  http://${RemoteHost}:8000" -ForegroundColor Cyan
Write-Host "Health check:  http://${RemoteHost}:8000/health" -ForegroundColor Cyan
Write-Host "Swagger docs:  http://${RemoteHost}:8000/docs" -ForegroundColor Cyan
Write-Host ""
Write-Host "Test command:" -ForegroundColor White
Write-Host "  curl -X POST http://${RemoteHost}:8000/recommend -H 'Content-Type: application/json' -d '{`"query`": `"台北 前端工程師`", `"talent_no`": 0}'"
Write-Host ""
