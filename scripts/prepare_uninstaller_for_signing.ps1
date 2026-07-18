param(
    [Parameter(Mandatory = $true)][string]$IsccPath,
    [string]$ScriptPath = "packaging\Sightline.iss",
    [string]$SignedUninstallerDirectory = "build\signed-uninstallers"
)

$ErrorActionPreference = "Stop"
$SignedUninstallerDirectory = [System.IO.Path]::GetFullPath((Join-Path $PWD $SignedUninstallerDirectory))
if (Test-Path -LiteralPath $SignedUninstallerDirectory) {
    Remove-Item -LiteralPath $SignedUninstallerDirectory -Recurse -Force
}
New-Item -ItemType Directory -Path $SignedUninstallerDirectory | Out-Null
$env:SIGHTLINE_SIGNED_UNINSTALLER_DIR = $SignedUninstallerDirectory

$process = Start-Process -FilePath $IsccPath -ArgumentList @($ScriptPath) -WindowStyle Hidden -PassThru
$deadline = (Get-Date).AddMinutes(10)
$candidate = $null
while ((Get-Date) -lt $deadline) {
    $candidate = Get-ChildItem -LiteralPath $SignedUninstallerDirectory -Filter "*.e32" -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) {
        Start-Sleep -Milliseconds 500
        break
    }
    if ($process.HasExited) {
        throw "Inno Setup exited with code $($process.ExitCode) before producing its cached uninstaller"
    }
    Start-Sleep -Milliseconds 200
}
if (-not $candidate) {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    throw "Timed out waiting for Inno Setup's cached uninstaller"
}
Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
$process.WaitForExit()
Write-Host "Prepared unsigned Inno Setup uninstaller for SignPath: $($candidate.FullName)"
