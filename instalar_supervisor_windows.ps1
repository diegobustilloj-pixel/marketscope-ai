param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$taskName = "PolyMarker QuantBot Supervisor"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$supervisor = Join-Path $projectRoot "supervisor_quantbot.py"

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    Write-Output "Tarea eliminada. Los collectors y monitores no fueron detenidos."
    exit 0
}

if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "Falta Python del proyecto: $pythonw"
}
if (-not (Test-Path -LiteralPath $supervisor -PathType Leaf)) {
    throw "Falta supervisor: $supervisor"
}

$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument ('"' + $supervisor + '" --monitor') `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Recupera seis procesos paper-only base y dos v0.15 condicionales de PolyMarker QuantBot."

Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
Write-Output "Tarea instalada: $taskName"
Write-Output "Inicio: al iniciar sesion $userId"
Write-Output "Privilegios: limitados"
