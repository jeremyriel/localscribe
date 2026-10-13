# Regression and desktop smoke testing

`python -m tests.test_workflow_e2e` exercises a synthetic, local-only workflow:
checksummed archive import, fixture-based processing, human text/speaker edits,
validation, SRT/VTT/DOCX/JSON output, and a verified archive round trip. Golden
fixtures cover each export and the resulting `.lsproj` manifest. No real audio,
personal data, network service, or Box account is used.

To intentionally refresh the golden outputs after reviewing a format change,
run:

```sh
LOCALSCRIBE_UPDATE_GOLDENS=1 python -m tests.test_workflow_e2e
```

Review all generated fixture changes before accepting them. The standalone
`test_project_archives` suite separately protects the version-1 archive fixture
and current version-2 import behavior. `test_box_sync` uses synthetic projects
and mocked local destinations; it must not be changed to contact Box or use
real research data.

The native packaging workflow builds and launches each supported desktop
artifact on macOS, Windows, and Linux. Before a release, run the regression
suite on each supported desktop runtime and perform an install-over-install
check: update the application while confirming that projects, settings,
downloaded models, and snapshots remain in the configured user data directory.
On Windows the installer is per-user; macOS and Linux launchers place mutable
data outside their application bundle.
