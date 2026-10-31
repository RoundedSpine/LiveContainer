#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
build_root="${RUNNER_TEMP:-$repo_root/.memory-build}/livecontainer-memory-fix"
log_root="$repo_root/build-logs-memory"
mkdir -p "$log_root"

python3 "$repo_root/BuildSupport/SideStoreMemoryFix/prepare.py" "$build_root" 2>&1 | tee "$log_root/prepare.log"
side_source="$build_root/SideStore"

# The PR's URLProtocol tests use synthetic credentials and never contact Apple.
swift test --package-path "$side_source/Dependencies/SideSign" --jobs 2 --filter CapabilityTests \
    2>&1 | tee "$log_root/sidesign-capability-tests.log"

# Standalone upstream's CLI build/fakesign flow; no Apple account or certificate.
(
    cd "$side_source"
    xcodebuild archive -archivePath "$side_source/SideStore" \
        -project AltStore.xcodeproj -scheme SideStore -sdk iphoneos \
        -configuration Release -arch arm64 -jobs 2 \
        -clonedSourcePackagesDirPath "$repo_root/.memory-build/SourcePackages" \
        CODE_SIGNING_REQUIRED=NO CODE_SIGNING_ALLOWED=NO AD_HOC_CODE_SIGNING_ALLOWED=YES \
        DEVELOPMENT_TEAM=XYZ0123456 ORG_IDENTIFIER=com.SideStore
    make fakesign
) 2>&1 | tee "$log_root/sidestore-build.log"

cd "$repo_root"
xcodebuild archive -project LiveContainer.xcodeproj -scheme LiveContainer \
    -archivePath "$build_root/LiveContainer.xcarchive" -sdk iphoneos -arch arm64 \
    -configuration Release -jobs 2 CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO \
    CODE_SIGN_IDENTITY= DEVELOPMENT_TEAM=AAAAA11111 \
    2>&1 | tee "$log_root/livecontainer-build.log"

python3 "$repo_root/BuildSupport/SideStoreMemoryFix/package.py" \
    --live-app "$build_root/LiveContainer.xcarchive/Products/Applications/LiveContainer.app" \
    --side-app "$side_source/SideStore.xcarchive/Products/Applications/SideStore.app" \
    --side-source "$side_source" --dylibify "$build_root/dylibify" \
    --output "$repo_root/dist-memory" \
    2>&1 | tee "$log_root/package.log"
