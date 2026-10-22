# Plugin-to-companion protocol

`POST /_navdio/events` is private. Never expose it through a public proxy.
Send JSON with `Authorization: Bearer <event token>`.
The body limit is 64 KiB. Accepted events return 204. Rejected events leave playback state unchanged.

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
    "path": "Artist/Album/track.flac"
  },
  "coverArt": null
}
```

`timestamp` is Navidrome's Unix report time in seconds. `positionMs` is the position at that time.
States are `starting`, `playing`, `paused`, `stopped`, and `expired`.
The companion uses monotonic time for deadlines and position estimates.

Optional `coverArt` contains base64-encoded JPEG, PNG, or WebP data, limited to 32 KiB after decoding.
The plugin requests size 50. The companion validates the image and converts it to a 50×50 JPEG.
Covers must contain one frame, at most 2048 pixels per axis and 1,048,576 pixels total.
Invalid event artwork rejects the event. Missing plugin artwork does not prevent audio playback.

## Session selection and liveness

The plugin filters reports by its configured user and client.
The companion prefers playing sessions, then the newest report, then stable player identity.
Older reports cannot replace a session's newer state.
Paths must resolve inside a mapped library root. Only normal-speed playback is broadcast.

The plugin sends heartbeats every five seconds.
Any accepted event refreshes source liveness.
After 15 seconds without an event, playback becomes idle and the cover disappears.

Session expiry is separate from source liveness:

- Playing sessions expire after their expected remaining duration plus five seconds.
- Paused sessions expire after 30 minutes.
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
  "durationMs": 180500
}
```

Public responses exclude native paths, user and player identifiers, credentials, and internal artwork URLs.
Info and cover use `Cache-Control: no-store`.
Idle info clears track fields. Missing artwork returns 404.
Fetch artwork separately from `GET /radio/cover`. Info does not include a cover URL.
