"""HaloPad network policy v1: guest bridge, publisher decisions, signatures, app verifier."""
import base64
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/xbox'))
import network_bridge as bridge  # noqa: E402
import network_policy as policy  # noqa: E402

CLIENT = (b'\tunsigned int ours = HALO_PORT_NETWORK_VERSION;\n\tunsigned int theirs;\n'
          b'\t/* HALO_PORT_NETWORK_VERSION in a comment */\n' + bridge.CLIENT_ANCHOR +
          b'\tif (theirs == ours && distributed)\n')
SERVER = b'\t\t\t/* the version */\n' + bridge.SERVER_ANCHOR
LOBBY = (b'// HALO_PORT_NETWORK_VERSION\n' + bridge.LOBBY_DEFINE_ANCHOR + b'{\n' + bridge.LOBBY_LISTING +
         b'}\n\t\tif (!listing_read(p, s, &listing) || ' + bridge.LOBBY_FILTER + b'\n')


def engine_tree(root, extra=None):
    files = {bridge.CLIENT: CLIENT, bridge.SERVER: SERVER, bridge.LOBBY: LOBBY,
             bridge.IMPORTS: b'host_sdl_init\n', bridge.LIMITS: b'#define HALO_PORT_NETWORK_VERSION 24\n',
             pathlib.Path('source/other.c'): b'int x;\n', **(extra or {})}
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
    return files


class BridgeTests(unittest.TestCase):
    def test_four_sites_and_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            engine_tree(root)
            result = {path.relative_to(root): modified for path, _, modified in bridge.changes(root)}
        self.assertIn(b'host_halopad_network_accepts_v1(ours, theirs)', result[bridge.CLIENT])
        self.assertLess(result[bridge.CLIENT].index(bridge.CLIENT_ANCHOR),
                        result[bridge.CLIENT].index(b'host_halopad_network_accepts_v1(ours'))
        self.assertIn(b'announced >> 8', result[bridge.SERVER])
        self.assertIn(bridge.SERVER_ANCHOR, result[bridge.SERVER])  # kept for non-Android builds
        self.assertIn(b'halopad_network_announced() >> 8', result[bridge.LOBBY])
        self.assertIn(b'!halopad_network_accepts(listing.version) ||', result[bridge.LOBBY])
        self.assertNotIn(bridge.LOBBY_FILTER, result[bridge.LOBBY])
        self.assertTrue(result[bridge.IMPORTS].endswith(
            b'\nhost_halopad_network_announce_v1\nhost_halopad_network_accepts_v1\n'))

    def test_unreviewed_use_or_changed_anchor_stops(self):
        for extra in ({pathlib.Path('port/linux/src/new.c'): b'int v = HALO_PORT_NETWORK_VERSION;\n'},
                      {bridge.CLIENT: CLIENT + b'\tours = HALO_PORT_NETWORK_VERSION;\n'}):
            with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, 'unreviewed'):
                engine_tree(pathlib.Path(tmp), extra)
                bridge.changes(pathlib.Path(tmp))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, 'anchor changed'):
            engine_tree(pathlib.Path(tmp), {bridge.SERVER: SERVER.replace(b'& 0xFF', b'&0xFF')})
            bridge.changes(pathlib.Path(tmp))
        with self.assertRaisesRegex(ValueError, 'already declares'):
            bridge.adapt(bridge.IMPORTS, b'host_halopad_network_accepts_v1\n')

    def test_native_policy_rules(self):
        source = r'''
#include <assert.h>
#include "xg_network_policy.h"
int main(void) {
    struct xg_network_policy p = {0};
    assert(xg_network_policy_announce(&p, 24) == 24 && xg_network_policy_accepts(&p, 24, 24));
    assert(!xg_network_policy_accepts(&p, 24, 25) && !xg_network_policy_accepts(&p, 24, 23));
    assert(xg_network_policy_set(&p, 24, 26, 24, 26));
    assert(xg_network_policy_announce(&p, 24) == 26);
    assert(xg_network_policy_accepts(&p, 24, 25) && xg_network_policy_accepts(&p, 24, 26));
    assert(!xg_network_policy_accepts(&p, 24, 27) && !xg_network_policy_accepts(&p, 24, 23));
    /* a policy for another engine never applies */
    assert(xg_network_policy_announce(&p, 25) == 25 && !xg_network_policy_accepts(&p, 25, 26));
    /* rows that could refuse our own version, or announce less, are rejected */
    assert(!xg_network_policy_set(&p, 24, 26, 25, 26) && !p.set);
    assert(!xg_network_policy_set(&p, 24, 23, 23, 26) && !xg_network_policy_set(&p, 24, 27, 24, 26));
    assert(!xg_network_policy_set(&p, 24, 24, 24, 70000) && !xg_network_policy_set(&p, 0, 0, 0, 0));
    assert(xg_network_policy_announce(&p, 24) == 24 && !xg_network_policy_accepts(&p, 24, 25));
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            (path / 'test.c').write_text(source)
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT / 'port/xbox'),
                            str(path / 'test.c'), '-o', str(path / 'test')], check=True)
            subprocess.run([str(path / 'test')], check=True)


class DecisionTests(unittest.TestCase):
    def test_network_version_requires_one_integer(self):
        self.assertEqual(policy.network_version(b'#define HALO_PORT_NETWORK_VERSION 29\n'), 29)
        for bad in (b'#define HALO_PORT_NETWORK_VERSION 24\n#define HALO_PORT_NETWORK_VERSION 25\n',
                    b'#define HALO_PORT_NETWORK_VERSION 0\n'):
            with self.assertRaises(policy.PolicyError):
                policy.network_version(bad)

    def test_no_unreviewed_transition_is_enabled(self):
        self.assertEqual(policy.REVIEWED_TRANSITIONS, frozenset())
        for engine in (24, 25, 26, 27, 28, 29):
            self.assertEqual(policy.decide(engine, engine), (policy.exact(engine), 'current'))
            self.assertEqual(policy.decide(engine, 29)[0], policy.exact(engine))
        self.assertEqual(policy.decide(25, 27)[1], 'waiting')
        self.assertEqual(policy.decide(25, 29)[1], 'needs-release')
        self.assertEqual(policy.decide(27, 28)[1], 'needs-release')
        self.assertEqual(policy.decide(29, 30)[1], 'waiting')

    def test_recompute_withdraws_published_unsafe_ranges(self):
        previous = {'announce': 29, 'minimum': 24, 'maximum': 29, 'follows': 'build-206'}
        self.assertEqual(policy.decide(25, 29, previous), (policy.exact(25), 'needs-release'))
        self.assertEqual(policy.decide(25, 27, previous), (policy.exact(25), 'waiting'))
        self.assertEqual(policy.decide(29, 30, previous), (policy.exact(29), 'waiting'))
        self.assertEqual(policy.decide(29, 29, previous), (policy.exact(29), 'current'))

    def test_reviewed_transitions_stop_at_unknown_or_breaking(self):
        # A synthetic local review, not an actual 24/25 compatibility approval.
        with mock.patch.object(policy, 'REVIEWED_TRANSITIONS', frozenset({25})):
            previous = {'announce': 25, 'minimum': 24, 'maximum': 25, 'follows': 'build-170'}
            self.assertEqual(policy.decide(24, 25, upstream_tag='build-170'), (previous, 'following'))
            self.assertEqual(policy.decide(24, 26, previous), (previous, 'waiting'))
            self.assertEqual(policy.decide(24, 29, previous), (previous, 'needs-release'))
        with mock.patch.object(policy, 'REVIEWED_TRANSITIONS', frozenset({26, 27, 28, 29})):
            self.assertEqual(policy.decide(25, 29),
                             ({'announce': 27, 'minimum': 25, 'maximum': 27}, 'needs-release'))
            self.assertEqual(policy.decide(28, 29), (policy.exact(28), 'needs-release'))

    def test_plan_is_stable_monotonic_and_withdrawable(self):
        old = {'format': 1, 'serial': 1000, 'engines': {
            '24': {'announce': 29, 'minimum': 24, 'maximum': 29, 'follows': 'build-206'},
            '25': {'announce': 29, 'minimum': 25, 'maximum': 29, 'follows': 'build-206'}}}
        document, report = policy.plan(old, {29}, 29, 'build-206', now=900)
        corrected = json.loads(document)
        self.assertEqual(corrected['serial'], 1001)
        self.assertEqual(corrected['engines'], {str(v): policy.exact(v) for v in (24, 25, 29)})
        self.assertTrue(report['engines']['24']['withdrawn'])
        self.assertTrue(report['engines']['25']['withdrawn'])
        self.assertNotIn('withdrawn', report['engines']['29'])
        self.assertEqual(policy.plan(corrected, {29}, 29, 'build-207', now=2000)[0], None)
        self.assertEqual(policy.plan(corrected, {29}, 30, 'build-210', now=3000)[0], None)
        exact_document, exact_report = policy.plan(old, {29}, 29, 'build-206', mode='exact', now=900)
        self.assertEqual(json.loads(exact_document)['engines'], corrected['engines'])
        self.assertEqual(exact_report['engines']['25']['status'], 'withdrawn')

    def test_gather_uses_only_pinned_opence_sources(self):
        def api(path):
            if path.endswith('/releases/latest'):
                return {'tag_name': 'build-206'}
            if '/commits?' in path:
                return [{'commit': {'committer': {'date': '2026-10-14T10:08:39Z'}}}]
            return {'sha': 'a' * 40}
        with mock.patch.object(policy, 'api', side_effect=api) as get, \
                mock.patch.object(policy, 'raw', return_value=b'#define HALO_PORT_NETWORK_VERSION 29\n') as raw:
            result = policy.gather()
        self.assertEqual(result['upstream'], 29)
        self.assertTrue(all(call.args[0].startswith('repos/' + policy.OPENCE + '/') for call in get.call_args_list))
        self.assertTrue(all(call.args[0] == policy.OPENCE for call in raw.call_args_list))
        self.assertEqual(raw.call_args_list[0].args[1], 'a' * 40)

    def test_notify_is_summary_only_even_when_attention_is_required(self):
        report = {'upstream': {'release': 'build-206', 'network_version': 29}, 'engines': {
            '25': {'status': 'needs-release', 'withdrawn': True, **policy.exact(25)}},
            'changed': True, 'attention': True}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict('os.environ', {'GITHUB_STEP_SUMMARY': str(pathlib.Path(tmp) / 'summary.md')}), \
                mock.patch.object(policy.subprocess, 'run') as run, \
                mock.patch.object(policy.subprocess, 'check_output') as output, \
                mock.patch('builtins.print'):
            policy.notify(report)
            self.assertIn('unsafe widening withdrawn', (pathlib.Path(tmp) / 'summary.md').read_text())
        run.assert_not_called()
        output.assert_not_called()

    def test_document_checks(self):
        good = {'format': 1, 'serial': 5, 'engines': {'24': policy.exact(24)}}
        policy.check_document(json.dumps(good).encode())
        for bad in ({**good, 'serial': True}, {**good, 'serial': 0}, {**good, 'format': 2},
                    {**good, 'engines': {'024': policy.exact(24)}},
                    {**good, 'engines': {'24': {'announce': 25, 'minimum': 25, 'maximum': 25}}},
                    {**good, 'engines': {'24': {'announce': 24.0, 'minimum': 24, 'maximum': 24}}},
                    {**good, 'engines': {'24': {**policy.exact(24), 'follows': 'a b'}}}):
            with self.assertRaises(policy.PolicyError):
                policy.check_document(json.dumps(bad).encode())


OPENSSL = shutil.which('openssl')


def test_key(folder):
    key = pathlib.Path(folder) / 'key.pem'
    subprocess.run([OPENSSL, 'ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', str(key)],
                   check=True, capture_output=True)
    return key


def raw_public(pem_or_key, private=False):
    args = [OPENSSL, 'ec', '-in', str(pem_or_key), '-pubout', '-outform', 'DER']
    if not private:
        args.insert(2, '-pubin')
    return subprocess.run(args, check=True, capture_output=True).stdout[-65:]


@unittest.skipUnless(OPENSSL, 'openssl is required')
class SignatureTests(unittest.TestCase):
    def test_round_trip_and_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = test_key(tmp)
            document, _ = policy.plan(None, {25}, 29, 'build-206', now=1000)
            envelope = policy.sign(document, key)
            public = policy.public_key_of(key)
            self.assertEqual(policy.verify(envelope, public)['engines']['25']['announce'], 25)
            outer = json.loads(envelope)
            outer['document'] = base64.b64encode(document.replace(b'25', b'26')).decode()
            with self.assertRaisesRegex(policy.PolicyError, 'does not verify'):
                policy.verify(json.dumps(outer).encode(), public)
            with self.assertRaisesRegex(policy.PolicyError, 'does not verify'):
                policy.verify(envelope, policy.PUBLIC_KEY)  # the production key did not sign it

    def test_app_key_matches_published_public_key(self):
        header = (ROOT / 'port/ios/HaloPadXboxNetworkPolicy.h').read_text()
        block = header[header.index('HPNetworkPolicyPublicKey[65]'):]
        block = block[block.index('{') + 1:block.index('}')]
        constant = bytes(int(b, 16) for b in re.findall(r'0x([0-9a-f]{2})', block))
        self.assertEqual(constant, raw_public(policy.PUBLIC_KEY))

    @unittest.skipUnless(sys.platform == 'darwin', 'Security.framework is required')
    def test_app_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            key = test_key(tmp)
            # Validly signed historical policy: a new binary must not apply its widening.
            first = policy.render({'format': 1, 'serial': 1000, 'engines': {
                '24': {'announce': 25, 'minimum': 24, 'maximum': 25, 'follows': 'build-170'},
                '25': {'announce': 29, 'minimum': 25, 'maximum': 29, 'follows': 'build-206'},
                '29': {'announce': 30, 'minimum': 25, 'maximum': 30, 'follows': 'build-210'}}})
            newer, _ = policy.plan(json.loads(first), {29}, 29, 'build-206', mode='exact', now=1000)
            (path / 'first.json').write_bytes(policy.sign(first, key))
            (path / 'newer.json').write_bytes(policy.sign(newer, key))
            outer = json.loads((path / 'first.json').read_text())
            outer['document'] = base64.b64encode(first.replace(b'"announce": 25', b'"announce": 26')).decode()
            (path / 'tampered.json').write_text(json.dumps(outer))
            loose = json.dumps({'format': 1, 'serial': 7, 'engines': {'24': {'announce': 25, 'minimum': 25,
                                'maximum': 25}}}).encode()
            envelope = json.loads((path / 'first.json').read_text())
            signature = subprocess.run([OPENSSL, 'dgst', '-sha256', '-sign', str(key)], input=loose,
                                       check=True, capture_output=True).stdout
            envelope.update(document=base64.b64encode(loose).decode(), signature=base64.b64encode(signature).decode())
            (path / 'signed-but-narrowing.json').write_text(json.dumps(envelope))
            key_bytes = ', '.join(f'0x{b:02x}' for b in raw_public(key, private=True))
            source = r'''
#import "HaloPadXboxNetworkPolicy.h"
#include <assert.h>
static NSData *file(const char *name) { return [NSData dataWithContentsOfFile:[@"%DIR%" stringByAppendingPathComponent:@(name)]]; }
int main(void) { @autoreleasepool {
    (void)HPNetworkPolicyCached; (void)HPNetworkPolicyRefresh;
    const unsigned char bytes[65] = { %KEY% };
    NSData *key = [NSData dataWithBytes:bytes length:65];
    NSDictionary *first = HPNetworkPolicyRead(file("first.json"), 24, key);
    assert([first[@"serial"] isEqual:@1000]);
    assert([first[@"row"][@"announce"] isEqual:@24] && [first[@"row"][@"minimum"] isEqual:@24] &&
           [first[@"row"][@"maximum"] isEqual:@24] && [first[@"row"][@"follows"] isEqual:@""]);
    for (unsigned int engine = 25; engine <= 29; engine += 4) {
        NSDictionary *bounded = HPNetworkPolicyRead(file("first.json"), engine, key);
        assert([bounded[@"serial"] isEqual:@1000]);
        assert([bounded[@"row"][@"announce"] unsignedIntValue] == engine);
        assert([bounded[@"row"][@"minimum"] unsignedIntValue] == engine);
        assert([bounded[@"row"][@"maximum"] unsignedIntValue] == engine);
    }
    assert(HPNetworkPolicyRead(file("first.json"), 26, key)[@"row"] == nil);   /* absent engines: exact */
    assert(!HPNetworkPolicyRead(file("first.json"), 24, HPNetworkPolicyDefaultKey()));
    assert(!HPNetworkPolicyRead(file("tampered.json"), 24, key));
    assert(!HPNetworkPolicyRead(file("signed-but-narrowing.json"), 24, key));
    assert(!HPNetworkPolicyRead([@"{}" dataUsingEncoding:NSUTF8StringEncoding], 24, key));
    assert(!HPNetworkPolicyRead(nil, 24, key));
    NSURL *cache = [NSURL fileURLWithPath:@"%DIR%/cache/network-policy.json"];
    assert(!HPNetworkPolicyStore(file("tampered.json"), 24, cache, key));
    assert(HPNetworkPolicyStore(file("first.json"), 24, cache, key));
    NSDictionary *oldCache = HPNetworkPolicyRead([NSData dataWithContentsOfURL:cache], 29, key);
    assert([oldCache[@"serial"] isEqual:@1000] && [oldCache[@"row"][@"announce"] isEqual:@29]);
    assert([oldCache[@"row"][@"minimum"] isEqual:@29] && [oldCache[@"row"][@"maximum"] isEqual:@29]);
    assert(!HPNetworkPolicyStore(file("first.json"), 24, cache, key));          /* same serial */
    assert(HPNetworkPolicyStore(file("newer.json"), 24, cache, key));
    assert(!HPNetworkPolicyStore(file("first.json"), 24, cache, key));          /* never rolls back */
    NSDictionary *cached = HPNetworkPolicyRead([NSData dataWithContentsOfURL:cache], 24, key);
    assert([cached[@"serial"] isEqual:@1001] && [cached[@"row"][@"announce"] isEqual:@24]);
    assert([HPNetworkPolicySummary(24, cached) isEqual:@"Online play: OpenCE network version 24."]);
    assert([HPNetworkPolicySummary(24, first) isEqual:@"Online play: OpenCE network version 24."]);
    assert([HPNetworkPolicySourceURL().absoluteString isEqual:HPNetworkPolicyURL]);
    setenv("HALOPAD_NETWORK_POLICY_URL", "https://evil.invalid/network-policy.json", 1);
    assert([HPNetworkPolicySourceURL().absoluteString isEqual:HPNetworkPolicyURL]);
    setenv("HALOPAD_NETWORK_POLICY_URL", "https://raw.githubusercontent.com/chrissotraidis/projectreach/test/network-policy.json", 1);
    assert([HPNetworkPolicySourceURL().absoluteString hasSuffix:@"/test/network-policy.json"]);
} return 0; }
'''.replace('%DIR%', str(path)).replace('%KEY%', key_bytes)
            (path / 'test.m').write_text(source)
            subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT / 'port/ios'),
                            str(path / 'test.m'), '-framework', 'Foundation', '-framework', 'Security',
                            '-o', str(path / 'test')], check=True)
            subprocess.run([str(path / 'test')], check=True)


if __name__ == '__main__':
    unittest.main()
