import base64
import json

import pytest
import pytest_asyncio
from fastapi import Response
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db_session
from app.enums import RoutingMode
from app.main import app
from app.services.subscription_service import create_subscription
from app.services.subscription_proxy import build_subscription_headers
from app.services.user_service import create_user


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def override_db_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        yield factory
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        await engine.dispose()


@pytest_asyncio.fixture
async def client(session_factory):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client


async def create_user_subscription(session_factory, token: str = "ARVX-MVP-TEST"):
    async with session_factory() as session:
        user = await create_user(session, display_name="MVP")
        subscription = await create_subscription(
            session=session,
            user_id=user.id,
            original_sub_url="https://example.com/sub",
            routing_mode=RoutingMode.SMART,
            public_token=token,
            device_limit=2,
        )
        await session.commit()
        return user, subscription


async def register(client: AsyncClient) -> str:
    response = await client.post(
        "/api/auth/register",
        json={"email": "mvp-complete@example.com", "password": "strongpass123", "display_name": "MVP"},
    )
    assert response.status_code == 201
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_public_subscription_html_and_raw(client, session_factory, monkeypatch):
    user, _ = await create_user_subscription(session_factory)

    async def fake_proxy(session, subscription):
        return Response(content="raw-subscription", media_type="text/plain")

    monkeypatch.setattr("app.routers.public_subscription.proxy_subscription", fake_proxy)

    html = await client.get("/u/ARVX-MVP-TEST", headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"})
    raw = await client.get("/u/ARVX-MVP-TEST?format=raw", headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"})

    assert html.status_code == 200
    assert "Arvexo Connect Subscription" in html.text
    assert "original_sub_url" not in html.text
    assert raw.status_code == 200
    assert raw.text == "raw-subscription"


@pytest.mark.asyncio
async def test_subscription_headers_include_v2raytun_direct_routing(session_factory):
    _, subscription = await create_user_subscription(session_factory, token="ARVX-ROUTING-TEST")

    headers = build_subscription_headers(subscription)
    routing = json.loads(base64.b64decode(headers["routing"]))

    assert routing["domainStrategy"] == "IPIfNonMatch"
    assert routing["rules"][0]["outboundTag"] == "direct"
    assert "domain:ru" in routing["rules"][0]["domain"]
    assert "domain:school.mos.ru" in routing["rules"][0]["domain"]
    assert "127.0.0.0/8" in routing["rules"][1]["ip"]
    assert not any(
        rule.startswith(("geosite:", "geoip:"))
        for routing_rule in routing["rules"]
        for rule in routing_rule.get("domain", []) + routing_rule.get("ip", [])
    )


@pytest.mark.asyncio
async def test_raw_subscription_access_records_real_device(client, session_factory, monkeypatch):
    await create_user_subscription(session_factory, token="ARVX-REAL-DEVICE")

    async def fake_proxy(session, subscription):
        return Response(content="raw-subscription", media_type="text/plain")

    monkeypatch.setattr("app.routers.public_subscription.proxy_subscription", fake_proxy)

    response = await client.get(
        "/u/ARVX-REAL-DEVICE?format=raw",
        headers={"User-Agent": "Happ/1.0 iPhone", "X-Forwarded-For": "203.0.113.10"},
    )

    assert response.status_code == 200
    async with session_factory() as session:
        from sqlalchemy import select
        from app.models.device import Device

        devices = list((await session.execute(select(Device))).scalars().all())
        assert len(devices) == 1
        assert devices[0].name == "Happ iPhone"
        assert devices[0].source == "raw_subscription"


@pytest.mark.asyncio
async def test_raw_subscription_access_detects_android_model(client, session_factory, monkeypatch):
    await create_user_subscription(session_factory, token="ARVX-SAMSUNG-DEVICE")

    async def fake_proxy(session, subscription):
        return Response(content="raw-subscription", media_type="text/plain")

    monkeypatch.setattr("app.routers.public_subscription.proxy_subscription", fake_proxy)

    response = await client.get(
        "/u/ARVX-SAMSUNG-DEVICE?format=raw",
        headers={"User-Agent": "V2RayTun/1.0 (Linux; Android 14; SM-S921B Build/UP1A.231005.007)"},
    )

    assert response.status_code == 200
    async with session_factory() as session:
        from sqlalchemy import select
        from app.models.device import Device

        devices = list((await session.execute(select(Device))).scalars().all())
        assert len(devices) == 1
        assert devices[0].name == "samsung SM-S921B"
        assert devices[0].type == "phone"


@pytest.mark.asyncio
async def test_raw_subscription_access_deduplicates_generic_client(client, session_factory, monkeypatch):
    await create_user_subscription(session_factory, token="ARVX-GENERIC-DEVICE")

    async def fake_proxy(session, subscription):
        return Response(content="raw-subscription", media_type="text/plain")

    monkeypatch.setattr("app.routers.public_subscription.proxy_subscription", fake_proxy)

    first = await client.get(
        "/u/ARVX-GENERIC-DEVICE?format=raw",
        headers={"User-Agent": "v2raytun/android", "X-Forwarded-For": "203.0.113.10"},
    )
    second = await client.get(
        "/u/ARVX-GENERIC-DEVICE?format=raw",
        headers={"User-Agent": "v2raytun/android", "X-Forwarded-For": "203.0.113.11"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    async with session_factory() as session:
        from sqlalchemy import select
        from app.models.device import Device

        devices = list((await session.execute(select(Device))).scalars().all())
        assert len(devices) == 1
        assert devices[0].name == "V2RayTun Android"


@pytest.mark.asyncio
async def test_raw_subscription_access_reuses_legacy_ip_fingerprinted_device(client, session_factory, monkeypatch):
    _, subscription = await create_user_subscription(session_factory, token="ARVX-LEGACY-DEVICE")
    async with session_factory() as session:
        from app.models.device import Device

        session.add_all(
            [
                Device(
                    subscription_id=subscription.id,
                    name="V2RayTun",
                    type="phone",
                    source="raw_subscription",
                    fingerprint_hash="legacy-ip-fingerprint-1",
                    user_agent="v2raytun/android",
                ),
                Device(
                    subscription_id=subscription.id,
                    name="V2RayTun",
                    type="phone",
                    source="raw_subscription",
                    fingerprint_hash="legacy-ip-fingerprint-2",
                    user_agent="v2raytun/android",
                ),
            ]
        )
        await session.commit()

    async def fake_proxy(session, subscription):
        return Response(content="raw-subscription", media_type="text/plain")

    monkeypatch.setattr("app.routers.public_subscription.proxy_subscription", fake_proxy)

    response = await client.get("/u/ARVX-LEGACY-DEVICE?format=raw", headers={"User-Agent": "v2raytun/android"})

    assert response.status_code == 200
    async with session_factory() as session:
        from sqlalchemy import select
        from app.models.device import Device

        devices = list((await session.execute(select(Device))).scalars().all())
        active_devices = [device for device in devices if device.is_active]
        assert len(devices) == 2
        assert len(active_devices) == 1
        assert active_devices[0].name == "V2RayTun Android"


@pytest.mark.asyncio
async def test_telegram_link_flow(client):
    jwt = await register(client)
    created = await client.post("/api/cabinet/telegram/link-token", headers={"Authorization": f"Bearer {jwt}"})
    token = created.json()["telegram_link_url"].split("start=", 1)[1]

    consumed = await client.post(
        "/api/telegram/link-token/consume",
        headers={"X-Bot-Token": "change_me_bot_token"},
        json={"token": token, "telegram_id": 123456, "username": "family"},
    )
    status = await client.get("/api/cabinet/telegram/status", headers={"Authorization": f"Bearer {jwt}"})

    assert consumed.status_code == 200
    assert status.json()["connected"] is True
    assert status.json()["telegram_ids"] == [123456]

    unlinked = await client.delete("/api/cabinet/telegram/link", headers={"Authorization": f"Bearer {jwt}"})
    status_after = await client.get("/api/cabinet/telegram/status", headers={"Authorization": f"Bearer {jwt}"})

    assert unlinked.status_code == 200
    assert unlinked.json()["removed"] == 1
    assert status_after.json()["connected"] is False


@pytest.mark.asyncio
async def test_cabinet_devices_crud(client, session_factory):
    user, subscription = await create_user_subscription(session_factory, token="ARVX-DEVICE-TEST")
    token = await client.get("/api/auth/me", headers={"Authorization": "Bearer invalid"})
    assert token.status_code == 401
    jwt_response = await client.post(
        "/api/auth/register",
        json={"email": "devices@example.com", "password": "strongpass123", "display_name": "Devices"},
    )
    jwt = jwt_response.json()["access_token"]
    async with session_factory() as session:
        subscription.user_id = user.id
    # Use an access key-free token by moving subscription to registered user.
    async with session_factory() as session:
        from sqlalchemy import select
        from app.models.user import User
        from app.models.vpn_subscription import VpnSubscription

        registered = (await session.execute(select(User).where(User.email == "devices@example.com"))).scalar_one()
        sub = (await session.execute(select(VpnSubscription).where(VpnSubscription.public_token == "ARVX-DEVICE-TEST"))).scalar_one()
        sub.user_id = registered.id
        await session.commit()

    created = await client.post(
        f"/api/cabinet/subscription/{subscription.public_token}/devices",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"name": "iPhone", "type": "phone"},
    )
    listed = await client.get(f"/api/cabinet/subscription/{subscription.public_token}/devices", headers={"Authorization": f"Bearer {jwt}"})
    device_id = listed.json()["devices"][0]["id"]
    deleted = await client.delete(f"/api/cabinet/subscription/{subscription.public_token}/devices/{device_id}", headers={"Authorization": f"Bearer {jwt}"})

    assert created.status_code == 201
    assert listed.status_code == 200
    assert listed.json()["devices"][0]["name"] == "iPhone"
    assert deleted.status_code == 200
