"""Render model output as Markdown without executing embedded HTML or images."""
import html
from functools import lru_cache


@lru_cache(maxsize=1)
def parser():
    from markdown_it import MarkdownIt
    md = MarkdownIt('commonmark', {'html': False, 'linkify': False, 'breaks': True})
    md.enable('table').enable('strikethrough')
    def math_inline(state, silent):
        pos = state.pos
        for left, right, display in [('$$','$$',True), ('\\[','\\]',True), ('\\(','\\)',False), ('$','$',False)]:
            if not state.src.startswith(left, pos):
                continue
            start = pos + len(left)
            end = state.src.find(right, start)
            if end < 0 or (left == '$' and (state.src[start:start+1].isspace() or state.src[end-1:end].isspace())):
                return False
            if not silent:
                token = state.push('math_source', '', 0)
                token.content = state.src[start:end]
                token.meta = {'display':display}
            state.pos = end + len(right)
            return True
        return False
    def math_block(state, start, end, silent):
        line = state.src[state.bMarks[start]+state.tShift[start]:state.eMarks[start]].strip()
        if line not in ('$$', '\\['):
            return False
        closing = '$$' if line == '$$' else '\\]'
        stop = start + 1
        while stop < end and state.src[state.bMarks[stop]:state.eMarks[stop]].strip() != closing:
            stop += 1
        if stop == end:
            return False
        if not silent:
            token = state.push('math_source', '', 0)
            token.content = state.getLines(start+1, stop, 0, False)
            token.meta = {'display':True}
            state.line = stop + 1
        return True
    def math_render(tokens, idx, options, env):
        token=tokens[idx]
        return '<span class="math-source" data-display="'+str(bool(token.meta.get('display'))).lower()+'">'+html.escape(token.content)+'</span>'
    md.inline.ruler.before('escape', 'math_source', math_inline)
    md.block.ruler.before('fence', 'math_source', math_block)
    md.renderer.rules['math_source'] = math_render
    # Remote images can leak browsing metadata; show their alt text instead.
    def image(tokens, idx, options, env):
        return '<span class="image-reference">[图片：' + html.escape(tokens[idx].content) + ']</span>'
    md.renderer.rules['image'] = image
    return md


def render(text):
    try:
        return parser().render(text)
    except ImportError:
        return '<pre>' + html.escape(text) + '</pre>'


STYLE = '''<style>
.say.markdown{white-space:normal;overflow-wrap:anywhere;line-height:1.7}
.markdown h1,.markdown h2,.markdown h3{margin:1em 0 .45em;line-height:1.35}
.markdown h1{font-size:1.5em}.markdown h2{font-size:1.3em}.markdown h3{font-size:1.15em}
.markdown p{margin:.6em 0}.markdown ul,.markdown ol{padding-left:1.8em}
.markdown pre{white-space:pre;overflow:auto;padding:14px;background:#151820;border-radius:8px}
.markdown code{font-family:Consolas,monospace;background:#151820;padding:2px 4px;border-radius:4px}
.markdown pre code{padding:0}.markdown table{display:block;overflow-x:auto;border-collapse:collapse;margin:12px 0}
.markdown th,.markdown td{padding:8px 12px;border:1px solid #ffffff26;text-align:left}
.markdown blockquote{border-left:3px solid #7b8bd1;padding-left:12px;margin-left:0;color:#b7c0d3}
.markdown a{color:#8eb9ff;text-decoration:underline}.markdown hr{border:0;border-top:1px solid #ffffff26}
</style>'''
