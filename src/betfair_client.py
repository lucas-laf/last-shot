"""Shared Betfair login + app-key capability detection."""
from __future__ import annotations

import logging
import os
from typing import Any

import betfairlightweight
import requests
from betfairlightweight import APIClient

logger = logging.getLogger(__name__)


def make_client(cfg: dict[str, Any]) -> APIClient:
    """Login order: existing session token, cert login, interactive login."""
    bf = cfg["betfair"]
    if not bf["app_key"]:
        raise RuntimeError("BETFAIR_APP_KEY missing from .env")

    have_certs = bool(
        bf["cert_file"] and bf["key_file"]
        and os.path.isfile(bf["cert_file"]) and os.path.isfile(bf["key_file"])
    )
    client = APIClient(
        username=bf["username"] or "session",
        password=bf["password"] or "session",
        app_key=bf["app_key"],
        cert_files=(bf["cert_file"], bf["key_file"]) if have_certs else None,
        lightweight=False,
    )

    if bf["session_token"]:
        client.set_session_token(bf["session_token"])
        try:
            client.keep_alive()
            logger.info("Betfair session token valid")
            return client
        except betfairlightweight.exceptions.BetfairError:
            logger.warning("Session token rejected; trying credential login")

    if not (bf["username"] and bf["password"]):
        raise RuntimeError("No valid BETFAIR_SESSION_TOKEN and no username/password")

    if have_certs:
        client.login()
        logger.info("Betfair cert login ok")
    else:
        # No client certs available: use the interactive login endpoint.
        client.login_interactive()
        logger.info("Betfair interactive login ok")
    return client


def app_key_is_delayed(client: APIClient) -> bool | None:
    """True if the key in use serves delayed data, None if undeterminable.

    Delayed vs live decides Stream API vs polling and is recorded on every
    tick, since it changes which latency conclusions are valid.
    betfairlightweight doesn't wrap getDeveloperAppKeys, so call it raw.
    """
    try:
        r = requests.post(
            "https://api.betfair.com/exchange/account/json-rpc/v1",
            json={
                "jsonrpc": "2.0",
                "method": "AccountAPING/v1.0/getDeveloperAppKeys",
                "params": {},
                "id": 1,
            },
            headers={
                "X-Application": client.app_key,
                "X-Authentication": client.session_token,
            },
            timeout=15,
        )
        r.raise_for_status()
        result = r.json().get("result", [])
    except Exception as e:
        logger.warning("Could not fetch developer app keys: %s", e)
        return None
    for app in result:
        for version in app.get("appVersions", []):
            if version.get("applicationKey") == client.app_key:
                return bool(version.get("delayData"))
    return None
