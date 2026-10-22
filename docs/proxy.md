# Public proxy

The NixOS module starts Navdio on loopback. It does not configure a domain, TLS, or firewall rules.
Configure public access in the consuming flake. No separate audio server or API application is needed.

## Local nginx

Expose only the three public GET routes.
For an nginx host with a domain and TLS already configured, add these locations:

```nix
services.nginx.appendHttpConfig = ''
  limit_conn_zone navdio zone=navdio_public:1m;
'';

services.nginx.virtualHosts."radio.example.org".extraConfig = ''
  limit_conn navdio_public 52;
'';

services.nginx.virtualHosts."radio.example.org".locations = {
  "/".return = "404";
  "= /radio/stream" = {
    proxyPass = "http://127.0.0.1:8750/radio/stream";
    recommendedProxySettings = false;
    extraConfig = ''
      limit_except GET { deny all; }
      proxy_http_version 1.1;
      proxy_set_header Connection "";
      proxy_buffering off;
      proxy_cache off;
      proxy_read_timeout 1d;
      send_timeout 15s;
      gzip off;
      add_header Cache-Control "no-store" always;
      add_header Access-Control-Allow-Origin "*" always;
    '';
  };
  "= /radio/info" = {
    proxyPass = "http://127.0.0.1:8750/radio/info";
    extraConfig = ''
      limit_except GET { deny all; }
      add_header Access-Control-Allow-Origin "*" always;
    '';
  };
  "= /radio/cover" = {
    proxyPass = "http://127.0.0.1:8750/radio/cover";
    extraConfig = ''
      limit_except GET { deny all; }
      add_header Access-Control-Allow-Origin "*" always;
    '';
  };
};
```

Use a dedicated radio virtual host for this example. Its fallback returns 404.
Never proxy the companion's entire port or expose MPD's HTTP port and control socket.
The companion port includes the private `/_navdio/events` endpoint.
CORS permits browser reads. It does not authorise private events.
Use a specific allowed origin instead of `*` if needed.

The aggregate limit permits 52 public requests at once, including streams, info, and cover.
The default companion permits 56 requests, which leaves four slots for private events.
Excess public requests receive 503.
If you change `maxListeners`, set this limit no higher than `maxListeners + 12`.
Apply the limit across all public routes and ingress hosts that use the same companion.
If a location adds its own `limit_conn` directives, repeat the aggregate limit there.

## Remote nginx ingress

For a remote public server, proxy requests over a private VPN to nginx on the Navidrome host.
The source nginx forwards only the three radio routes to loopback Navdio.
Each listener opens a separate stream across the VPN. This is not a shared-stream relay.
Keep encoding on the Navidrome host beside the music library.

For example, the public proxy can shorten the route paths:

| Public route | Companion route |
| --- | --- |
| `/stream` | `/radio/stream` |
| `/info` | `/radio/info` |
| `/cover` | `/radio/cover` |

Return 404 for all other public paths.
Clients fetch artwork from `/cover` directly in this example.
Info does not include a cover URL.

Two upstream settings matter:

- Set `Host` to the source's private virtual-host name, not the public radio hostname.
- Set `proxy_bind` to the ingress host's VPN address so the source allowlist sees the expected IP.

Allow only that ingress IP on the source's exact `/radio/stream`, `/radio/info`, and `/radio/cover` locations.
Do not expose the whole companion port to the VPN.
Disable proxy buffering on both nginx stream locations and use a long read timeout.
Apply the aggregate public-request limit on the source nginx.
At public ingress, allow only GET requests and set CORS headers on stream, info, and cover.
When an upstream already sends CORS headers, hide them before adding the public header to avoid duplicates.

## Traffic and playback

`maxListeners` limits companion stream connections, including each listener behind nginx.
For example, allow two streams per IP and limit new stream requests to 2/s with a burst of four.
Set separate request limits for info and cover.
The companion does not trust forwarded client-IP headers. Apply per-IP limits at public ingress.

At 40 listeners and 32 kbps, encoded audio uses about 1.28 Mbps, plus protocol overhead and VBR variation.
Connection and request limits do not enforce a strict shared bandwidth cap.
Use a separate traffic shaper if you need one.
Idle listeners receive compressed silence.

Do not enable ICY metadata negotiation.
It inserts non-Ogg bytes into the stream and can cause audio gaps.
Navdio does not forward listeners' ICY headers. Read metadata from the info endpoint instead.
The player also avoids repeated seek corrections, which can interrupt playback.
