# Plugin-to-companion protocol

`POST /_navdio/events` is private. Never expose it through a public proxy.
Send JSON with `Authorization: Bearer <event token>`.
The body limit is 64 KiB. Accepted events return 204. Rejected events leave playback state unchanged.
The module reserves the listener through systemd. Navidrome requires that socket to remain active.

## Events

Protocol version 1 accepts heartbeat and playback events:

```json
{"version": 1, "type": "heartbeat"}
```

```json
{
  "version": 1,
  "type": "playback",
  "username": "alice",
  "playerId": "feishin-session",
  "playerName": "Feishin",
  "state": "playing",
  "positionMs": 12500,
  "playbackRate": 1,
  "timestamp": 1700000000,
  "track": {
    "id": "navidrome-track-id",
    "title": "Track",
    "artist": "First • Second",
    "artists": [{"name": "First"}, {"name": "Second"}],
    "album": "Album",
    "duration": 180.5,
    "libraryId": 1,
    "bpm": 124,
    "genres": ["House"],
    "playCount": 42,
    "path": "Artist/Album/track.flac"
  },
  "coverArt": null
}
```

`timestamp` is Navidrome's Unix report time in seconds. `positionMs` is the position at that time.
States are `starting`, `playing`, `paused`, `stopped`, and `expired`.
The companion uses monotonic time for deadlines and position estimates.

Track metadata fields are optional. `bpm` is a positive 32-bit integer or null.
`genres` contains at most 32 nonempty strings, each limited to 256 UTF-8 bytes.
`playCount` is a nonnegative 64-bit integer or null.
The plugin reads these fields through a user-scoped, exact-ID matcher lookup on each eligible playback report.
A failed lookup does not prevent playback.

Optional `coverArt` contains base64-encoded JPEG, PNG, or WebP data, limited to 32 KiB after decoding.
The plugin requests size 50. The companion validates the image and converts it to a 50×50 JPEG.
Covers must contain one frame, at most 2048 pixels per axis and 1,048,576 pixels total.
Invalid event artwork rejects the event. Missing plugin artwork does not prevent audio playback.

## Session selection and liveness

The plugin filters reports by its configured user and client.
The companion prefers playing sessions, then the newest report, then stable player identity.
Older reports cannot replace a session's newer state.
Paths must resolve inside a mapped library root.
The companion validates an open file handle and keeps it until MPD releases the track.
Only normal-speed playback is broadcast.

The plugin sends heartbeats every five seconds.
Any accepted event refreshes source liveness.
After 15 seconds without an event, playback becomes idle. The last played metadata and cover remain available.

Session expiry is separate from source liveness:

- Playing sessions expire after their expected remaining duration plus five seconds.
- Paused sessions expire after 60 seconds. Repeated pause reports for the same track do not extend this deadline.
- Stopped and expired sessions are not eligible for playback.

Heartbeats do not extend session expiry.

## Playback synchronisation

The companion repositions MPD only at startup, track changes, resume, and natural repeats.
Progress reports and mid-song client seeks do not change the radio's clock.
Public position reports radio playback, not the latest client position.

## Public metadata

```json
{
  "state": "playing",
  "title": "Track",
  "artists": ["First", "Second"],
  "album": "Album",
  "positionMs": 12500,
  "durationMs": 180500,
  "bpm": 124,
  "genres": ["House"],
  "playCount": 42,
  "coverVersion": "sha256-of-the-final-jpeg",
  "lastPlayedAt": 1700000000
}
```

Public responses exclude native paths, user and player identifiers, credentials, and internal artwork URLs.
Info uses `Cache-Control: no-store`.
When idle, info retains the last successfully published playing track and sets `positionMs` to zero.
`lastPlayedAt` is the latest playing report's Unix timestamp in seconds, not a completed-play or scrobble time.
Pauses, heartbeats, and metadata requests do not advance it. A new playing report updates it, including repeats.
Without a track, track fields and `lastPlayedAt` are empty or null. A paused-only track has no last played time.
The cache holds one track and its cover in memory. A companion restart clears it.
Cached metadata never resumes audio. Use `state` to distinguish live playback from the last played track.
Missing BPM and play count use null. Missing genres use an empty array.
`playCount` is the broadcast user's Navidrome count, not the number of radio listens.
Zero is a known count. The count refreshes when the plugin receives another playback report.
Navdio does not calculate BPM or submit plays.

`coverVersion` identifies the final 50×50 JPEG by its SHA-256 hash. It is null without artwork.
Fetch artwork separately from `GET /radio/cover?v=<coverVersion>`. Info does not include a cover URL.
The companion returns 404 if the requested version does not match the displayed track, including during a track change.
When idle, the displayed cover belongs to the last played track. Unversioned cover requests remain supported.
Other query parameters do not affect routing.

Successful cover responses use `Cache-Control: no-cache` and a quoted content-hash `ETag`.
A matching `If-None-Match` returns 304 without a body.
Missing artwork and version mismatches return 404 with `Cache-Control: no-store`.
The version check runs before conditional validation, so an old validator cannot hide a version mismatch.
