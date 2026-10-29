<#
.SYNOPSIS
    Task Scheduler Preparation and Registration Script for WebsiteLead Hunter Standalone Durable Worker.

.DESCRIPTION
    Prepares, validates, and optionally registers the Windows Task Scheduler background task
    `WebsiteLeadHunter-DurableWorker`.
    
    SAFETY MANDATE:
    By default, this script operates in DRY-RUN / INSPECTION mode. It will NEVER register or enable
    the task without the explicit `-RegisterTask` switch.

.PARAMETER RegisterTask
    Explicit switch required to register the task in Windows Task Scheduler.

.PARAMETER UnregisterTask
    Explicit switch to unregister/remove the task from Windows Task Scheduler.

.PARAMETER ExportXmlOnly
    Exports the generated Task Scheduler XML definition to a specified or default path without registering.

.PARAMETER TaskName
    Name of the scheduled task. Default: WebsiteLeadHunter-DurableWorker.

.EXAMPLE
    # Inspect task configuration (Dry-run mode, zero OS modifications):
    .\setup_task_scheduler.ps1

.EXAMPLE
    # Export task XML definition without registration:
    .\setup_task_scheduler.ps1 -ExportXmlOnly

.EXAMPLE
    # Register task after explicit user authorization (Gate 2):
    .\setup_task_scheduler.ps1 -RegisterTask
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$RegisterTask = $false,
    [switch]$UnregisterTask = $false,
    [switch]$ExportXmlOnly = $false,
    [string]$TaskName = "WebsiteLeadHunter-DurableWorker",
    [string]$XmlOutputPath = ""
)

$ErrorActionPreference = "Stop"

# Resolve absolute backend and repository paths
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Split-Path -Parent $scriptDir
$repoDir = Split-Path -Parent $backendDir
$logDir = Join-Path $backendDir "logs"
$logFile = Join-Path $logDir "worker.log"

# Detect Python interpreter
$pythonExe = (Get-Command "python.exe" -ErrorAction SilentlyContinue).Source
if (-not $pythonExe) {
    $pythonExe = "C:\Users\Yatha\AppData\Local\Programs\Python\Python313\python.exe"
}

# Target arguments
$workerModule = "app.queue.worker_daemon"
$arguments = "-m $workerModule"

# Detect current Windows user
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " WebsiteLead Hunter - Task Scheduler Supervision Preparation" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "Task Name:           $TaskName"
Write-Host "Startup Identity:    $currentUser"
Write-Host "Working Directory:   $backendDir"
Write-Host "Python Executable:   $pythonExe"
Write-Host "Arguments:           $arguments"
Write-Host "Log Output Path:     $logFile"
Write-Host 'Log Bounded Cap:     25 MB (5 MB x 5 rotating backups)'
Write-Host 'Restart Policy:      Retry 3 times, every 1 minute on failure'
Write-Host 'Execution Limit:     PT0S (Indefinite / Continuous background)'
Write-Host 'Instance Policy:     IgnoreNew (Strict single-instance mutex)'
Write-Host 'Sleep Prevention:    Win32 SetThreadExecutionState (ES_AWAYMODE_REQUIRED)'
Write-Host "-----------------------------------------------------------------"

# Validation: Check path existence
if (-not (Test-Path $backendDir)) {
    Write-Error "Backend directory does not exist: $backendDir"
}

if (-not (Test-Path $pythonExe)) {
    Write-Warning "Python executable not verified at: $pythonExe"
}

# Cloud Sync Guard: Verify database is NOT inside a cloud-sync directory
$dbPath = [System.Environment]::GetEnvironmentVariable("DATABASE_PATH")
if (-not $dbPath) { $dbPath = Join-Path $backendDir "websitelead_hunter.db" }
if ($dbPath -match "OneDrive|Dropbox|Google Drive|iCloudDrive") {
    Write-Warning "CRITICAL WARNING: Database path resides in a cloud-synced folder ($dbPath)."
    Write-Warning "Cloud sync locks .db-wal files asynchronously, leading to split-brain corruption."
}

# Default XML Output Path
if (-not $XmlOutputPath) {
    $XmlOutputPath = Join-Path $scriptDir "$TaskName.xml"
}

# Generate Task Scheduler XML definition
$xmlContent = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>2026-10-26T00:00:00</Date>
    <Author>$currentUser</Author>
    <Description>WebsiteLead Hunter Standalone Durable Worker Daemon. Supervised background crawler and auditor with SQLite WAL fencing, heartbeat renewal, and automatic failure restart.</Description>
    <URI>\$TaskName</URI>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT30S</Delay>
    </BootTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT15S</Delay>
      <UserId>$currentUser</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$currentUser</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$pythonExe</Command>
      <Arguments>$arguments</Arguments>
      <WorkingDirectory>$backendDir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

# Export XML if requested or by default for inspection
[System.IO.File]::WriteAllText($XmlOutputPath, $xmlContent, [System.Text.Encoding]::Unicode)
Write-Host "[OK] Task Scheduler XML definition generated at: $XmlOutputPath" -ForegroundColor Green

# Handle Unregistration
if ($UnregisterTask) {
    if ($PSCmdlet.ShouldProcess($TaskName, "Unregister Scheduled Task")) {
        Write-Host "Unregistering scheduled task '$TaskName'..." -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Continue
        Write-Host "[OK] Task '$TaskName' unregistered successfully." -ForegroundColor Green
    }
    return
}

# Handle Registration
if ($RegisterTask) {
    if ($PSCmdlet.ShouldProcess($TaskName, "Register Scheduled Task in Windows Task Scheduler")) {
        Write-Host "Registering task '$TaskName' with Windows Task Scheduler..." -ForegroundColor Cyan
        
        # Register via XML to preserve fine-grained triggers and restart-on-failure settings
        $res = & schtasks.exe /Create /TN $TaskName /XML $XmlOutputPath /F
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[OK] Successfully registered '$TaskName' in Windows Task Scheduler." -ForegroundColor Green
            Write-Host "To inspect in Windows GUI, run: taskschd.msc"
        } else {
            Write-Error "Failed to register task via schtasks.exe (Exit code: $LASTEXITCODE)."
        }
    }
    return
}

# Default safety notice when neither -RegisterTask nor -UnregisterTask is specified
Write-Host ""
Write-Host "=================================================================" -ForegroundColor Yellow
Write-Host " SAFETY STATUS: TASK CREATION AND ACTIVATION DISABLED" -ForegroundColor Yellow
Write-Host "=================================================================" -ForegroundColor Yellow
Write-Host "This script ran in validation and export mode."
Write-Host "No tasks were created or modified in Windows Task Scheduler."
Write-Host "Task definition XML is exported and ready for Gate 2 sign-off."
Write-Host ""
Write-Host "To register the task when authorized, execute:"
Write-Host "  powershell.exe -ExecutionPolicy Bypass -File .\setup_task_scheduler.ps1 -RegisterTask" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Yellow
