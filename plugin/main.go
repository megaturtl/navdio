package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"math"
	"mime"
	"net/url"
	"strconv"
	"strings"

	"github.com/navidrome/navidrome/plugins/pdk/go/host"
	"github.com/navidrome/navidrome/plugins/pdk/go/lifecycle"
	"github.com/navidrome/navidrome/plugins/pdk/go/pdk"
	"github.com/navidrome/navidrome/plugins/pdk/go/scheduler"
	"github.com/navidrome/navidrome/plugins/pdk/go/scrobbler"
	"github.com/navidrome/navidrome/plugins/pdk/go/types"
)

const (
	heartbeatScheduleID = "navdio-heartbeat"
	maxCoverBytes       = 32 * 1024
	maxEventBytes       = 64 * 1024
)

type source struct{}

type configuration struct {
	username string
	player   string
	endpoint string
	token    string
}

type eventTrack struct {
	ID        string            `json:"id"`
	Title     string            `json:"title"`
	Album     string            `json:"album"`
	Artist    string            `json:"artist"`
	Artists   []types.ArtistRef `json:"artists"`
	Duration  float32           `json:"duration"`
	LibraryID int32             `json:"libraryId"`
	Path      string            `json:"path"`
}

type playbackEvent struct {
	Version      int        `json:"version"`
	Type         string     `json:"type"`
	Username     string     `json:"username"`
	PlayerID     string     `json:"playerId"`
	PlayerName   string     `json:"playerName"`
	State        string     `json:"state"`
	PositionMs   int64      `json:"positionMs"`
	PlaybackRate float64    `json:"playbackRate"`
	Timestamp    int64      `json:"timestamp"`
	Track        eventTrack `json:"track"`
	CoverArt     []byte     `json:"coverArt"`
}

func init() {
	plugin := source{}
	scrobbler.Register(plugin)
	lifecycle.Register(plugin)
	scheduler.Register(plugin)
}

func main() {}

func readConfiguration() (configuration, error) {
	username, _ := pdk.GetConfig("username")
	player, _ := pdk.GetConfig("player")
	companionURL, _ := pdk.GetConfig("companionUrl")
	token, _ := pdk.GetConfig("token")
	if strings.TrimSpace(username) == "" {
		return configuration{}, errors.New("navdio: username is required")
	}
	if token == "" {
		return configuration{}, errors.New("navdio: token is required")
	}
	for _, c := range token {
		if c <= ' ' || c >= 127 {
			return configuration{}, errors.New("navdio: token must contain only visible ASCII characters")
		}
	}
	endpoint, err := eventEndpoint(companionURL)
	if err != nil {
		return configuration{}, err
	}
	return configuration{username: username, player: player, endpoint: endpoint, token: token}, nil
}

func eventEndpoint(value string) (string, error) {
	if value == "" {
		value = "http://127.0.0.1:8750"
	}
	u, err := url.Parse(value)
	if err != nil || u == nil {
		return "", errors.New("navdio: invalid companionUrl")
	}
	if (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.Opaque != "" ||
		u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.RawFragment != "" ||
		(u.Path != "" && u.Path != "/") || u.RawPath != "" ||
		(u.Hostname() != "127.0.0.1" && u.Hostname() != "::1") {
		return "", errors.New("navdio: companionUrl must be a loopback HTTP origin (127.0.0.1 or [::1])")
	}
	if port := u.Port(); port != "" {
		n, err := strconv.Atoi(port)
		if err != nil || n < 1 || n > 65535 {
			return "", errors.New("navdio: invalid companionUrl port")
		}
	} else if strings.HasSuffix(u.Host, ":") {
		return "", errors.New("navdio: invalid companionUrl port")
	}
	u.Path = "/_navdio/events"
	return u.String(), nil
}

func (source) IsAuthorized(req scrobbler.IsAuthorizedRequest) (bool, error) {
	cfg, err := readConfiguration()
	if err != nil {
		return false, err
	}
	return req.Username == cfg.username, nil
}

// These notifications lack authoritative client and playback state. Only reports
// control the broadcast. These callbacks must not write listening history.
func (source) NowPlaying(scrobbler.NowPlayingRequest) error { return nil }
func (source) Scrobble(scrobbler.ScrobbleRequest) error     { return nil }

func (source) OnInit() error {
	cfg, err := readConfiguration()
	if err != nil {
		return err
	}
	if err := ensureHeartbeat(); err != nil {
		return err
	}
	if err := sendHeartbeat(cfg); err != nil {
		// The companion can start after Navidrome. The registered schedule retries.
		pdk.Log(pdk.LogWarn, "navdio: companion unavailable during init")
	}
	return nil
}

func (source) OnCallback(req scheduler.SchedulerCallbackRequest) error {
	if req.ScheduleID != heartbeatScheduleID {
		return nil
	}
	cfg, err := readConfiguration()
	if err != nil {
		return err
	}
	return sendHeartbeat(cfg)
}

func ensureHeartbeat() error {
	_, err := host.SchedulerScheduleRecurring("@every 5s", "", heartbeatScheduleID)
	// Navidrome owns schedules. Reloads can reset WASM instances or skip hooks.
	// Register on each selected report without moving an existing schedule.
	// Do not rely on instance-local registration flags.
	if err != nil && err.Error() != `schedule ID "navdio-heartbeat" already exists` {
		return errors.New("navdio: could not register heartbeat")
	}
	return nil
}

func sendHeartbeat(cfg configuration) error {
	return postEvent(cfg, []byte(`{"version":1,"type":"heartbeat"}`))
}

func (source) PlaybackReport(req scrobbler.PlaybackReportRequest) error {
	cfg, err := readConfiguration()
	if err != nil {
		return err
	}
	if req.Username != cfg.username || (cfg.player != "" && !strings.EqualFold(req.PlayerName, cfg.player)) {
		return nil
	}
	if !validReport(req) {
		return errors.New("navdio: invalid playback report")
	}
	scheduleErr := ensureHeartbeat()
	track := req.Track
	event := playbackEvent{
		Version: 1, Type: "playback", Username: req.Username,
		PlayerID: req.PlayerId, PlayerName: req.PlayerName, State: req.State,
		PositionMs: req.PositionMs, PlaybackRate: req.PlaybackRate, Timestamp: req.Timestamp,
		Track: eventTrack{
			ID: track.ID, Title: track.Title, Album: track.Album, Artist: track.Artist,
			Artists: track.Artists, Duration: track.Duration, LibraryID: track.LibraryID, Path: track.Path,
		},
	}
	if event.State != "stopped" && event.State != "expired" && (track.LibraryID <= 0 || track.Path == "") {
		// Navidrome withholds paths outside granted libraries. Stop the session
		// so its previous, permitted track does not continue.
		event.State = "stopped"
		event.Track = eventTrack{}
	}
	if event.State != "stopped" && event.State != "expired" {
		event.CoverArt = coverArt(req.Username, track.ID)
	}
	body, err := json.Marshal(event)
	if err != nil {
		return errors.New("navdio: could not encode playback report")
	}
	if len(body) > maxEventBytes && event.CoverArt != nil {
		event.CoverArt = nil
		body, err = json.Marshal(event)
		if err != nil {
			return errors.New("navdio: could not encode playback report")
		}
	}
	if err := postEvent(cfg, body); err != nil {
		return err
	}
	return scheduleErr
}

func validReport(req scrobbler.PlaybackReportRequest) bool {
	switch req.State {
	case "starting", "playing":
		if req.PlaybackRate <= 0 {
			return false
		}
	case "paused", "stopped", "expired":
	default:
		return false
	}
	if req.PlayerId == "" || req.PositionMs < 0 || req.Timestamp < 0 ||
		math.IsNaN(req.PlaybackRate) || math.IsInf(req.PlaybackRate, 0) || req.PlaybackRate < 0 {
		return false
	}
	if req.State == "stopped" || req.State == "expired" {
		return true
	}
	return req.Track.ID != "" && !math.IsNaN(float64(req.Track.Duration)) &&
		!math.IsInf(float64(req.Track.Duration), 0) && req.Track.Duration > 0
}

func coverCacheKey(username, trackID string) string {
	sum := sha256.Sum256([]byte(username + "\x00" + trackID))
	return "cover-v1:" + hex.EncodeToString(sum[:])
}

func coverArt(username, trackID string) []byte {
	key := coverCacheKey(username, trackID)
	if data, exists, err := host.CacheGetBytes(key); err == nil && exists {
		if len(data) == 0 || len(data) > maxCoverBytes {
			return nil
		}
		return data
	}
	query := url.Values{"id": {trackID}, "size": {"50"}, "u": {username}}
	contentType, data, err := host.SubsonicAPICallRaw("getCoverArt?" + query.Encode())
	mediaType, _, mimeErr := mime.ParseMediaType(contentType)
	if err != nil || mimeErr != nil || !strings.HasPrefix(mediaType, "image/") || !validCover(data) {
		// Cache failures to limit repeated requests for missing or oversized artwork.
		_ = host.CacheSetBytes(key, nil, 30)
		return nil
	}
	_ = host.CacheSetBytes(key, data, 3600)
	return data
}

func validCover(data []byte) bool {
	if len(data) == 0 || len(data) > maxCoverBytes {
		return false
	}
	cfg, _, err := image.DecodeConfig(bytes.NewReader(data))
	if err != nil || cfg.Width < 1 || cfg.Height < 1 || cfg.Width > 512 || cfg.Height > 512 {
		return false
	}
	_, _, err = image.Decode(bytes.NewReader(data))
	return err == nil
}

func postEvent(cfg configuration, body []byte) error {
	if len(body) > maxEventBytes {
		return errors.New("navdio: playback report exceeds event size limit")
	}
	response, err := host.HTTPSend(host.HTTPRequest{
		Method: "POST", URL: cfg.endpoint,
		Headers: map[string]string{"Authorization": "Bearer " + cfg.token, "Content-Type": "application/json"},
		Body:    body, TimeoutMs: 1000, NoFollowRedirects: true,
	})
	if err != nil || response == nil || response.StatusCode != 204 {
		// Host errors and response bodies can contain request headers or secrets.
		return errors.New("navdio: companion did not accept event")
	}
	return nil
}
