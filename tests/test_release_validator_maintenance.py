"""Catalog v2 is a declaration, not a runtime permission or release result."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('maintenance_validator', ROOT / 'tools/release/validate.py')
rv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rv)
SCHEMAS = ROOT / 'docs/schemas'


def profile(**changes):
    return {'beginRequired': False, 'stateFreeze': False,
            'blockedOperations': [], 'notes': [], **changes}


class MaintenanceCatalogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'src.py').write_text('pass\n')
        (self.root / 'README.md').write_text('UC\n')
        self.catalog = {'schemaVersion': 2, 'repository': 'sample', 'buildUnits': [],
                        'targets': [{'id': 'app', 'inputs': ['src.py'],
                                     'deployment': {'kind': 'owner-controlled',
                                                    'procedure': 'README.md', 'environments': ['test']},
                                     'verification': ['health'], 'maintenance': profile()}]}

    def validate(self, data):
        rv.validate_schema(data, rv.catalog_schema_path(data, SCHEMAS))
        return rv.validate_catalog(data, self.root, 'sample')

    def test_explicit_none_notes_scope_freeze_and_exclusive_begin(self):
        for p in (profile(), profile(notes=['Notify operators']),
                  profile(beginRequired=True),
                  profile(beginRequired=True, blockedOperations=[
                      {'service': 'registry', 'operation': 'begin_release'}]),
                  profile(beginRequired=True, stateFreeze=True)):
            with self.subTest(p=p):
                self.catalog['targets'][0]['maintenance'] = p
                self.assertEqual(self.validate(self.catalog)['targets'], 1)
                self.assertEqual(rv.maintenance_profile(self.catalog, self.catalog['targets'][0]), p)

    def test_v1_unknown_not_unrestricted(self):
        old = deepcopy(self.catalog)
        old['schemaVersion'] = 1
        del old['targets'][0]['maintenance']
        self.validate(old)
        self.assertIsNone(rv.maintenance_profile(old, old['targets'][0]))
        old['targets'][0]['maintenance'] = profile()
        with self.assertRaises(rv.ValidationError):
            self.validate(old)

    def test_unknown_and_noninteger_schema_versions_rejected(self):
        for version in (None, True, 1.0, '2', 3):
            with self.subTest(version=version), self.assertRaises(rv.ValidationError):
                rv.catalog_schema_path({'schemaVersion': version}, SCHEMAS)

    def test_missing_profile_denied(self):
        del self.catalog['targets'][0]['maintenance']
        with self.assertRaises(rv.ValidationError):
            self.validate(self.catalog)

    def test_invalid_profiles(self):
        blocked = {'service': 'registry', 'operation': 'begin_release'}
        for p in (
            profile(stateFreeze=True),
            profile(blockedOperations=[blocked]),
            profile(beginRequired=True, blockedOperations=[blocked, blocked]),
            profile(beginRequired=True, blockedOperations=[{'service': '*', 'operation': '*'}]),
            profile(beginRequired=True, blockedOperations=[{**blocked, 'target': '*'}]),
            profile(beginRequired=True, blockedOperations=[{'service': 'a', 'operation': 'b\n'}]),
            profile(beginRequired=1),
            profile(notes=[' ']), profile(notes=['a\nb']), profile(notes=['same', 'same']),
            profile(notes=['x'*501]), profile(notes=['x']*21),
            profile(policy='ALLOW'), profile(command='anything'),
        ):
            with self.subTest(p=p):
                self.catalog['targets'][0]['maintenance'] = p
                with self.assertRaises(rv.ValidationError):
                    self.validate(self.catalog)

    def test_non_deployable_cannot_hold(self):
        self.catalog['targets'][0]['deployment']['kind'] = 'none'
        self.catalog['targets'][0]['maintenance'] = profile(beginRequired=True)
        with self.assertRaises(rv.ValidationError):
            self.validate(self.catalog)

    def test_catalog_change_still_requires_impact(self):
        self.validate(self.catalog)
        impact = {'releaseImpact': {'schemaVersion': 1, 'classification': 'no-deployment',
                                    'reason': 'Catalog change', 'targets': [],
                                    'dependencies': {'pullRequests': [], 'targets': []},
                                    'verification': ['tests']}}
        with self.assertRaises(rv.ValidationError):
            rv.validate_impact(impact, self.catalog, ['release/targets.yaml'])

    def test_legacy_schema_not_edited_by_v2(self):
        legacy = json.loads((SCHEMAS / 'build-target-catalog-v1.schema.json').read_text())
        v2 = json.loads((SCHEMAS / 'build-target-catalog-v2.schema.json').read_text())
        v2['$id'] = legacy['$id']
        v2['title'] = legacy['title']
        v2['properties']['schemaVersion']['const'] = 1
        target = v2['properties']['targets']['items']
        target['required'].remove('maintenance')
        del target['properties']['maintenance']
        self.assertEqual(v2, legacy)


if __name__ == '__main__':
    unittest.main()
