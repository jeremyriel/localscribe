# Distribution and code-signing feasibility

**Decision record — reviewed 2026-10-12.** This is a feasibility assessment,
not a claim of institutional approval, vendor eligibility, or signed releases.
Prices, platform rules, and program terms can change; confirm them before
committing funds or credentials.

## Decision

- Keep the existing per-user Windows installer, macOS app/DMG, and Linux
  AppImage for now. Do not add MSIX or Store packaging until a native
  compatibility spike passes the checks below.
- Pursue macOS Developer ID/notarization only after the project owner confirms
  who can enroll and whether an eligible educational institution will sponsor
  enrollment or request Apple's fee waiver.
- Ask SignPath Foundation about Windows signing after confirming the source
  repository and release ownership. The MIT license is a promising eligibility
  factor, but there is no guaranteed acceptance; do not make a release depend on
  it.
- Until a signing route is approved and operating, continue to disclose the
  unsigned Windows/macOS warnings. Linux does not use platform code signing.

## Options compared

| Route | Cost and user friction | Work and supportability | Decision |
|---|---|---|---|
| Current installers | No certificate cost; users see first-run Gatekeeper/SmartScreen warnings | Low incremental work; existing native build and launch smoke tests | Keep as the supported baseline |
| macOS Developer ID + notarization | Apple Developer Program is typically USD $99/year; eligible educational institutions, nonprofits, or government entities may request a waiver. Enrollment and notarization reduce first-run friction | Medium initial CI/keychain/notary work and recurring certificate/notary maintenance; familiar direct-download path | Investigate an institutional sponsor/waiver; approval is not assumed |
| Windows signing certificate or Azure Artifact Signing | Certificate/service pricing varies by provider, identity type, and region; signing reduces publisher warnings over time but does not promise immediate SmartScreen reputation | Medium CI integration, identity verification, renewal, and signature verification work | Re-price and verify availability when ownership and release frequency are known |
| SignPath Foundation | No signing charge if accepted; publisher identity is SignPath Foundation and users must be told | Application, eligibility review, disclosure, controlled build/signing workflow, and external approval | Apply only after release ownership and prerequisites are confirmed |
| Windows MSIX / Microsoft Store | Store and signing terms must be checked for the selected distribution route; Store discovery may lower install friction | High packaging, certification, update, and support work | No-go until a compatibility spike demonstrates the app works |

## Windows MSIX compatibility assessment

Local Scribe's current Windows installer already keeps writable user data in
`%LOCALAPPDATA%\Local Scribe`, separate from the installed application under
`%LOCALAPPDATA%\Programs\Local Scribe`. macOS and Linux launchers similarly put
mutable state in Application Support and the XDG user data directory. This is a
good starting point: MSIX package files are not a writable data store and must
not hold projects, settings, downloaded models, or the virtual environment.

The existing MSIX feasibility is **unproven**, not a pass. Before building an
MSIX prototype, test on clean supported Windows versions:

1. Install and launch the bundled Python/runtime with packaged files treated as
   read-only; verify the app never writes into the package location.
2. Create, close, update, and reopen synthetic project data, settings, snapshots,
   downloaded-model state, and vocabulary profiles in a persistent per-user
   location. Define and test migration from the existing Inno Setup data folder.
3. Download a model and runtime packages on demand, then run CPU transcription
   with VAD. Check CTranslate2, ONNX Runtime, Python extension DLL loading,
   WebView startup, Offline Lock, disk accounting, and cancellation.
4. Install a newer package over an existing one and confirm projects and models
   survive. Check uninstall/reinstall behavior and document what data remains.
5. Verify Store/sideload signing, update delivery, model-download disclosures,
   and support diagnostics against current Microsoft requirements.

MSIX's protected install location and package identity/update model add meaningful
compatibility work. Until the spike passes, the per-user Inno Setup package has
lower migration and support risk.

## macOS Developer ID and waiver path

Direct distribution requires a Developer ID signing identity and notarization
workflow. Sign the app's executables and nested code with the hardened runtime,
submit the final app/DMG using Apple's supported notarization tooling, staple the
ticket where supported, and test Gatekeeper on a clean machine and while
offline. The bundled Python, launcher, and any code shipped inside the app must
be included in the signing/verification inventory; runtime packages and models
are installed into user data, not the sealed app bundle.

Apple documents fee waivers for qualifying nonprofit organizations, accredited
educational institutions, and government entities distributing free apps. The
possible U of I path is for the responsible legal entity or eligible unit to
confirm its enrollment authority and request the waiver through Apple's
Developer Program process. Local Scribe's association with a university does
not itself establish eligibility, sponsorship, or approval.

## SignPath Foundation eligibility

The repository uses the MIT License, an OSI-approved license. That alone is not
enough. SignPath's public conditions also require an actively maintained,
publicly released project, source-to-artifact ownership and build transparency,
and compliance with its disclosure and program conditions. Acceptance is
reviewed by the Foundation and is not promised.

This `Andrew-Copilot/localscribe` repository currently has no GitHub Releases
listed, while the README links to a separate release host. Before applying,
confirm which organization owns the maintained source, build scripts, and
Windows release artifacts, and whether the project has an eligible public
release in the intended signed form. Review the licenses and provenance of
bundled components and be prepared to disclose SignPath Foundation as the
publisher. Do not treat the program as available until an application is
accepted.

## CI credentials and verification plan

There are no code-signing credentials in the current build workflow. If signing
is approved:

- Separate build, sign, and publish permissions. Keep routine build jobs
  read-only; give release upload permissions only to the protected release job.
- Run signing only for trusted release events, never for pull requests from
  forks. Put credentials in a protected GitHub Actions environment with required
  reviewers and least privilege; never place private keys, PFX passwords, API
  keys, or tokens in source, artifacts, logs, or fixture files.
- Prefer short-lived workload identity/OIDC for a supported Azure Artifact
  Signing workflow. For Apple, keep the signing identity and notarization API
  key in the protected environment, import them into a temporary keychain, and
  remove temporary key material after signing. If SignPath accepts the project,
  use its controlled signing service rather than copying a private key into CI.
- Verify signatures before uploading and again in a clean release-download
  smoke test. Generate SHA-256 checksums for all release assets; checksums
  alone detect transfer errors but do not authenticate the publisher.

Verification commands to include in the eventual platform jobs:

```sh
# macOS
codesign --verify --deep --strict --verbose=2 "Local Scribe.app"
spctl --assess --type execute --verbose=2 "Local Scribe.app"
xcrun stapler validate "Local Scribe.app"

# Windows (PowerShell)
Get-AuthenticodeSignature .\LocalScribe-*.exe
signtool verify /pa /all /v .\LocalScribe-*.exe
```

The Windows job should require a valid signature and the expected publisher,
not only a successful command exit. The macOS job should require a valid
Developer ID chain, notarization assessment, and a valid stapled ticket.

## References

- [Apple Developer ID signing](https://developer.apple.com/developer-id/)
- [Apple notarization guidance](https://developer.apple.com/documentation/security/notarizing_macos_software_before_distribution)
- [Apple membership fee waivers](https://developer.apple.com/help/account/membership/fee-waivers/)
- [Microsoft MSIX overview](https://learn.microsoft.com/en-us/windows/msix/overview)
- [MSIX packaged desktop app behavior](https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes)
- [Microsoft Artifact Signing](https://learn.microsoft.com/en-us/azure/artifact-signing/)
- [GitHub Actions OIDC with Azure](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-azure)
- [SignPath Foundation application conditions](https://signpath.org/apply.html)
- [SignPath Foundation terms](https://signpath.org/terms.html)
