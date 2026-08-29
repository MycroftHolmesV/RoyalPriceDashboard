# Security policy

## Supported versions

Royal Price Dashboard is currently an experimental preview. Security fixes are
made on the latest release only.

## Reporting a vulnerability

Please do not include credentials, Home Assistant tokens, reservation details,
or a copy of the App's `/data` directory in a public issue. Use the repository's
**Report a vulnerability** form to send suspected vulnerabilities privately. If
that form is unavailable, open an issue containing no vulnerability details and
ask the maintainer to arrange a private reporting channel.

## Security boundary

The App does not request host networking, host filesystem mappings, Docker or
Supervisor access, privileged mode, or full Home Assistant access. It requests
Ingress and Home Assistant API access; the API is used only to create persistent
notifications for products the user explicitly watches. The distributed image
also restricts its HTTP service to the Supervisor Ingress proxy and loopback
clients.

The App calls public Royal Caribbean and Celebrity endpoints through a
checksum-pinned copy of the upstream browser. It does not ask for or store
cruise-line account credentials. Catalogs, preferences, and SQLite history stay
inside the App-private `/data` volume unless the user exports watch-list YAML.

Container construction verifies the pinned upstream source checksum. The
release workflow prepares Cosign-signed `amd64` and `aarch64` images, but no
image is published without an explicit matching GitHub release. A custom
AppArmor profile is not currently included.
