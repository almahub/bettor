from types import SimpleNamespace

from better.desktop.maintenance import DesktopActions


def test_bridge_exposes_only_two_actions_and_never_walks_native_window(tmp_path):
    api = DesktopActions(tmp_path, 8001)

    class NativeTrap:
        def __dir__(self):
            raise AssertionError('Native window must never be traversed by bridge discovery')

    api._window = NativeTrap()
    public = {name: getattr(api, name) for name in dir(api) if not name.startswith('_')}
    assert set(public) == {'restart', 'install_update'}
    assert all(callable(value) for value in public.values())
    assert not any(isinstance(value, NativeTrap) for value in public.values())


def test_window_attaches_native_reference_privately(monkeypatch, tmp_path):
    import sys

    from better.desktop import window

    created = []
    native = SimpleNamespace()
    fake = SimpleNamespace(settings={}, start=lambda **kw: None)

    def create(*args, **kwargs):
        created.append(kwargs['js_api'])
        return native

    fake.create_window = create
    monkeypatch.setitem(sys.modules, 'webview', fake)
    window.show('http://127.0.0.1:8001/', home=tmp_path, port=8001)
    assert created[0]._window is native
    assert not hasattr(created[0], 'window')
