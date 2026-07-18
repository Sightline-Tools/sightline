# Future SignPath project configuration

Sightline currently publishes explicitly unsigned public-beta installers. This document is retained as the transition plan for enabling SignPath after the project has established a public release history and reputation; none of these variables are required by the current unsigned workflow.

Create a SignPath project for this repository, install the SignPath GitHub App, and configure a release signing policy that requires MFA-backed submitter, reviewer, and approver roles. Configure three artifact configurations:

1. `sightline-application`: a GitHub artifact ZIP whose Sightline-owned application executable (`Sightline.exe`) is Authenticode-signed while preserving the ZIP layout. Do not re-sign third-party Python/runtime DLLs, including the native libraries shipped by the `tesserocr` wheel.
2. `sightline-uninstaller`: a GitHub artifact ZIP containing Inno Setup's cached `.e32` uninstaller stub, which is Authenticode-signed before being embedded in Setup.
3. `sightline-installer`: a GitHub artifact ZIP containing `Sightline-Setup-x64-vX.Y.Z.exe`, which is Authenticode-signed.

Set these repository variables:

- `SIGNPATH_ORGANIZATION_ID`
- `SIGNPATH_PROJECT_SLUG`
- `SIGNPATH_SIGNING_POLICY_SLUG`
- `SIGNPATH_APPLICATION_ARTIFACT_CONFIGURATION_SLUG`
- `SIGNPATH_UNINSTALLER_ARTIFACT_CONFIGURATION_SLUG`
- `SIGNPATH_INSTALLER_ARTIFACT_CONFIGURATION_SLUG`

Set `SIGNPATH_API_TOKEN` as an Actions secret. The token must belong to a SignPath user with submitter permission only. The private Gatekeeper release authorization remains the final manual gate before the public version tag is created; SignPath approval then governs each signing stage.
