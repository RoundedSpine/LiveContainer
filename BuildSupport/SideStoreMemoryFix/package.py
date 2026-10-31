#!/usr/bin/env python3
"""Package source-built standalone SideStore into LiveContainer for external signing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = Path(__file__).resolve().parent
MEMORY = 'com.apple.developer.kernel.increased-memory-limit'


def read_plist(path):
    with Path(path).open('rb') as stream:
        return plistlib.load(stream)


def write_plist(path, value):
    with Path(path).open('wb') as stream:
        plistlib.dump(value, stream)


def executable(bundle):
    info = read_plist(bundle / 'Info.plist')
    name = info['CFBundleExecutable']
    if not name or Path(name).name != name:
        raise ValueError('Invalid bundle executable name')
    path = bundle / name
    if not path.is_file():
        raise ValueError(f'Missing executable: {path}')
    return path


def macho_type(path):
    with Path(path).open('rb') as stream:
        header = stream.read(16)
    if len(header) != 16:
        raise ValueError(f'Missing Mach-O header: {path}')
    magic, cpu, subtype, kind = struct.unpack('<IiiI', header)
    if magic != 0xfeedfacf or cpu != 0x0100000c:
        raise ValueError(f'Expected thin arm64 Mach-O: {path}')
    return kind


def is_macho(path):
    with path.open('rb') as stream:
        return stream.read(4) in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca')


class NativeTools:
    def __init__(self, dylibify, ldid):
        self.dylibify = str(dylibify)
        self.ldid = str(ldid)

    def convert(self, source, destination):
        subprocess.run([self.dylibify, str(source), str(destination)], check=True)

    def sign(self, path, entitlements):
        with tempfile.TemporaryDirectory(prefix='request-carrier-') as directory:
            plist = Path(directory) / 'request.plist'
            write_plist(plist, entitlements)
            subprocess.run([self.ldid, '-S' + str(plist), str(path)], check=True)
        observed = self.entitlements(path)
        if observed != entitlements:
            raise ValueError(f'Embedded entitlement request did not survive signing: {path}')

    def entitlements(self, path):
        data = subprocess.check_output([self.ldid, '-e', str(path)])
        return plistlib.loads(data) if data.strip() else {}


def expand_entitlements(template, bundle):
    identifier = read_plist(bundle / 'Info.plist')['CFBundleIdentifier']
    values = {
        'DEVELOPMENT_TEAM': 'AAAAA11111',
        'PRODUCT_BUNDLE_IDENTIFIER': identifier,
        'AppIdentifierPrefix': 'AAAAA11111.',
        'APP_GROUP_SIDESTORE': 'group.com.SideStore.SideStore',
        'APP_GROUP_ALTSTORE': 'group.com.rileytestut.AltStore',
    }

    def expand(value):
        if isinstance(value, str):
            def replace(match):
                if match[1] not in values:
                    raise ValueError('Unknown entitlement variable: ' + match[1])
                return values[match[1]]
            return re.sub(r'\$\(([^)]+)\)', replace, value)
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        return value
    return expand(read_plist(template))


def clean_signatures(bundle):
    for directory, folders, files in os.walk(bundle, topdown=True, followlinks=False):
        if '_CodeSignature' in folders:
            shutil.rmtree(Path(directory) / '_CodeSignature')
            folders.remove('_CodeSignature')
        if 'embedded.mobileprovision' in files:
            (Path(directory) / 'embedded.mobileprovision').unlink()


def zip_payload(payload, output):
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for directory, folders, files in os.walk(payload, followlinks=False):
            for name in sorted(folders + files):
                path = Path(directory) / name
                if name == '.DS_Store' or name.startswith('._'):
                    continue
                member = path.relative_to(payload.parent).as_posix()
                if path.is_symlink():
                    entry = zipfile.ZipInfo(member)
                    entry.create_system = 3
                    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(entry, os.readlink(path))
                elif path.is_file():
                    archive.write(path, member)


def sign_livecontainer(bundle, tools):
    requests = {}
    for extension in bundle.rglob('*.appex'):
        binary = executable(extension)
        if extension.name == 'LiveWidgetExtension.appex':
            template = ROOT / '.github/sidelc/LiveWidgetExtension_adhoc.xml'
        else:
            template = ROOT / binary.name / (binary.name + '.entitlements')
        if not template.is_file():
            raise ValueError(f'No reviewed entitlement template for extension: {extension.name}')
        requests[binary] = expand_entitlements(template, extension)
    main = executable(bundle)
    requests[main] = expand_entitlements(ROOT / 'entitlements.xml', bundle)
    requests[main][MEMORY] = True
    for path in bundle.rglob('*'):
        if path.is_file() and not path.is_symlink() and is_macho(path):
            tools.sign(path, requests.get(path, {}))
    if tools.entitlements(main).get(MEMORY) is not True:
        raise ValueError('LiveContainer is missing its memory entitlement request')


def merge_strings(info, key, additions):
    current = list(info.get(key, []))
    info[key] = list(dict.fromkeys(current + additions))


def prepare_combined(live_app, side_app, destination, tools, provenance):
    shutil.copytree(live_app, destination, symlinks=True)
    if not (destination / 'Frameworks/SideStoreSupport.framework').is_dir():
        raise ValueError('LiveContainer archive lacks SideStoreSupport.framework')
    embedded = destination / 'Frameworks/SideStoreApp.framework'
    shutil.copytree(side_app, embedded, symlinks=True)
    binary = executable(embedded)
    if macho_type(binary) != 2:
        raise ValueError('Standalone SideStore input is not an executable')
    converted = binary.with_name(binary.name + '.converted')
    tools.convert(binary, converted)
    if macho_type(converted) != 6:
        raise ValueError('dylibify did not produce a dylib')
    converted.replace(binary)
    shutil.copy2(ROOT / '.github/sidelc/LCAppInfo.plist', embedded / 'LCAppInfo.plist')

    widget = embedded / 'PlugIns/AltWidgetExtension.appex'
    if not widget.is_dir():
        raise ValueError('Standalone SideStore archive lacks AltWidgetExtension.appex')
    moved_widget = destination / 'PlugIns/LiveWidgetExtension.appex'
    moved_widget.parent.mkdir(exist_ok=True)
    shutil.move(str(widget), str(moved_widget))
    widget_info = read_plist(moved_widget / 'Info.plist')
    old_widget_binary = executable(moved_widget)
    widget_info['CFBundleIdentifier'] = read_plist(destination / 'Info.plist')['CFBundleIdentifier'] + '.LiveWidget'
    widget_info['CFBundleExecutable'] = 'LiveWidgetExtension'
    old_widget_binary.rename(moved_widget / 'LiveWidgetExtension')
    write_plist(moved_widget / 'Info.plist', widget_info)
    if list(embedded.rglob('*.appex')):
        raise ValueError('Unmapped SideStore app extension remains inside the framework')
    shutil.rmtree(embedded / 'PlugIns')

    for name in ('Intents.intentdefinition', 'ViewApp.intentdefinition'):
        source = embedded / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    metadata = embedded / 'Metadata.appintents'
    if not metadata.is_dir():
        raise ValueError('Standalone SideStore archive lacks AppIntents metadata')
    target_metadata = destination / 'Metadata.appintents'
    if target_metadata.exists():
        shutil.rmtree(target_metadata)
    shutil.copytree(metadata, target_metadata)
    actions = target_metadata / 'extract.actionsdata'
    if actions.is_file():
        data = actions.read_bytes()
        data = data.replace(b'9SideStore20RefreshAllAppsIntentV', b'16SideStoreSupport20RefreshAllAppsIntentV')
        data = data.replace(b'9SideStore26RefreshAllAppsWidgetIntentV', b'16SideStoreSupport26RefreshAllAppsWidgetIntentV')
        actions.write_bytes(data)

    info = read_plist(destination / 'Info.plist')
    merge_strings(info, 'ALTAppGroups', ['group.com.SideStore.SideStore'])
    merge_strings(info, 'INIntentsSupported', ['RefreshAllIntent', 'ViewAppIntent'])
    merge_strings(info, 'NSUserActivityTypes', ['RefreshAllIntent', 'ViewAppIntent'])
    url_types = info.setdefault('CFBundleURLTypes', [])
    existing_schemes = {scheme for item in url_types for scheme in item.get('CFBundleURLSchemes', [])}
    for scheme in ('sidestore', 'sidestore-' + info['CFBundleIdentifier']):
        if scheme not in existing_schemes:
            url_types.append({'CFBundleURLName': scheme, 'CFBundleURLSchemes': [scheme]})
    write_plist(destination / 'Info.plist', info)
    settings_path = destination / 'Settings.bundle/Root.plist'
    settings = read_plist(settings_path)
    specifiers = settings['PreferenceSpecifiers']
    if not any(item.get('Key') == 'LCOpenSideStore' for item in specifiers):
        specifiers.insert(min(3, len(specifiers)), {'Type': 'PSToggleSwitchSpecifier', 'Title': 'Open SideStore', 'Key': 'LCOpenSideStore', 'DefaultValue': False})
    write_plist(settings_path, settings)
    (destination / 'MemoryFixBuild.json').write_text(json.dumps(provenance, indent=2) + '\n')
    clean_signatures(destination)
    sign_livecontainer(destination, tools)


def package(live_app, side_app, side_source, output, tools, provenance):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lc-standalone-package-', dir=output) as directory:
        stage = Path(directory)
        standalone_payload = stage / 'standalone/Payload'
        standalone_payload.mkdir(parents=True)
        standalone = standalone_payload / 'SideStore.app'
        shutil.copytree(side_app, standalone, symlinks=True)
        clean_signatures(standalone)
        entitlements = read_plist(side_source / 'AltStore/Resources/ReleaseEntitlements.plist')
        entitlements[MEMORY] = True
        tools.sign(executable(standalone), entitlements)
        (standalone / 'MemoryFixBuild.json').write_text(json.dumps(provenance, indent=2) + '\n')
        zip_payload(standalone_payload, output / 'SideStore-standalone-memory-fix.ipa')

        payload = stage / 'combined/Payload'
        payload.mkdir(parents=True)
        combined = payload / 'LiveContainer.app'
        prepare_combined(live_app, standalone, combined, tools, provenance)
        zip_payload(payload, output / 'LiveContainer+SideStore-standalone-memory-fix.ipa')
    for path in sorted(output.glob('*.ipa')):
        with zipfile.ZipFile(path) as archive:
            error = archive.testzip()
            if error:
                raise ValueError('Invalid ZIP member: ' + error)
        print(path.name + ' sha256=' + hashlib.sha256(path.read_bytes()).hexdigest())
    (output / 'BUILD-MANIFEST.json').write_text(json.dumps(provenance, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-app', required=True, type=Path)
    parser.add_argument('--side-app', required=True, type=Path)
    parser.add_argument('--side-source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--dylibify', required=True, type=Path)
    parser.add_argument('--ldid', default='ldid')
    args = parser.parse_args()
    provenance = json.loads((SUPPORT / 'sources.json').read_text())
    provenance['livecontainer_build_revision'] = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    provenance['capability_grant'] = 'Requires external signing, Apple provisioning and installed-process verification'
    package(args.live_app, args.side_app, args.side_source, args.output, NativeTools(args.dylibify, args.ldid), provenance)


if __name__ == '__main__':
    main()
