#!/usr/bin/env python3
"""Verify real packaging layout and request retention using explicit native-tool stand-ins."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('memory_package', HERE / 'package.py')
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)


def bundle(path, identifier, binary, kind=2):
    path.mkdir(parents=True)
    pack.write_plist(path / 'Info.plist', {'CFBundleIdentifier': identifier, 'CFBundleExecutable': binary})
    (path / binary).write_bytes(struct.pack('<IiiIIIII', 0xfeedfacf, 0x0100000c, 0, kind, 0, 0, 0, 0))
    return path


class FakeNativeTools:
    def __init__(self, bad_conversion=False):
        self.requests = {}
        self.bad_conversion = bad_conversion

    def convert(self, source, destination):
        data = bytearray(source.read_bytes())
        struct.pack_into('<I', data, 12, 2 if self.bad_conversion else 6)
        destination.write_bytes(data)

    def sign(self, path, entitlements):
        self.requests[path] = copy.deepcopy(entitlements)

    def entitlements(self, path):
        return self.requests.get(path, {})


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ipa package with spaces ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.live = bundle(self.root / 'live/LiveContainer.app', 'com.kdt.livecontainer', 'LiveContainer')
        bundle(self.live / 'Frameworks/SideStoreSupport.framework', 'com.kdt.support', 'SideStoreSupport', 6)
        bundle(self.live / 'PlugIns/LiveProcess.appex', 'com.kdt.livecontainer.LiveProcess', 'LiveProcess')
        (self.live / 'Settings.bundle').mkdir()
        pack.write_plist(self.live / 'Settings.bundle/Root.plist', {'PreferenceSpecifiers': [{'Key': 'existing', 'DefaultValue': True}]})
        self.side = bundle(self.root / 'side/SideStore.app', 'com.SideStore.SideStore', 'SideStore')
        bundle(self.side / 'PlugIns/AltWidgetExtension.appex', 'com.SideStore.SideStore.Widget', 'AltWidgetExtension')
        metadata = self.side / 'Metadata.appintents'
        metadata.mkdir()
        (metadata / 'extract.actionsdata').write_text(json.dumps(['9SideStore20RefreshAllAppsIntentV', '9SideStore26RefreshAllAppsWidgetIntentV']))
        (self.side / 'resource').write_text('preserved')
        (self.side / 'resource-link').symlink_to('resource')
        (self.side / '_CodeSignature').mkdir()
        (self.side / '_CodeSignature/CodeResources').write_text('obsolete')
        (self.side / 'embedded.mobileprovision').write_text('synthetic obsolete profile')
        self.source = self.root / 'side-source'
        templates = self.source / 'AltStore/Resources'
        templates.mkdir(parents=True)
        pack.write_plist(templates / 'ReleaseEntitlements.plist', {'get-task-allow': True, 'com.apple.security.application-groups': ['group.com.SideStore.SideStore']})
        self.output = self.root / 'output'
        self.tools = FakeNativeTools()

    def test_combined_uses_standalone_payload_and_preserves_requests_widgets_metadata_and_inputs(self):
        pack.package(self.live, self.side, self.source, self.output, self.tools, {'fix': 'pinned human PR'})
        with zipfile.ZipFile(self.output / 'LiveContainer+SideStore-standalone-memory-fix.ipa') as archive:
            prefix = 'Payload/LiveContainer.app/'
            info = plistlib.loads(archive.read(prefix + 'Info.plist'))
            schemes = [scheme for item in info['CFBundleURLTypes'] for scheme in item['CFBundleURLSchemes']]
            self.assertIn('sidestore', schemes)
            self.assertIn('sidestore-com.kdt.livecontainer', schemes)
            self.assertIn('group.com.SideStore.SideStore', info['ALTAppGroups'])
            embedded = prefix + 'Frameworks/SideStoreApp.framework/'
            self.assertEqual(6, struct.unpack_from('<I', archive.read(embedded + 'SideStore'), 12)[0])
            self.assertIn(embedded + 'LCAppInfo.plist', archive.namelist())
            self.assertNotIn(embedded + 'PlugIns/AltWidgetExtension.appex/Info.plist', archive.namelist())
            widget = plistlib.loads(archive.read(prefix + 'PlugIns/LiveWidgetExtension.appex/Info.plist'))
            self.assertEqual('LiveWidgetExtension', widget['CFBundleExecutable'])
            self.assertEqual('com.kdt.livecontainer.LiveWidget', widget['CFBundleIdentifier'])
            actions = archive.read(prefix + 'Metadata.appintents/extract.actionsdata').decode()
            self.assertIn('16SideStoreSupport20RefreshAllAppsIntentV', actions)
            self.assertIn('16SideStoreSupport26RefreshAllAppsWidgetIntentV', actions)
            self.assertFalse(any('_CodeSignature' in name or 'embedded.mobileprovision' in name for name in archive.namelist()))
            self.assertEqual('resource', archive.read(embedded + 'resource-link').decode())
            self.assertTrue((archive.getinfo(embedded + 'resource-link').external_attr >> 16) & 0o120000)
        main_requests = [value for path, value in self.tools.requests.items() if path.name in ('LiveContainer', 'LiveProcess', 'SideStore') and value]
        self.assertEqual(3, len(main_requests))
        self.assertTrue(all(value[pack.MEMORY] is True for value in main_requests))
        self.assertTrue(all('$(' not in str(value) for value in self.tools.requests.values()))
        self.assertTrue(any(path.name == 'SideStore' and value == {} for path, value in self.tools.requests.items()))
        self.assertEqual(2, pack.macho_type(self.side / 'SideStore'))
        self.assertTrue((self.side / 'PlugIns/AltWidgetExtension.appex/AltWidgetExtension').exists())
        self.assertNotIn('ALTAppGroups', pack.read_plist(self.live / 'Info.plist'))

    def test_conversion_must_produce_a_dylib(self):
        with self.assertRaisesRegex(ValueError, 'did not produce a dylib'):
            pack.package(self.live, self.side, self.source, self.output, FakeNativeTools(True), {})
        self.assertFalse((self.output / 'LiveContainer+SideStore-standalone-memory-fix.ipa').exists())

    def test_unreviewed_extension_fails_instead_of_shipping_nested_code(self):
        bundle(self.side / 'PlugIns/Unexpected.appex', 'com.example.extra', 'Unexpected')
        with self.assertRaisesRegex(ValueError, 'Unmapped SideStore app extension'):
            pack.package(self.live, self.side, self.source, self.output, self.tools, {})

    def test_missing_metadata_or_host_support_is_not_silently_ignored(self):
        import shutil
        shutil.rmtree(self.live / 'Frameworks/SideStoreSupport.framework')
        with self.assertRaisesRegex(ValueError, 'SideStoreSupport.framework'):
            pack.package(self.live, self.side, self.source, self.output, self.tools, {})

    def test_native_signer_must_retain_the_entitlement_request(self):
        tools = pack.NativeTools('/unused/dylibify', '/unused/ldid')
        with patch.object(pack.subprocess, 'run'), patch.object(pack.subprocess, 'check_output', return_value=plistlib.dumps({})):
            with self.assertRaisesRegex(ValueError, 'did not survive signing'):
                tools.sign(self.side / 'SideStore', {pack.MEMORY: True})

    def test_source_patch_integrity_and_scope(self):
        pins = json.loads((HERE / 'sources.json').read_text())
        data = (HERE / 'sidesign-pr4.patch').read_bytes()
        self.assertEqual(pins['sidesign_patch_sha256'], hashlib.sha256(data).hexdigest())
        files = [line.split(' b/')[1] for line in data.decode().splitlines() if line.startswith('diff --git ')]
        self.assertEqual({'Sources/DeveloperPortal/AppIDs.swift', 'Sources/DeveloperPortal/DeveloperPortalAPI.swift', 'Sources/Models/Feature.swift', 'Tests/SideSignTests/CapabilityTests.swift'}, set(files))


if __name__ == '__main__':
    unittest.main()
