"""Render model output as Markdown without executing embedded HTML or images."""
import html
from functools import lru_cache


@lru_cache(maxsize=1)
def parser():
    from markdown_it import MarkdownIt
    md = MarkdownIt('commonmark', {'html': False, 'linkify': False, 'breaks': True})
    md.enable('table').enable('strikethrough')
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
