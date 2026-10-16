#!/usr/bin/env python3
"""G3: build and run HaloPad's native core from Halo's PE entry point.

Links the VA-model translation (scripts/va-model.py) with guest memory, dispatch, the
main-thread environment and the Windows services implemented so far, then runs it with
the prepared image and game files. The run stops at the first trap and reports it;
evidence goes to docs/artifacts/<date>/G3/core-<target>-<stamp>/.

Usage: .venv/bin/python scripts/run-core.py [--work RUN_DIR] [--target TRIPLE] [--timeout S]
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import xiph  # noqa: E402  (libogg/libvorbis for vorbisfile.dll)
import vabuild  # noqa: E402  (translated DLLs)

ROOT = pathlib.Path(__file__).resolve().parents[1]
SUPPORT = ROOT / 'port' / 'llasm-support'
PROFILE = ROOT / 'generated' / 'srw' / 'custom-en-1.0.10.0621'
IMAGE = ROOT / 'generated' / 'analysis' / 'custom-en-1.0.10.0621' / 'image.bin'
GAME_ROOT = ROOT / 'ref' / 'inputs' / 'custom-original'


def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def sdk_path(target):
    """Use a target-compatible SDK, including when Apple's Python injects SDKROOT."""
    override = os.environ.get('SDKROOT')
    if 'ios' not in target:
        return override  # desktop core builds retain the compiler's default
    sdk = 'macosx' if 'macabi' in target else 'iphonesimulator' if 'simulator' in target else 'iphoneos'
    # /usr/bin/python3 adds the Mac SDK to its environment even when PadMint
    # removed it before starting the backend. Keep an intentional matching SDK
    # override, but never compile iPhone/Simulator code against that host SDK.
    if override and re.fullmatch(rf'{sdk}(?:[0-9]+(?:\.[0-9]+)*)?\.sdk',
                                 pathlib.Path(override).name.lower()):
        return override
    return subprocess.run(['xcrun', '--sdk', sdk, '--show-sdk-path'], check=True, capture_output=True, text=True).stdout.strip()


def build(work, target, main_src, extra=()):
    va = work / 'va'
    if not (va / 'haloce.va.ll').exists():
        sys.exit('run scripts/va-model.py first')
    out = work / f'core-{target}'
    out.mkdir(exist_ok=True)
    obj = work / f'slices-va-{target}' / 'haloce.va.o'   # shared with run-slices.py
    obj.parent.mkdir(exist_ok=True)
    if not obj.exists() or obj.stat().st_mtime < (va / 'haloce.va.ll').stat().st_mtime:
        print('compiling translated Halo (about 90 s)...', flush=True)
        subprocess.run(['clang', '-target', target, *(['-isysroot', sdk_path(target)] if sdk_path(target) else []), '-c', '-O1', '-fno-fast-math', '-ffp-contract=off', '-Wno-override-module',
                        str(va / 'haloce.va.ll'), '-o', str(obj)], check=True)
    runtime_ll = sorted(va.glob('halopad-*.ll'))
    mod_objs, mod_lls = vabuild.module_objects(va, target, obj.parent)
    subprocess.run([sys.executable, str(ROOT / 'scripts/gen-import-stubs.py'), str(va / 'haloce.va.ll'), str(out / 'stubs.ll'),
                    str(va / 'dispatch.ll'), *map(str, runtime_ll), *map(str, mod_lls)], check=True, capture_output=True)
    subprocess.run([sys.executable, str(ROOT / 'scripts/gen-nls-tables.py')], check=True, capture_output=True)
    exe = out / ('halopad-core' if main_src.name == 'halopad_core_main.c' else main_src.stem)
    ios = 'ios' in target
    sdk = sdk_path(target)
    # Mac Catalyst: UIKit and its iOS frameworks live in the macOS SDK's iOSSupport tree
    catalyst = ['-iframework', f'{sdk}/System/iOSSupport/System/Library/Frameworks', '-isystem', f'{sdk}/System/iOSSupport/usr/include',
                '-L', f'{sdk}/System/iOSSupport/usr/lib'] if 'macabi' in target else []
    cmd = ['clang', '-target', target, *(['-isysroot', sdk] if sdk else []), *catalyst, '-O2', '-fno-fast-math', '-ffp-contract=off', '-w', '-DPTROFS_64BIT=1', '-std=c2x',
           '-Wno-override-module', '-I', str(SUPPORT), '-I', str(ROOT / 'generated' / 'runtime'), *xiph.include_flags(),
           str(main_src), *map(str, extra), *map(str, sorted((ROOT / 'port/runtime').glob('*.c'))),
           *map(str, sorted(SUPPORT.glob('llasm_*.c'))), str(va / 'dispatch.ll'), *map(str, runtime_ll),
           str(out / 'stubs.ll'), str(obj), *map(str, mod_objs), *[str(m) for m in sorted((ROOT / 'port/apple').glob('*.m'))], str(xiph.archive(target, sdk)), '-fobjc-arc', '-framework', 'CoreGraphics', '-framework', 'CoreText', '-framework', 'UIKit' if ios else 'Cocoa', '-framework', 'Metal', '-framework', 'GameController', '-framework', 'QuartzCore', '-framework', 'AudioToolbox', '-framework', 'AVFoundation', '-framework', 'UniformTypeIdentifiers', '-lz', '-o', str(exe)]
    link = subprocess.run(cmd, capture_output=True, text=True)
    (out / 'link.log').write_text(' '.join(cmd) + '\n' + link.stdout + link.stderr)
    if link.returncode:
        sys.exit('link failed:\n' + link.stderr[-2000:])
    return exe, obj


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--work', type=pathlib.Path)
    ap.add_argument('--target')
    ap.add_argument('--timeout', type=int, default=120)
    ap.add_argument('--fresh-state', action='store_true',
                    help='start from an empty state folder (no profile, settings or saves), kept in the evidence')
    ap.add_argument('--relaunch', action='store_true',
                    help='run twice, one after the other, on the same fresh state folder (HALOPAD_LAUNCH=1, 2); evidence under launch-<n>/')
    ap.add_argument('--clients', type=int, default=1,
                    help='run this many instances at once (network tests): each gets HALOPAD_CLIENT=<i>, its own state\n'
                         'folder generated/halopad-disk-client<i> and evidence under client-<i>/')
    ap.add_argument('--run-prefix', nargs='*', default=[], help='command prefix to run the binary (e.g. xcrun simctl spawn <device>)')
    ap.add_argument('--main', type=pathlib.Path, default=ROOT / 'port/core/halopad_core_main.c',
                    help='program to link in place of the core (e.g. tests/halo_d3d9_test.c)')
    a = ap.parse_args()
    work = (a.work or max(PROFILE.glob('run-*/va/haloce.va.ll'), key=lambda p: p.stat().st_mtime).parent.parent).resolve()
    target = a.target or json.loads((ROOT / 'toolchains.lock.json').read_text())['target']
    exe, obj = build(work, target, a.main.resolve())
    stamp = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    evid = ROOT / 'docs' / 'artifacts' / datetime.date.today().isoformat() / 'G3' / f'core-{target}-{stamp}'
    evid.mkdir(parents=True, exist_ok=True)
    # each run starts from the reference machine's registry; the final state is evidence
    env = dict(os.environ, HALOPAD_IMAGE=str(IMAGE), HALOPAD_MODULE_IMAGES=str(IMAGE.parent / 'modules'), HALOPAD_REFERENCE_ROOT=str(ROOT / 'ref' / 'inputs' / 'reference-machine'), HALOPAD_GAME_ROOT=str(GAME_ROOT), HALOPAD_STATE_ROOT=str(ROOT / 'generated' / 'halopad-disk'), HALOPAD_REPO_ROOT=str(ROOT),
               HALOPAD_REGISTRY=str(evid / 'registry.txt'))
    if a.fresh_state or a.relaunch:
        env['HALOPAD_STATE_ROOT'] = str(evid / 'state')
    env.setdefault('HALOPAD_NET', 'lan')                   # tests stay on the LAN unless set (scripts/public-join.sh: internet)
    acceptance = ROOT / 'generated' / 'runtime-state' / 'eula-acceptance.txt'   # written only by scripts/accept-eula.sh
    if acceptance.exists():
        env['HALOPAD_EULA_ACCEPTANCE'] = str(acceptance)
    if a.run_prefix:                                    # e.g. xcrun simctl spawn <device>: it passes SIMCTL_CHILD_* variables on
        env.update({'SIMCTL_CHILD_' + k: v for k, v in list(env.items()) if k.startswith('HALOPAD_')})
    if a.relaunch:
        runs = []
        for i in (1, 2):
            d = evid / f'launch-{i}'
            d.mkdir()
            e = dict(env, HALOPAD_LAUNCH=str(i), HALOPAD_REGISTRY=str(d / 'registry.txt'))
            if a.run_prefix:
                e.update({'SIMCTL_CHILD_' + k: v for k, v in list(e.items()) if k.startswith('HALOPAD_')})
            runs.append((d, e))
    elif a.clients == 1:
        runs = [(evid, env)]
    else:
        runs = []
        for i in range(a.clients):
            d = evid / f'client-{i}'
            d.mkdir()
            e = dict(env, HALOPAD_CLIENT=str(i), HALOPAD_STATE_ROOT=str(ROOT / 'generated' / f'halopad-disk-client{i}'),
                     HALOPAD_REGISTRY=str(d / 'registry.txt'))
            if a.run_prefix:
                e.update({'SIMCTL_CHILD_' + k: v for k, v in list(e.items()) if k.startswith('HALOPAD_')})
            runs.append((d, e))
    start = lambda e: subprocess.Popen([*a.run_prefix, str(exe)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
    # clients run at once; launches run one after the other (the second starts when the first has exited)
    procs = [(d, e if a.relaunch else start(e)) for d, e in runs]
    codes = []
    previous_registry = None
    for d, p in procs:
        if a.relaunch:
            # Keep each launch's registry snapshot as evidence, while carrying the
            # actual persisted state forward just as an app restart would.
            registry = pathlib.Path(p['HALOPAD_REGISTRY'])
            if previous_registry is not None and previous_registry.exists():
                shutil.copy2(previous_registry, registry)
            previous_registry = registry
            p = start(p)
        try:
            out, err = p.communicate(timeout=a.timeout)
            code = p.returncode
        except subprocess.TimeoutExpired:
            if not a.run_prefix:                            # a hang: every thread's stack before it is stopped
                subprocess.run(['sample', str(p.pid), '3', '-file', str(d / 'hang-sample.txt')], capture_output=True)
            p.kill()
            out, err = p.communicate()
            code = 'timeout'
        (d / 'stdout.txt').write_text(out)
        hostpcs = next((l for l in err.splitlines() if l.startswith('HALOPAD HOSTPCS ')), None)
        if hostpcs:                                         # name the translated procedures the fault happened in
            load = hostpcs.split('load=')[1].split()[0]
            pcs = hostpcs.split('pcs=')[1].split()
            sym = subprocess.run(['xcrun', 'atos', '-o', str(exe), '-l', load, *pcs], capture_output=True, text=True).stdout
            err += 'HALOPAD HOSTPCS symbolized: ' + ' | '.join(l.strip() for l in sym.splitlines()) + '\n'
        (d / 'stderr.txt').write_text(err)
        stop = next((l for l in reversed(err.splitlines()) if l.startswith('HALOPAD TRAP') or l.startswith('HALOPAD FAULT')), None)
        report = {'target': target, 'work': str(work.relative_to(ROOT)), 'exit': code, 'stopped_at': stop,
                  'identities': {'image.bin': sha(IMAGE), 'haloce.va.o': sha(obj), 'executable': sha(exe)}}
        (d / 'result.json').write_text(json.dumps(report, indent=1) + '\n')
        if a.clients > 1 or a.relaunch:
            print(f'--- {"launch" if a.relaunch else "client"} {len(codes) + (1 if a.relaunch else 0)}')
        print(err[-1500:])
        print('exit', code, '| stopped at:', stop)
        codes.append(1 if stop and code == 0 else code)
    print('evidence', evid.relative_to(ROOT))
    # Evidence is still written for every launch. Shell callers must also see failures;
    # otherwise a crashed/timed-out client makes an enclosing regression look green.
    if 'timeout' in codes:
        return 124
    return next((code if code > 0 else 128 - code for code in codes if code != 0), 0)


if __name__ == '__main__':
    sys.exit(main())
