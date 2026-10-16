"""Presentation diagnostics must bracket the real guest blit, not replace it."""
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class XboxGLGeneratorTests(unittest.TestCase):
    def test_base_vertex_core_and_extensions_keep_index_offsets(self):
        # GLES 3.0 selects EXT or OES instead of the core name. These pointers
        # address the bound index buffer, so adding xg_base breaks every draw.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source, output = root / 'guest.c', root / 'host.c'
            names = ('glDrawElementsBaseVertex', 'glDrawElementsBaseVertexEXT', 'glDrawElementsBaseVertexOES')
            source.write_text(''.join('void hostgl_' + name +
                '(GLenum, GLsizei, GLenum, const void *, GLint);\n' for name in names) +
                'void hostgl_glUniform4fv(GLint, GLsizei, const GLfloat *);\n')
            subprocess.run([sys.executable, str(ROOT / 'scripts/xbox/gen-host-gl.py'),
                            str(source), str(output)], check=True, capture_output=True)
            (root / 'GLES3').mkdir()
            (root / 'GLES3/gl32.h').write_text('''#pragma once
typedef unsigned GLenum, GLuint;
typedef int GLint, GLsizei;
typedef float GLfloat;
''')
            (root / 'test.c').write_text(r'''
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include "host.c"
uintptr_t xg_base;
static unsigned calls, timed, traced;
static uintptr_t expected;
static void draw(GLenum mode, GLsizei count, GLenum type, const void *indices, GLint base) {
    assert(mode==4 && count==6 && type==0x1403 && base==-2);
    assert((uintptr_t)indices==expected); calls++;
}
static void uniform(GLint location, GLsizei count, const GLfloat *data) {
    assert(location==7 && count==1 && (uintptr_t)data==xg_base+32);
}
static GLenum no_error(void) { return 0; }
void *xg_gl_proc(const char *name) {
    if (!strncmp(name, "glDrawElementsBaseVertex", 24)) return draw;
    if (!strcmp(name, "glUniform4fv")) return uniform;
    if (!strcmp(name, "glGetError")) return no_error;
    abort();
}
void xg_log(const char *format, ...) { (void)format; abort(); }
double xg_gl_cost_begin(void) { return 1; }
void xg_gl_cost_end(int kind, double started) { assert(kind==3 && started==1); timed++; }
void xg_gl_trace_query_draw(GLenum mode, GLint first, GLsizei count) {
    assert(mode==0 && first==0 && count==0); traced++;
}
int main(void) {
    unsigned char memory[128]; xg_base=(uintptr_t)memory;
    xg_gl_load();
    unsigned offsets[]={0, 32, 0xf0000000u};
    for (unsigned i=0;i<3;i++) {
        expected=offsets[i];
        xh_hostgl_glDrawElementsBaseVertex(4,6,0x1403,offsets[i],-2);
        xh_hostgl_glDrawElementsBaseVertexEXT(4,6,0x1403,offsets[i],-2);
        xh_hostgl_glDrawElementsBaseVertexOES(4,6,0x1403,offsets[i],-2);
    }
    xh_hostgl_glUniform4fv(7,1,32);
    assert(calls==9 && timed==9 && traced==9);
}
''')
            binary = root / 'test'
            subprocess.run(['clang', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                            '-I', str(root), '-I', str(ROOT / 'port/xbox'), str(root / 'test.c'),
                            '-o', str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True)

    def test_query_observation_follows_call_and_converts_guest_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / 'guest.c'
            output = pathlib.Path(tmp) / 'host.c'
            source.write_text('void hostgl_glBeginQuery(GLenum, GLuint);\n'
                              'void hostgl_glEndQuery(GLenum);\n'
                              'void hostgl_glDrawArrays(GLenum, GLint, GLsizei);\n'
                              'void hostgl_glDrawElements(GLenum, GLsizei, GLenum, const void *);\n'
                              'void hostgl_glGetQueryObjectuiv(GLuint, GLenum, GLuint *);\n')
            subprocess.run([sys.executable, str(ROOT / 'scripts/xbox/gen-host-gl.py'),
                            str(source), str(output)], check=True, capture_output=True)
            generated = output.read_text()
            self.assertLess(generated.index('if (a1 == 0x48504356u)'),
                            generated.index('p_glGetQueryObjectuiv(a0, a1, (GLuint *)GP(a2));'))
            self.assertIn('xg_gl_query_samples(a0, a2 ? G(GLuint *, a2) : NULL); return;', generated)
            self.assertLess(generated.index('p_glBeginQuery(a0, a1);'),
                            generated.index('xg_gl_trace_query(1, a1, a0, 0);'))
            self.assertLess(generated.index('p_glGetQueryObjectuiv(a0, a1, (GLuint *)GP(a2));'),
                            generated.index('if (a2) xg_gl_trace_query(0, a0, a1, *G(GLuint *, a2));'))
            for call, observer in (
                ('p_glEndQuery(a0);', 'xg_gl_trace_query_end(a0);'),
                ('p_glDrawArrays(a0, a1, a2);', 'xg_gl_trace_query_draw(a0, a1, a2);'),
                ('p_glDrawElements(a0, a1, a2, (const void *)(uintptr_t)a3);',
                 'xg_gl_trace_query_draw(0, 0, 0);'),
            ):
                self.assertLess(generated.index(call), generated.index(observer))

    def test_blit_coordinates_and_widened_stack_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / 'guest.c'
            output = pathlib.Path(tmp) / 'host.c'
            source.write_text(
                'static void guest_glBlitFramebuffer(GLint x0, GLint y0, GLint x1, GLint y1, '
                'GLint x2, GLint y2, GLint x3, GLint y3, GLbitfield mask, GLenum filter)\n'
                'void hostgl_glBlitFramebuffer(GLint, GLint, GLint, GLint, GLint, GLint, GLint, GLint, long long, long long);\n')
            subprocess.run([sys.executable, str(ROOT / 'scripts/xbox/gen-host-gl.py'),
                            str(source), str(output)], check=True, capture_output=True)
            generated = output.read_text()
            before = generated.index('xg_gl_trace_blit(0, a0, a1, a2, a3);')
            call = generated.index('p_glBlitFramebuffer(a0, a1, a2, a3, a4, a5, a6, a7, (GLbitfield)a8, (GLenum)a9);')
            after = generated.index('xg_gl_trace_blit(1, a4, a5, a6, a7);')
            self.assertLess(before, call)
            self.assertLess(call, after)


if __name__ == '__main__':
    unittest.main()
