//go:build !wasip1

package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"image"
	"image/png"
	"math"
	"net/url"
	"strconv"
	"strings"
	"testing"

	"github.com/navidrome/navidrome/plugins/pdk/go/host"
	"github.com/navidrome/navidrome/plugins/pdk/go/pdk"
	"github.com/navidrome/navidrome/plugins/pdk/go/scrobbler"
	"github.com/navidrome/navidrome/plugins/pdk/go/types"
	"github.com/stretchr/testify/mock"
)

func resetHostMocks(t *testing.T) {
	t.Helper()
	pdk.ResetMock()
	host.CacheMock.Mock = mock.Mock{}
	host.HTTPMock.Mock = mock.Mock{}
	host.SchedulerMock.Mock = mock.Mock{}
	host.SubsonicAPIMock.Mock = mock.Mock{}
	t.Cleanup(func() {
		pdk.PDKMock.AssertExpectations(t)
		host.CacheMock.AssertExpectations(t)
		host.HTTPMock.AssertExpectations(t)
		host.SchedulerMock.AssertExpectations(t)
		host.SubsonicAPIMock.AssertExpectations(t)
	})
}

func configure(t *testing.T, username, player string) {
	t.Helper()
	pdk.PDKMock.On("GetConfig", "username").Return(username, true)
	pdk.PDKMock.On("GetConfig", "player").Return(player, true)
	pdk.PDKMock.On("GetConfig", "companionUrl").Return("http://127.0.0.1:8750", true)
	pdk.PDKMock.On("GetConfig", "token").Return("test-event-token", true)
}

func exampleReport() scrobbler.PlaybackReportRequest {
	return scrobbler.PlaybackReportRequest{
		Username: "alice", PlayerId: "player-1", PlayerName: "Feishin",
		State: "playing", PositionMs: 1234, PlaybackRate: 1.25, Timestamp: 1700000000,
		Track: scrobbler.TrackInfo{
			ID: "track-1", Title: "Song", Album: "Album", Artist: "First • Second",
			Artists:  []types.ArtistRef{{Name: "First"}, {Name: "Second"}},
			Duration: 180.5, LibraryID: 1, Path: "Artist/Album/song.flac",
		},
	}
}

func testCover(t *testing.T) []byte {
	t.Helper()
	var out bytes.Buffer
	if err := png.Encode(&out, image.NewRGBA(image.Rect(0, 0, 50, 50))); err != nil {
		t.Fatal(err)
	}
	return out.Bytes()
}

func acceptPlayback(t *testing.T, inspect func(playbackEvent)) {
	t.Helper()
	host.HTTPMock.On("Send", mock.Anything).Run(func(args mock.Arguments) {
		req := args.Get(0).(host.HTTPRequest)
		if req.Method != "POST" || req.URL != "http://127.0.0.1:8750/_navdio/events" ||
			req.TimeoutMs != 1000 || !req.NoFollowRedirects || len(req.Body) > maxEventBytes ||
			req.Headers["Authorization"] != "Bearer test-event-token" || req.Headers["Content-Type"] != "application/json" {
			t.Error("event request does not meet the authenticated bounded loopback contract")
		}
		var event playbackEvent
		if err := json.Unmarshal(req.Body, &event); err != nil {
			t.Fatal(err)
		}
		if event.Version != 1 || event.Type != "playback" {
			t.Error("incorrect protocol version or event type")
		}
		inspect(event)
	}).Return(&host.HTTPResponse{StatusCode: 204}, nil).Once()
}

func TestEventEndpointRestrictsOrigins(t *testing.T) {
	for _, value := range []string{"", "http://127.0.0.1:8750", "http://127.0.0.1:8750/", "http://[::1]:8750", "https://127.0.0.1"} {
		t.Run("accept_"+value, func(t *testing.T) {
			endpoint, err := eventEndpoint(value)
			if err != nil || !strings.HasSuffix(endpoint, "/_navdio/events") {
				t.Fatalf("expected a loopback endpoint: %q, %v", endpoint, err)
			}
		})
	}
	for _, value := range []string{
		"http://localhost:8750", "http://127.0.0.2:8750", "http://192.168.1.2", "http://example.com",
		"http://[::ffff:127.0.0.1]", "file:///tmp/navdio", "//127.0.0.1:8750", "http://127.0.0.1:0",
		"http://127.0.0.1:65536", "http://127.0.0.1:", "http://127.0.0.1:8750/path",
		"http://127.0.0.1:8750/?secret=value", "http://127.0.0.1:8750/?", "http://127.0.0.1:8750/#fragment",
		"http://username:private-password@127.0.0.1:8750", "http://127.0.0.1:bad-secret-port", "http://%invalid",
	} {
		t.Run("reject_"+value, func(t *testing.T) {
			_, err := eventEndpoint(value)
			if err == nil {
				t.Fatal("unsafe companion origin was accepted")
			}
			if strings.Contains(err.Error(), value) || strings.Contains(err.Error(), "private-password") || strings.Contains(err.Error(), "bad-secret-port") {
				t.Fatal("configuration error echoed credential-bearing input")
			}
		})
	}
}

func TestFiltersAndLegacyCallbacksHaveNoSideEffects(t *testing.T) {
	resetHostMocks(t)
	configure(t, "alice", "feishin")
	for _, req := range []scrobbler.PlaybackReportRequest{
		{Username: "bob", PlayerName: "Feishin"},
		{Username: "alice", PlayerName: "Other player"},
		{Username: "alice", PlayerName: "Feishin mobile"},
	} {
		if err := (source{}).PlaybackReport(req); err != nil {
			t.Fatal(err)
		}
	}
	if err := (source{}).NowPlaying(scrobbler.NowPlayingRequest{}); err != nil {
		t.Fatal(err)
	}
	if err := (source{}).Scrobble(scrobbler.ScrobbleRequest{}); err != nil {
		t.Fatal(err)
	}
	for username, expected := range map[string]bool{"alice": true, "Alice": false, "bob": false} {
		ok, err := (source{}).IsAuthorized(scrobbler.IsAuthorizedRequest{Username: username})
		if err != nil || ok != expected {
			t.Fatalf("authorization mismatch: %v, %v", ok, err)
		}
	}
	host.HTTPMock.AssertNotCalled(t, "Send", mock.Anything)
	host.SchedulerMock.AssertNotCalled(t, "ScheduleRecurring", mock.Anything, mock.Anything, mock.Anything)
}

func TestCoverFetchIsBoundedValidatedAndUserScoped(t *testing.T) {
	for _, scenario := range []string{"valid", "missing", "oversized", "malformed", "not-image", "gif"} {
		t.Run(scenario, func(t *testing.T) {
			resetHostMocks(t)
			username, trackID := "alice+one", "track?&id"
			key := coverCacheKey(username, trackID)
			if key == coverCacheKey("bob", trackID) || key == coverCacheKey(username, "another-track") {
				t.Fatal("cover cache key is not scoped by both user and track")
			}
			host.CacheMock.On("GetBytes", key).Return(nil, false, nil).Once()
			contentType, data, apiErr := "image/png", testCover(t), error(nil)
			switch scenario {
			case "missing":
				apiErr = errors.New("internal error with private request details")
			case "oversized":
				data = make([]byte, maxCoverBytes+1)
			case "malformed":
				data = []byte("not an image")
			case "not-image":
				contentType = "application/json"
			case "gif":
				contentType, data = "image/gif", []byte("GIF89a")
			}
			query := url.Values{"id": {trackID}, "size": {"50"}, "u": {username}}
			host.SubsonicAPIMock.On("CallRaw", "getCoverArt?"+query.Encode()).Return(contentType, data, apiErr).Once()
			if scenario == "valid" {
				host.CacheMock.On("SetBytes", key, data, int64(3600)).Return(nil).Once()
			} else {
				host.CacheMock.On("SetBytes", key, []byte(nil), int64(30)).Return(nil).Once()
			}
			got := coverArt(username, trackID)
			if (scenario == "valid" && !bytes.Equal(got, data)) || (scenario != "valid" && got != nil) {
				t.Fatal("invalid cover was forwarded or valid cover lost")
			}
		})
	}
}

func TestCoverFailureDoesNotStopPlayback(t *testing.T) {
	resetHostMocks(t)
	configure(t, "alice", "")
	key := coverCacheKey("alice", "track-1")
	host.SchedulerMock.On("ScheduleRecurring", "@every 5s", "", heartbeatScheduleID).Return(heartbeatScheduleID, nil)
	host.CacheMock.On("GetBytes", key).Return(nil, false, errors.New("cache unavailable"))
	host.SubsonicAPIMock.On("CallRaw", mock.Anything).Return("", nil, errors.New("cover unavailable"))
	host.CacheMock.On("SetBytes", key, []byte(nil), int64(30)).Return(errors.New("cache unavailable"))
	acceptPlayback(t, func(event playbackEvent) {
		if event.CoverArt != nil || event.State != "playing" {
			t.Error("cover failure changed playback")
		}
	})
	if err := (source{}).PlaybackReport(exampleReport()); err != nil {
		t.Fatal(err)
	}
}

func TestInitKeepsHeartbeatWhenCompanionIsUnavailable(t *testing.T) {
	resetHostMocks(t)
	configure(t, "alice", "")
	host.SchedulerMock.On("ScheduleRecurring", "@every 5s", "", heartbeatScheduleID).Return(heartbeatScheduleID, nil).Once()
	host.HTTPMock.On("Send", mock.MatchedBy(func(req host.HTTPRequest) bool {
		return string(req.Body) == `{"version":1,"type":"heartbeat"}` && req.TimeoutMs == 1000 && req.NoFollowRedirects
	})).Return(nil, errors.New("private-request-token")).Once()
	pdk.PDKMock.On("Log", pdk.LogWarn, mock.Anything).Once()
	if err := (source{}).OnInit(); err != nil {
		t.Fatalf("temporary companion outage blocked plugin initialization: %v", err)
	}
}

func TestTransportErrorsNeverEchoCredentialsOrFollowRedirects(t *testing.T) {
	for _, status := range []int32{0, 200, 301, 401, 500} {
		t.Run(strconv.Itoa(int(status)), func(t *testing.T) {
			resetHostMocks(t)
			var networkErr error
			if status == 0 {
				networkErr = errors.New("private-request-token")
			}
			host.HTTPMock.On("Send", mock.MatchedBy(func(req host.HTTPRequest) bool {
				return req.NoFollowRedirects && req.TimeoutMs == 1000
			})).Return(&host.HTTPResponse{StatusCode: status, Body: []byte("private-response-token")}, networkErr).Once()
			err := postEvent(configuration{endpoint: "http://127.0.0.1:8750/_navdio/events", token: "private-event-token"}, []byte(`{"version":1,"type":"heartbeat"}`))
			if err == nil || strings.Contains(err.Error(), "private-") {
				t.Fatal("transport error was not sanitized")
			}
		})
	}
}

func TestOversizedEventsAreNotPosted(t *testing.T) {
	resetHostMocks(t)
	if err := postEvent(configuration{}, make([]byte, maxEventBytes+1)); err == nil {
		t.Fatal("oversized event was accepted")
	}
}

func TestReportValidationAcceptsTerminalStatesWithoutTrack(t *testing.T) {
	for _, state := range []string{"stopped", "expired"} {
		if !validReport(scrobbler.PlaybackReportRequest{PlayerId: "player", State: state}) {
			t.Fatalf("terminal %s report without track was rejected", state)
		}
	}
	for _, change := range []func(*scrobbler.PlaybackReportRequest){
		func(req *scrobbler.PlaybackReportRequest) { req.State = "unknown" },
		func(req *scrobbler.PlaybackReportRequest) { req.PositionMs = -1 },
		func(req *scrobbler.PlaybackReportRequest) { req.PlaybackRate = math.NaN() },
		func(req *scrobbler.PlaybackReportRequest) { req.PlaybackRate = math.Inf(1) },
		func(req *scrobbler.PlaybackReportRequest) { req.PlaybackRate = 0 },
		func(req *scrobbler.PlaybackReportRequest) { req.Track.Duration = float32(math.Inf(1)) },
	} {
		report := exampleReport()
		change(&report)
		if validReport(report) {
			t.Fatal("invalid report was accepted")
		}
	}
}

func TestUngrantedLibraryStopsThePreviousBroadcast(t *testing.T) {
	resetHostMocks(t)
	configure(t, "alice", "")
	host.SchedulerMock.On("ScheduleRecurring", "@every 5s", "", heartbeatScheduleID).Return(heartbeatScheduleID, nil).Once()
	report := exampleReport()
	report.Track.LibraryID = 0
	report.Track.Path = ""
	acceptPlayback(t, func(event playbackEvent) {
		if event.State != "stopped" || event.Track.ID != "" || event.CoverArt != nil {
			t.Error("an ungranted track must clear the session without forwarding its metadata")
		}
	})
	if err := (source{}).PlaybackReport(report); err != nil {
		t.Fatal(err)
	}
}

func TestCachedCoverBoundsAndNegativeEntries(t *testing.T) {
	for _, scenario := range []string{"valid", "negative", "oversized"} {
		t.Run(scenario, func(t *testing.T) {
			resetHostMocks(t)
			var data []byte
			switch scenario {
			case "valid":
				data = testCover(t)
			case "oversized":
				data = make([]byte, maxCoverBytes+1)
			}
			host.CacheMock.On("GetBytes", coverCacheKey("alice", "track")).Return(data, true, nil).Once()
			got := coverArt("alice", "track")
			if scenario == "valid" {
				if !bytes.Equal(got, data) {
					t.Fatal("cached cover was changed")
				}
			} else if got != nil {
				t.Fatal("negative or oversized cached cover was forwarded")
			}
			host.SubsonicAPIMock.AssertNotCalled(t, "CallRaw", mock.Anything)
		})
	}
}

func TestInvalidConfigurationNeverSchedulesOrEchoesSecrets(t *testing.T) {
	for _, scenario := range []string{"username", "token", "token-newline", "origin"} {
		t.Run(scenario, func(t *testing.T) {
			resetHostMocks(t)
			values := map[string]string{
				"username": "alice", "player": "", "token": "private-secret-token",
				"companionUrl": "http://127.0.0.1:8750",
			}
			switch scenario {
			case "username", "token":
				values[scenario] = ""
			case "token-newline":
				values["token"] = "private-secret-token\n"
			case "origin":
				values["companionUrl"] = "http://user:private-secret-token@example.com"
			}
			for key, value := range values {
				pdk.PDKMock.On("GetConfig", key).Return(value, true).Once()
			}
			err := (source{}).OnInit()
			if err == nil || strings.Contains(err.Error(), "private-secret-token") {
				t.Fatal("invalid configuration was accepted or its secret was echoed")
			}
			host.SchedulerMock.AssertNotCalled(t, "ScheduleRecurring", mock.Anything, mock.Anything, mock.Anything)
			host.HTTPMock.AssertNotCalled(t, "Send", mock.Anything)
		})
	}
}

func TestOptionalCoverIsDroppedBeforeRejectingLargeMetadata(t *testing.T) {
	resetHostMocks(t)
	configure(t, "alice", "")
	im := image.NewRGBA(image.Rect(0, 0, 50, 50))
	var seed uint32 = 1
	for n := range im.Pix {
		seed = seed*1664525 + 1013904223
		im.Pix[n] = byte(seed >> 24)
		if n%4 == 3 {
			im.Pix[n] = 255
		}
	}
	var encoded bytes.Buffer
	if err := png.Encode(&encoded, im); err != nil {
		t.Fatal(err)
	}
	cover := encoded.Bytes()
	if len(cover) > maxCoverBytes || len(cover) < 4096 {
		t.Fatal("test cover must exercise event-size fallback within the cover limit")
	}
	host.SchedulerMock.On("ScheduleRecurring", "@every 5s", "", heartbeatScheduleID).Return(heartbeatScheduleID, nil).Once()
	host.CacheMock.On("GetBytes", coverCacheKey("alice", "track-1")).Return(cover, true, nil).Once()
	report := exampleReport()
	report.Track.Title = strings.Repeat("x", 60000)
	acceptPlayback(t, func(event playbackEvent) {
		if event.CoverArt != nil || event.Track.Title != report.Track.Title {
			t.Error("event-size fallback lost metadata or retained the cover")
		}
	})
	if err := (source{}).PlaybackReport(report); err != nil {
		t.Fatal(err)
	}
}
