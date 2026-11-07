import struct
import sys
from pathlib import Path
from types import SimpleNamespace

from better.desktop import launcher, window

BRAND = Path(__file__).resolve().parents[2] / 'better/web/brand'


def test_png_and_windows_icon_have_real_required_resolutions():
    for size in (16, 32, 48, 256):
        data = (BRAND / f'bettor-{size}.png').read_bytes()
        assert data[:8] == b'\x89PNG\r\n\x1a\n'
        assert struct.unpack('>II', data[16:24]) == (size, size)
    data = (BRAND / 'bettor.ico').read_bytes()
    reserved, kind, count = struct.unpack('<HHH', data[:6])
    assert (reserved, kind) == (0, 1)
    sizes = {data[6 + i * 16] or 256 for i in range(count)}
    assert {16, 32, 48, 256} <= sizes


def test_webview_uses_bundled_brand_icon_without_changing_runtime_settings(monkeypatch):
    calls = {}
    fake = SimpleNamespace(settings={}, create_window=lambda *args, **kw: calls.update(window=(args, kw)),
                           start=lambda **kw: calls.update(start=kw))
    monkeypatch.setitem(sys.modules, 'webview', fake)
    monkeypatch.setattr(launcher, 'resource_root', lambda: BRAND.parents[2])
    window.show('http://127.0.0.1:8017/')
    assert calls['window'][0] == ('Bettor', 'http://127.0.0.1:8017/')
    assert Path(calls['start']['icon']) == BRAND / 'bettor.ico'
    assert calls['start']['gui'] == 'edgechromium'
    assert calls['start']['private_mode'] is True
    assert calls['start']['debug'] is False


def test_primary_text_palette_meets_aa_on_all_surfaces():
    def luminance(color):
        rgb = [int(color[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [n / 12.92 if n <= .04045 else ((n + .055) / 1.055) ** 2.4 for n in rgb]
        return sum(a * b for a, b in zip(linear, (.2126, .7152, .0722), strict=True))

    for text in ('E9F0F5', '9BAFBD', '46D6B2', 'E8AF54', 'F18A91', 'A995ED'):
        for surface in ('101923', '1B2935', '253644', '10251D', '19362A', '1F3D30'):
            light, dark = sorted((luminance(text), luminance(surface)), reverse=True)
            assert (light + .05) / (dark + .05) >= 4.5, (text, surface)
