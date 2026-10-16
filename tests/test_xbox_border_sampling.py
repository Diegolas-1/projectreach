"""Asset-free policy, sampling boundary and two-file adaptation checks."""
import hashlib
import math
import pathlib
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts/xbox'))
import guest_adaptation as guest
border = guest.border_sampling


class BorderSamplingTests(unittest.TestCase):
    def test_anchors_refuse_missing_or_ambiguous_inputs(self):
        for edits in (border.RENDERER_EDITS, border.SHADER_EDITS,
                      border.GLES_RENDERER_EDITS, border.GLES_SHADER_EDITS):
            original = b'\n'.join(a for a, _ in edits)
            border.apply_edits(original, edits)
            for anchor, _ in edits:
                for bad in (original.replace(anchor, b''), original + anchor):
                    with self.assertRaises(ValueError):
                        border.apply_edits(bad, edits)
        with self.assertRaises(ValueError):
            border.adapt_shader(b'changed upstream')

    def test_old_recipe_stays_identical_and_border_changes_identity(self):
        self.assertEqual(guest.identity('render-water-v1', '13c14df95c63156429e96a9694bb9cecd0379228')['recipe_sha256'],
                         'ab7a4178dd3ab4363cc5ab7203e84b5acebecf6ed5af2c1e26c2c30a12302db2')
        self.assertNotEqual(guest.identity('render-border-v1'), guest.identity('render-water-v1'))
        self.assertIn('render-border-v1', guest.COUNTED_ADAPTATIONS)
        # Uniform updates must not be skipped by the unrelated cached serial.
        self.assertIn(b'uniform_vec4(entry->hp_border_state', border.RENDERER_EDITS[-1][1])

    def test_gles_shader_and_uniforms_are_a_distinct_guarded_recipe(self):
        renderer = border.GLES_RENDERER_SHA256
        source = b'\n'.join(a for a, _ in border.GLES_SHADER_EDITS)
        with patch.object(border, 'GLES_SHADER_SHA256', hashlib.sha256(source).hexdigest()):
            shader = border.adapt_shader(source, renderer)
            self.assertIn(b'#ifdef HALO_GLES\n', shader)
            self.assertNotIn(b'HALO_ANDROID', shader)
            self.assertIn(b'hp_border_sample(tex%d', shader)
            self.assertIn(b'uniform vec4 hp_border_state[4]', shader)
            with self.assertRaises(ValueError):
                border.adapt_shader(source)
            with self.assertRaises(ValueError):
                border.adapt_shader(source + b'unreviewed change', renderer)
        self.assertNotEqual(border.recipe(renderer), border.recipe())
        # The draw's border values reach the selected program even when the
        # ordinary uniforms are cached, without dropping source-blend uploads.
        anchor, replacement = border.GLES_RENDERER_EDITS[-1]
        self.assertEqual(anchor, border.GLES_UPLOAD_ANCHOR)
        self.assertLess(replacement.index(b'uniform_vec4(entry->hp_border_state'),
                        replacement.index(b'if (key.source_blend'))
        self.assertEqual(replacement.count(b'state_program(entry->program);'), 1)

    def test_two_sources_restore_on_failure_and_preserve_concurrent_edit(self):
        renderer = b'\n'.join((guest.ANCHOR, guest.FILTER_ANCHOR, guest.COUNT_ANCHOR,
                              guest.ATOMIC_ANCHOR, guest.WATER_SAVE_ANCHOR, guest.WATER_RESTORE_ANCHOR,
                              *(a for a, _ in border.RENDERER_EDITS)))
        shader = b'\n'.join(a for a, _ in border.SHADER_EDITS)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict('os.environ', {'XBOX_REV': 'synthetic-fixture'}), \
             patch.object(guest, 'SOURCE_SHA256', hashlib.sha256(renderer).hexdigest()), \
             patch.object(border, 'SHADER_SHA256', hashlib.sha256(shader).hexdigest()):
            root = pathlib.Path(tmp)
            a, b = root / guest.RENDERER, root / border.SHADER
            a.parent.mkdir(parents=True)
            for concurrent in (False, True):
                a.write_bytes(renderer); b.write_bytes(shader)
                with self.assertRaises(RuntimeError):
                    with guest.renderer_adaptation(root, guest.identity('render-border-v1')):
                        self.assertNotEqual(a.read_bytes(), renderer)
                        self.assertNotEqual(b.read_bytes(), shader)
                        if concurrent:
                            b.write_bytes(b'user edit')
                        raise RuntimeError('failed build')
                self.assertEqual(a.read_bytes(), renderer)
                self.assertEqual(b.read_bytes(), b'user edit' if concurrent else shader)

    def test_actual_c_policy(self):
        source = r'''
#include <assert.h>
#include <string.h>
typedef int BOOL;
typedef unsigned DWORD;
typedef unsigned GLenum;
enum { GL_TEXTURE_2D, GL_TEXTURE_3D, D3DTEXF_POINT=10, D3DTEXF_LINEAR,
       D3DTEXF_ANISOTROPIC, D3DTADDRESS_BORDER=20, D3DTADDRESS_WRAP,
       D3DTSS_MINFILTER=0, D3DTSS_MAGFILTER, D3DTSS_ADDRESSU, D3DTSS_ADDRESSV, D3DTSS_BORDERCOLOR };
static struct { int border_clamp; } xgpu_capabilities;
static DWORD D3D__TextureState[4][5];
static void color_to_vec4(DWORD c, float *v) { v[0] = c; }
''' + border.POLICY + r'''
int main(void) {
    DWORD *s = D3D__TextureState[0];
    s[D3DTSS_MINFILTER] = s[D3DTSS_MAGFILTER] = D3DTEXF_LINEAR;
    s[D3DTSS_ADDRESSU] = D3DTADDRESS_BORDER; s[D3DTSS_ADDRESSV] = D3DTADDRESS_WRAP;
    s[D3DTSS_BORDERCOLOR] = 123;
    hp_border_configure(0, GL_TEXTURE_2D, 1, 0);
    assert(hp_border_state[0][0] == 1 && hp_border_state[0][1] == 0 && hp_border_state[0][2] == 1);
    assert(hp_border_color[0][0] == 123);
    for (int reject = 0; reject < 5; reject++) {
        memset(hp_border_state, 0, sizeof(hp_border_state));
        xgpu_capabilities.border_clamp = reject == 0;
        s[D3DTSS_MAGFILTER] = reject == 4 ? D3DTEXF_POINT : D3DTEXF_LINEAR;
        hp_border_configure(0, reject == 1 ? GL_TEXTURE_3D : GL_TEXTURE_2D,
                            reject == 2 ? 2 : 1, reject == 3);
        assert(hp_border_state[0][0] == 0);
    }
    xgpu_capabilities.border_clamp = 0;
    s[D3DTSS_MINFILTER] = s[D3DTSS_MAGFILTER] = D3DTEXF_POINT;
    hp_border_configure(0, GL_TEXTURE_2D, 1, 0);
    assert(hp_border_state[0][0] == 1 && hp_border_state[0][2] == 0);
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            binary = pathlib.Path(tmp) / 'policy'
            result = subprocess.run(['clang', '-x', 'c', '-', '-Wall', '-Werror',
                                     '-fsanitize=address,undefined', '-o', str(binary)],
                                    input=source, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(binary)], check=True, capture_output=True)

    def test_linear_coverage_matches_independent_bilinear_border_oracle(self):
        # Full arbitrary-color 2D oracle; not merely zeroing outside UV 0..1.
        rng = random.Random(739)
        tex = [[rng.random() for _ in range(5)] for _ in range(3)]
        for mask in ((0, 0), (1, 0), (0, 1), (1, 1)):
            for u, v in [(0, 0), (1, 1), (.5, .5), (-.1, .5), (1.1, .5)] + [(rng.uniform(-.3, 1.3), rng.uniform(-.5, 1.5)) for _ in range(300)]:
                border_color = .37
                x, y = u * 5 - .5, v * 3 - .5
                ix, iy = math.floor(x), math.floor(y)
                fx, fy = x - ix, y - iy
                expected = native_edge = 0
                for dx, wx in ((0, 1-fx), (1, fx)):
                    for dy, wy in ((0, 1-fy), (1, fy)):
                        a, b = ix + dx, iy + dy
                        value = tex[min(2, max(0, b))][min(4, max(0, a))]
                        native_edge += wx * wy * value
                        outside = (mask[0] and not 0 <= a < 5) or (mask[1] and not 0 <= b < 3)
                        expected += wx * wy * (border_color if outside else value)
                coverage = [max(0, min(1, p*n+.5, (1-p)*n+.5)) if flag else 1
                            for p, n, flag in zip((u, v), (5, 3), mask)]
                actual = border_color + (native_edge-border_color) * math.prod(coverage)
                self.assertAlmostEqual(actual, expected, places=12)
        self.assertIn('coverage.x * coverage.y', border.GLSL)
        self.assertIn('textureSize(image, 0)', border.GLSL)
