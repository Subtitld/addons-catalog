# Subtitld add-ons catalog

This repository is the canonical source for `catalog.json`, the file the
Subtitld desktop app fetches to discover installable add-ons (TTS, ASR,
translation engines that ship as separate downloads).

## How it works

```
github.com/Subtitld/<addon-repo>           ← one repo per add-on
        │ release published
        ▼
github.com/Subtitld/addons-catalog         ← this repo
        │ workflow rebuilds catalog.json
        ▼
catalog.json (in this repo, on main)
        │ served via GitHub Pages or proxied at subtitld.org/addons/
        ▼
Subtitld app  → installer.fetch_catalog()  → list, download, sha256 verify
```

`addons.yml` is the **allowlist**: it names the repos that are part of the
official Subtitld catalog. `build_catalog.py` walks that list, asks the
GitHub API for each repo's latest release, parses the platform zip assets
and their `.sha256` sidecars, and writes the result to `catalog.json`.

The workflow runs on:
- `repository_dispatch` (fired by each add-on's release workflow),
- a 6-hourly cron (safety net),
- pushes that touch `addons.yml` / `build_catalog.py`,
- manual `workflow_dispatch`.

## Asset naming convention

Each add-on release uploads, for every supported platform:

```
<id>-<version>-<platform-suffix>.zip
<id>-<version>-<platform-suffix>.zip.sha256
```

Recognized platform suffixes:

| suffix             | host platform |
|--------------------|---------------|
| `linux-x86_64`     | linux         |
| `linux-aarch64`    | linux         |
| `macos-universal`  | macos         |
| `macos-x86_64`     | macos         |
| `macos-arm64`      | macos         |
| `windows-x86_64`   | windows       |

The sidecar is a text file containing the sha256 hex digest (either bare
or in `<digest>  <filename>` `sha256sum` format — both are accepted).

## Adding a new add-on

1. Create `github.com/Subtitld/<addon-repo>` with a `manifest.json`
   matching the schema in
   [`subtitld/packaging/addons/catalog/README.md`](https://github.com/Subtitld/subtitld/blob/main/packaging/addons/catalog/README.md#manifest-schema-manifestjson-inside-each-zip).
2. Wire its release workflow (see the `piper-tts` repo for a reference).
   The workflow uploads the platform zips, the `.sha256` sidecars, and
   triggers a `repository_dispatch` here:

   ```yaml
   - name: Trigger catalog rebuild
     env:
       GH_TOKEN: ${{ secrets.SUBTITLD_BOT_TOKEN }}
     run: |
       gh api repos/Subtitld/addons-catalog/dispatches \
         -f event_type=catalog-rebuild
   ```

   `SUBTITLD_BOT_TOKEN` is an org-level fine-grained PAT scoped to
   `Contents: write` on this repo only.
3. Open a PR adding the entry to `addons.yml`.

## Local testing

```sh
# Builds against current GitHub state.
gh auth login                # or set GH_TOKEN
pip install pyyaml
python build_catalog.py
cat catalog.json | jq '.addons[].id'

# CI guard: fails if the regenerated catalog would differ from the file
# in git, ignoring the `generated` timestamp.
python build_catalog.py --check
```

To exercise the full installer flow against a local catalog without
publishing anything to GitHub Releases, see
`packaging/addons/catalog/serve_local.py` in the Subtitld repo — it
generates stub zips and serves them from `localhost`.

## Schema

The output is the JSON Schema documented in the Subtitld repo
(`packaging/addons/catalog/README.md`). Bumping `schema_version` here
must happen in lockstep with `installer.CATALOG_SCHEMA_VERSION` in
the host so older clients refuse to parse newer catalogs cleanly.

## Security notes

- v0 verifies sha256 of each downloaded zip but does **not** sign
  `catalog.json` itself. The catalog is served over HTTPS and lives in
  this repo whose write access is restricted; that's the trust root for
  v0.
- v2 will add ed25519 signatures over `catalog.json`. The public key is
  embedded in Subtitld releases; the private key lives offline.

## License

The catalog assembly tooling (this repo) is MIT — see [`LICENSE`](LICENSE).
Each add-on's content has its own licensing; check the per-add-on repo.
