<#
    Install-Organizer.ps1

    Installs the Folder Organizer TUI and wires it into PowerShell:
      - Copies organizer.py + theme.tcss to $HOME\.organizer
      - Adds an `organizer` function to your $PROFILE
      - Binds Ctrl+O at the prompt to launch it instantly (your "button")

    Run once:  powershell -ExecutionPolicy Bypass -File .\Install-Organizer.ps1
#>

$ErrorActionPreference = "Stop"

# Where the actual app files live once installed - kept out of any repo,
# separate from wherever the user happens to run this installer from.
$InstallDir = Join-Path $HOME ".organizer"
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

Copy-Item -Path (Join-Path $PSScriptRoot "organizer.py") -Destination $InstallDir -Force
Copy-Item -Path (Join-Path $PSScriptRoot "theme.tcss") -Destination $InstallDir -Force

# --- find a *real* Python (skips the Microsoft Store "python" stub) -------
function Resolve-PythonCommand {
    # Each candidate is a command name plus any fixed args (e.g. `py -3`).
    # We don't trust path heuristics (e.g. "contains WindowsApps") since that
    # can misfire in both directions - instead we actually run each one and
    # check it prints real "Python x.y.z" output.
    $candidates = @(
        @{ Exe = "py"; Args = @("-3") },
        @{ Exe = "py"; Args = @() },
        @{ Exe = "python"; Args = @() },
        @{ Exe = "python3"; Args = @() }
    )
    foreach ($c in $candidates) {
        $label = ($c.Exe, ($c.Args -join " ") | Where-Object { $_ }) -join " "
        $cmdInfo = Get-Command $c.Exe -ErrorAction SilentlyContinue
        if (-not $cmdInfo) {
            Write-Host "  [skip] '$($c.Exe)' not found on PATH" -ForegroundColor DarkGray
            continue
        }

        # Temporarily relax $ErrorActionPreference for this one call: with
        # it set to "Stop" (as at the top of this script), PowerShell 7+
        # turns ANY stderr output from a native command into a terminating
        # exception - which is exactly what the Store's "python" stub
        # writes when invoked with arguments. Without this, probing a
        # broken stub would crash the whole installer instead of just
        # being skipped as a failed candidate.
        $savedEAP = $ErrorActionPreference
        $ErrorActionPreference = "SilentlyContinue"
        $output = ""
        $ok = $false
        try {
            $output = (& $c.Exe @($c.Args) --version 2>&1 | Out-String).Trim()
            $ok = ($LASTEXITCODE -eq 0) -and ($output -match "Python\s+\d+\.\d+")
        } catch {
            $output = $_.Exception.Message
            $ok = $false
        } finally {
            $ErrorActionPreference = $savedEAP
        }

        if ($ok) {
            Write-Host "  [ok]   '$label' -> $output" -ForegroundColor DarkGray
            return $c
        }
        else {
            Write-Host "  [skip] '$label' -> $output" -ForegroundColor DarkGray
        }
    }
    return $null
}

Write-Host "Looking for a working Python..." -ForegroundColor Cyan
$py = Resolve-PythonCommand
if (-not $py) {
    Write-Warning "Couldn't find a working Python install."
    Write-Host "Install Python 3.9+ from https://www.python.org/downloads/ - during setup, check 'Add python.exe to PATH'." -ForegroundColor Yellow
    Write-Host "If typing 'python' currently opens the Microsoft Store, turn that off: Settings > Apps > Advanced app settings > App execution aliases > disable 'python.exe' / 'python3.exe'." -ForegroundColor Yellow
    Write-Host "Then re-run this installer." -ForegroundColor Yellow
    exit 1
}

$pythonLaunchCmd = ($py.Exe, ($py.Args -join " ") | Where-Object { $_ } ) -join " "
Write-Host "Using Python: $pythonLaunchCmd" -ForegroundColor Cyan

# Check whether `textual` is already importable before trying to install
# it again - same ErrorActionPreference dance as above, since this is
# also a native command call that could otherwise throw.
$savedEAP = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
& $py.Exe @($py.Args) -c "import textual" *> $null
$hasTextual = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $savedEAP

if (-not $hasTextual) {
    Write-Host "Installing the 'textual' package..." -ForegroundColor Cyan
    & $py.Exe @($py.Args) -m pip install textual
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "pip install failed - install it manually with: $pythonLaunchCmd -m pip install textual"
    }
}

# --- function + keybinding block to add to $PROFILE -----------------------
# Everything below builds the literal text that gets written into the
# user's PowerShell profile: an `organizer` function plus a Ctrl+O
# keybinding. Wrapped in blockStart/blockEnd markers so re-running this
# installer later can find and replace just this block, not the user's
# entire profile.
$blockStart = "# >>> Folder Organizer >>>"
$blockEnd   = "# <<< Folder Organizer <<<"

# This is a double-quoted here-string, so PowerShell expands $blockStart,
# $blockEnd, $InstallDir, and $pythonLaunchCmd right now, at install time,
# baking their current values into the profile text. Every OTHER dollar
# sign is escaped with a backtick (`$line, `$stateFile, etc.) so it's
# instead written out literally, to be evaluated later, each time the
# `organizer` function actually runs in the user's live shell.
$block = @"
$blockStart
function organizer {
    `$scriptPath = "$InstallDir\organizer.py"
    `$stateFile  = Join-Path `$env:TEMP "organizer_action.txt"
    if (Test-Path `$stateFile) { Remove-Item `$stateFile -Force }

    $pythonLaunchCmd `$scriptPath (Get-Location).Path

    if (Test-Path `$stateFile) {
        `$targetPath = `$null
        `$activate   = `$null
        foreach (`$line in Get-Content `$stateFile) {
            if (`$line -match '^PATH=(.*)$')     { `$targetPath = `$matches[1] }
            if (`$line -match '^ACTIVATE=(.*)$') { `$activate   = `$matches[1] }
        }
        if (`$targetPath -and (Test-Path `$targetPath)) { Set-Location `$targetPath }
        if (`$activate -and (Test-Path `$activate))     { & `$activate }
        Remove-Item `$stateFile -Force
    }
}

# Ctrl+O at the prompt launches the organizer, like pressing a "button"
if (Get-Module -ListAvailable -Name PSReadLine) {
    Set-PSReadLineKeyHandler -Chord "Ctrl+o" -ScriptBlock {
        [Microsoft.PowerShell.PSConsoleReadLine]::RevertLine()
        [Microsoft.PowerShell.PSConsoleReadLine]::Insert("organizer")
        [Microsoft.PowerShell.PSConsoleReadLine]::AcceptLine()
    }
}
$blockEnd
"@

if (-not (Test-Path $PROFILE)) {
    New-Item -ItemType File -Path $PROFILE -Force | Out-Null
}

$existing = Get-Content $PROFILE -Raw -ErrorAction SilentlyContinue
if ($existing -and $existing.Contains($blockStart)) {
    # Re-running the installer: replace the previous block in place so
    # profiles don't accumulate duplicate copies on repeat installs.
    Write-Host "Existing Folder Organizer block found in `$PROFILE` - replacing it." -ForegroundColor Yellow
    $pattern = [regex]::Escape($blockStart) + "(.|\n)*?" + [regex]::Escape($blockEnd)
    # Use a MatchEvaluator, not a plain replacement string: .NET's [regex]::Replace
    # treats sequences like $', $1, $& specially in replacement strings, and our
    # generated block legitimately contains "$'" inside its regex patterns - a
    # literal replacement string would silently corrupt it.
    $evaluator = [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $block.Trim() }
    $existing = [regex]::Replace($existing, $pattern, $evaluator)
    Set-Content -Path $PROFILE -Value $existing
}
else {
    # First install: just append the block to whatever's already there.
    Add-Content -Path $PROFILE -Value "`n$block"
}

Write-Host ""
Write-Host "Installed. Restart PowerShell (or run '. `$PROFILE') then:" -ForegroundColor Green
Write-Host "  - type 'organizer'  to open the folder browser"
Write-Host "  - press Ctrl+O      to open it instantly from the prompt"
Write-Host "  - inside the app, 'Back to Shell' (or Esc) returns you here"