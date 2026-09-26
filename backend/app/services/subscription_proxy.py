import base64
import json

import httpx
from fastapi import HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.vpn_subscription import VpnSubscription
from app.services.audit_service import write_audit_log
from app.utils.time import utc_now


NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

# v2rayTun reads this response header when it refreshes a subscription.  The
# routing object uses the standard local `direct` outbound provided by the
# client, so non-v2rayTun apps can safely ignore it.
V2RAYTUN_ROUTING = {
    "domainStrategy": "IPIfNonMatch",
    "rules": [
        {
            "type": "field",
            "domain": [
                "domain:localhost",
                "domain:local",
                "domain:internal",
                "domain:ru",
                "domain:su",
                "domain:moscow",
                "domain:xn--p1ai",
                "domain:school.mos.ru",
            ],
            "outboundTag": "direct",
        },
        {
            "type": "field",
            "ip": [
                "0.0.0.0/8",
                "127.0.0.0/8",
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "169.254.0.0/16",
                "224.0.0.0/4",
                "255.255.255.255",
            ],
            "outboundTag": "direct",
        },
    ],
}


def build_v2raytun_routing_header() -> str:
    routing_json = json.dumps(V2RAYTUN_ROUTING, separators=(",", ":"), ensure_ascii=True)
    return base64.b64encode(routing_json.encode("utf-8")).decode("ascii")


def build_subscription_headers(subscription: VpnSubscription) -> dict[str, str]:
    headers = dict(NO_CACHE_HEADERS)
    upload_bytes = 0
    download_bytes = 0
    total_bytes = int(subscription.traffic_limit_gb * 1024 * 1024 * 1024) if subscription.traffic_limit_gb else 0
    expire_at = int(subscription.expires_at.timestamp()) if subscription.expires_at else 0
    title = base64.b64encode("Arvexo Connect".encode("utf-8")).decode("ascii")

    headers["Subscription-Userinfo"] = (
        f"upload={upload_bytes}; download={download_bytes}; total={total_bytes}; expire={expire_at}"
    )
    headers["Profile-Update-Interval"] = "1"
    headers["Profile-Title"] = f"base64:{title}"
    headers["Support-Url"] = settings.public_frontend_base_url.rstrip("/")
    headers["Profile-Web-Page-Url"] = settings.public_frontend_base_url.rstrip("/")
    headers["routing"] = build_v2raytun_routing_header()
    return headers


async def proxy_subscription(session: AsyncSession, subscription: VpnSubscription) -> Response:
    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout,
            verify=settings.upstream_ssl_verify,
            follow_redirects=True,
        ) as client:
            upstream = await client.get(subscription.original_sub_url)
            upstream.raise_for_status()
    except httpx.HTTPError as exc:
        await write_audit_log(
            session,
            "subscription_upstream_failed",
            user_id=subscription.user_id,
            metadata={"token_prefix": subscription.public_token[:9], "error": exc.__class__.__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to fetch upstream subscription",
        ) from exc

    subscription.last_access_at = utc_now()
    await write_audit_log(
        session,
        "subscription_accessed",
        user_id=subscription.user_id,
        metadata={"token_prefix": subscription.public_token[:9]},
    )
    content_type = upstream.headers.get("content-type", "text/plain; charset=utf-8")
    return Response(content=upstream.content, media_type=content_type, headers=build_subscription_headers(subscription))
