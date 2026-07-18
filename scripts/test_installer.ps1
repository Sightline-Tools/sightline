param(
    [Parameter(Mandatory = $true)][string]$InstallerPath,
    [ValidateSet("Valid", "NotSigned")][string]$ExpectedSignatureStatus = "Valid"
)

$ErrorActionPreference = "Stop"
if (-not [System.IO.Path]::IsPathRooted($InstallerPath)) {
    $InstallerPath = Join-Path $PWD $InstallerPath
}
$InstallerPath = [System.IO.Path]::GetFullPath($InstallerPath)
$InstallDirectory = Join-Path $env:LOCALAPPDATA "Programs\Sightline"
$DataDirectory = Join-Path $env:LOCALAPPDATA "Sightline"
$RuntimeState = Join-Path $DataDirectory "runtime.json"

function Invoke-CheckedProcess([string]$Path, [string[]]$Arguments) {
    $Process = Start-Process -FilePath $Path -ArgumentList $Arguments -WindowStyle Hidden -Wait -PassThru
    if ($Process.ExitCode -ne 0) {
        throw "$Path failed with exit code $($Process.ExitCode)"
    }
}

function Assert-SignatureStatus([string]$Path) {
    $Signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($Signature.Status.ToString() -ne $ExpectedSignatureStatus) {
        throw "Unexpected Authenticode status for $Path`: expected $ExpectedSignatureStatus, got $($Signature.Status)"
    }
}

Assert-SignatureStatus $InstallerPath
Invoke-CheckedProcess $InstallerPath @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/NOICONS")
$Application = Join-Path $InstallDirectory "Sightline.exe"
$Tessdata = Join-Path $InstallDirectory "_internal\tesseract\tessdata\eng.traineddata"
$TesseractDll = Join-Path $InstallDirectory "_internal\tesserocr\tesseract55.dll"
$TesserocrExtension = Get-ChildItem -LiteralPath (Join-Path $InstallDirectory "_internal\tesserocr") -Filter "tesserocr*.pyd" -File -ErrorAction SilentlyContinue | Select-Object -First 1
$TclRuntime = Join-Path $InstallDirectory "_internal\_tcl_data\init.tcl"
$TkRuntime = Join-Path $InstallDirectory "_internal\_tk_data\tk.tcl"
$ReplayHarness = Join-Path $InstallDirectory "_internal\app\static\replay.html"
$OpenDyslexicFont = Join-Path $InstallDirectory "_internal\app\fonts\OpenDyslexic-Bold.otf"
$OpenDyslexicLicense = Join-Path $InstallDirectory "_internal\app\fonts\OFL.txt"
$OpenDyslexicFaq = Join-Path $InstallDirectory "_internal\app\fonts\OFL-FAQ.txt"
$Uninstaller = (Get-ChildItem -LiteralPath $InstallDirectory -Filter "unins*.exe" -File | Select-Object -First 1).FullName
Assert-SignatureStatus $Application
Assert-SignatureStatus $Uninstaller
if (-not (Test-Path -LiteralPath $Tessdata) -or -not (Test-Path -LiteralPath $TesseractDll) -or $null -eq $TesserocrExtension) {
    throw "Installer omitted the in-process tesserocr backend or English OCR model"
}
if (-not (Test-Path -LiteralPath $TclRuntime) -or -not (Test-Path -LiteralPath $TkRuntime)) {
    throw "Installer omitted the Tcl/Tk runtime required by region selection and overlays"
}
if (-not (Test-Path -LiteralPath $ReplayHarness)) {
    throw "Installer omitted the user-toggleable replay harness"
}
if (-not (Test-Path -LiteralPath $OpenDyslexicFont) -or -not (Test-Path -LiteralPath $OpenDyslexicLicense) -or -not (Test-Path -LiteralPath $OpenDyslexicFaq)) {
    throw "Installer omitted the OpenDyslexic font or its license files"
}
$ForbiddenBundlePaths = @(
    "_internal\replay",
    "_internal\sample1.png",
    "_internal\sample2.png",
    "_internal\ocr_composite_cli.py",
    "_internal\tesseract_test.py",
    "_internal\tesseract_color_test.py",
    "_internal\tesseract_composite_test.py",
    "_internal\tesseract\tesseract.exe",
    "_internal\pytesseract",
    "_internal\brand\web\brand-preview.html",
    "_internal\brand\web\sightline-theme.css",
    "_internal\migrations\script.py.mako"
)
foreach ($RelativePath in $ForbiddenBundlePaths) {
    if (Test-Path -LiteralPath (Join-Path $InstallDirectory $RelativePath)) {
        throw "Installer unexpectedly contains developer-only payload: $RelativePath"
    }
}
Invoke-CheckedProcess $Application @("--smoke-test")

New-Item -ItemType Directory -Path $DataDirectory -Force | Out-Null
$Marker = Join-Path $DataDirectory "acceptance-marker.txt"
Set-Content -LiteralPath $Marker -Value "preserve-me" -Encoding utf8

# Occupy the preferred port to verify fallback selection.
$Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 8765)
$Listener.Start()
$ApplicationProcess = Start-Process -FilePath $Application -ArgumentList @("--no-browser") -WindowStyle Hidden -PassThru
$Deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $Deadline -and -not (Test-Path -LiteralPath $RuntimeState)) {
    Start-Sleep -Milliseconds 100
}
if (-not (Test-Path -LiteralPath $RuntimeState)) {
    throw "Sightline did not create runtime state"
}
$State = Get-Content -LiteralPath $RuntimeState -Raw | ConvertFrom-Json
if ($State.port -eq 8765) {
    throw "Sightline did not fall back from occupied port 8765"
}
$BaseUrl = "http://127.0.0.1:$($State.port)"
$Health = Invoke-RestMethod -Uri "$BaseUrl/api/health"
if ($Health.status -ne "ok") {
    throw "Sightline health check failed"
}
$Unauthorized = Invoke-WebRequest -Uri "$BaseUrl/api/app-info" -SkipHttpErrorCheck
if ($Unauthorized.StatusCode -ne 401) {
    throw "Sightline API did not reject an unauthenticated request"
}
$Session = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
Invoke-WebRequest -Uri "$BaseUrl/launch?token=$($State.token)" -WebSession $Session -MaximumRedirection 5 | Out-Null
$AppInfo = Invoke-RestMethod -Uri "$BaseUrl/api/app-info" -WebSession $Session
if ($AppInfo.name -ne "Sightline") {
    throw "Sightline authenticated launch handshake failed"
}
$Listener.Stop()

# A second launch exits without starting a second server.
Invoke-CheckedProcess $Application @("--no-browser")
Invoke-CheckedProcess $Application @("--shutdown")
$ApplicationProcess.WaitForExit(15000) | Out-Null
if (-not $ApplicationProcess.HasExited) {
    throw "Sightline did not shut down cleanly"
}

# In-place upgrade must preserve data.
Invoke-CheckedProcess $InstallerPath @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/NOICONS")
if (-not (Test-Path -LiteralPath $Marker)) {
    throw "Sightline upgrade did not preserve user data"
}

$Uninstaller = (Get-ChildItem -LiteralPath $InstallDirectory -Filter "unins*.exe" -File | Select-Object -First 1).FullName
Assert-SignatureStatus $Uninstaller
Invoke-CheckedProcess $Uninstaller @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
if (-not (Test-Path -LiteralPath $Marker)) {
    throw "Default uninstall removed user data"
}

# Explicit all-data removal is available and defaults off.
Invoke-CheckedProcess $InstallerPath @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/NOICONS")
$Uninstaller = (Get-ChildItem -LiteralPath $InstallDirectory -Filter "unins*.exe" -File | Select-Object -First 1).FullName
Invoke-CheckedProcess $Uninstaller @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/REMOVEUSERDATA")
if (Test-Path -LiteralPath $DataDirectory) {
    throw "Explicit all-data uninstall did not remove Sightline user data"
}
Write-Host "Sightline install, OCR, launch, authentication, upgrade, shutdown, and uninstall acceptance passed."
