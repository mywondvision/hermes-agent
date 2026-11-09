"""HTTP surfaces attach to an existing owner or bootstrap one, never both."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from hermes_cli import plugin_services, web_server_app


@pytest.mark.parametrize('attached', [False, True])
def test_host_services_wrap_standalone_lifetime_only(monkeypatch, tmp_path, attached):
    import tui_gateway
    events = []
    owner = SimpleNamespace(_launch_home=lambda: tmp_path)
    monkeypatch.setattr(tui_gateway, 'server', owner, raising=False)

    @asynccontextmanager
    async def surface(_app):
        events.append('surface-start')
        try:
            yield
        finally:
            events.append('surface-stop')

    monkeypatch.setattr(web_server_app, 'http_lifespan', surface)
    monkeypatch.setattr(web_server_app, 'standalone_lifespan', surface)
    monkeypatch.setattr(plugin_services, 'start_host_services', lambda _owner, _home: events.append('services-start'))
    monkeypatch.setattr(plugin_services, 'close_host_service_admission', lambda _owner: events.append('admission-close'))
    monkeypatch.setattr(plugin_services, 'stop_host_services', lambda _owner: events.append('services-stop'))
    app = SimpleNamespace(state=SimpleNamespace(gateway_runner=object() if attached else None))

    async def exercise():
        async with web_server_app.app_lifespan(app):
            events.append('request')
    asyncio.run(exercise())
    expected = ['surface-start', 'request', 'surface-stop'] if attached else [
        'surface-start', 'services-start', 'request', 'admission-close', 'surface-stop', 'services-stop']
    assert events == expected
