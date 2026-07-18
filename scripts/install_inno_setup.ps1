$ErrorActionPreference = "Stop"
$Version = "6.7.3"
$ExpectedSha256 = "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732"
$Url = "https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe"
$TempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [System.IO.Path]::GetTempPath() }
$Installer = Join-Path $TempRoot "innosetup-$Version.exe"
$InstallDirectory = Join-Path $TempRoot "InnoSetup-$Version"

Invoke-WebRequest -Uri $Url -OutFile $Installer
$ActualSha256 = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ActualSha256 -ne $ExpectedSha256) {
    throw "Inno Setup download hash mismatch: $ActualSha256"
}
$Signature = Get-AuthenticodeSignature -LiteralPath $Installer
if ($Signature.Status -ne "Valid") {
    throw "Inno Setup download has invalid Authenticode status: $($Signature.Status)"
}
$Process = Start-Process -FilePath $Installer -ArgumentList @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/DIR=$InstallDirectory"
) -WindowStyle Hidden -Wait -PassThru
if ($Process.ExitCode -ne 0) {
    throw "Inno Setup installation failed with exit code $($Process.ExitCode)"
}
$Compiler = Join-Path $InstallDirectory "ISCC.exe"
if (-not (Test-Path -LiteralPath $Compiler)) {
    throw "Inno Setup compiler was not installed at $Compiler"
}
Write-Output $Compiler
