# Sightline privacy policy

Sightline is a local-only, passive screen-capture application. It has no telemetry, analytics, cloud service, account system, advertising, or automatic update check.

Sightline captures only the screen rectangle selected by the user while parsing is active. Capture can optionally be restricted to an explicit allowlist of foreground applications; when that restriction is enabled, capture pauses unless an allowed application is in the foreground. Images used by the first-run OCR test and normal parsing are processed in memory and are not persisted. Debug and replay features are disabled by default and should only be used with non-sensitive test material.

Settings, encounter data, exports, migration backups, and logs are stored under `%LOCALAPPDATA%\Sightline`. Uninstalling the application preserves this directory unless the user explicitly chooses to remove all data. A diagnostics export contains versions, sanitized settings, backend status, and rotating application logs. It excludes screenshots, capture coordinates, encounter text, exports, and the database.

The local web server binds only to `127.0.0.1`. A random secret created for each launch protects its API through an HttpOnly, SameSite cookie. Sightline does not intentionally transmit local data to its maintainers or any third party.

Questions or suspected privacy issues may be reported using the private security-reporting process in [SECURITY.md](SECURITY.md).
