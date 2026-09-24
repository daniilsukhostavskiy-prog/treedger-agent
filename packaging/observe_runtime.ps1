<#
.SYNOPSIS
    D-17 external runtime observer for the Treedger local sync agent's built .exe.

.DESCRIPTION
    Launches the given .exe and measures, from OUTSIDE the process, the three
    D-17 facts (40-CONTEXT.md):
      1. Not a single listening port is opened by the process (or any child it
         spawns) for the whole observation window.
      2. Every outbound connection the process makes goes only to the resolved
         address(es) of the configured base-url host (loopback is excluded
         from that comparison and reported separately).
      3. Every file the process writes lands only inside its own
         %APPDATA%\TreedgerAgent folder.

    THIS SCRIPT MUST NEVER READ, PARSE OR TRUST THE AGENT'S OWN
    STDOUT/STDERR/LOG OUTPUT FOR ANY OF THE THREE FACTS ABOVE. A compromised
    build reports exactly what it should (40-CONTEXT.md D-17) -- only
    externally observable OS state (the connection table, the filesystem) is
    evidence for the three booleans below. Capturing the agent's own
    stdout/stderr IS permitted, but ONLY for diagnostic display -- any such
    capture is written into a field explicitly named "diagnostic" in both the
    JSON result and the printed summary, and it never contributes to
    NoListeningPortHeld / OutboundOnlyToBaseUrlHeld / WritesOnlyInOwnFolderHeld
    or to AllFactsHeld below. If a future change makes this script read the
    agent's own output to decide a fact, that change is the exact defect this
    script exists to prevent -- do not make it.

    Driver-free by design: every measurement below uses a plain PowerShell
    cmdlet or a filesystem diff. Procmon/Sysmon (which require installing a
    kernel-mode driver) are deliberately NOT used here -- driver installability
    on a hosted GitHub Actions runner is UNKNOWN (40-RESEARCH.md Pitfall 6 /
    Assumption A5), and this script must work without that being resolved.

.PARAMETER ExePath
    Path to the built .exe to launch and observe.

.PARAMETER BaseUrlHost
    The hostname the observed process's config.json "base_url" points at
    (e.g. "treedger.com", no scheme, no path). Fact 2 is evaluated against
    this host's resolved A/AAAA records, via Resolve-DnsName.

.PARAMETER DurationSeconds
    How long to observe after launch, in seconds, before terminating the
    process if it has not already exited on its own. Facts are measured for
    the whole window, not just the endpoints.

.PARAMETER PollIntervalSeconds
    Cadence, in seconds, for the listening-port and outbound-connection polls
    during the observation window.

.PARAMETER OutputPath
    Where to write the machine-readable JSON result. Defaults to
    runtime-observation.json in the current directory.

.PARAMETER ExeArguments
    Extra command-line arguments to pass to the observed .exe. Defaults to
    none -- the D-27 probe launches with NO flags at all, "the way a person
    launches it" (40-CONTEXT.md D-27), and this parameter exists only so a
    later caller (e.g. a future --minimized autostart check) can reuse this
    same script without a flag-handling fork; it must never be used to smuggle
    a flag the ordinary launch path would not also accept.

.OUTPUTS
    Exits 0 if all three facts held for the whole observation window; exits
    non-zero (1) if any fact failed. Writes -OutputPath as a JSON object with
    one boolean field per D-17 fact plus the raw evidence lists that field is
    based on, and a separate "diagnostic" object that is documentary only.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ExePath,

    [Parameter(Mandatory = $true)]
    [string]$BaseUrlHost,

    [int]$DurationSeconds = 30,

    [int]$PollIntervalSeconds = 1,

    [string]$OutputPath = "runtime-observation.json",

    [string[]]$ExeArguments = @()
)

$ErrorActionPreference = "Stop"

function Get-DescendantProcessIds {
    <#
    Returns the root PID plus every descendant it has spawned by the time this
    is called, walking Win32_Process's ParentProcessId. The agent is not
    expected to spawn children at all -- this exists so an external observer
    does not simply ASSUME that and silently miss a listening port opened by
    a child process instead of the process it launched directly.
    #>
    param([int]$RootProcessId)

    $seen = @{}
    $frontier = [System.Collections.Generic.Queue[int]]::new()
    $frontier.Enqueue($RootProcessId)
    while ($frontier.Count -gt 0) {
        $currentPid = $frontier.Dequeue()
        if ($seen.ContainsKey($currentPid)) { continue }
        $seen[$currentPid] = $true
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$currentPid" -ErrorAction SilentlyContinue
        foreach ($child in $children) {
            $frontier.Enqueue([int]$child.ProcessId)
        }
    }
    return [int[]]$seen.Keys
}

function Get-WatchedRootSnapshot {
    <#
    Snapshots a filesystem root as a hashtable keyed by full path, each value
    a small object of {Length, LastWriteTimeUtc}. $Recurse controls whether
    the whole tree is walked (APPDATA/LOCALAPPDATA/TEMP) or only the
    immediate children are listed (USERPROFILE -- non-recursive, to bound the
    cost of scanning a real user's home directory per the plan's own note).
    #>
    param(
        [string]$RootPath,
        [bool]$Recurse
    )

    $snapshot = @{}
    if (-not (Test-Path -LiteralPath $RootPath)) {
        return $snapshot
    }
    $items = if ($Recurse) {
        Get-ChildItem -LiteralPath $RootPath -Recurse -Force -File -ErrorAction SilentlyContinue
    } else {
        Get-ChildItem -LiteralPath $RootPath -Force -File -ErrorAction SilentlyContinue
    }
    foreach ($item in $items) {
        $snapshot[$item.FullName] = @{
            Length          = $item.Length
            LastWriteTimeUtc = $item.LastWriteTimeUtc.ToString("o")
        }
    }
    return $snapshot
}

function Get-ChangedPaths {
    <#
    Diffs two Get-WatchedRootSnapshot outputs. A path is "changed" if it is
    new in $After, or present in both but with a different Length or
    LastWriteTimeUtc. Deletions are deliberately not flagged -- D-17 fact 3
    is about WRITES, and a file the process merely removed is not a write
    outside its own folder.
    #>
    param(
        [hashtable]$Before,
        [hashtable]$After
    )

    $changed = New-Object System.Collections.Generic.List[string]
    foreach ($path in $After.Keys) {
        if (-not $Before.ContainsKey($path)) {
            $changed.Add($path)
            continue
        }
        $beforeEntry = $Before[$path]
        $afterEntry = $After[$path]
        if ($beforeEntry.Length -ne $afterEntry.Length -or $beforeEntry.LastWriteTimeUtc -ne $afterEntry.LastWriteTimeUtc) {
            $changed.Add($path)
        }
    }
    return $changed
}

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

$appDataDir = Join-Path $env:APPDATA "TreedgerAgent"
$localAppDataDir = $env:LOCALAPPDATA
$tempDir = $env:TEMP
$userProfileDir = $env:USERPROFILE

# This script's OWN diagnostic-capture artifacts live under a dedicated
# subfolder of TEMP and are excluded from the fact-3 diff below -- they are
# writes made by THIS OBSERVER, not by the observed process, and conflating
# them would falsely blame the agent for output this script itself created.
$diagDir = Join-Path $tempDir "treedger-observe-runtime"
New-Item -ItemType Directory -Path $diagDir -Force | Out-Null
$stdoutLogPath = Join-Path $diagDir "agent-stdout.log"
$stderrLogPath = Join-Path $diagDir "agent-stderr.log"

Write-Host "=== D-17 runtime observation starting ==="
Write-Host "ExePath: $ExePath"
Write-Host "BaseUrlHost: $BaseUrlHost"
Write-Host "DurationSeconds: $DurationSeconds"

# ---------------------------------------------------------------------------
# BEFORE snapshot
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# AMBIENT CONTROL WINDOW -- measured BEFORE the agent is launched
# ---------------------------------------------------------------------------
#
# This script cannot attribute a filesystem write to a process: it diffs whole
# directory trees, and it deliberately refuses to install a kernel-mode driver
# (Procmon/Sysmon) to get real per-process attribution -- driver installability
# on a hosted runner is UNKNOWN (40-RESEARCH.md Pitfall 6 / Assumption A5).
#
# Without a control, EVERY write that Windows, .NET, Defender, the PowerShell
# host or the CI runner itself makes to TEMP/APPDATA/LOCALAPPDATA during the
# observation window is charged to the agent, and Fact 3 reports False for a
# perfectly well-behaved program. That is not a hypothetical: probe runs #1-#3
# all reported Fact 3 False, every one of them against a process that had
# already died -- a process that cannot write anything at all.
#
# So: run the identical snapshot/diff over an equal-length window with NO agent
# running, and treat those paths as the machine's noise floor. A path is only
# charged to the agent if it changed during the agent's window AND did not
# change during the control window. This is still not true attribution -- it
# cannot be, without a driver -- so the violating paths are printed in full and
# the noise floor is reported alongside, for a human to adjudicate. Fact 3 is
# evidence to read, never a verdict to accept unseen.

$controlStartAppData = Get-WatchedRootSnapshot -RootPath $env:APPDATA -Recurse $true
$controlStartLocalAppData = Get-WatchedRootSnapshot -RootPath $localAppDataDir -Recurse $true
$controlStartTemp = Get-WatchedRootSnapshot -RootPath $tempDir -Recurse $true
$controlStartUserProfile = Get-WatchedRootSnapshot -RootPath $userProfileDir -Recurse $false

Write-Host "Measuring ambient filesystem noise for $DurationSeconds seconds (no agent running)..."
Start-Sleep -Seconds $DurationSeconds

$controlEndAppData = Get-WatchedRootSnapshot -RootPath $env:APPDATA -Recurse $true
$controlEndLocalAppData = Get-WatchedRootSnapshot -RootPath $localAppDataDir -Recurse $true
$controlEndTemp = Get-WatchedRootSnapshot -RootPath $tempDir -Recurse $true
$controlEndUserProfile = Get-WatchedRootSnapshot -RootPath $userProfileDir -Recurse $false

$ambientChangedPaths = @()
$ambientChangedPaths += Get-ChangedPaths -Before $controlStartAppData -After $controlEndAppData
$ambientChangedPaths += Get-ChangedPaths -Before $controlStartLocalAppData -After $controlEndLocalAppData
$ambientChangedPaths += Get-ChangedPaths -Before $controlStartTemp -After $controlEndTemp
$ambientChangedPaths += Get-ChangedPaths -Before $controlStartUserProfile -After $controlEndUserProfile

$ambientPathSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
foreach ($ambientPath in $ambientChangedPaths) { [void]$ambientPathSet.Add($ambientPath) }
Write-Host "Ambient noise floor: $($ambientPathSet.Count) path(s) changed with no agent running."

# ---------------------------------------------------------------------------
# The agent's own observation window starts here
# ---------------------------------------------------------------------------

$beforeAppData = Get-WatchedRootSnapshot -RootPath $env:APPDATA -Recurse $true
$beforeLocalAppData = Get-WatchedRootSnapshot -RootPath $localAppDataDir -Recurse $true
$beforeTemp = Get-WatchedRootSnapshot -RootPath $tempDir -Recurse $true
$beforeUserProfile = Get-WatchedRootSnapshot -RootPath $userProfileDir -Recurse $false

# ---------------------------------------------------------------------------
# Launch (this script does its own launch -- see the workflow comment in
# probe-window.yml for why this is a separate process from that workflow's
# own window-detection launch, rather than a shared PID)
# ---------------------------------------------------------------------------

$startArgs = @{
    FilePath               = $ExePath
    PassThru                = $true
    RedirectStandardOutput  = $stdoutLogPath
    RedirectStandardError   = $stderrLogPath
}
if ($ExeArguments.Count -gt 0) {
    $startArgs["ArgumentList"] = $ExeArguments
}

$proc = Start-Process @startArgs
$trackedPids = Get-DescendantProcessIds -RootProcessId $proc.Id

# ---------------------------------------------------------------------------
# Observation window -- poll for the whole duration, not just the endpoints
# ---------------------------------------------------------------------------

$listeningEvidence = New-Object System.Collections.Generic.List[object]
$outboundRemoteAddresses = New-Object System.Collections.Generic.HashSet[string]

$deadline = (Get-Date).AddSeconds($DurationSeconds)
while ((Get-Date) -lt $deadline) {
    # Refresh the tracked PID set each poll in case a child appeared since
    # the last check.
    $allTrackedPids = New-Object System.Collections.Generic.HashSet[int]
    foreach ($p in $trackedPids) { [void]$allTrackedPids.Add($p) }
    if (-not $proc.HasExited) {
        foreach ($p in (Get-DescendantProcessIds -RootProcessId $proc.Id)) {
            [void]$allTrackedPids.Add($p)
        }
    }
    $trackedPids = [int[]]$allTrackedPids

    if ($trackedPids.Count -gt 0) {
        $listening = Get-NetTCPConnection -State Listen -OwningProcess $trackedPids -ErrorAction SilentlyContinue
        foreach ($conn in $listening) {
            $listeningEvidence.Add(@{
                LocalAddress = $conn.LocalAddress
                LocalPort    = $conn.LocalPort
                OwningProcess = $conn.OwningProcess
            })
        }

        $outbound = Get-NetTCPConnection -OwningProcess $trackedPids -ErrorAction SilentlyContinue |
            Where-Object { $_.State -in @("Established", "SynSent") }
        foreach ($conn in $outbound) {
            [void]$outboundRemoteAddresses.Add($conn.RemoteAddress)
        }
    }

    if ($proc.HasExited) { break }
    Start-Sleep -Seconds $PollIntervalSeconds
}

# Give the process the rest of its budgeted duration to exit on its own; if
# it is still running, terminate it so the observation window has a definite
# end and the AFTER filesystem snapshot is taken against a quiesced process.
if (-not $proc.HasExited) {
    try {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    } catch {
        # Already exited between the check above and here -- not an error.
    }
}
$proc.WaitForExit(5000) | Out-Null
$exitCode = if ($proc.HasExited) { $proc.ExitCode } else { $null }

# ---------------------------------------------------------------------------
# AFTER snapshot + diff
# ---------------------------------------------------------------------------

$afterAppData = Get-WatchedRootSnapshot -RootPath $env:APPDATA -Recurse $true
$afterLocalAppData = Get-WatchedRootSnapshot -RootPath $localAppDataDir -Recurse $true
$afterTemp = Get-WatchedRootSnapshot -RootPath $tempDir -Recurse $true
$afterUserProfile = Get-WatchedRootSnapshot -RootPath $userProfileDir -Recurse $false

$changedAppData = Get-ChangedPaths -Before $beforeAppData -After $afterAppData
$changedLocalAppData = Get-ChangedPaths -Before $beforeLocalAppData -After $afterLocalAppData
$changedTemp = Get-ChangedPaths -Before $beforeTemp -After $afterTemp
$changedUserProfile = Get-ChangedPaths -Before $beforeUserProfile -After $afterUserProfile

# This observer's own diagnostic-capture files are excluded here -- they are
# writes made by THIS SCRIPT, not by the observed process (see $diagDir note
# above).
$changedTemp = $changedTemp | Where-Object { $_ -ne $stdoutLogPath -and $_ -ne $stderrLogPath -and -not $_.StartsWith($diagDir) }

$allChangedPaths = @($changedAppData) + @($changedLocalAppData) + @($changedTemp) + @($changedUserProfile)
$appDataAgentPrefix = $appDataDir + [System.IO.Path]::DirectorySeparatorChar

$outsideOwnFolderPaths = $allChangedPaths | Where-Object { -not $_.StartsWith($appDataAgentPrefix, [System.StringComparison]::OrdinalIgnoreCase) }

# Subtract the machine's measured noise floor. A path that also changed during
# the control window -- when no agent was running -- cannot be evidence about
# the agent. See the ambient-control block above for why this is necessary and
# why it is still not true per-process attribution.
$filesystemViolations = $outsideOwnFolderPaths | Where-Object { -not $ambientPathSet.Contains($_) }
$ambientSuppressedPaths = $outsideOwnFolderPaths | Where-Object { $ambientPathSet.Contains($_) }

# ---------------------------------------------------------------------------
# Fact 2 -- resolve the base-url host and classify every observed remote address
# ---------------------------------------------------------------------------

$resolvedAddresses = New-Object System.Collections.Generic.HashSet[string]
try {
    $dnsResults = Resolve-DnsName -Name $BaseUrlHost -ErrorAction Stop
    foreach ($record in $dnsResults) {
        if ($record.Type -in @("A", "AAAA") -and $record.IPAddress) {
            [void]$resolvedAddresses.Add($record.IPAddress)
        }
    }
} catch {
    Write-Warning "Resolve-DnsName failed for '$BaseUrlHost': $($_.Exception.Message)"
}

function Test-LoopbackAddress {
    param([string]$Address)
    return ($Address -eq "127.0.0.1") -or ($Address -eq "::1") -or ($Address.StartsWith("127."))
}

$loopbackAddresses = New-Object System.Collections.Generic.List[string]
$violatingOutboundAddresses = New-Object System.Collections.Generic.List[string]
foreach ($address in $outboundRemoteAddresses) {
    if (Test-LoopbackAddress -Address $address) {
        $loopbackAddresses.Add($address)
        continue
    }
    if (-not $resolvedAddresses.Contains($address)) {
        $violatingOutboundAddresses.Add($address)
    }
}

# ---------------------------------------------------------------------------
# Evaluate the three D-17 facts
# ---------------------------------------------------------------------------

$noListeningPortHeld = ($listeningEvidence.Count -eq 0)
$outboundOnlyToBaseUrlHeld = ($violatingOutboundAddresses.Count -eq 0)
$writesOnlyInOwnFolderHeld = ($filesystemViolations.Count -eq 0)
$allFactsHeld = $noListeningPortHeld -and $outboundOnlyToBaseUrlHeld -and $writesOnlyInOwnFolderHeld

# ---------------------------------------------------------------------------
# Diagnostic-only capture -- NEVER used above to compute any of the three facts
# ---------------------------------------------------------------------------

$diagnosticStdout = if (Test-Path -LiteralPath $stdoutLogPath) { Get-Content -LiteralPath $stdoutLogPath -Raw -ErrorAction SilentlyContinue } else { $null }
$diagnosticStderr = if (Test-Path -LiteralPath $stderrLogPath) { Get-Content -LiteralPath $stderrLogPath -Raw -ErrorAction SilentlyContinue } else { $null }

# ---------------------------------------------------------------------------
# Result object + JSON output
# ---------------------------------------------------------------------------

$result = [ordered]@{
    noListeningPortHeld        = $noListeningPortHeld
    outboundOnlyToBaseUrlHeld  = $outboundOnlyToBaseUrlHeld
    writesOnlyInOwnFolderHeld  = $writesOnlyInOwnFolderHeld
    allFactsHeld               = $allFactsHeld
    process                    = [ordered]@{
        pid       = $proc.Id
        exitCode  = $exitCode
        hasExited = $proc.HasExited
    }
    evidence = [ordered]@{
        listeningPorts       = $listeningEvidence
        outboundConnections  = [ordered]@{
            allRemoteAddresses      = @($outboundRemoteAddresses)
            baseUrlResolvedAddresses = @($resolvedAddresses)
            loopbackAddresses        = @($loopbackAddresses)
            violatingAddresses       = @($violatingOutboundAddresses)
        }
        filesystemWrites = [ordered]@{
            violatingPaths           = @($filesystemViolations)
            allChangedPaths          = @($allChangedPaths)
            outsideOwnFolderPaths    = @($outsideOwnFolderPaths)
            ambientNoiseFloorPaths   = @($ambientPathSet)
            ambientSuppressedPaths   = @($ambientSuppressedPaths)
            attributionNote          = "This script diffs directory trees; it cannot attribute a write to a process. Paths that also changed during an equal-length control window with no agent running are subtracted as machine noise. Read violatingPaths before believing Fact 3 either way."
        }
    }
    diagnostic = [ordered]@{
        note   = "Captured for diagnostic display only. Never used to compute noListeningPortHeld, outboundOnlyToBaseUrlHeld, writesOnlyInOwnFolderHeld or allFactsHeld above (40-CONTEXT.md D-17)."
        stdout = $diagnosticStdout
        stderr = $diagnosticStderr
    }
}

$result | ConvertTo-Json -Depth 10 | Out-File -FilePath $OutputPath -Encoding utf8

# ---------------------------------------------------------------------------
# Human-readable summary
# ---------------------------------------------------------------------------

Write-Host "=== D-17 runtime observation result ==="
Write-Host "Fact 1 (no listening port):         $noListeningPortHeld"
Write-Host "Fact 2 (outbound only to base_url):  $outboundOnlyToBaseUrlHeld"
Write-Host "Fact 3 (writes only in own folder):  $writesOnlyInOwnFolderHeld"
Write-Host "    ambient noise floor: $($ambientPathSet.Count) path(s); suppressed as noise: $(@($ambientSuppressedPaths).Count)"
if (@($filesystemViolations).Count -gt 0) {
    Write-Host "    Fact 3 charged these path(s) to the agent -- inspect before treating as a finding:"
    foreach ($violation in $filesystemViolations) { Write-Host "      $violation" }
}
if ($proc.HasExited -and $null -ne $exitCode -and $exitCode -ne 0) {
    Write-Host "    WARNING: the observed process exited with $exitCode. All three facts above are"
    Write-Host "    measurements of a process that was not running normally, and none of them is"
    Write-Host "    evidence about the shipped program until it is re-measured on a healthy build."
}
Write-Host "ALL FACTS HELD:                      $allFactsHeld"
Write-Host "Process exit code: $exitCode (HasExited=$($proc.HasExited))"
Write-Host "--- DIAGNOSTIC (not used for any fact above) ---"
Write-Host "stdout: $diagnosticStdout"
Write-Host "stderr: $diagnosticStderr"
Write-Host "Result written to: $OutputPath"

if (-not $allFactsHeld) {
    Write-Error "D-17 runtime observation FAILED -- see $OutputPath for evidence."
    exit 1
}

exit 0
