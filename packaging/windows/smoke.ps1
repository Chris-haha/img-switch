param(
    [Parameter(Mandatory=$true)][string]$Installer,
    [string]$EvidenceDirectory
)
$ErrorActionPreference = 'Stop'
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('img-switch-smoke-' + [guid]::NewGuid().ToString('N'))
$appDir = Join-Path $testRoot 'Installed App'
$homeDir = Join-Path $testRoot 'Test Home'
$appDataDir = Join-Path $testRoot 'AppData'
$pythonPath = if ($env:PYTHON) { (Resolve-Path -LiteralPath $env:PYTHON -ErrorAction Stop).Path } else { (Get-Command python -ErrorAction Stop).Source }
if (!$EvidenceDirectory) {
    $EvidenceDirectory = Join-Path (Split-Path -Parent $installerPath) ('windows-acceptance-' + [guid]::NewGuid().ToString('N'))
}
$evidence = [IO.Path]::GetFullPath($EvidenceDirectory)
if ((Test-Path -LiteralPath $evidence) -and @(Get-ChildItem -LiteralPath $evidence -Force).Count -gt 0) {
    throw 'Evidence directory must be empty so a prior report cannot masquerade as this run.'
}
$mockProcess = $null
$previous = @{}
function Invoke-CheckedProcess([string]$FilePath, [string[]]$Arguments) {
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -PassThru
    if (!$process.WaitForExit(120000)) {
        & (Join-Path $env:SystemRoot 'System32\taskkill.exe') /PID $process.Id /T /F | Out-Null
        throw "Process timed out: $FilePath"
    }
    $process.Refresh()
    if ($process.ExitCode -ne 0) { throw "Process failed with exit code $($process.ExitCode): $FilePath" }
}
foreach ($name in @('APPDATA','FAILOVER_IMAGEGEN_APP_HOME','FAILOVER_IMAGEGEN_TOOL_CONFIG',
        'FAILOVER_IMAGEGEN_JOBS_DIR','FAILOVER_IMAGEGEN_APP_SMOKE','FAILOVER_IMAGEGEN_APP_SELFTEST',
        'FAILOVER_IMAGEGEN_CHILD_OUT','PYTHONHOME','PYTHONPATH','PYTHONDONTWRITEBYTECODE','PATH')) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
New-Item -ItemType Directory -Force -Path $testRoot, $homeDir, $appDataDir, $evidence | Out-Null
try {
    foreach ($hive in @('HKCU:', 'HKLM:')) {
        if (Test-Path "$hive\Software\Microsoft\Windows\CurrentVersion\Uninstall\{AD6F616E-EE64-467A-8DD7-935BCB23A218}_is1") {
            throw 'An IMG Switch installation already exists; run this smoke test on an isolated machine.'
        }
    }
    $env:APPDATA = $appDataDir
    $env:FAILOVER_IMAGEGEN_APP_HOME = $homeDir
    $env:FAILOVER_IMAGEGEN_TOOL_CONFIG = Join-Path $appDataDir 'failover-imagegen-tool\config.json'
    $env:FAILOVER_IMAGEGEN_JOBS_DIR = Join-Path $appDataDir 'failover-imagegen-tool\jobs'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    Remove-Item Env:FAILOVER_IMAGEGEN_APP_SMOKE, Env:FAILOVER_IMAGEGEN_APP_SELFTEST, Env:FAILOVER_IMAGEGEN_CHILD_OUT -ErrorAction SilentlyContinue
    $portFile = Join-Path $testRoot 'mock-port.txt'
    $mockScript = Join-Path $PSScriptRoot 'mock_server.py'
    $mockProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-B', ('"' + $mockScript + '"'), ('"' + $portFile + '"')) -PassThru -WindowStyle Hidden
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    while (!(Test-Path -LiteralPath $portFile) -and [DateTime]::UtcNow -lt $deadline) {
        if ($mockProcess.HasExited) { throw 'Local mock server exited before becoming ready' }
        Start-Sleep -Milliseconds 100
    }
    if (!(Test-Path -LiteralPath $portFile)) { throw 'Local mock server startup timed out' }
    $port = (Get-Content -LiteralPath $portFile -Raw).Trim()
    if ($port -notmatch '^\d+$') { throw 'Local mock server returned invalid port' }
    # JPEG uses the built-in PowerShell/GDI+ decoder. Neither system directory
    # contains Python; the controller keeps its previously resolved absolute path.
    $env:PATH = (Join-Path $env:SystemRoot 'System32') + ';' + (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0')
    Remove-Item Env:PYTHONHOME, Env:PYTHONPATH -ErrorAction SilentlyContinue
    Invoke-CheckedProcess $installerPath @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
        ('/DIR="' + $appDir + '"'), ('/LOG="' + (Join-Path $evidence 'install.log') + '"'))
    $app = Join-Path $appDir 'IMG-Switch.exe'
    if (!(Test-Path -LiteralPath $app)) { throw 'Installed app missing' }
    $native = Join-Path $PSScriptRoot 'native_smoke.py'
    & $pythonPath -B $native --phase exercise --app $app --work $testRoot --evidence $evidence --port $port
    if ($LASTEXITCODE -ne 0) { throw 'Installed Windows GUI/engine acceptance failed; see windows-native-smoke.json' }
    # Additional source-level native tests force the fallback termination branch
    # with real child and grandchild processes; these are distinct from package acceptance.
    & $pythonPath -B (Join-Path $PSScriptRoot 'test_native_processes.py') -v 2>&1 | Tee-Object -FilePath (Join-Path $evidence 'native-process-tests.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Native Windows process-tree tests failed' }
    $uninstaller = Join-Path $appDir 'unins000.exe'
    Invoke-CheckedProcess $uninstaller @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
        ('/LOG="' + (Join-Path $evidence 'uninstall.log') + '"'))
    & $pythonPath -B $native --phase verify-uninstall --app $app --work $testRoot --evidence $evidence
    if ($LASTEXITCODE -ne 0) { throw 'Uninstall byte-preservation acceptance failed' }
    Write-Output 'Windows installed GUI/engine acceptance and native process-tree tests passed (loopback image provider).'
} finally {
    if ($null -ne $mockProcess -and !$mockProcess.HasExited) { Stop-Process -Id $mockProcess.Id -Force -ErrorAction SilentlyContinue }
    $uninstaller = Join-Path $appDir 'unins000.exe'
    if (Test-Path -LiteralPath $uninstaller) {
        try { Invoke-CheckedProcess $uninstaller @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART') } catch { Write-Warning "Uninstaller cleanup failed: $_" }
    }
    foreach ($name in $previous.Keys) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    $report = Join-Path $evidence 'windows-native-smoke.json'
    if (Test-Path -LiteralPath $report) { Get-Content -LiteralPath $report -Raw | Write-Output }
    Write-Output "Windows acceptance evidence: $evidence"
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}
