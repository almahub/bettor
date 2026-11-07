"""Generate original vector artwork and raster exports. Optional tools: fonttools, brotli, pillow, resvg-py."""
from io import BytesIO
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from PIL import Image
import resvg_py

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'better/web/brand'
OUT.mkdir(exist_ok=True)
FONT = TTFont(ROOT / 'better/web/fonts/InterVariable.woff2')

# Manual reconstruction of Concept A; the supplied reference remains the visual authority.
# Three ascending, slightly slanted bars and the diagonal cut form the open spine of the B.
MARK = '''<path d="M53 24H147C191 24 213 49 207 82C203 106 186 123 160 136C190 142 202 160 196 184C189 215 162 232 123 232H12L17 214C40 203 63 195 92 187H126C144 187 155 178 157 166C159 156 154 150 145 147L22 201L36 139Q37 133 43 133H61Q66 133 65 139L57 179L71 171L82 116Q83 110 89 110H107Q112 110 111 116L103 158L117 151L126 94Q127 88 133 88H151Q156 88 155 94L149 137C165 125 177 109 177 92C177 72 164 62 143 62H43Z"/>'''


def svg(body, view='0 0 240 256'):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}">{body}</svg>'


def lettering(text, size, weight, x, baseline, color, tracking=0, skew=0):
    font = instantiateVariableFont(FONT, {'wght': weight}, inplace=False)
    glyphs, cmap = font.getGlyphSet(), font.getBestCmap()
    scale = size / font['head'].unitsPerEm
    pen = SVGPathPen(glyphs)
    cursor = x
    for char in text:
        name = cmap[ord(char)]
        transform = TransformPen(pen, (scale, 0, skew * scale, -scale, cursor, baseline))
        glyphs[name].draw(transform)
        cursor += glyphs[name].width * scale + tracking
    return f'<path fill="{color}" d="{pen.getCommands()}"/>'


for theme, ink, primary in [('dark', '#E9F0F5', '#46D6B2'), ('light', '#101923', '#087F69')]:
    symbol = svg(f'<g fill="{primary}">{MARK}</g>')
    (OUT / f'bettor-symbol-{theme}.svg').write_text(symbol, encoding='utf-8')
    logo = svg(f'<g fill="{primary}">{MARK}</g>'
               + lettering('BETTOR', 125, 850, 265, 152, ink, 1, .16)
               + lettering('FOOTBALL INTELLIGENCE', 22, 500, 273, 201, '#9BAFBD' if theme == 'dark' else '#526A7B', 4), '0 0 900 256')
    (OUT / f'bettor-logo-{theme}.svg').write_text(logo, encoding='utf-8')

icon = svg('''<defs><linearGradient id="surface" x2="0.8" y2="1"><stop stop-color="#253644"/><stop offset="1" stop-color="#101923"/></linearGradient></defs><rect x="2" y="2" width="252" height="252" rx="54" fill="url(#surface)" stroke="#365362" stroke-width="2"/><g transform="translate(27 12) scale(.85)" fill="#46D6B2">'''+MARK+'</g>', '0 0 256 256')
(OUT / 'bettor-app.svg').write_text(icon, encoding='utf-8')
for size in [16, 32, 48, 256]:
    (OUT / f'bettor-{size}.png').write_bytes(resvg_py.svg_to_bytes(svg_string=icon, width=size, height=size))
large = Image.open(BytesIO(resvg_py.svg_to_bytes(svg_string=icon, width=256, height=256)))
large.save(OUT / 'bettor.ico', sizes=[(s,s) for s in [16,20,24,32,40,48,64,128,256]])
(ROOT / 'better/web/favicon.svg').write_text(icon, encoding='utf-8')
print('Generated vector logos/symbols, SVG application icon, PNG 16/32/48/256 and multi-resolution ICO.')
