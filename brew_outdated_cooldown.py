#!/usr/bin/env python3

# Copyright 2026 Wolfgang Hoschek AT mac DOT com
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Prints the name of Homebrew packages that can be upgraded without pulling in transitive dependencies with age < N days.

This can help to reduce the risk of supply chain attacks or teething issues around critical releases, similar to uv's
`exclude-newer-than` and pip's `--uploaded-prior-to` and dependabot's `cooldown`. For some background see
https://nesbitt.io/2026/03/04/package-managers-need-to-cool-down.html

Package age loosely corresponds to how many days ago a package version was released.
More precisely, package age is determined by the most recent git commit touching the local Homebrew tap definition file. If
that timestamp cannot be determined, the age is reported as unknown.

Output section `Proposed upgrade command for leaf formulae and casks (not executed)`:
- Lists the subset of `brew outdated` formulae that are either in `brew leaves` or casks.
- Except an item is included only if:
1. the formula age is known and is at least `--min-age-days` (which is an integer that defaults to 7, 0 is also valid);
2. every transitive runtime dependency age is known; and
3. if transitive runtime dependencies exist, the newest dependency age is also at least `--min-age-days`.
If a formula has no transitive runtime dependencies, or a cask has no Homebrew formula dependencies, only its own age is
checked.

Output sections `Leaf and non-leaf packages` and `Leaf formulae and casks`:
- Output section `Leaf and non-leaf packages`: lists every outdated entry reported by `brew outdated`.
- Output section `Leaf formulae and casks`: lists the subset of `brew outdated`
  formulae that are either in `brew leaves` or casks.
- Each package line starts with Homebrew's verbose outdated line when available, otherwise a synthesized verbose-style line,
  and then shows the package age, for example `11 days ago`, or `unknown age` if the tap timestamp cannot be determined.
- If the newest transitive runtime dependency is newer than the package itself, the line also shows
  `dependency-name X days ago`.
- The reported number of days is a rounded down integer, for example 23h ago prints '0 days ago', 24h ago prints '1 day ago'.

Example output:
Leaf and non-leaf packages:
harfbuzz (13.2.1) < 14.0.0 (1 day ago)
iterm2 (3.5.0) != 3.5.1 (9 days ago)
jpeg-turbo (3.1.4) < 3.1.4.1 (5 days ago)
ocrmypdf (17.4.0) < 17.4.0_1 (11 days ago, harfbuzz 1 day ago)
uv (0.11.2) < 0.11.3 (7 days ago)

Leaf formulae and casks:
iterm2 (3.5.0) != 3.5.1 (9 days ago)
ocrmypdf (17.4.0) < 17.4.0_1 (11 days ago, harfbuzz 1 day ago)
uv (0.11.2) < 0.11.3 (7 days ago)

Proposed upgrade command for leaf formulae and casks (not executed): brew upgrade iterm2 uv
"""

from __future__ import annotations
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import as_completed
from concurrent.futures.thread import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import cache
from typing import Any, TypedDict, cast

SOURCE_TAP_GIT = "TAP"

MIN_PYTHON_VERSION = (3, 9)
if sys.version_info < MIN_PYTHON_VERSION:
    print(f"ERROR: This program requires Python version >= {'.'.join(map(str, MIN_PYTHON_VERSION))}!")
    sys.exit(3)

def main() -> int:
    """Render the outdated package report and proposed upgrade command.

    Assumes Homebrew is installed and reachable in PATH. The report keeps the existing upgrade suggestion while enriching
    each line with rounded-down release-age metadata.
    """

    brew = shutil.which("brew")
    if not brew:
        print("brew not found in PATH", file=sys.stderr)
        return 1

    cli_args = _parse_cli_args(sys.argv[1:])
    min_age_days = cli_args["min_age_days"]
    outdated = _run_brew_json(brew, ["outdated", "--json=v2"])
    verbose_output = _run_brew(brew, ["outdated", "--verbose"]).strip()
    leaves_output = _run_brew(brew, ["leaves"])
    leaf_tokens = frozenset(
        line.strip() for line in leaves_output.splitlines() if line.strip()
    )
    selected_tokens = _preferred_upgrade_tokens(outdated, leaf_tokens)

    lines = (
        verbose_output.splitlines()
        if verbose_output
        else _fallback_verbose_lines(outdated)
    )
    if not lines:
        return 0

    info_by_token = _load_outdated_info(brew, outdated)
    age_by_token = _build_age_by_token(outdated, info_by_token)
    rendered_lines = _rendered_report_lines(lines, age_by_token)
    upgrade_candidates: list[str] = []

    print("Leaf and non-leaf packages:")
    for line in rendered_lines:
        token = _extract_token(line)
        age_info = age_by_token.get(token)
        print(line)
        if (
            age_info is not None
            and token in selected_tokens
            and _is_upgrade_candidate(age_info, min_age_days=min_age_days)
        ):
            upgrade_candidates.append(token)

    print()
    print("Leaf formulae and casks:")
    for line in rendered_lines:
        token = _extract_token(line)
        if token in selected_tokens:
            print(line)

    print()
    if upgrade_candidates:
        quoted = " ".join(shlex.quote(token) for token in upgrade_candidates)
        print(
            f"Proposed upgrade command for leaf formulae and casks (not executed): brew upgrade {quoted}"
        )
    else:
        print(
            f"Proposed upgrade command for leaf formulae and casks (not executed): "
            f"no packages satisfy --min-age-days={min_age_days}"
        )
    return 0


class RuntimeDependencyStatus(TypedDict):
    """Describe newest-runtime-dependency metadata for upgrade gating.

    Assumes the script computes release ages separately from graph traversal. Keeping these fields typed makes the fail-
    closed candidate decision explicit and checkable.
    """

    has_runtime_dependencies: bool
    has_unknown_runtime_dependency_age: bool
    newest_runtime_dependency_days: int | None
    label_dependency: tuple[str, int, str] | None


class AgeInfo(TypedDict):
    """Describe one rendered age plus provenance and gating metadata.

    Assumes every known age comes from exactly one metadata source. Keeping the rendered label and raw fields together lets
    package and dependency output stay consistent while upgrade gating remains explicit.
    """

    days: int | None
    label: str
    source: str | None
    has_runtime_dependencies: bool
    has_unknown_runtime_dependency_age: bool
    newest_runtime_dependency_days: int | None


class ParsedCliArgs(TypedDict):
    min_age_days: int


def _parse_cli_args(args: list[str]) -> ParsedCliArgs:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--min-age-days",
        type=_parse_nonnegative_int,
        default=7,
        help="Minimum age in days required for proposed upgrades. Default: 7.",
    )
    namespace = parser.parse_args(args)
    return {
        "min_age_days": namespace.min_age_days,
    }


def _parse_nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def _preferred_upgrade_tokens(
    outdated: dict[str, Any], leaf_tokens: frozenset[str]
) -> frozenset[str]:
    """Return the filtered set shown in the second section and upgrade proposal.

    Assumes leaf formulae and outdated casks are the user-facing top-level items worth proposing. The union keeps formula
    leaf selection while ensuring casks are treated as first-class entries even though `brew leaves` reports only formulae.
    """

    tokens = set(leaf_tokens)
    for entry in outdated.get("casks", []):
        token = entry.get("name") or entry.get("token")
        if isinstance(token, str) and token:
            tokens.add(token)
    return frozenset(tokens)


def _rendered_report_lines(
    lines: list[str],
    age_by_token: dict[str, AgeInfo],
) -> list[str]:
    """Return report lines with age labels appended when known.

    Assumes each verbose Homebrew line starts with the package token. Rendering the labels once keeps the leaf-only section
    and the full report section byte-for-byte consistent.
    """

    rendered_lines: list[str] = []
    for line in lines:
        token = _extract_token(line)
        age_info = age_by_token.get(token)
        age_label = age_info["label"] if age_info else None
        if age_label:
            rendered_lines.append(f"{line} ({age_label})")
        else:
            rendered_lines.append(line)
    return rendered_lines


def _run_brew_json(brew: str, args: list[str]) -> dict[str, Any]:
    """Run a Homebrew command and parse its stdout as a JSON object.

    Assumes the invoked subcommand emits JSON when the passed arguments request it. Rejecting non-object payloads keeps the
    downstream field access predictable.
    """

    stdout = _run_brew(brew, args)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        print(f"failed to parse JSON from: brew {' '.join(args)}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not isinstance(payload, dict):
        print(f"expected JSON object from: brew {' '.join(args)}", file=sys.stderr)
        raise SystemExit(1)
    return cast(dict[str, Any], payload)


def _run_brew(brew: str, args: list[str]) -> str:
    """Run one Homebrew subprocess with auto-update disabled.

    Assumes callers want deterministic output without implicit Homebrew updates. Stderr is preserved for operator context
    while stdout is returned for parsing.
    """

    proc = subprocess.run(
        [brew, *args],
        capture_output=True,
        text=True,
        env=_brew_env(),
    )
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return proc.stdout


def _brew_env() -> dict[str, str]:
    """Return the environment used for Homebrew read-only metadata queries.

    Assumes this script should never trigger an implicit `brew update` while
    inspecting local metadata. Centralizing the environment avoids helper drift
    and keeps every Homebrew subprocess consistently read-only.
    """

    env = os.environ.copy()
    env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
    return env


def _load_outdated_info(
    brew: str, outdated: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Load detailed Homebrew metadata for outdated roots and their runtime dependencies.

    Assumes `installed[*].runtime_dependencies` is a flattened transitive graph. The helper expands the initial outdated
    roots to the dependency closure so later age resolution can inspect every installed runtime dependency.
    """

    info_by_token: dict[str, dict[str, Any]] = {}

    formula_tokens = [entry["name"] for entry in outdated.get("formulae", [])]
    if formula_tokens:
        formula_info = _run_brew_json(brew, ["info", "--json=v2", *formula_tokens])
        for entry in formula_info.get("formulae", []):
            info_by_token[entry["name"]] = entry

    cask_tokens = [
        entry.get("name") or entry.get("token") for entry in outdated.get("casks", [])
    ]
    cask_tokens = [token for token in cask_tokens if token]
    if cask_tokens:
        cask_info = _run_brew_json(brew, ["info", "--json=v2", "--cask", *cask_tokens])
        for entry in cask_info.get("casks", []):
            info_by_token[entry["token"]] = entry

    pending_formula_tokens = _collect_missing_runtime_dependency_tokens(info_by_token)
    while pending_formula_tokens:
        dependency_info = _run_brew_json(
            brew, ["info", "--json=v2", *pending_formula_tokens]
        )
        for entry in dependency_info.get("formulae", []):
            info_by_token[entry["name"]] = entry
        pending_formula_tokens = _collect_missing_runtime_dependency_tokens(
            info_by_token
        )

    return info_by_token


def _build_age_by_token(
    outdated: dict[str, Any],
    info_by_token: dict[str, dict[str, Any]],
) -> dict[str, AgeInfo]:
    """Resolve rounded-down release ages and source labels for each token.

    Assumes each token can be processed independently from its package metadata. A second pass decorates each outdated
    formula or cask with the newest runtime dependency when that dependency release is newer than the package release.
    """

    now = datetime.now(timezone.utc)
    age_by_token: dict[str, AgeInfo] = {}

    jobs: list[tuple[str, dict[str, Any]]] = []
    for entry in outdated.get("formulae", []):
        token = entry["name"]
        info = info_by_token.get(token, {})
        jobs.append((token, info))

    for entry in outdated.get("casks", []):
        token = entry.get("name") or entry.get("token")
        if not token:
            continue
        info = info_by_token.get(token, {})
        jobs.append((token, info))

    root_tokens = {token for token, _info in jobs}
    for token, info in info_by_token.items():
        if token in root_tokens:
            continue
        jobs.append((token, info))

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as executor:
        future_map = {
            executor.submit(_resolve_age_label, info, now): token
            for token, info in jobs
        }
        for future in as_completed(future_map):
            token = future_map[future]
            try:
                age_by_token[token] = future.result()
            except Exception:
                age_by_token[token] = _age_info_label(None)

    for entry in outdated.get("formulae", []):
        token = entry["name"]
        age_info = age_by_token.get(token)
        age_days = age_info["days"] if age_info else None
        age_source = age_info["source"] if age_info else None
        if not isinstance(age_days, int) or not isinstance(age_source, str):
            continue
        dependency_status = _newest_runtime_dependency_status(
            token, info_by_token, age_by_token
        )
        age_by_token[token] = _age_info_label(
            age_days,
            source=age_source,
            newest_dependency=dependency_status["label_dependency"],
            has_runtime_dependencies=dependency_status["has_runtime_dependencies"],
            has_unknown_runtime_dependency_age=dependency_status[
                "has_unknown_runtime_dependency_age"
            ],
            newest_runtime_dependency_days=dependency_status[
                "newest_runtime_dependency_days"
            ],
        )

    for entry in outdated.get("casks", []):
        token = entry.get("name") or entry.get("token")
        if not token:
            continue
        age_info = age_by_token.get(token)
        age_days = age_info["days"] if age_info else None
        age_source = age_info["source"] if age_info else None
        if not isinstance(age_days, int) or not isinstance(age_source, str):
            continue
        dependency_status = _newest_runtime_dependency_status(
            token, info_by_token, age_by_token
        )
        age_by_token[token] = _age_info_label(
            age_days,
            source=age_source,
            newest_dependency=dependency_status["label_dependency"],
            has_runtime_dependencies=dependency_status["has_runtime_dependencies"],
            has_unknown_runtime_dependency_age=dependency_status[
                "has_unknown_runtime_dependency_age"
            ],
            newest_runtime_dependency_days=dependency_status[
                "newest_runtime_dependency_days"
            ],
        )

    return age_by_token


def _resolve_age_label(info: dict[str, Any], now: datetime) -> AgeInfo:
    """Build the display label and provenance for one outdated package version.

    Assumes the Homebrew tap file is the source of truth for package availability on this machine. Dependency suffixes are
    resolved separately once all package ages are known.
    """

    released_at, source = _resolve_release_timestamp(info)
    if released_at is None or source is None:
        return _age_info_label(None)

    if released_at.tzinfo is None:
        released_at = released_at.replace(tzinfo=timezone.utc)

    age_days = max(0, int((now - released_at).total_seconds() // 86400))
    return _age_info_label(age_days, source=source)


def _age_info_label(
    age_days: int | None,
    source: str | None = None,
    newest_dependency: tuple[str, int, str] | None = None,
    has_runtime_dependencies: bool = False,
    has_unknown_runtime_dependency_age: bool = False,
    newest_runtime_dependency_days: int | None = None,
) -> AgeInfo:
    """Format release age text for one package line.

    Assumes callers already rounded day counts down to integers. TAP provenance stays implicit in the visible label while
    remaining available internally, and dependency text is appended only when a transitive dependency is strictly newer than
    the package itself.
    """

    if age_days is None:
        return {
            "days": None,
            "label": "unknown age",
            "source": None,
            "has_runtime_dependencies": has_runtime_dependencies,
            "has_unknown_runtime_dependency_age": has_unknown_runtime_dependency_age,
            "newest_runtime_dependency_days": newest_runtime_dependency_days,
        }

    if not isinstance(source, str) or not source:
        raise ValueError("known ages require a source label")

    label = _format_days_with_source(age_days, source)
    if newest_dependency is not None:
        dependency_name, dependency_age_days, dependency_source = newest_dependency
        if dependency_age_days < age_days:
            label = f"{label}, {dependency_name} {_format_days_with_source(dependency_age_days, dependency_source)}"
    return {
        "days": age_days,
        "label": label,
        "source": source,
        "has_runtime_dependencies": has_runtime_dependencies,
        "has_unknown_runtime_dependency_age": has_unknown_runtime_dependency_age,
        "newest_runtime_dependency_days": newest_runtime_dependency_days,
    }


def _is_upgrade_candidate(age_info: AgeInfo, min_age_days: int = 7) -> bool:
    """Return True when the package and newest runtime dependency satisfy the minimum age.

    Assumes `age_info` comes from `_age_info_label`. A package without runtime dependencies uses only its own age; unknown
    dependency ages fail closed. The caller supplies the minimum acceptable age in days, which may be zero.
    """

    age_days = age_info.get("days")
    if not isinstance(age_days, int) or age_days < min_age_days:
        return False

    has_runtime_dependencies = age_info.get("has_runtime_dependencies")
    has_unknown_runtime_dependency_age = age_info.get(
        "has_unknown_runtime_dependency_age"
    )
    newest_runtime_dependency_days = age_info.get("newest_runtime_dependency_days")
    if has_runtime_dependencies is False:
        return True
    if has_unknown_runtime_dependency_age is True:
        return False
    if not isinstance(newest_runtime_dependency_days, int):
        return False
    return newest_runtime_dependency_days >= min_age_days


def _format_days_with_source(age_days: int, source: str) -> str:
    unit = "day" if age_days == 1 else "days"
    s = f"{age_days} {unit} ago"
    if source == SOURCE_TAP_GIT:
        return s
    return f"{s} [{source}]"


def _resolve_release_timestamp(info: dict[str, Any]) -> tuple[datetime | None, str | None]:
    """Resolve the package release timestamp and source from metadata.

    Assumes Homebrew tap history is the source of truth for installed formula and cask availability, including Homebrew-only
    revision bumps. If the tap timestamp cannot be determined, the caller treats the age as unknown.
    """

    tap_timestamp = _tap_file_release_timestamp(info)
    if tap_timestamp is not None:
        return tap_timestamp, SOURCE_TAP_GIT
    return None, None


def _newest_runtime_dependency_status(
    token: str,
    info_by_token: dict[str, dict[str, Any]],
    age_by_token: dict[str, AgeInfo],
) -> RuntimeDependencyStatus:
    """Return dependency-age metadata for one package's Homebrew dependency graph.

    Assumes `info_by_token` contains the currently known Homebrew metadata closure for the outdated roots. Walking that
    closure lets casks inherit the transitive runtime dependencies of their direct formula prerequisites. Missing dependency
    ages are treated as unknown so proposal gating can fail closed.
    """

    dependency_tokens = _transitive_runtime_dependency_tokens(token, info_by_token)
    if len(dependency_tokens) == 0:
        return {
            "has_runtime_dependencies": False,
            "has_unknown_runtime_dependency_age": False,
            "newest_runtime_dependency_days": None,
            "label_dependency": None,
        }

    has_unknown_runtime_dependency_age = False
    newest_runtime_dependency_days: int | None = None
    label_dependency: tuple[str, int, str] | None = None
    for dependency_name in dependency_tokens:
        if dependency_name == token:
            continue
        dependency_age_info = age_by_token.get(dependency_name)
        dependency_age_days = (
            dependency_age_info["days"] if dependency_age_info else None
        )
        dependency_age_source = (
            dependency_age_info["source"] if dependency_age_info else None
        )
        if not isinstance(dependency_age_days, int) or not isinstance(
            dependency_age_source, str
        ):
            has_unknown_runtime_dependency_age = True
            continue
        if (
            newest_runtime_dependency_days is None
            or dependency_age_days < newest_runtime_dependency_days
        ):
            newest_runtime_dependency_days = dependency_age_days
            label_dependency = (
                dependency_name,
                dependency_age_days,
                dependency_age_source,
            )
    return {
        "has_runtime_dependencies": True,
        "has_unknown_runtime_dependency_age": has_unknown_runtime_dependency_age,
        "newest_runtime_dependency_days": newest_runtime_dependency_days,
        "label_dependency": label_dependency,
    }


def _runtime_dependency_tokens(info: dict[str, Any]) -> tuple[str, ...]:
    """Return dependency tokens exposed directly by a Homebrew package payload.

    Assumes formulae record a flattened runtime graph under `installed[*].runtime_dependencies`, while casks may expose
    direct Homebrew formula dependencies under `depends_on.formula`. This helper returns the package-local dependency edges;
    callers that need a closure can expand them separately.
    """

    installed = info.get("installed")
    if not isinstance(installed, list) or len(installed) == 0:
        return _cask_formula_dependency_tokens(info)
    latest_install = installed[0]
    if not isinstance(latest_install, dict):
        return _cask_formula_dependency_tokens(info)
    runtime_dependencies = latest_install.get("runtime_dependencies")
    if not isinstance(runtime_dependencies, list):
        return _cask_formula_dependency_tokens(info)

    tokens: list[str] = []
    for dependency in runtime_dependencies:
        if not isinstance(dependency, dict):
            continue
        full_name = dependency.get("full_name")
        if isinstance(full_name, str) and full_name:
            tokens.append(full_name)
    if len(tokens) == 0:
        return _cask_formula_dependency_tokens(info)
    return tuple(tokens)


def _transitive_runtime_dependency_tokens(
    token: str,
    info_by_token: dict[str, dict[str, Any]],
) -> tuple[str, ...]:
    """Return the transitive Homebrew dependency closure for one package.

    Assumes formula payloads may already expose a flattened runtime graph, while casks may start from direct formula
    prerequisites only. Expanding the closure through `info_by_token` makes dependency-age gating consistent across both
    package kinds.
    """

    root_info = info_by_token.get(token, {})
    pending = list(_runtime_dependency_tokens(root_info))
    seen: set[str] = set()
    ordered_tokens: list[str] = []

    while pending:
        dependency_token = pending.pop()
        if dependency_token == token or dependency_token in seen:
            continue
        seen.add(dependency_token)
        ordered_tokens.append(dependency_token)
        dependency_info = info_by_token.get(dependency_token)
        if dependency_info is None:
            continue
        pending.extend(_runtime_dependency_tokens(dependency_info))

    return tuple(ordered_tokens)


def _cask_formula_dependency_tokens(info: dict[str, Any]) -> tuple[str, ...]:
    """Return direct Homebrew formula dependencies declared by a cask.

    Assumes casks may list formula prerequisites under `depends_on.formula`. Extracting those edges lets the caller reuse the
    formula runtime-dependency closure for cask upgrade gating.
    """

    depends_on = info.get("depends_on")
    if not isinstance(depends_on, dict):
        return ()
    formula_dependencies = depends_on.get("formula")
    if isinstance(formula_dependencies, str) and formula_dependencies:
        return (formula_dependencies,)
    if not isinstance(formula_dependencies, list):
        return ()
    tokens = [
        token for token in formula_dependencies if isinstance(token, str) and token
    ]
    return tuple(tokens)


def _collect_missing_runtime_dependency_tokens(
    info_by_token: dict[str, dict[str, Any]],
) -> list[str]:
    """Return runtime dependency tokens that still need Homebrew info metadata.

    Assumes the current `info_by_token` map may already contain a subset of the flattened dependency graph. Returning only
    missing tokens avoids repeated `brew info` requests while walking the closure.
    """

    missing_tokens: set[str] = set()
    for info in info_by_token.values():
        for dependency_token in _runtime_dependency_tokens(info):
            if dependency_token not in info_by_token:
                missing_tokens.add(dependency_token)
    return sorted(missing_tokens)


@cache
def _tap_repo_path(tap: str) -> str | None:
    """Return the local repository path for a Homebrew tap.

    Assumes `brew --repo <tap>` is the canonical way to locate an installed tap checkout. Caching avoids repeated subprocess
    calls when many packages come from the same tap.
    """

    brew = shutil.which("brew")
    if brew is None:
        return None
    proc = subprocess.run(
        [brew, "--repo", tap],
        capture_output=True,
        text=True,
        env=_brew_env(),
    )
    if proc.returncode != 0:
        return None
    path = proc.stdout.strip()
    return path or None


def _tap_file_release_timestamp(info: dict[str, Any]) -> datetime | None:
    """Return the latest git commit timestamp for the package definition file.

    Assumes installed package availability is defined by the tap file revision, including Homebrew-only revision bumps. Using
    the latest commit touching that file covers the installed formulae and casks on this machine more accurately than remote
    artifact timestamps.
    """

    tap = info.get("tap")
    ruby_source_path = info.get("ruby_source_path")
    if (
        not isinstance(tap, str)
        or not tap
        or not isinstance(ruby_source_path, str)
        or not ruby_source_path
    ):
        return None

    repo_path = _tap_repo_path(tap)
    if repo_path is None:
        return None

    git = shutil.which("git")
    if git is None:
        return None
    proc = subprocess.run(
        [
            git,
            "-C",
            repo_path,
            "log",
            "-1",
            "--follow",
            "--format=%cI",
            "--",
            ruby_source_path,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    if not value:
        return None
    try:
        return _parse_iso8601(value)
    except ValueError:
        return None


def _parse_iso8601(value: str) -> datetime:
    """Parse a git ISO 8601 timestamp into a `datetime`.

    Assumes a trailing `Z` denotes UTC. Replacing it with an explicit offset keeps parsing compatible with
    `datetime.fromisoformat`.
    """

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fallback_verbose_lines(outdated: dict[str, Any]) -> list[str]:
    """Synthesize verbose-style output when Homebrew emits no text lines.

    Assumes the JSON outdated payload remains authoritative for installed and candidate versions. Matching Homebrew's text
    shape keeps downstream token extraction unchanged.
    """

    lines: list[str] = []
    for entry in outdated.get("formulae", []):
        installed = ", ".join(entry.get("installed_versions") or [])
        current = entry.get("current_version") or "unknown"
        lines.append(f"{entry['name']} ({installed}) < {current}")

    for entry in outdated.get("casks", []):
        token = entry.get("name") or entry.get("token")
        if not token:
            continue
        installed = ", ".join(entry.get("installed_versions") or [])
        current = entry.get("current_version") or "unknown"
        lines.append(f"{token} ({installed}) < {current}")

    return lines


def _extract_token(line: str) -> str:
    """Extract the package token from one verbose output line.

    Assumes the token precedes either the first parenthesized version block or the first whitespace separator. This keeps
    lookups aligned with Homebrew's text output variants.
    """

    stripped = line.strip()
    if " (" in stripped:
        return stripped.split(" (", 1)[0]
    return stripped.split(maxsplit=1)[0]


if __name__ == "__main__":
    raise SystemExit(main())
