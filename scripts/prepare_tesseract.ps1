param(
    [string]$OutputDirectory = "tesseract"
)

$ErrorActionPreference = "Stop"
$TesseractCommit = "6e1d56a847e697de07b38619356550e5cf4e8633"
$TessdataFastCommit = "87416418657359cb625c412a48b6e1d6d41c29bd"
$VcpkgCommit = "8e8dfb4ba483886936ded5ca201b500b8d8b0096"
$WorkDirectory = Join-Path $env:RUNNER_TEMP "sightline-tesseract-5.5.2"
$TesseractSource = Join-Path $WorkDirectory "tesseract"
$TessdataSource = Join-Path $WorkDirectory "tessdata_fast"
$VcpkgSource = Join-Path $WorkDirectory "vcpkg"
$InstallDirectory = Join-Path $WorkDirectory "install"
$OutputDirectory = [System.IO.Path]::GetFullPath((Join-Path $PWD $OutputDirectory))

if (Test-Path -LiteralPath $WorkDirectory) {
    Remove-Item -LiteralPath $WorkDirectory -Recurse -Force
}
if (Test-Path -LiteralPath $OutputDirectory) {
    Remove-Item -LiteralPath $OutputDirectory -Recurse -Force
}
New-Item -ItemType Directory -Path $WorkDirectory, $OutputDirectory | Out-Null

git clone --quiet --no-checkout https://github.com/tesseract-ocr/tesseract.git $TesseractSource
git -C $TesseractSource checkout --quiet --detach $TesseractCommit
if ((git -C $TesseractSource rev-parse HEAD).Trim() -ne $TesseractCommit) {
    throw "Tesseract source verification failed"
}

git clone --quiet --no-checkout https://github.com/tesseract-ocr/tessdata_fast.git $TessdataSource
git -C $TessdataSource checkout --quiet --detach $TessdataFastCommit
if ((git -C $TessdataSource rev-parse HEAD).Trim() -ne $TessdataFastCommit) {
    throw "tessdata_fast source verification failed"
}

git clone --quiet --no-checkout https://github.com/microsoft/vcpkg.git $VcpkgSource
git -C $VcpkgSource checkout --quiet --detach $VcpkgCommit
if ((git -C $VcpkgSource rev-parse HEAD).Trim() -ne $VcpkgCommit) {
    throw "vcpkg source verification failed"
}
& (Join-Path $VcpkgSource "bootstrap-vcpkg.bat") -disableMetrics
$VcpkgExecutable = Join-Path $VcpkgSource "vcpkg.exe"
& $VcpkgExecutable install leptonica:x64-windows-static

cmake -S $TesseractSource -B (Join-Path $WorkDirectory "build") -A x64 `
    -DCMAKE_TOOLCHAIN_FILE="$VcpkgSource\scripts\buildsystems\vcpkg.cmake" `
    -DVCPKG_TARGET_TRIPLET=x64-windows-static `
    -DCMAKE_INSTALL_PREFIX="$InstallDirectory" `
    -DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded `
    -DBUILD_TRAINING_TOOLS=OFF `
    -DBUILD_TESTS=OFF `
    -DSW_BUILD=OFF `
    -DBUILD_SHARED_LIBS=OFF
if ($LASTEXITCODE -ne 0) {
    throw "Tesseract CMake configuration failed with exit code $LASTEXITCODE"
}
cmake --build (Join-Path $WorkDirectory "build") --config Release --target install --parallel
if ($LASTEXITCODE -ne 0) {
    throw "Tesseract CMake build failed with exit code $LASTEXITCODE"
}

Copy-Item -LiteralPath (Join-Path $InstallDirectory "bin\tesseract.exe") -Destination $OutputDirectory

$TessdataDestination = Join-Path $OutputDirectory "tessdata"
$LicenseDestination = Join-Path $OutputDirectory "licenses"
New-Item -ItemType Directory -Path $TessdataDestination, $LicenseDestination | Out-Null
Copy-Item -LiteralPath (Join-Path $TessdataSource "eng.traineddata") -Destination $TessdataDestination
Copy-Item -LiteralPath (Join-Path $TesseractSource "LICENSE") -Destination (Join-Path $LicenseDestination "Tesseract-Apache-2.0.txt")
Copy-Item -LiteralPath (Join-Path $TessdataSource "LICENSE") -Destination (Join-Path $LicenseDestination "tessdata_fast-Apache-2.0.txt")

$Policy = Get-Content -LiteralPath (Join-Path $PSScriptRoot "..\packaging\redistribution-licenses.json") -Raw | ConvertFrom-Json
$ReviewedLicenses = @{}
foreach ($Property in $Policy.native_packages.PSObject.Properties) {
    $ReviewedLicenses[$Property.Name] = [string]$Property.Value
}
$Components = @(
    [pscustomobject]@{ name = "Tesseract OCR"; version = "5.5.2"; license = "Apache-2.0"; source = "https://github.com/tesseract-ocr/tesseract" },
    [pscustomobject]@{ name = "tessdata_fast English model"; version = $TessdataFastCommit; license = "Apache-2.0"; source = "https://github.com/tesseract-ocr/tessdata_fast" }
)
$InstalledLines = & $VcpkgExecutable list
foreach ($Line in $InstalledLines) {
    if ($Line -notmatch '^([^:]+):x64-windows-static\s+(\S+)') {
        continue
    }
    $PackageName = $Matches[1]
    $PackageVersion = $Matches[2]
    if (-not $ReviewedLicenses.ContainsKey($PackageName)) {
        throw "Native redistribution review is missing for vcpkg package: $PackageName"
    }
    $Copyright = Join-Path $VcpkgSource "installed\x64-windows-static\share\$PackageName\copyright"
    if (-not (Test-Path -LiteralPath $Copyright)) {
        throw "Native license text is missing for vcpkg package: $PackageName"
    }
    Copy-Item -LiteralPath $Copyright -Destination (Join-Path $LicenseDestination "$PackageName.txt")
    $Components += [pscustomobject]@{
        name = $PackageName
        version = $PackageVersion
        license = $ReviewedLicenses[$PackageName]
        source = "https://github.com/microsoft/vcpkg/tree/$VcpkgCommit/ports/$PackageName"
    }
}
$Components | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $OutputDirectory "native-components.json") -Encoding utf8

$VersionOutput = & (Join-Path $OutputDirectory "tesseract.exe") --version 2>&1 | Out-String
if ($VersionOutput -notmatch "tesseract 5\.5\.2") {
    throw "Expected Tesseract 5.5.2, received: $VersionOutput"
}
Write-Host "Prepared verified static Tesseract 5.5.2 runtime and pinned tessdata_fast English model."
