# msal_device_auth.py
import os, msal
from office365.sharepoint.client_context import ClientContext

CACHE_PATH = os.getenv("MSAL_CACHE_PATH", os.path.join(os.getcwd(), ".msal_cache.bin"))

def _load_cache():
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE_PATH):
        try:
            cache.deserialize(open(CACHE_PATH, "r", encoding="utf-8").read())
        except Exception:
            pass
    return cache

def _save_cache(cache: msal.SerializableTokenCache):
    if cache.has_state_changed:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(cache.serialize())

def get_sharepoint_ctx_device(site_url: str, tenant_id: str, client_id: str) -> ClientContext:
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    scope = [f"{site_url.split('/sites/')[0]}/.default"]  # e.g. https://cnxsi.sharepoint.com/.default

    cache = _load_cache()
    app = msal.PublicClientApplication(client_id=client_id, authority=authority, token_cache=cache)

    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(scopes=scope, account=accounts[0])

    if not result:
        flow = app.initiate_device_flow(scopes=scope)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow init failed: {flow}")
        print(flow["message"])  # follow link & enter code
        result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        raise RuntimeError(result.get("error_description") or "No access_token in result")

    _save_cache(cache)
    return ClientContext(site_url).with_access_token(result["access_token"])
