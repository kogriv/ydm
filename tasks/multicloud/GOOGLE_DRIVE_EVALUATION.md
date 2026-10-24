# Google Drive delta and Shared Drives evaluation

*Date:* 2026-10-23
*Status:* design decision; no credentials or live identifiers

## Change feed

Google Drive exposes a durable change feed through
`changes.getStartPageToken` and paginated `changes.list`. The consumer must
store `nextPageToken` until a page returns `newStartPageToken`, retain removed
objects, and scope every token to the same account or Shared Drive.

Decision for YDM v1: do not emulate this through rclone listings. Keep the
current complete/partial inventory scans authoritative. A future native Drive
inventory adapter may add a target-scoped token under `state_dir`, but only
after it implements:

- transactional page application and token commit;
- removed-object tombstones and parent changes;
- token invalidation fallback to a full scan;
- target/provider/Drive-ID binding;
- the same incomplete-scan rule as full inventory.

Until then `revisions`/delta remains an explicit unsupported capability. This
avoids treating a missed page or an expired token as deletions.

## Shared Drives

Shared Drives are separate security and change-feed domains. Drive API calls
need `supportsAllDrives=true`; listing additionally needs
`includeItemsFromAllDrives=true`, and a drive-scoped query/change feed uses a
`driveId`. Permissions and ownership semantics differ from My Drive.

Decision for YDM v1: one Shared Drive is one YDM target and one rclone remote
configured with its Shared Drive ID. It gets its own local root, DB, policy,
filters, locks and scheduler instance. YDM must not aggregate `allDrives` into
one writable target. The existing target isolation contract is sufficient for
inventory and ordinary-file sync once that remote is configured; permission
management is outside scope and stays read-only/unsupported.

Shortcuts remain metadata objects and never expand the writable sync scope.
Cross-drive server-side moves are not assumed even when ordinary moves within
one configured Drive target are supported.

## Sources used for the decision

- Google Drive API: retrieve changes —
  <https://developers.google.com/workspace/drive/api/guides/manage-changes>
- Google Drive API: Shared Drive support —
  <https://developers.google.com/workspace/drive/api/guides/enable-shareddrives>
- rclone Google Drive backend — <https://rclone.org/drive/>
