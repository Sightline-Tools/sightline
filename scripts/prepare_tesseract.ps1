$ErrorActionPreference = "Stop"

# Backward-compatible entry point. Packaged builds no longer compile or ship a
# Tesseract CLI executable; the Python preparer audits the in-process wheel and
# downloads only the reviewed OCR model and license texts.
& python (Join-Path $PSScriptRoot "prepare_ocr_runtime.py") @args
if ($LASTEXITCODE -ne 0) {
    throw "In-process OCR runtime preparation failed with exit code $LASTEXITCODE"
}
