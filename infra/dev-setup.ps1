# One-command local setup for Gazi (Windows PowerShell 5.1+ or PowerShell 7). Run from the AG-SaaS folder:
#
#   powershell -ExecutionPolicy Bypass -File infra/dev-setup.ps1
#
# It starts Supabase, writes infra/.env (keeping an existing master key), starts Redis and the API,
# waits until the API is ready, creates a local demo user and prints a sign-in token for the API docs page.
# Safe to run again at any time. LOCAL DEVELOPMENT ONLY.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$EnvFile = "infra/.env"
$DemoEmail = "demo@gazi.local"
$DemoPassword = "gazi-demo-password"
$AuthUrl = "http://127.0.0.1:54321/auth/v1"

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host ""; Write-Host "Stopped: $text" -ForegroundColor Red; exit 1 }

function Set-EnvValue([string[]]$Lines, [string]$Name, [string]$Value) {
    $found = $false
    $out = foreach ($line in $Lines) {
        if ($line -like "$Name=*") { $found = $true; "$Name=$Value" } else { $line }
    }
    if (-not $found) { $out = @($out) + "$Name=$Value" }
    return , @($out)
}

Step "1/5 Checking Docker"
docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker isn't running. Open Docker Desktop, wait for it to start, then run this again." }

Step "2/5 Starting Supabase (the first run downloads for a few minutes)"
npx -y supabase start --workdir infra
if ($LASTEXITCODE -ne 0) { Fail "Supabase didn't start. Read the message above, or run: npx supabase stop --all" }

$status = @{}
foreach ($line in (npx -y supabase status --workdir infra -o env)) {
    if ($line -match '^([A-Z_]+)="?(.*?)"?$') { $status[$Matches[1]] = $Matches[2] }
}
foreach ($key in "ANON_KEY", "JWT_SECRET", "SERVICE_ROLE_KEY") {
    if (-not $status[$key]) { Fail "Couldn't read $key from 'npx supabase status'." }
}

Step "3/5 Writing infra/.env"
if (Test-Path $EnvFile) { $lines = @(Get-Content $EnvFile) } else { $lines = @(Get-Content "infra/.env.example") }
$kekLine = $lines | Where-Object { $_ -like "GAZI_KEK=?*" } | Select-Object -First 1
if ($kekLine) {
    Write-Host "Keeping the existing master key (changing it would make saved keys unreadable)."
} else {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $lines = Set-EnvValue $lines "GAZI_KEK" ([Convert]::ToBase64String($bytes))
    Write-Host "Created a new master key."
}
$lines = Set-EnvValue $lines "GAZI_JWT_JWKS_URL" "http://host.docker.internal:54321/auth/v1/.well-known/jwks.json"
$lines = Set-EnvValue $lines "GAZI_JWT_SECRET" $status["JWT_SECRET"]
$lines = Set-EnvValue $lines "GAZI_GOTRUE_SERVICE_KEY" $status["SERVICE_ROLE_KEY"]
$lines = Set-EnvValue $lines "GAZI_DEV_ANON_KEY" $status["ANON_KEY"]
Set-Content -Path $EnvFile -Value $lines -Encoding ascii
Write-Host "Saved $EnvFile (git ignores this file)."

$frontendEnv = @(
    "NEXT_PUBLIC_SUPABASE_URL=http://127.0.0.1:54321",
    "NEXT_PUBLIC_SUPABASE_ANON_KEY=$($status["ANON_KEY"])",
    "NEXT_PUBLIC_API_PUBLIC_URL=http://localhost:8000",
    "GAZI_API_URL=http://localhost:8000"
)
Set-Content -Path "frontend/.env.local" -Value $frontendEnv -Encoding ascii
Write-Host "Saved frontend/.env.local (git ignores this file)."

Step "4/5 Starting Redis and the API"
docker compose -f infra/docker-compose.dev.yml up -d --build --force-recreate
if ($LASTEXITCODE -ne 0) { Fail "The API didn't start. See the message above." }

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:8000/readyz" -TimeoutSec 3
        if ($r.status -eq "ready") { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $ready) { Fail "The API isn't ready. Check its log: docker compose -f infra/docker-compose.dev.yml logs api" }
Write-Host "The API is ready."

Step "5/5 Creating the demo user and a sign-in token"
$headers = @{ apikey = $status["ANON_KEY"] }
$body = @{ email = $DemoEmail; password = $DemoPassword } | ConvertTo-Json
try {
    Invoke-RestMethod -Method Post -Uri "$AuthUrl/signup" -Headers $headers -ContentType "application/json" -Body $body | Out-Null
    Write-Host "Created $DemoEmail."
} catch {
    $detail = "$($_.ErrorDetails.Message) $($_.Exception.Message)"
    if ($detail -match "already") { Write-Host "$DemoEmail already exists." }
    else { Fail "Couldn't create $DemoEmail. $detail" }
}
try {
    $session = Invoke-RestMethod -Method Post -Uri "$AuthUrl/token?grant_type=password" -Headers $headers `
        -ContentType "application/json" -Body $body
} catch {
    Fail "Couldn't sign in as $DemoEmail. If you changed its password in Studio, sign in with that instead."
}

Write-Host ""
Write-Host "Gazi's API is running." -ForegroundColor Green
Write-Host "  The app:   in a second PowerShell window run:  cd frontend; npm install; npm run dev"
Write-Host "             then open http://localhost:3000 and sign in as the demo user"
Write-Host "  API docs:  http://localhost:8000/docs   (click Authorize and paste the token below)"
Write-Host "  Studio:    http://localhost:54323       (browse every table)"
Write-Host "  Demo user: $DemoEmail / $DemoPassword"
Write-Host ""
Write-Host "Sign-in token (valid for 1 hour; run this script again for a new one):" -ForegroundColor Yellow
Write-Host $session.access_token
