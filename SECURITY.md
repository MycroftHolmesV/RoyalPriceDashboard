# Security policy

## Supported versions

Royal Price Dashboard is currently an experimental preview. Security fixes are
made on the latest release only.

## Reporting a vulnerability

Please do not include credentials, Home Assistant tokens, reservation details,
or a copy of the App's `/data` directory in a public issue. Once the public
repository is available, use its private vulnerability-reporting feature. Until
then, report suspected vulnerabilities privately to the repository owner.

## Security boundary

The App does not request host networking, host filesystem mappings, Docker or
Supervisor access, privileged mode, or full Home Assistant access. It requests
Ingress and Home Assistant API access; the API is used only to create persistent
notifications for products the user explicitly watches.

The App calls public Royal Caribbean and Celebrity endpoints through a
checksum-pinned copy of the upstream browser. It does not ask for or store
cruise-line account credentials. Catalogs, preferences, and SQLite history stay
inside the App-private `/data` volume unless the user exports watch-list YAML.

Container construction verifies the pinned upstream source checksum. A custom
AppArmor profile and signed, multi-architecture release images remain release
gates until they have been exercised on a real Home Assistant installation.
