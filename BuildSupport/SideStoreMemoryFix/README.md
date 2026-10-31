# LiveContainer + standalone SideStore: Increased Memory Limit build

This branch builds official **standalone SideStore from source**, applies the
existing human-authored [SideSign PR #4](https://github.com/SideStore/SideSign/pull/4),
and embeds that app using LiveContainer's SideStoreSupport/dylibify integration.
It does not download the prebuilt `LiveContainer/SideStore` fork.

The fix addresses [SideStore issue #1616](https://github.com/SideStore/SideStore/issues/1616):
allow Increased Memory Limit for free teams and send Apple's v1 capability
enablement request before obtaining the provisioning profile. It is an
experimental application of an open upstream PR. Its best-effort failure path
can still install an app without the capability; an installed-device check is
required. It does not add a separate EVA capability-enablement implementation.

## Build outputs

Push this branch or run **Actions → LiveContainer with standalone SideStore
memory fix**. The artifact `LiveContainer-StandaloneSideStore-memory-fix` contains:

- `SideStore-standalone-memory-fix.ipa`
- `LiveContainer+SideStore-standalone-memory-fix.ipa`
- `BUILD-MANIFEST.json` with exact source revisions, patch and tool hashes.

The Mac job runs the PR's capability tests with mocked networking, archives
standalone SideStore and LiveContainer, then validates the resulting layout and
embedded entitlement requests. All full build logs are uploaded separately.
No release, upstream PR, issue, or comment is automatically published.

These IPAs carry **ad-hoc entitlement requests**, not Apple-trusted installation
signatures. Sign/install them with your own provisioning profile and certificate.
The Increased Memory Limit request is embedded in the standalone SideStore and
LiveContainer executable signatures, and LiveProcess keeps its existing request.
The packager checks the signature contents with `ldid -e`; a separate plist alone
is not considered sufficient. The embedded SideStore dylib has no independent
application entitlements: its running process uses the LiveContainer host's.

## Installation and device verification

1. Back up LiveContainer guest apps/data and keep a working installer available.
   Use the same Apple team and bundle IDs for an in-place update.
2. Install/update the standalone patched SideStore IPA with your current signing
   tool. The patch does not require SideStore itself to have a higher RAM limit
   just to run the signing operation.
3. Use that patched standalone SideStore to **import and sign** the combined IPA.
   Keep app extensions so LiveContainer's multitasking and widget components are
   retained.
   Reusing an old installed app via Refresh All alone does not establish that its
   capability, provisioning profile and executable request were all updated.
4. Launch the combined app, check that its built-in SideStore opens, and test a
   normal installation/refresh before depending on the custom build.
5. Run the game inside LiveContainer and inspect `[IOS_MEMORY_ENTITLEMENTS]` in
   `generals-stderr.log`. The relevant result is the **running process's**
   `increased_memory_limit=true`; source files or a successful signing operation
   alone do not prove it was retained. `[IOS_MEMORY]` gives separate OS headroom.

Apple/device policy determines the final memory allowance. This build does not
promise a fixed RAM amount, prove the cause of a long-session crash, or repair a
graphics allocation leak. Device login, provisioning, background refresh, widget
and Shortcuts behavior still need validation after this source-build integration.

## Source provenance and checks

`sources.json` pins official SideStore and its SideSign submodule base, identifies
the original PR commit/parent, and hashes the verbatim patch. Preparation rejects
a mismatched base or patch and accepts no moving branch as the source version.
The existing dylibify 1.0 release is checksum-verified before execution.
An additional checksum-pinned **test-only** patch adds the missing Foundation
import to an existing SideSign test file so it compiles with the selected Swift
toolchain. It does not alter production signing behavior or the original PR.

No SideStore application source contribution is generated here. The checked-out
SideStore repository is used as a build input and consumes the existing human PR
and the separate test-import compatibility fix in its dependency. The custom workflow, packaging and tests live in this
LiveContainer fork. No Apple IDs, passwords, pairing records, provisioning
profiles, certificates, private keys or device identifiers are required by CI.

Run the portable packaging checks with:

```sh
python3 BuildSupport/SideStoreMemoryFix/test_package.py
```

They use explicit stand-ins for native signing/conversion and real file/ZIP
operations. Native compilation and actual signature extraction run on macOS;
these host fixtures do not establish device provisioning or runtime success.
