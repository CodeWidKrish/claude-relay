# Claude Relay one-line installer for Windows (PowerShell 5.1+).
#
#   irm https://raw.githubusercontent.com/CodeWidKrish/claude-relay/main/install.ps1 | iex
#
# Downloads relay.py (or updates a git clone), then runs `relay.py setup`, which installs Relay into
# Claude Code and, when it's present, Codex CLI. Nothing else is downloaded. Set $env:RELAY_DIR to
# choose the folder (default C:\claude-relay: no spaces, outside OneDrive).

$ErrorActionPreference = 'Stop'
$source = 'https://raw.githubusercontent.com/CodeWidKrish/claude-relay/main/relay.py'
$dir = if ($env:RELAY_DIR) { $env:RELAY_DIR } else { 'C:\claude-relay' }

function Find-Python {
    foreach ($candidate in @(@('python'), @('py', '-3'), @('python3'))) {
        $cmd = Get-Command $candidate[0] -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $extra = @($candidate | Select-Object -Skip 1)
        try {
            $version = & $cmd.Source @extra -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($version -and [version]$version -ge [version]'3.8') { return ,(@($cmd.Source) + $extra) }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host 'Claude Relay needs Python 3.8 or newer: https://www.python.org/downloads/' -ForegroundColor Red
    return
}

try {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
} catch {
    $dir = Join-Path $env:LOCALAPPDATA 'claude-relay'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

$relay = Join-Path $dir 'relay.py'
if ((Test-Path (Join-Path $dir '.git')) -and (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Updating the git clone in $dir ..."
    git -C $dir pull --ff-only
} else {
    Write-Host "Downloading relay.py to $dir ..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri $source -OutFile $relay
}

$exe = $python[0]
$extra = @($python | Select-Object -Skip 1)
& $exe @extra $relay setup
