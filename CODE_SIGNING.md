# Sightline release-signing status

Sightline public-beta installers are currently released unsigned. This is a deliberate first-stage distribution policy while the project establishes a public release history and user reputation before applying SignPath Foundation signing.

The release workflow still requires:

- a tag whose `vX.Y.Z` value exactly matches `pyproject.toml`;
- a GitHub-hosted clean build that passes tests and packaging checks;
- explicit private release authorization before Gatekeeper creates the public version tag;
- explicit verification that Sightline-owned executables, the uninstaller, and the installer have Authenticode status `NotSigned`;
- install, OCR, upgrade, uninstall, Microsoft Defender, and clean-machine acceptance checks;
- published SHA-256 checksums, a CycloneDX SBOM, third-party notices, and GitHub build provenance.

Windows will show `Unknown publisher`, and Microsoft SmartScreen may warn before running a release. Users should download `SHA256SUMS.txt` from the same GitHub release and verify the installer before running it:

```powershell
$expected = (Get-Content .\SHA256SUMS.txt).Split()[0]
$actual = (Get-FileHash .\Sightline-Setup-x64-vX.Y.Z.exe -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw "Sightline installer checksum mismatch" }
Get-AuthenticodeSignature .\Sightline-Setup-x64-vX.Y.Z.exe
```

The final command should report `NotSigned` during this release stage. Do not proceed if the checksum differs.

When SignPath is introduced, the release workflow and this policy will be changed together. Signing keys will remain in SignPath-managed hardware security modules, and public assets will then be required to have valid Authenticode signatures. The planned configuration is documented in `packaging/SIGNPATH.md`.
