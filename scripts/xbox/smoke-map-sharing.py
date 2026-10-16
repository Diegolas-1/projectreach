#!/usr/bin/env python3
"""Two real local HaloPad processes: custom-map transfer, join and persistence.

All writable data is copied into a new private evidence directory. The host
uses the user's CE Blood Gulch and resources; the client starts without the
map or resources. Neither process contacts public servers. Two distinct local
IPv4 addresses are required (127.0.0.1 is reserved for the engine's self peer).
See docs/XBOX-MAP-SHARING-TEST.md. Uses the real engine with its null renderer;
--render deliberately opens game windows instead.
"""
import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
import pathlib
import re
import shutil
import socket
import struct
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORK = ROOT / 'ref/xbox-build'
RESOURCES = ('bitmaps.map', 'sounds.map', 'loc.map')
PIECE_BYTES = 0x40000
CASES = ('missing-map', 'missing-resources', 'cached-rejoin', 'collision', 'interrupted')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def map_hash(path):
    """OpenCE's version hash: label+LE size+256KiB BLAKE2b piece hashes."""
    result = hashlib.blake2b(digest_size=32)
    # FILE_HASH_LABEL_BYTES is 12: the label excludes its C string terminator.
    result.update(b'OpenCE map 1' + struct.pack('<I', path.stat().st_size))
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(PIECE_BYTES), b''):
            result.update(hashlib.blake2b(block, digest_size=32).digest())
    return result.hexdigest()


def snapshot(folder):
    return {str(path.relative_to(folder)): digest(path)
            for path in sorted(folder.rglob('*')) if path.is_file()}


def copy_file(source, target):
    """APFS copy-on-write when available, never a hard link to player input."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError(f'Refusing to replace {target}')
    if sys.platform == 'darwin':
        attempt = subprocess.run(['cp', '-c', str(source), str(target)], capture_output=True)
        if attempt.returncode == 0:
            return
        if target.exists():
            raise OSError(f'APFS copy failed after creating {target}')
    shutil.copy2(source, target)


def prepare_data(folder, xbox_maps, ce_maps, *, resources=False, game_map=False):
    folder.mkdir()
    for source in sorted(xbox_maps.glob('*.map')):
        copy_file(source, folder / 'maps' / source.name)
    custom = folder / 'custom_maps'
    custom.mkdir()
    for name in (*RESOURCES, 'bloodgulch.map'):
        if (name in RESOURCES and resources) or (name == 'bloodgulch.map' and game_map):
            copy_file(ce_maps / name, custom / name)
    # A save unrelated to this run must survive every download/restart unchanged.
    save = folder / 'save' / 'preservation-sentinel.bin'
    save.parent.mkdir()
    save.write_bytes(b'HaloPad map-sharing preservation sentinel\n')
    return folder


def verify_addresses(host, client):
    if host == client:
        raise ValueError('Host and client need distinct local addresses')
    for address in (host, client):
        value = ipaddress.IPv4Address(address)
        if not value.is_private or address == '127.0.0.1':
            raise ValueError('Use private local addresses other than 127.0.0.1')
        for kind, port in ((socket.SOCK_DGRAM, 5150), (socket.SOCK_DGRAM, 5151),
                           (socket.SOCK_STREAM, 5150),
                           (socket.SOCK_STREAM, 5153)):
            with socket.socket(socket.AF_INET, kind) as probe:
                probe.bind((address, port))


def clean_environment(address, peer, role, upload=65536, render=False):
    # A developer's inherited debug/online overrides must not change this test.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('HALO_', 'XG_'))}
    env.update(HALO_NET_ADDRESS=address, HALO_NET_BROADCAST=peer,
               HALO_NET_ONLINE='false', HALO_NET_PUBLIC_LOBBY='false',
               HALO_NET_HOST_PUBLIC='false', HALO_NETWORK_TEST_PUBLIC='false',
               HALO_NET_ALLOW_UPNP='false', HALO_NET_JOIN_FROM_CLIPBOARD='false',
               HALO_NET_VOICE_MODE='off', HALO_NET_VOICE_LOBBY='false',
               HALO_NETWORK_TEST='host:custom_maps\\bloodgulch' if role == 'host' else 'join',
               HALO_NETWORK_TEST_START='12', HALO_TEST_INPUT='bot:7',
               HALO_NET_MAP_SHARE_UPLOAD=str(upload), HALO_VOLUME='0')
    if not render:
        env['HALO_NULL_RENDERER'] = '1'
    # Do not set MAP_DOWNLOAD, MAP_SHARE, MAP_SEED: exercise upstream defaults.
    return env


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def read_log(path):
    return path.read_text(errors='replace') if path.exists() else ''


def played(text):
    # A join request or a picture is insufficient: the client must simulate two
    # living players over multiple ticks after the real map load.
    lines = re.findall(r'network test: tick (\d+)([^\n]*)', text)
    shared = [int(tick) for tick, row in lines
              if len(re.findall(r'player \d+: \(', row)) >= 2]
    return len(shared) >= 5 and max(shared) - min(shared) >= 90


def session(options, out, host_data, client_data, name, *, interrupt=False):
    logs = {role: out / f'{name}-{role}.log' for role in ('host', 'client')}
    processes, handles = [], []
    began = time.monotonic()
    result = {'pass': False}
    for folder in (host_data, client_data):
        debug = folder / 'debug.txt'
        if debug.exists():
            debug.rename(out / f'{name}-{folder.name}-prior-debug.txt')
    try:
        for role, data, address, peer in (
                ('host', host_data, options.host_address, options.client_address),
                ('client', client_data, options.client_address, options.host_address)):
            handle = logs[role].open('w')
            handles.append(handle)
            process = subprocess.Popen(
                [str(options.exe), '--image', str(options.image), '--data', str(data),
                 '--angle', str(options.angle)], stdout=handle, stderr=subprocess.STDOUT,
                env=clean_environment(address, peer, role, 128 if interrupt else 65536, options.render))
            processes.append(process)
            if role == 'host':
                time.sleep(4)
        while time.monotonic() - began < options.timeout:
            if any(process.poll() is not None for process in processes):
                result['reason'] = 'game process exited before validation completed'
                break
            if interrupt:
                parts = list((client_data / 'custom_maps').glob('*.part'))
                # The file can be sparse. Require allocated data for >1 piece,
                # then allow fclose's buffer to reach disk before termination.
                if any(path.stat().st_blocks * 512 >= PIECE_BYTES * 2 for path in parts):
                    result.update({'pass': True, 'partial_bytes': sum(p.stat().st_size for p in parts)})
                    break
            elif played(read_log(logs['client'])) and played(read_log(logs['host'])):
                result['pass'] = True
                break
            time.sleep(.2)
        else:
            result['reason'] = 'timed out waiting for partial transfer' if interrupt else 'timed out waiting for both players'
    finally:
        for process in reversed(processes):
            stop(process)
        for handle in handles:
            handle.close()
    for role, data in (('host', host_data), ('client', client_data)):
        debug = out / f'{name}-{role}-debug.txt'
        debug.write_text(read_log(data / 'debug.txt'))
    result['seconds'] = round(time.monotonic() - began, 2)
    result['client_debug'] = read_log(out / f'{name}-client-debug.txt')
    result['host_debug'] = read_log(out / f'{name}-host-debug.txt')
    return result


def completed(result, client, source_hashes, *, filename='bloodgulch.map', expect_download=True):
    debug = result['client_debug']
    downloaded = 'map sharing: downloaded custom_maps\\bloodgulch' in debug
    files_ok = all((client / 'custom_maps' / (filename if name == 'bloodgulch.map' else name)).is_file()
                   and digest(client / 'custom_maps' / (filename if name == 'bloodgulch.map' else name)) == value
                   for name, value in source_hashes.items())
    result.update(files_match=files_ok, download_logged=downloaded,
                  partials_left=bool(list((client / 'custom_maps').glob('*.part'))))
    result['pass'] &= files_ok and downloaded == expect_download and not result['partials_left']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=pathlib.Path, required=True, help='new private evidence folder')
    parser.add_argument('--host-address', required=True)
    parser.add_argument('--client-address', required=True)
    parser.add_argument('--xbox-maps', type=pathlib.Path, default=WORK / 'data/maps')
    parser.add_argument('--ce-maps', type=pathlib.Path, default=ROOT / 'ref/inputs/custom-original/maps')
    parser.add_argument('--exe', type=pathlib.Path, default=WORK / 'out/halopad-xbox')
    parser.add_argument('--image', type=pathlib.Path, default=WORK / 'out/halo_guest.elf')
    parser.add_argument('--manifest', type=pathlib.Path, default=WORK / 'out/build-mac.json')
    parser.add_argument('--angle', type=pathlib.Path, default=WORK / 'angle')
    parser.add_argument('--case', choices=CASES, action='append', help='repeat to select cases')
    parser.add_argument('--render', action='store_true', help='open real game windows (default: null renderer)')
    parser.add_argument('--timeout', type=int, default=240, help='seconds per session')
    options = parser.parse_args()
    spec = importlib.util.spec_from_file_location('mac_smoke', ROOT / 'scripts/xbox/smoke-mac.py')
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    try:
        manifest = smoke.verify_build(options.exe, options.image, options.manifest)
        release = json.loads((ROOT / 'config/xbox-release.json').read_text())
        if manifest['revision'] != release['revision']:
            raise ValueError('Mac build does not match config/xbox-release.json; rebuild before testing')
        verify_addresses(options.host_address, options.client_address)
        for name in ('bloodgulch.map', 'beavercreek.map', *RESOURCES):
            if not (options.ce_maps / name).is_file():
                raise ValueError(f'Missing private CE fixture: {name}')
        if not (options.xbox_maps / 'ui.map').is_file():
            raise ValueError('Xbox maps folder lacks ui.map')
        options.out = options.out.resolve()
        options.out.mkdir(parents=True, exist_ok=False)
    except (ValueError, OSError) as error:
        parser.error(str(error))

    source_hashes = {name: digest(options.ce_maps / name) for name in ('bloodgulch.map', *RESOURCES)}
    original = {'xbox': snapshot(options.xbox_maps), 'ce': snapshot(options.ce_maps)}
    host = prepare_data(options.out / 'host-data', options.xbox_maps, options.ce_maps,
                        resources=True, game_map=True)
    cases = set(options.case or CASES)
    results = {}
    protected = {}
    try:
        for name in CASES:
            if name not in cases:
                continue
            if name == 'cached-rejoin':
                client = options.out / 'missing-map-data'
                if not client.exists():
                    client = prepare_data(client, options.xbox_maps, options.ce_maps, resources=True)
                    first = completed(session(options, options.out, host, client, 'cache-prime'), client, source_hashes)
                    results['cache-prime'] = first
                    if not first['pass']:
                        continue
                before = snapshot(client / 'custom_maps')
                result = completed(session(options, options.out, host, client, name), client, source_hashes,
                                   expect_download=False)
                result['cached_files_unchanged'] = snapshot(client / 'custom_maps') == before
                result['pass'] &= result['cached_files_unchanged']
            else:
                client = prepare_data(options.out / f'{name}-data', options.xbox_maps, options.ce_maps,
                                      resources=name != 'missing-resources')
                if name == 'collision':
                    existing = client / 'custom_maps/bloodgulch.map'
                    copy_file(options.ce_maps / 'beavercreek.map', existing)
                    before = digest(existing)
                    filename = f'bloodgulch_{map_hash(options.ce_maps / "bloodgulch.map")[:8]}.map'
                    result = completed(session(options, options.out, host, client, name), client, source_hashes,
                                       filename=filename)
                    result['existing_map_preserved'] = digest(existing) == before
                    result['pass'] &= result['existing_map_preserved']
                elif name == 'interrupted':
                    first = session(options, options.out, host, client, 'interrupted-partial', interrupt=True)
                    first['no_complete_map'] = not (client / 'custom_maps/bloodgulch.map').exists()
                    first['pass'] &= first['no_complete_map']
                    results['interrupted-partial'] = first
                    if not first['pass']:
                        continue
                    result = completed(session(options, options.out, host, client, name), client, source_hashes)
                    result['resumed_logged'] = bool(re.search(r'map sharing: took up .* \([1-9]\d* of \d+ pieces\)',
                                                             result['client_debug']))
                    result['pass'] &= result['resumed_logged']
                else:
                    result = completed(session(options, options.out, host, client, name), client, source_hashes)
            results[name] = result
            print(f'{name}: {"PASS" if result["pass"] else "FAIL"}', flush=True)
        for folder in options.out.glob('*-data'):
            protected[folder.name] = (
                snapshot(folder / 'maps') == original['xbox'] and
                (folder / 'save/preservation-sentinel.bin').read_bytes() ==
                b'HaloPad map-sharing preservation sentinel\n')
    finally:
        preserved = original == {'xbox': snapshot(options.xbox_maps), 'ce': snapshot(options.ce_maps)}
        # Keep details in their individual private log files, not public summaries.
        for result in results.values():
            result.pop('client_debug', None)
            result.pop('host_debug', None)
        summary = {'engine_revision': manifest['revision'], 'guest_sha256': manifest['guest_sha256'],
                   'executable_sha256': manifest['executable_sha256'], 'results': results,
                   'source_maps_unchanged': preserved, 'scratch_maps_and_sentinels_unchanged': protected,
                   'pass': cases.issubset(results) and all(r['pass'] for r in results.values()) and
                           preserved and bool(protected) and all(protected.values()),
                   'limits': 'Local native Mac pair only; no iOS, internet tunnel, third peer or human-input acceptance.'}
        (options.out / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps(summary, indent=2))
    return 0 if summary['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
