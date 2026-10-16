# Installed Anki API audit

Pinned package: `anki==26.9.3`, Python 3.11. Verified against installed wheel source.

- `Collection(path)` opens a robot collection; `close()` releases it.
- `sync_login(username, password, endpoint)` returns `anki.sync_pb2.SyncAuth`.
- `sync_status(auth).required`: `SyncStatusResponse.FULL_SYNC` means stop.
- `sync_collection(auth, sync_media=False)` returns `SyncCollectionResponse`;
  only NO_CHANGES and NORMAL_SYNC are accepted by the incremental wrapper.
- No standalone `full_download()` exists. The package exposes the bidirectional
  `full_upload_or_download(auth=..., server_usn=..., upload=...)`. Wortling has
  exactly one call site, in empty-collection bootstrap, with literal `upload=False`.
- Bootstrap calls `close_for_full_sync()`, then the download, then
  `reopen(after_full_sync=True)` to restore the Python DB proxy.
- `collection.db.all(sql, *args)` is used for read-only card metadata.
- `get_note(nid)`, `note.items()`, `note.note_type()`, `decks.all_names_and_ids()`
  supply deck/field mappings for the known index.
- `sched.reposition_new_cards(card_ids, starting_from, step_size, randomize,
  shift_existing)` is the scheduling API to audit again before M4 implementation.

Import Collection before importing scheduler implementation modules: the installed
package has circular imports when scheduler.v3 is imported first.

Real accounts remain untouched. The default sync endpoint is localhost:8082.
