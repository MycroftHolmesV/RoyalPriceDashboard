# Release procedure

Royal Price Dashboard uses an explicit release event as the container
publication gate. A merge or ordinary push never publishes an image.

## Prepare a version

1. Update the version consistently in the App metadata, Dockerfile, backend,
   browser assets, and changelog. The backend regression suite checks these
   markers together.
2. From `royal-price-dashboard`, run:

   ```text
   python -m unittest discover -s tests -v
   python -m py_compile server.py upstream_adapter.py tests/test_server.py
   node --check static/app.js
   ```

3. Review the complete diff, repeat the tracked-file and history scans, and
   confirm that no token, `.env`, live catalog, preference file, SQLite file,
   backup, or deployment artifact is present.
4. Let pull-request CI build both declared architectures. Do not infer
   `aarch64` support from an `amd64` build.

## Publish with explicit approval

1. Confirm that `royal-price-dashboard/config.yaml` names the intended generic
   GHCR image and that its `version` is the intended image tag.
2. Publish a GitHub release tagged `vX.Y.Z`, where `X.Y.Z` exactly matches the
   App version. The release workflow rejects any mismatch.
3. The workflow uses Home Assistant's builder actions to create native
   `amd64` and `aarch64` images, sign them with GitHub OIDC and Cosign, and
   publish one generic multi-architecture manifest tagged `X.Y.Z`.
4. Make the GHCR package public. Image publication and package visibility are
   separate GitHub actions.
5. Inspect the published manifest and verify its Cosign signature before an
   installation test.

The App metadata does not use `latest`. Home Assistant selects the immutable
version tag that matches `config.yaml`.

## Home Assistant acceptance matrix

Use a disposable Home Assistant test installation and non-sensitive fixture
cruises before announcing a release.

- Add the repository through the My Home Assistant link and by manual URL.
- Install and start on `amd64` and on real `aarch64` hardware, or record an
  equivalent hardware-backed result.
- Confirm Ingress, the sidebar panel, `/health`, clean onboarding, first
  catalog creation, restart during catalog creation, and explicit-watch-only
  notifications.
- Create a Home Assistant backup with the App selected. The App uses a cold
  backup, so Supervisor stops it while copying `/data` and restarts it
  afterward.
- Restore the backup into a disposable installation and verify cruise
  registry, catalogs, pins, watches, history, and App startup.
- Upgrade from the prior repository version and verify that the same App-private
  `/data` is retained. A local development App has a different repository
  identity and is not this upgrade path.
- Test uninstall and reinstall behavior without assuming that deleted App data
  is recoverable outside a verified backup.
- Review App logs and the Home Assistant host audit log. Develop any custom
  AppArmor profile in complain mode first, then enforce it only after the full
  acceptance matrix is clean.

Before each release, repeat the clean export and secret scan. Confirm that
private vulnerability reporting and branch protection are enabled, then verify
the one-click repository link from a signed-out browser.
