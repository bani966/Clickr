# Clickr backend. Users run Install.bat / Clickr.bat / Uninstall.bat, not this.
#   setup      Install.bat. Gets Python 3.11+ and the pip packages, then runs install.
#   install    Admin. Adds the usbip-win2 drivers. USB drops out 1-2 s.
#   start      No admin. Starts device.py and plugs the mouse in. Exit 0 = attached.
#   stop       No admin. Unplugs the mouse, kills device.py. Drivers stay.
#   uninstall  Uninstall.bat. Admin. Removes every trace of the drivers, then verifies. Safe to rerun.
param(
    [Parameter(Mandatory, Position = 0)][ValidateSet('setup', 'install', 'start', 'stop', 'uninstall')][string]$Command,
    [ValidateRange(1, 255)][int]$PollMs = 1
)

$Root        = $PSScriptRoot
$Bin         = Join-Path $Root 'bin'
$DriverDir   = Join-Path $Root 'driver'
$LogDir      = Join-Path $Root 'logs'
$StateFile   = Join-Path $Root 'installed.json'
$System32    = Join-Path $env:SystemRoot 'System32'
$Address     = '127.0.0.1'
$UsbipPort   = 3240  # must match USBIP_PORT in device.py
$ControlPort = 3241  # must match CONTROL_PORT in device.py
$BusId       = '1-1'
$UdeHwid     = 'ROOT\USBIP_WIN2\UDE'
$DriverInfs  = 'usbip2_filter.inf', 'usbip2_ude.inf'
$Services    = 'usbip2_ude', 'usbip2_filter'
$ShutdownTask = 'Clickr Detach On Shutdown'
$Tasks       = $ShutdownTask, 'USBip Detach All On Reboot Or Shutdown'  # 2nd: made by the official USBip installer
$KeepLogs    = 20
# The controller's instance ID is ROOT\USB\<n>, not its hardware ID. Find it by hardware ID, never by name.
$OwnDevicePattern = '^(USB\\ROOT_HUB30|USB\\VID_1209&PID_0001|HID\\VID_1209&PID_0001)'

# --- plumbing ---

function Start-Log([string]$name) {
    New-Item -ItemType Directory -Force $LogDir | Out-Null
    Get-ChildItem $LogDir -Filter '*.log' | Sort-Object LastWriteTime -Descending |
        Select-Object -Skip ($KeepLogs - 1) | Remove-Item -ErrorAction SilentlyContinue
    $script:LogFile = Join-Path $LogDir ('{0}-{1:yyyyMMdd-HHmmss}.log' -f $name, (Get-Date))
}

function Write-Log([string]$msg) {
    $line = '{0:HH:mm:ss} {1}' -f (Get-Date), $msg
    Write-Host $line
    if ($script:LogFile) { Add-Content -Path $script:LogFile -Value $line -Encoding UTF8 }
}

# Runs a program, logs its output, sets $LASTEXITCODE. Kills it after $timeoutSec:
# driver tools hang forever when Windows is waiting on a reboot, and an elevated hang can't be killed by the user.
function Invoke-Logged([string]$exe, [string[]]$arguments, [int]$timeoutSec = 120) {
    Write-Log "> $exe $($arguments -join ' ')"
    $psi = New-Object Diagnostics.ProcessStartInfo $exe
    $psi.Arguments = ($arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $p = [Diagnostics.Process]::Start($psi)
    $out, $err = $p.StandardOutput.ReadToEndAsync(), $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit($timeoutSec * 1000)) {
        $p.Kill()
        Write-Log "  TIMED OUT after $timeoutSec s and was killed. Reboot, then run this again."
        $script:RebootNeeded = $true
        $global:LASTEXITCODE = -1
        return
    }
    $p.WaitForExit()
    foreach ($l in ($out.Result + $err.Result) -split "`r?`n") { if ($l.Trim()) { Write-Log "  $l" } }
    Write-Log "  (exit $($p.ExitCode))"
    $global:LASTEXITCODE = $p.ExitCode
    if ($p.ExitCode -eq 3010) { $script:RebootNeeded = $true }  # pnputil: done, but only fully after a reboot
}

function Test-Admin {
    $me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Runs "clickr.ps1 <command>" elevated in a hidden window, then replays its log here. Returns its exit code.
function Invoke-Elevated([string]$command) {
    $since = Get-Date
    try {
        $p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", $command)
    } catch {
        Write-Log 'ERROR: admin prompt was cancelled.'
        return 1
    }
    Get-ChildItem $LogDir -Filter "$command-*.log" | Where-Object LastWriteTime -ge $since |
        Sort-Object LastWriteTime | Select-Object -Last 1 | Get-Content | Write-Host
    $p.ExitCode
}

# A real Python 3.11+. Skips the WindowsApps stub that just opens the Microsoft Store.
function Find-Python {
    foreach ($c in Get-Command python.exe -All -ErrorAction SilentlyContinue) {
        if ($c.Source -like '*\WindowsApps\*') { continue }
        & $c.Source -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>$null
        if ($LASTEXITCODE -eq 0) { return $c.Source }
    }
}

function Wait-Until([scriptblock]$probe, [double]$seconds) {
    $end = (Get-Date).AddSeconds($seconds)
    do {
        $result = & $probe
        if ($result) { return $result }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $end)
}

# Asks device.py for its status line. $null = not running.
function Get-DeviceStatus {
    $c = New-Object Net.Sockets.TcpClient
    try {
        # Plain Connect() takes ~2 s to fail on a closed localhost port. Cap it.
        if (-not $c.ConnectAsync($Address, $ControlPort).Wait(300)) { return $null }
        $c.ReceiveTimeout = 2000
        $stream = $c.GetStream()
        $writer = New-Object IO.StreamWriter($stream)
        $writer.NewLine = "`n"
        $writer.AutoFlush = $true
        $writer.WriteLine('status')
        (New-Object IO.StreamReader($stream)).ReadLine()
    } catch { $null } finally { $c.Close() }
}

function Get-Controller {
    Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object { $_.HardwareID -contains $UdeHwid }
}

# Status 'OK' lies: a stopped controller still shows OK. Only the DN_STARTED bit (0x8) means usbip.exe can reach it.
function Test-Started([string]$instanceId) {
    [bool]((Get-PnpDeviceProperty -InstanceId $instanceId -KeyName DEVPKEY_Device_DevNodeStatus -ErrorAction SilentlyContinue).Data -band 0x8)
}

function Get-WorkingController {
    Get-Controller | Where-Object { Test-Started $_.InstanceId }
}

function Get-OwnDriverPackages {
    Get-WindowsDriver -Online -ErrorAction SilentlyContinue |
        Where-Object { $DriverInfs -contains (Split-Path $_.OriginalFileName -Leaf) }
}

# Real USB root hubs match the pattern too. A hub is only ours if it's new since install or hangs off our controller.
function Get-OwnDevices([string[]]$preexisting = @()) {
    $all = @(Get-PnpDevice -ErrorAction SilentlyContinue)
    $controllers = @($all | Where-Object { $_.HardwareID -contains $UdeHwid })
    $controllerIds = @($controllers | ForEach-Object { $_.InstanceId })
    $controllers
    $all | Where-Object { $_.InstanceId -match $OwnDevicePattern -and $preexisting -notcontains $_.InstanceId } |
        Where-Object {
            if ($_.InstanceId -notmatch '^USB\\ROOT_HUB30') { return $true }
            $parent = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_Parent -ErrorAction SilentlyContinue).Data
            ($controllerIds -contains $parent) -or ($parent -match '^ROOT\\') -or ($preexisting.Count -gt 0)
        }
}

# Virtual USB ports our mouse is plugged into. Other USB/IP devices are left alone.
function Get-OwnPorts {
    if (-not (Test-Path "$Bin\usbip.exe")) { return }
    $port = $null
    foreach ($line in (& "$Bin\usbip.exe" port 2>&1 | ForEach-Object { "$_" })) {
        if ($line -match '^Port (\d+):') { $port = [int]$Matches[1] }
        elseif ($port -and $line -match [regex]::Escape("usbip://${Address}:${UsbipPort}/$BusId")) { $port }
    }
}

# Get-ScheduledTask can't see admin-made tasks from a normal session, and Test-Path says $true for any key it
# can't read. Opening the task-cache key works: null = missing, access denied = exists.
function Test-TaskExists([string]$name) {
    try {
        $k = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey("SOFTWARE\Microsoft\Windows NT\CurrentVersion\Schedule\TaskCache\Tree\$name")
        if ($k) { $k.Close(); return $true }
        $false
    } catch [System.Security.SecurityException] { $true }
}

# usbip-win2's own installer ships this too: an attached virtual device can hang a restart. Detach first.
function Register-ShutdownTask {
    $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Author>Clickr</Author><Description>Clickr: unplug the virtual mouse before restart or shutdown</Description></RegistrationInfo>
  <Triggers>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="System"&gt;&lt;Select Path="System"&gt;*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=109)]] or *[System[Provider[@Name='User32'] and (EventID=1074)]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
    </EventTrigger>
  </Triggers>
  <Principals><Principal id="LocalService"><UserId>S-1-5-19</UserId><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT30S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="LocalService"><Exec><Command>$Bin\usbip.exe</Command><Arguments>detach --all=closeonly</Arguments></Exec></Actions>
</Task>
"@
    Register-ScheduledTask -TaskName $ShutdownTask -Xml $xml -Force | Out-Null
    Write-Log "Scheduled task `"$ShutdownTask`" registered."
}

# The filter sits on EVERY USB 3 root hub, real ones included (upstream design). It's listed under
# Enum\<hub>\Filters\*Upper, not in the UpperFilters property. Left behind on a real hub = that hub may not start.
function Get-HubsWithFilter {
    Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Enum\USB\ROOT_HUB30' -ErrorAction SilentlyContinue | Where-Object {
        $k = Get-Item -LiteralPath "$($_.PSPath)\Filters\*Upper" -ErrorAction SilentlyContinue
        $k -and ($k.GetValueNames() -contains 'usbip2_filter')
    } | ForEach-Object { "USB\ROOT_HUB30\$($_.PSChildName)" }
}

function Test-Leftover([string]$label, $items) {
    $items = @($items | Where-Object { $_ })
    Write-Log ('{0}: {1}' -f $label, $(if ($items) { $items -join ', ' } else { 'none' }))
    $items.Count -gt 0
}

# --- commands ---

function Setup-Clickr {
    $python = Find-Python
    if (-not $python) {
        if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
            Write-Log 'ERROR: no Python 3.11+ and no winget. Install Python from python.org (tick "Add python.exe to PATH"), then run Install.bat again.'
            return 1
        }
        Write-Log 'No Python 3.11+ found. Installing Python 3.14 with winget (takes a minute)...'
        # The python.org installer skips PATH by default. Clickr.bat needs pythonw on PATH.
        Invoke-Logged (Get-Command winget.exe).Source @('install', '--id', 'Python.Python.3.14', '--exact', '--scope', 'user', '--silent',
            '--accept-package-agreements', '--accept-source-agreements',
            '--override', '/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1') 900
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
        $python = Find-Python
        if (-not $python) { Write-Log 'ERROR: Python install failed. Install it from python.org, then run Install.bat again.'; return 1 }
    }
    Write-Log "Python: $python"

    Invoke-Logged $python @('-m', 'pip', 'install', '--user', '--upgrade', '--disable-pip-version-check', 'pystray', 'pillow') 600
    if ($LASTEXITCODE -ne 0) { Write-Log 'ERROR: pip install failed. See above.'; return 1 }

    $hasTask = Test-TaskExists $ShutdownTask
    if ((Get-WorkingController) -and $hasTask) { Write-Log 'Driver already installed and running. Done.'; return 0 }
    if (Get-WorkingController) { Write-Log 'Adding the shutdown task. Click Yes on the admin prompt.' }
    elseif (Get-Controller) { Write-Log 'Driver is installed but stopped. Restarting it. Click Yes on the admin prompt.' }
    else { Write-Log 'Installing the driver. Click Yes on the admin prompt. USB devices drop out for a second or two.' }
    if (Test-Admin) { Install-Clickr } else { Invoke-Elevated 'install' }
}

function Install-Clickr {
    $ErrorActionPreference = 'Stop'
    try {
        if (Get-OwnDriverPackages) {
            $stopped = @(Get-Controller | Where-Object { -not (Test-Started $_.InstanceId) })
            Register-ShutdownTask
            if (-not $stopped) { Write-Log 'Already installed and running.'; return 0 }
            # Happens after an interrupted uninstall: installed, but Windows never started it again.
            foreach ($c in $stopped) { Invoke-Logged "$System32\pnputil.exe" @('/restart-device', $c.InstanceId) 60 }
            if (Wait-Until { Get-WorkingController } 10) { Write-Log 'Driver restarted and running.'; return 0 }
            Write-Log 'ERROR: driver still stopped. Run Uninstall.bat, reboot, then Install.bat.'
            return 1
        }

        # uninstall must never touch these. Record them now.
        $preexisting = @(Get-PnpDevice -ErrorAction SilentlyContinue |
            Where-Object { $_.InstanceId -match $OwnDevicePattern } | ForEach-Object { $_.InstanceId })
        Write-Log "Pre-existing matching devices: $($preexisting -join ', ')"

        Invoke-Logged "$System32\pnputil.exe" @('/add-driver', "$DriverDir\usbip2_filter.inf", '/install')
        Invoke-Logged "$Bin\devnode.exe" @('install', "$DriverDir\usbip2_ude.inf", $UdeHwid) 60

        $packages = @(Get-OwnDriverPackages)
        foreach ($p in $packages) { Write-Log "Driver store: $($p.Driver) <- $(Split-Path $p.OriginalFileName -Leaf) $($p.Version)" }
        $controller = Get-Controller
        foreach ($d in $controller) { Write-Log "Device: $($d.InstanceId) [$($d.Status)] $($d.FriendlyName)" }
        Register-ShutdownTask

        [pscustomobject]@{
            InstalledAt = (Get-Date).ToString('s')
            Packages    = @($packages | ForEach-Object { $_.Driver })
            Preexisting = $preexisting
        } | ConvertTo-Json | Set-Content $StateFile -Encoding UTF8

        if ($packages.Count -ne 2 -or -not (Wait-Until { Get-WorkingController } 10)) {
            Write-Log 'WARNING: install incomplete (expected 2 driver packages and a working controller). Run "clickr.ps1 uninstall" to roll back.'
            return 1
        }
        Write-Log $(if ($script:RebootNeeded) { 'Install complete. Reboot to finish (works now, the hub filter loads after reboot).' } else { 'Install complete.' })
        0
    } catch {
        Write-Log "ERROR: $($_.Exception.Message)"
        1
    }
}

function Start-Clickr {
    if (-not (Get-WorkingController)) {
        Write-Log $(if (Get-Controller) { 'ERROR: driver is stopped. Run Install.bat to fix it.' } else { 'ERROR: driver not installed. Run Install.bat.' })
        return 1
    }
    $status = Get-DeviceStatus
    if (-not $status) {
        $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
        if (-not $pythonw) { Write-Log 'ERROR: pythonw.exe not found on PATH.'; return 1 }
        Write-Log "Starting device (polling $PollMs ms)"
        Start-Process $pythonw -ArgumentList "`"$Root\device.py`"", '--poll-ms', $PollMs -WorkingDirectory $Root -WindowStyle Hidden
        $status = Wait-Until { Get-DeviceStatus } 5
        if (-not $status) { Write-Log "ERROR: device didn't start. See device.log."; return 1 }
    }
    if ($status -match 'detached') {
        Invoke-Logged "$Bin\usbip.exe" @('attach', '-r', $Address, '-b', $BusId, '--once', '--receive-mode=low-latency') 30
        $status = Wait-Until { $s = Get-DeviceStatus; if ($s -match ' attached') { $s } } 5
    }
    if ("$status" -notmatch ' attached') { Write-Log 'ERROR: mouse did not attach.'; return 1 }  # quotes: $null -notmatch is not $true
    Write-Log "Status: $status"
    0
}

function Stop-Clickr {
    foreach ($port in Get-OwnPorts) { Invoke-Logged "$Bin\usbip.exe" @('detach', '-p', $port) 30 }
    Get-CimInstance Win32_Process -Filter "Name LIKE 'python%.exe'" |
        Where-Object { $_.CommandLine -like "*$Root\device.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Log "Stopped device (pid $($_.ProcessId))" }
    Write-Log 'Stopped.'
    0
}

function Uninstall-Clickr {
    $preexisting = @()
    if (Test-Path $StateFile) { $preexisting = @((Get-Content $StateFile -Raw | ConvertFrom-Json).Preexisting) }

    Get-CimInstance Win32_Process -Filter "Name LIKE 'python%.exe'" |
        Where-Object { $_.CommandLine -like "*$Root\clickr.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Log "Closed Clickr (pid $($_.ProcessId))" }
    Stop-Clickr | Out-Null
    if (Get-Controller) {
        Invoke-Logged "$Bin\devnode.exe" @('remove', $UdeHwid, 'root') 60
        if ($LASTEXITCODE -eq -1) { Write-Log 'Nothing else was changed. Reboot, then run Uninstall.bat again.'; return 1 }
    }

    # /uninstall also pulls the filter off the real USB 3 hubs. Skip it and those hubs keep a dead filter.
    foreach ($p in Get-OwnDriverPackages) {
        Invoke-Logged "$System32\pnputil.exe" @('/delete-driver', $p.Driver, '/uninstall', '/force')
    }
    foreach ($d in Get-OwnDevices $preexisting) {
        Invoke-Logged "$System32\pnputil.exe" @('/remove-device', $d.InstanceId)
    }
    $filteredHubs = @(Get-HubsWithFilter)
    foreach ($svc in $Services) {
        if (-not (Test-Path "HKLM:\SYSTEM\CurrentControlSet\Services\$svc")) { continue }
        # Never delete the filter's service while a hub still lists it: that hub (maybe your real USB) won't start.
        if ($svc -eq 'usbip2_filter' -and $filteredHubs) { Write-Log "Kept service usbip2_filter: still listed on $($filteredHubs -join ', ')."; continue }
        Invoke-Logged "$System32\sc.exe" @('delete', $svc)
    }
    foreach ($task in $Tasks) {
        if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $task -Confirm:$false
            Write-Log "Removed scheduled task `"$task`"."
        }
    }

    Write-Log '--- Verification ---'
    $problems = @(
        Test-Leftover 'Driver packages' (Get-OwnDriverPackages | ForEach-Object { $_.Driver })
        Test-Leftover 'Device entries' (Get-OwnDevices $preexisting | ForEach-Object { $_.InstanceId })
        Test-Leftover 'Services' ($Services | Where-Object { Test-Path "HKLM:\SYSTEM\CurrentControlSet\Services\$_" })
        Test-Leftover 'Driver store folders' (Get-ChildItem "$System32\DriverStore\FileRepository" -Directory -Filter 'usbip2_*' -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
        Test-Leftover 'Filter on USB root hubs' (Get-HubsWithFilter)
        Test-Leftover 'Scheduled tasks' ($Tasks | Where-Object { Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue })
    ) | Where-Object { $_ }

    if ($problems) {
        Write-Log "$(@($problems).Count) item(s) still present. Reboot and run uninstall again."
        return 1
    }
    Remove-Item $StateFile -ErrorAction SilentlyContinue
    Write-Log 'Uninstall complete. Nothing left behind (logs kept).'
    if ($script:RebootNeeded) { Write-Log 'Reboot to finish: Windows unloads the hub filter from memory then.' }
    0
}

if ($Command -in 'install', 'uninstall' -and -not (Test-Admin)) {
    New-Item -ItemType Directory -Force $LogDir | Out-Null
    exit (Invoke-Elevated $Command)
}
Start-Log $Command
$code = switch ($Command) {
    'setup'     { Setup-Clickr }
    'install'   { Install-Clickr }
    'start'     { Start-Clickr }
    'stop'      { Stop-Clickr }
    'uninstall' { Uninstall-Clickr }
}
exit [int](@($code)[-1])
