"""The download smoke must fail without both gameplay and verified files."""
import hashlib
import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('map_smoke', ROOT / 'scripts/xbox/smoke-map-sharing.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class MapSharingEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        (self.root / 'custom_maps').mkdir()
        self.data = b'fixture map contents'
        (self.root / 'custom_maps/bloodgulch.map').write_bytes(self.data)
        self.hashes = {'bloodgulch.map': hashlib.sha256(self.data).hexdigest()}

    def evaluate(self, playing=True, downloaded=True, expected=True):
        return smoke.completed({'pass': playing, 'client_debug':
                                'map sharing: downloaded custom_maps\\bloodgulch (42 bytes)' if downloaded else ''},
                               self.root, self.hashes, expect_download=expected)

    def test_requires_gameplay_after_correct_transfer(self):
        self.assertTrue(self.evaluate()['pass'])
        self.assertFalse(self.evaluate(playing=False)['pass'])

    def test_rejects_bad_or_missing_file_despite_success_log(self):
        (self.root / 'custom_maps/bloodgulch.map').write_bytes(b'corrupt transfer')
        self.assertFalse(self.evaluate()['pass'])
        (self.root / 'custom_maps/bloodgulch.map').unlink()
        self.assertFalse(self.evaluate()['pass'])

    def test_cached_rejoin_must_not_download_again(self):
        self.assertTrue(self.evaluate(downloaded=False, expected=False)['pass'])
        self.assertFalse(self.evaluate(downloaded=True, expected=False)['pass'])

    def test_partial_is_not_a_completed_transfer(self):
        (self.root / 'custom_maps/bloodgulch.map.part').write_bytes(b'partial')
        self.assertFalse(self.evaluate()['pass'])

    def test_join_alone_and_one_player_ticks_do_not_pass(self):
        self.assertFalse(smoke.played('network test: joining\n'))
        text = ''.join(f'network test: tick {tick} player 1: (1 2 3)\n' for tick in range(0, 151, 30))
        self.assertFalse(smoke.played(text))
        shared = text.replace('player 1: (1 2 3)', 'player 1: (1 2 3) player 2: (4 5 6)')
        self.assertTrue(smoke.played(shared))
        self.assertFalse(smoke.played(shared.splitlines()[0]))

    def test_private_processes_do_not_inherit_public_or_download_overrides(self):
        with patch.dict(os.environ, {'HALO_NET_ONLINE': 'true', 'HALO_NET_MAP_DOWNLOAD': 'false',
                                     'XG_FRAME_DUMP': '/some/file'}):
            env = smoke.clean_environment('192.168.1.2', '169.254.1.2', 'client')
        self.assertEqual(env['HALO_NET_ONLINE'], 'false')
        self.assertEqual(env['HALO_NET_PUBLIC_LOBBY'], 'false')
        self.assertEqual(env['HALO_NET_ALLOW_UPNP'], 'false')
        self.assertEqual(env['HALO_VOICE_CHAT'], 'off')
        self.assertEqual(env['HALO_UPDATE_AUTO'], 'false')
        self.assertEqual(env['HALO_NULL_RENDERER'], '1')
        self.assertNotIn('HALO_NET_MAP_DOWNLOAD', env)
        self.assertNotIn('HALO_NET_MAP_SHARE', env)
        self.assertNotIn('XG_FRAME_DUMP', env)

    def test_version_hash_matches_opence_monocypher_known_answer(self):
        # Independently evaluated with build206's native monocypher.c. The
        # 12-byte label has no C string terminator in the hashed bytes.
        source = self.root / 'hash-input'
        source.write_bytes(b'abc')
        self.assertEqual(smoke.map_hash(source),
                         'c3f9fbc531ef0e4d8c6410b590207c51004c6511f0c0bc7ae093459f59612ab7')

    def test_local_route_checks_real_bidirectional_udp(self):
        smoke.verify_local_route('127.0.0.1', '127.0.0.1')

    def test_route_rejects_reverse_send_failure_before_starting_game(self):
        first, second = MagicMock(), MagicMock()
        for endpoint in (first, second):
            endpoint.__enter__.return_value = endpoint
        first.getsockname.return_value = ('192.168.1.2', 1234)
        second.getsockname.return_value = ('169.254.1.2', 1235)
        second.recvfrom.return_value = (b'x' * 16, first.getsockname.return_value)
        second.sendto.side_effect = OSError(65, 'No route to host')
        with patch.object(smoke.socket, 'socket', side_effect=[first, second]), \
                patch.object(smoke.os, 'urandom', return_value=b'x' * 16):
            with self.assertRaisesRegex(ValueError, 'both directions'):
                smoke.verify_local_route('192.168.1.2', '169.254.1.2')

    def test_copies_never_modify_original_or_replace_destination(self):
        source = self.root / 'source'
        target = self.root / 'copy'
        source.write_bytes(self.data)
        smoke.copy_file(source, target)
        self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)
        target.write_bytes(b'changed scratch data')
        self.assertEqual(source.read_bytes(), self.data)
        with self.assertRaises(ValueError):
            smoke.copy_file(source, target)

    def test_copy_refuses_a_dangling_destination_symlink(self):
        source = self.root / 'source'
        target = self.root / 'copy'
        elsewhere = self.root / 'unrelated-player-file'
        source.write_bytes(self.data)
        target.symlink_to(elsewhere)
        with self.assertRaises(ValueError):
            smoke.copy_file(source, target)
        self.assertTrue(target.is_symlink())
        self.assertFalse(elsewhere.exists())


if __name__ == '__main__':
    unittest.main()
