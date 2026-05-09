#!/usr/bin/env python3
"""Rebuild `catalog.json` from the GitHub Releases of every add-on listed in
`addons.yml`.

Design:
  - One catalog entry per add-on repo.
  - We treat the latest non-prerelease GitHub Release as the current version.
  - Each release attaches platform zips named `<id>-<version>-<platform>.zip`
    plus a sibling `<id>-<version>-<platform>.zip.sha256` text file with the
    hex digest. That sidecar lets us assemble the catalog without having to
    download (and re-hash) gigabyte-scale binaries on every rebuild.

Why YAML for the source list and JSON for the output:
  - `addons.yml` is human-edited; comments + minimal punctuation matter.
  - `catalog.json` is machine-consumed by Subtitld; JSON keeps the parser
    in the host trivial (stdlib `json` only).

Run:
    python build_catalog.py            # writes catalog.json next to script
    python build_catalog.py --check    # exit 1 if the generated file would
                                       # differ from the on-disk catalog.json
                                       # (CI guard)

Auth:
  We use `gh api` rather than `requests` so credentials come from the
  GitHub CLI (already authenticated in CI via the default GITHUB_TOKEN).
  Anonymous access works for public repos but rate-limits faster.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml  # PyYAML
except ImportError:  # pragma: no cover
    sys.stderr.write(
        'PyYAML is required: pip install pyyaml (CI image already has it)\n'
    )
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent
ADDONS_YML = REPO_ROOT / 'addons.yml'
CATALOG_JSON = REPO_ROOT / 'catalog.json'

# Subtitld host floor that this catalog format targets. Bump in lockstep
# with `installer.CATALOG_SCHEMA_VERSION` if the schema changes.
SCHEMA_VERSION = 1
DEFAULT_MIN_SUBTITLD = '0.6.0'

# Asset filename convention: <id>-<version>-<platform>.zip
# `version` matches the release tag with an optional `v` prefix stripped.
# Platform is one of linux-x86_64 / macos-universal / windows-x86_64; we map
# back to the short `linux`/`macos`/`windows` keys the host expects.
PLATFORM_FROM_SUFFIX = {
    'linux-x86_64':    'linux',
    'linux-aarch64':   'linux',
    'macos-universal': 'macos',
    'macos-x86_64':    'macos',
    'macos-arm64':     'macos',
    'windows-x86_64':  'windows',
}
# Anchor `plat` to the closed set of known platform suffixes. We can't use
# a permissive `[a-z0-9_-]+` here because SemVer pre-release versions
# (e.g. `1.0.0-rc1`) and our platform suffixes (e.g. `1.0.0-linux-x86_64`)
# are structurally indistinguishable — a greedy version group will eat
# `-linux` as a pre-release tag and leave only `x86_64` for `plat`.
# Listing platforms explicitly makes the regex unambiguous: the version
# group only matches up to the first `-<known-platform>-` boundary.
_PLATFORM_ALT = '|'.join(re.escape(k) for k in PLATFORM_FROM_SUFFIX)
ASSET_RE = re.compile(
    r'^(?P<id>[a-z][a-z0-9-]*)'
    r'-(?P<version>[0-9]+(?:\.[0-9]+){0,3}(?:[-+][\w.]+)?)'
    rf'-(?P<plat>{_PLATFORM_ALT})\.zip$'
)


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------
def gh_api(path: str) -> dict | list:
    """Run `gh api <path>` and return the parsed JSON. Bubbles up errors so
    the caller (or CI) sees the actual failure rather than a silent empty
    catalog."""
    proc = subprocess.run(
        ['gh', 'api', path, '-H', 'Accept: application/vnd.github+json'],
        check=False, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f'gh api {path!r} failed (exit {proc.returncode}): {proc.stderr.strip()}'
        )
    return json.loads(proc.stdout)


def fetch_latest_release(owner: str, repo: str) -> dict | None:
    """Return the latest non-draft release for `owner/repo`, or None if the
    repo has no releases yet (a freshly created add-on repo will hit this)."""
    try:
        rel = gh_api(f'repos/{owner}/{repo}/releases/latest')
    except RuntimeError as exc:
        # 404 = no releases yet; anything else is a real problem.
        if 'HTTP 404' in str(exc) or 'Not Found' in str(exc):
            return None
        raise
    return rel if isinstance(rel, dict) else None


def fetch_sha256_sidecar(asset: dict) -> str | None:
    """Download the `.sha256` sidecar text body for an asset and return the
    hex digest. Returns None if the sidecar isn't reachable — the asset is
    skipped rather than getting a placeholder."""
    proc = subprocess.run(
        ['gh', 'api', f'/repos/{asset["_owner_repo"]}/releases/assets/{asset["id"]}',
         '-H', 'Accept: application/octet-stream'],
        check=False, capture_output=True,
    )
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode('utf-8', errors='replace').strip()
    # Sidecar may be `<digest>  <filename>` (sha256sum format) or just the
    # bare digest. Take the first 64-char hex token either way.
    m = re.search(r'\b([a-f0-9]{64})\b', text)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Catalog assembly
# ---------------------------------------------------------------------------
def parse_assets(release: dict, owner_repo: str) -> tuple[str, list[dict]]:
    """Walk `release.assets`, extract `(version, [download dicts])`.

    Returns ('', []) if the release has no recognizable platform zips —
    happens for releases that haven't finished uploading or that ship
    only source archives. The catalog rebuild treats this as "skip add-on
    until next release".
    """
    assets = release.get('assets') or []
    by_name = {a['name']: a for a in assets}
    downloads: list[dict] = []
    detected_version: str | None = None

    for name, asset in by_name.items():
        m = ASSET_RE.match(name)
        if not m:
            continue
        version = m.group('version')
        plat_suffix = m.group('plat')
        platform = PLATFORM_FROM_SUFFIX.get(plat_suffix)
        if platform is None:
            sys.stderr.write(f'  skip asset {name}: unknown platform suffix {plat_suffix!r}\n')
            continue
        detected_version = detected_version or version
        if version != detected_version:
            sys.stderr.write(
                f'  skip asset {name}: version {version} differs from {detected_version}\n'
            )
            continue

        sidecar_name = f'{name}.sha256'
        sidecar = by_name.get(sidecar_name)
        if sidecar is None:
            sys.stderr.write(f'  skip asset {name}: no {sidecar_name} sidecar uploaded\n')
            continue
        # Tag the sidecar with its repo so fetch_sha256_sidecar can hit the
        # raw asset endpoint.
        sidecar = dict(sidecar)
        sidecar['_owner_repo'] = owner_repo
        sha = fetch_sha256_sidecar(sidecar)
        if not sha:
            sys.stderr.write(f'  skip asset {name}: failed to read sidecar body\n')
            continue

        downloads.append({
            'platform': platform,
            'url': asset['browser_download_url'],
            'size_bytes': asset.get('size', 0),
            'sha256': sha,
        })

    return detected_version or '', downloads


def build_addon_entry(addon_cfg: dict) -> dict | None:
    """Turn one entry from `addons.yml` into a catalog `addons[]` entry.

    Returns None when the add-on doesn't have a usable release yet — the
    rebuild logs a warning and the entry is omitted rather than published
    half-broken.
    """
    addon_id = addon_cfg['id']
    repo = addon_cfg.get('repo') or addon_id
    owner = addon_cfg.get('owner') or 'Subtitld'
    owner_repo = f'{owner}/{repo}'

    sys.stderr.write(f'» {owner_repo}\n')

    release = fetch_latest_release(owner, repo)
    if release is None:
        sys.stderr.write('  no releases yet; skipping\n')
        return None
    if release.get('draft') or release.get('prerelease'):
        sys.stderr.write(f'  latest release is draft/prerelease ({release.get("tag_name")}); skipping\n')
        return None

    version, downloads = parse_assets(release, owner_repo)
    if not downloads:
        sys.stderr.write('  no platform zips with sidecars; skipping\n')
        return None

    # The catalog entry blends declarative metadata from addons.yml with
    # release-derived facts. Anything the host needs at *browse time* (icon,
    # license, summary) lives in addons.yml so we don't pay an extra fetch
    # per add-on. Anything that changes per release (version, downloads)
    # comes from GitHub.
    entry = {
        'id': addon_id,
        'display_name': addon_cfg.get('display_name', addon_id),
        'summary': addon_cfg.get('summary', ''),
        'tasks': addon_cfg.get('tasks', []),
        # `languages` lets the host's AddonsDialog filter catalog rows
        # by language at *browse* time. Sourced from addons.yml — see the
        # field reference comment there. Empty list is fine for add-ons
        # whose languages aren't a meaningful filter (e.g. non-locale
        # tasks like translation pipelines), but we still emit the key
        # so the host can rely on it being present.
        'languages': list(addon_cfg.get('languages') or []),
        'license': addon_cfg.get('license', ''),
        'homepage': addon_cfg.get('homepage', f'https://github.com/{owner_repo}'),
        'latest_version': version,
        'releases': [{
            'version': version,
            'min_subtitld': addon_cfg.get('min_subtitld', DEFAULT_MIN_SUBTITLD),
            'changelog': (release.get('body') or '').strip(),
            'downloads': sorted(downloads, key=lambda d: d['platform']),
        }],
    }
    if 'icon' in addon_cfg:
        entry['icon'] = addon_cfg['icon']
    return entry


def build_catalog() -> dict:
    cfg = yaml.safe_load(ADDONS_YML.read_text(encoding='utf-8')) or {}
    addons_cfg = cfg.get('addons') or []
    if not addons_cfg:
        sys.stderr.write('addons.yml has no `addons:` entries; emitting empty catalog\n')

    addons_out: list[dict] = []
    for addon_cfg in addons_cfg:
        try:
            entry = build_addon_entry(addon_cfg)
        except Exception as exc:
            sys.stderr.write(f'  ERROR: {exc}\n')
            entry = None
        if entry is not None:
            addons_out.append(entry)

    return {
        'schema_version': SCHEMA_VERSION,
        'generated': datetime.datetime.now(datetime.timezone.utc)
            .strftime('%Y-%m-%dT%H:%M:%SZ'),
        'min_subtitld': cfg.get('min_subtitld', DEFAULT_MIN_SUBTITLD),
        'addons': addons_out,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--check', action='store_true',
        help='Exit non-zero if the rebuild would change catalog.json '
             '(use as a CI freshness guard; does not write).'
    )
    parser.add_argument(
        '--out', type=Path, default=CATALOG_JSON,
        help='Output path (default: catalog.json next to this script).'
    )
    args = parser.parse_args(argv)

    catalog = build_catalog()
    serialized = json.dumps(catalog, ensure_ascii=False, indent=2) + '\n'

    if args.check:
        existing = args.out.read_text(encoding='utf-8') if args.out.is_file() else ''
        # `generated` is a timestamp — strip it from both before comparison
        # so a no-op rebuild doesn't false-positive the freshness check.
        if _drop_generated(existing) == _drop_generated(serialized):
            sys.stderr.write('catalog up to date\n')
            return 0
        sys.stderr.write('catalog would change — run build_catalog.py and commit\n')
        return 1

    args.out.write_text(serialized, encoding='utf-8')
    sys.stderr.write(f'wrote {args.out} ({len(catalog["addons"])} add-on(s))\n')
    return 0


def _drop_generated(blob: str) -> str:
    """Stable comparison: blank the `generated` timestamp."""
    return re.sub(r'"generated"\s*:\s*"[^"]*"', '"generated": ""', blob)


if __name__ == '__main__':
    sys.exit(main())
