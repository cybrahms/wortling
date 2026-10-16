from __future__ import annotations

import os
from urllib.parse import urlparse

from anki.collection import Collection
from anki.sync_pb2 import SyncAuth, SyncCollectionResponse, SyncStatusResponse


class FullSyncRequired(RuntimeError):
    pass


def check_endpoint(endpoint: str | None, real_access_approved: bool) -> None:
    local = endpoint and urlparse(endpoint).hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
        "syncserver",
    }
    if not local and not real_access_approved:
        raise RuntimeError("Real AnkiWeb access requires explicit user confirmation")
    if endpoint and urlparse(endpoint).scheme not in {"http", "https"}:
        raise ValueError("Anki sync endpoint must use HTTP or HTTPS")


def login(collection: Collection, user, endpoint: str | None) -> SyncAuth:
    check_endpoint(endpoint, user.real_ankiweb_approved)
    username = os.environ.get(user.ankiweb_user_env)
    password = os.environ.get(user.ankiweb_pass_env)
    if not username or not password:
        raise RuntimeError(f"Missing Anki credentials for {user.id}")
    return collection.sync_login(username, password, endpoint)


def incremental_sync(collection: Collection, auth: SyncAuth) -> None:
    if collection.sync_status(auth).required == SyncStatusResponse.FULL_SYNC:
        raise FullSyncRequired("Full sync required; no automatic resolution is allowed")
    result = collection.sync_collection(auth, sync_media=False)
    if result.required not in {
        SyncCollectionResponse.NO_CHANGES,
        SyncCollectionResponse.NORMAL_SYNC,
    }:
        raise FullSyncRequired("Full sync required; no automatic resolution is allowed")
    if result.HasField("new_endpoint"):
        auth.endpoint = result.new_endpoint


def bootstrap_download(collection: Collection, auth: SyncAuth) -> None:
    if collection.db.scalar("SELECT COUNT(*) FROM notes"):
        raise RuntimeError("Bootstrap requires an empty collection")
    # AnkiWeb assigns a shard in the handshake. Downloading at the generic
    # login endpoint can fail even though the same call works on a local server.
    result = collection.sync_collection(auth, sync_media=False)
    if result.HasField("new_endpoint"):
        auth.endpoint = result.new_endpoint
    collection.close_for_full_sync()
    try:
        # Anki exposes one two-direction method. The direction is an invariant,
        # never a configurable argument. There is no full-upload wrapper.
        collection.full_upload_or_download(auth=auth, server_usn=None, upload=False)
    finally:
        collection.reopen(after_full_sync=True)
