# nixard

A terminal UI to explore NixOS packages, inspect real closure costs, and generate ready-to-use Nix declarations.

![nixard screenshot](screenshot.png)

## Why?

On NixOS, installing a package can pull hundreds of dependencies into the Nix store. Most tools only show the full closure size or the download size — they don't explain:

- what is already available locally
- what actually needs downloading
- how much new disk space will really be used
- which packages are responsible for the largest costs

`nixard` solves that — interactively.

This tool is aimed at:
- NixOS newcomers trying to understand closures
- advanced users auditing dependency impact
- storage-conscious systems
- anyone who wants to plan installations before committing

## Features

- 🖥️ Full terminal UI — keyboard-driven, no mouse needed
- 🔍 Browse installed packages or search `cache.nixos.org` candidates
- 📦 Real closure analysis — distinguishes packages in your active profile, in the store (GC-able), or fully absent
- 💾 Calculates actual additional disk usage and download size
- ⚡ Concurrent closure crawling via `cache.nixos.org` for fast analysis
- 🏠 Flake-aware — detects `flake.nix` and evaluates scopes accordingly
- 🎯 Mark packages and export ready-to-use `.nixard` declaration files
- 📋 Export includes `environment.systemPackages`, `home.packages`, `nix-shell` and `nix profile install` snippets
- 🕓 Persistent export history with restore and re-export support
- 🔄 Restore previous selections and extend them with new packages

## Installation

### Try instantly (without installing)

```bash
nix run github:manelinux/nixard
```

Or enter a temporary shell:

```bash
nix shell github:manelinux/nixard
```

### Install permanently

#### Flake / profile install

```bash
nix profile install github:manelinux/nixard
```

#### Legacy nix-env

```bash
git clone https://github.com/manelinux/nixard.git
cd nixard
nix-env -i -f .
```

After installation, launch with:

```bash
nixard
```

## Usage

nixard is fully interactive — just launch it and navigate with the keyboard.

### Key bindings

| Key | Action |
|-----|--------|
| `↑ ↓` | Navigate package list |
| `Space` | Mark / unmark package for export |
| `Enter` | Select package and inspect closure |
| `e` | Open export panel for marked packages |
| `h` | Open export history |
| `r` | Reload local system data |
| `Esc` | Reset to initial state |
| `q` | Quit |

### Scopes (left panel)

nixard shows where each package is present across your system:

| Scope | Source |
|-------|--------|
| `System (active)` | `/run/current-system` — currently active generation |
| `User profile (user)` | `nix profile install` packages for that user |
| `Home Manager (user)` | Packages managed via `home.packages` |
| `Config: system pkgs` | Declared in `environment.systemPackages` |
| `Config: user <name>` | Declared in `users.users.<name>.packages` |

Flake-based configurations are detected automatically.

## The `.nixard` export file

When you mark packages and export, nixard generates a `.nixard` file — a plain text, commented, copy-paste-ready declaration:

```nix
# nixard export — "dev-tools"
# Generated: 2026-05-27 23:50
# Packages: 3
#
# Note: download and disk totals shown in the export panel.
# Copy the relevant section into your NixOS configuration.

# ── environment.systemPackages (configuration.nix or flake nixosConfiguration) ──
environment.systemPackages = with pkgs; [
  ripgrep
  fd
  bat
];

# ── home.packages (home-manager home.nix) ──
home.packages = with pkgs; [
  ripgrep
  fd
  bat
];

# ── nix-shell (try without installing) ──
nix-shell -p ripgrep fd bat

# ── nix profile install (per-user installation) ──
nix profile install nixpkgs#ripgrep nixpkgs#fd nixpkgs#bat
```

Export history is stored at `~/.local/share/nixard/history.json` and survives across sessions. You can restore any previous selection, extend it with new packages, and re-export.

## How it works

nixard:
- scans `/nix/store`, the active profile, Home Manager and `configuration.nix` / `flake.nix` on startup
- resolves store hashes via `nix path-info --derivation` + `nix show-derivation`, with fallback to `nix-instantiate`
- recursively crawls `.narinfo` metadata from `cache.nixos.org` with concurrent requests
- reconstructs the full dependency closure
- compares it against the local store and active profile to compute the real incremental cost

No builds are performed. No system state is modified.

## Requirements

- NixOS (or Nix on Linux)
- Python 3
- [`textual`](https://github.com/Textualize/textual) Python library
- Internet access to `cache.nixos.org`
- Nix experimental features: `nix-command flakes` (recommended)

## Contributing

Pull requests and issues are welcome.

Ideas for future improvements:
- `--json` output mode
- Side-by-side package comparison
- Dependency graph visualization
- `--why-depends` to trace the heaviest dependency chain
- Local narinfo cache for faster repeated queries
- Tag and filter exports by category

## License

MIT
