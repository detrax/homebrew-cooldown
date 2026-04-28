# homebrew-cooldown

`homebrew-cooldown` is a small standalone [`brew_outdated_cooldown.py`](brew_outdated_cooldown.py) script for people who
want to wait a few days before upgrading newly released Homebrew packages.

This can help reduce stability issues and supply chain attacks. Keep Homebrew's convenience without pulling in very
fresh releases the moment they land.

The script shows the age of outdated formulae and casks, then proposes a `brew upgrade ...` command only for leaf
formulae and leaf casks whose own age and transitive Homebrew dependencies are old enough. Leaf formulae are installed
formulae that no other installed formula or cask depends on. Leaf casks are installed casks that no other installed cask
depends on. The default cooldown is 7 days. The script never executes upgrades for you.
For formulae, dependency checks include required dependencies of the current upgrade candidate, including packages that
Homebrew would newly install.

## Quick Start

Prerequisites:

- macOS with Homebrew installed
- Python >= 3.9 in `PATH`
- `git` in `PATH`

Run:

```bash
brew update
./brew_outdated_cooldown.py
```

To use a different cooldown window:

```bash
./brew_outdated_cooldown.py --min-age-days=14
```

## Why

Homebrew makes it easy to upgrade everything right away. That is convenient, but it also means you can pull in
dependency changes that are only hours old.

This script adds a cooldown filter similar in spirit to:

- `uv --exclude-newer-than`
- `pip --uploaded-prior-to`
- Dependabot cooldown windows
- [Package managers need to cool down](https://nesbitt.io/2026/03/04/package-managers-need-to-cool-down.html)

The goal is simple: keep using `brew outdated` and `brew upgrade`, but make it easier to defer upgrades that still sit
too close to a new release or a newly changed dependency chain.

Ideally, `brew` would grow a feature like this. Until then, this script can serve as a proof of concept.

## Example

```bash
brew update
./brew_outdated_cooldown.py --min-age-days=7  # default is also 7 days
```

See [example output](brew_outdated_cooldown_example_output.txt).

That sample reflects the actual state observed on April 12, 2026. In that run, many installed packages were outdated,
but only `b3sum` and `parallel` qualified for the default 7-day cooldown across their transitive runtime dependency
chain.

For example, `node` itself was old enough, but its newest transitive runtime dependency was not, so the script did not
propose upgrading it:

```
node (25.8.2) < 25.9.0_1 (8 days ago, sqlite 0 days ago)
```

## How to Read the Output

- `pkg (old) < new (12 days ago)` means the local Homebrew definition for that upgrade target last changed 12 full days
  ago.
- `pkg ... (10 days ago, dep 1 day ago)` means the package itself is older, but its newest transitive Homebrew
  dependency is only 1 day old.
- `unknown age` means the tap git history could not be resolved for that entry. Unknown ages are excluded from the
  proposed upgrade command.

## What It Does

1. You run `brew update` explicitly when you want fresh tap metadata.
2. The script disables Homebrew auto-update internally for deterministic output while it queries `brew outdated`,
   `brew leaves`, and `brew info --json=v2`.
3. For each outdated formula or cask, it estimates package age from the latest git commit that touched the local
   Homebrew tap definition file.
4. For leaf formulae and installed leaf casks, it proposes upgrades only when:
   - the package age is known;
   - the package age is at least `--min-age-days`;
   - every transitive Homebrew dependency age is known; and
   - the newest transitive Homebrew dependency is also at least `--min-age-days`.
5. It prints three sections:
   - all outdated packages;
   - the leaf formulae and installed leaf casks subset; and
   - a non-executed proposed `brew upgrade ...` command.

If a relevant age cannot be determined, that package is not proposed for upgrade.

## Notes

- The proposed `brew upgrade ...` command is advisory only. Review it, then run it yourself if it matches your risk
  tolerance.
- Package age is an estimate based on the most recent git commit touching the local Homebrew tap definition file.
