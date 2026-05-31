#!/usr/bin/env python3
"""
nixard.py  –  Nixard v2.0.0
TUI per explorar paquets NixOS amb anàlisi de closure real via cache.nixos.org.

Lògica de closures: portada de nix-deps.py (autor original).
Integració TUI: nixard.py.
"""

import asyncio
import json
import os
import re
import subprocess
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from textual.app import App, ComposeResult, Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.screen import ModalScreen, Screen
from textual.widgets import Header, Footer, Label, Input, DataTable, RadioSet, RadioButton, LoadingIndicator, Static, TextArea, Select, Button

VERSION = "2.0.0"
NIXOS_CONFIG = "/etc/nixos/configuration.nix"
FLAKE_PATHS = ["/etc/nixos/flake.nix", "/root/flake.nix"]


def get_hostname():
    out, rc = run_cmd(["hostname", "-s"])
    return out.strip() if rc == 0 and out.strip() else "default"


def find_flake():
    """Retorna el directori del flake si existeix, o None."""
    for p in FLAKE_PATHS:
        if os.path.exists(p):
            return os.path.dirname(p)
    return None


# ──────────────────────────────────────────────
#  Utilitats de sistema
# ──────────────────────────────────────────────

def run_cmd(cmd, **kw):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, **kw)
        return result.stdout.strip(), result.returncode
    except Exception:
        return "", -1


def human_bytes(n):
    if n is None or n == 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ──────────────────────────────────────────────
#  Store local i perfil
# ──────────────────────────────────────────────

def get_local_paths():
    """Retorna (noms_complets, noms_sense_hash) del /nix/store local."""
    paths, names_only = set(), set()
    store = Path("/nix/store")
    if store.exists():
        try:
            for p in store.iterdir():
                if p.is_dir():
                    paths.add(p.name)
                    parts = p.name.split("-", 1)
                    if len(parts) == 2:
                        names_only.add(parts[1])
        except Exception:
            pass
    return paths, names_only


def get_profile_paths():
    """Retorna (noms_complets, noms_sense_hash) dels paquets actius al perfil."""
    profile_names, profile_names_only = set(), set()

    out, rc = run_cmd([
        "nix", "profile", "list", "--json",
        "--extra-experimental-features", "nix-command flakes",
    ], timeout=15)
    if rc == 0 and out.strip():
        try:
            data = json.loads(out)
            elements = data.get("elements", [])
            items = elements if isinstance(elements, list) else list(elements.values())
            for el in items:
                for sp in el.get("storePaths", []):
                    name = os.path.basename(sp)
                    profile_names.add(name)
                    parts = name.split("-", 1)
                    if len(parts) == 2:
                        profile_names_only.add(parts[1])
            if profile_names:
                return profile_names, profile_names_only
        except (json.JSONDecodeError, AttributeError):
            pass

    # Fallback via ~/.nix-profile
    nix_profile = Path(os.environ.get("NIX_PROFILE", Path.home() / ".nix-profile"))
    store = Path("/nix/store")
    if nix_profile.exists() and store.exists():
        try:
            bin_dir = nix_profile / "bin"
            for p in (bin_dir.iterdir() if bin_dir.exists() else []):
                if p.is_symlink():
                    target = p.resolve()
                    if str(target).startswith("/nix/store/"):
                        parts_path = target.parts
                        if len(parts_path) > 3:
                            name = parts_path[3]
                            profile_names.add(name)
                            np = name.split("-", 1)
                            if len(np) == 2:
                                profile_names_only.add(np[1])
        except Exception:
            pass

    return profile_names, profile_names_only


def find_local_installed_version(pkg_name):
    store = Path("/nix/store")
    if not store.exists():
        return None, None
    best_path, best_version = None, None
    pattern = re.compile(rf"^[a-z0-9]{{32}}-{re.escape(pkg_name)}-([\d\.]+[^/]*)$")
    try:
        for p in store.iterdir():
            if p.is_dir():
                m = pattern.match(p.name)
                if m:
                    ver = m.group(1)
                    if not best_version or ver > best_version:
                        best_version, best_path = ver, str(p)
    except Exception:
        pass
    return best_path, best_version


def find_local_store_hash(pkg_name, version=""):
    store = Path("/nix/store")
    if not store.exists():
        return None
    if version:
        pattern = re.compile(rf"^([a-z0-9]{{32}})-{re.escape(pkg_name)}-{re.escape(version)}$")
    else:
        pattern = re.compile(rf"^([a-z0-9]{{32}})-{re.escape(pkg_name)}(?:-[\d\.].*)?$")
    try:
        for p in store.iterdir():
            if p.is_dir():
                m = pattern.match(p.name)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


def pkgs_from_store(store_path) -> set:
    try:
        result = subprocess.run(
            ["nix", "path-info", "--recursive", store_path],
            capture_output=True, text=True, check=True,
        )
        return {
            line.replace("/nix/store/", "").split("-", 1)[1]
            for line in result.stdout.splitlines()
            if line.startswith("/nix/store/") and "-" in line
        }
    except Exception:
        return set()


def _nix_pkg_list_expr(attr_path, config_expr):
    """Expressió Nix comuna per obtenir noms de paquets d'un atribut."""
    return (
        f'let sys = {config_expr};'
        f' pkgName = p: if builtins.isString p then p else p.pname or (builtins.parseDrvName p.name).name;'
        f' in builtins.concatStringsSep "\\n"'
        f' (builtins.filter (n: n != null)'
        f' (builtins.map pkgName (sys.config.{attr_path} or [])))'
    )


def _collect_nix_files(base_dir: str) -> list:
    """Recull recursivament tots els fitxers .nix d'un directori (exclou .nixard-backup)."""
    result = []
    try:
        for p in Path(base_dir).rglob("*.nix"):
            if ".nixard-backup" not in p.name and p.is_file():
                result.append(str(p))
    except Exception:
        pass
    return result


def _find_balanced_block(text: str, start: int) -> str:
    """
    Donada una posició on comença '{', retorna el contingut fins al '}' balancejat.
    """
    depth = 0
    i = start
    while i < len(text):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
        i += 1
    return text[start + 1:]  # fallback si no es tanca


def _tokens_from_list_block(block: str) -> set:
    """Extreu tokens vàlids d'un bloc [ ... ]."""
    pkgs = set()
    for token in re.split(r'[\s\n]+', block):
        token = token.strip()
        if token and not token.startswith('(') and '.' not in token \
                and token not in ('', 'pkgs', 'with', 'let', 'in'):
            pkgs.add(token)
    return pkgs


def _extract_pkg_list_from_block(text: str, attr: str) -> set:
    """
    Extreu els noms de paquets d'un bloc Nix de la forma:
      attr = with pkgs; [ pkg1 pkg2 ... ];
      attr = [ pkg1 pkg2 ... ];
    Suporta llistes multilínia, blocs anidats (users.users.X = { packages = [...]; })
    i ignora comentaris.
    """
    # Elimina comentaris de línia
    text_nc = re.sub(r'#[^\n]*', '', text)

    parts = attr.rsplit('.', 1)
    if len(parts) == 2:
        parent, key = parts
        # Cerca el bloc pare amb claus balancejades
        parent_re = re.compile(rf'{re.escape(parent)}\s*=\s*{{', re.DOTALL)
        pkgs = set()
        for pm in parent_re.finditer(text_nc):
            brace_start = pm.end() - 1  # posició del '{'
            inner = _find_balanced_block(text_nc, brace_start)
            inner_pattern = re.compile(
                rf'{re.escape(key)}\s*=\s*(?:with\s+[^;\[]+;\s*)?\[([^\]]*?)\]',
                re.DOTALL
            )
            for m in inner_pattern.finditer(inner):
                pkgs.update(_tokens_from_list_block(m.group(1)))
        if pkgs:
            return pkgs

    # Cerca directa: attr = [ ... ] o attr = with pkgs; [ ... ]
    pattern = re.compile(
        rf'{re.escape(attr)}\s*=\s*(?:with\s+[^;\[]+;\s*)?\[([^\]]*?)\]',
        re.DOTALL
    )
    pkgs = set()
    for m in pattern.finditer(text_nc):
        pkgs.update(_tokens_from_list_block(m.group(1)))
    return pkgs


def parse_packages_from_nix_files(nix_dir: str, attr_path: str) -> set:
    """
    Fallback: llegeix els fitxers .nix directament i extreu paquets per parsing de text.
    Suporta:
      - environment.systemPackages
      - users.users.<nom>.packages
      - home.packages  (home-manager)
    """
    files = _collect_nix_files(nix_dir)
    pkgs = set()

    # Determinem quin patró buscar
    if attr_path == "environment.systemPackages":
        attrs_to_find = ["environment.systemPackages"]
    elif attr_path.startswith("users.users.") and attr_path.endswith(".packages"):
        username = attr_path.split(".")[2]
        attrs_to_find = [
            f"users.users.{username}.packages",
            "home.packages",  # home-manager inline
        ]
    else:
        attrs_to_find = [attr_path]

    for filepath in files:
        try:
            content = Path(filepath).read_text(errors='replace')
            for attr in attrs_to_find:
                pkgs.update(_extract_pkg_list_from_block(content, attr))
        except Exception:
            pass

    return pkgs


def get_packages_from_nix_config(attr_path) -> set:
    expr = _nix_pkg_list_expr(attr_path, f'import <nixpkgs/nixos> {{ configuration = {NIXOS_CONFIG}; }}')
    try:
        result = subprocess.run(
            ["nix", "eval", "--impure", "--raw", "--expr", expr],
            capture_output=True, text=True, check=True,
        )
        pkgs = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        if pkgs:
            return pkgs
    except Exception:
        pass
    # Fallback: parsing de text
    return parse_packages_from_nix_files(os.path.dirname(NIXOS_CONFIG), attr_path)


def get_packages_from_flake(flake_dir, attr_path) -> set:
    hostname = get_hostname()
    expr = _nix_pkg_list_expr(
        attr_path,
        f'(builtins.getFlake "{flake_dir}").nixosConfigurations."{hostname}"'
    )
    try:
        result = subprocess.run(
            ["nix", "eval", "--impure", "--raw", "--expr", expr,
             "--extra-experimental-features", "nix-command flakes"],
            capture_output=True, text=True, check=True,
        )
        pkgs = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        if pkgs:
            return pkgs
    except Exception:
        pass
    # Fallback: parsing de text
    return parse_packages_from_nix_files(flake_dir, attr_path)


def get_flake_users(flake_dir) -> set:
    """Retorna els usuaris declarats al flake."""
    hostname = get_hostname()
    nix_expr = (
        f'let sys = (builtins.getFlake "{flake_dir}").nixosConfigurations."{hostname}";'
        f' in builtins.concatStringsSep "\\n" (builtins.attrNames (sys.config.users.users or {{}}))'
    )
    try:
        result = subprocess.run(
            ["nix", "eval", "--impure", "--raw", "--expr", nix_expr,
             "--extra-experimental-features", "nix-command flakes"],
            capture_output=True, text=True, check=True,
        )
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}
    except Exception:
        return set()


def build_version_map() -> dict:
    """Construeix un mapa nom_base → versió des de /run/current-system."""
    try:
        result = subprocess.run(
            ["nix", "path-info", "--recursive", "/run/current-system"],
            capture_output=True, text=True, timeout=30
        )
        vmap = {}
        ver_pattern = re.compile(r'^[a-z0-9]{32}-(.+?)-([\d][\d\w\.\-]*)$')
        for line in result.stdout.splitlines():
            basename = os.path.basename(line.strip())
            m = ver_pattern.match(basename)
            if m:
                vmap[m.group(1)] = m.group(2)
        return vmap
    except Exception:
        return {}




# ──────────────────────────────────────────────
#  Cache.nixos.org – resolució de closure real
# ──────────────────────────────────────────────

def fetch_narinfo(nar_hash):
    """Consulta cache.nixos.org/{hash}.narinfo i retorna metadades."""
    url = f"https://cache.nixos.org/{nar_hash}.narinfo"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nixard-claude/2.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode()
        file_size, nar_size, references, store_path = 0, 0, [], ""
        for line in content.splitlines():
            if line.startswith("StorePath:"):
                store_path = line.split(":", 1)[1].strip()
            elif line.startswith("FileSize:"):
                file_size = int(line.split(":", 1)[1].strip())
            elif line.startswith("NarSize:"):
                nar_size = int(line.split(":", 1)[1].strip())
            elif line.startswith("References:"):
                refs_raw = line.split(":", 1)[1].strip()
                if refs_raw:
                    references = refs_raw.split()
        return {"store_path": store_path, "file_size": file_size,
                "nar_size": nar_size, "references": references}
    except Exception:
        return None


def resolve_remote_closure(root_hash):
    """Recorre recursivament el graf de dependències via cache.nixos.org."""
    closure, to_visit, visited = {}, [root_hash], set()
    while to_visit:
        batch = [h for h in to_visit if h not in visited]
        to_visit = []
        if not batch:
            break
        with ThreadPoolExecutor(max_workers=15) as ex:
            futures = {ex.submit(fetch_narinfo, h): h for h in batch}
            for future in as_completed(futures):
                h = futures[future]
                visited.add(h)
                res = future.result()
                if res:
                    closure[h] = res
                    for ref_name in res["references"]:
                        ref_hash = ref_name.split("-")[0]
                        if ref_hash not in visited and ref_hash not in to_visit:
                            to_visit.append(ref_hash)
    return closure


def get_root_hash(attr_name, remote_version, pkg_name):
    """Resol el hash del store per a un paquet (local → derivació → fallback)."""
    # 1. Local
    local_hash = find_local_store_hash(pkg_name, remote_version)
    if local_hash:
        return local_hash

    # 2. nix path-info --derivation + show-derivation
    out, rc = run_cmd([
        "nix", "path-info", "--derivation", f"nixpkgs#{attr_name}",
        "--extra-experimental-features", "nix-command flakes",
    ], timeout=30)
    if rc == 0 and out:
        drv_path = out.strip().splitlines()[0]
        out2, rc2 = run_cmd([
            "nix", "show-derivation", drv_path,
            "--extra-experimental-features", "nix-command flakes",
        ], timeout=30)
        if rc2 == 0 and out2:
            try:
                drv_data = json.loads(out2)
                # NixOS 26.05+: {"derivations": {"hash.drv": {...}}, "version": 4}
                # NixOS <26.05:  {"hash.drv": {...}}
                drv_entries = drv_data.get("derivations", drv_data)
                for drv_info in drv_entries.values():
                    if not isinstance(drv_info, dict):
                        continue
                    out_val = drv_info.get("outputs", {}).get("out", {})
                    if not isinstance(out_val, dict):
                        continue
                    out_path = out_val.get("path", "")
                    if out_path:
                        # 26.05: path es "hash-name" sense /nix/store/
                        # <26.05: path es "/nix/store/hash-name"
                        return os.path.basename(out_path).split("-")[0]
            except json.JSONDecodeError:
                pass

    # 3. nix-instantiate fallback
    out3, rc3 = run_cmd(
        ["nix-instantiate", "--eval", "-E", f'(import <nixpkgs> {{}}).{attr_name}.outPath']
    )
    if rc3 == 0 and out3:
        return os.path.basename(out3.strip().strip('"')).split("-")[0]

    return None


def find_store_path_for_pkg(pkg_name):
    """Troba el path real al /nix/store per a un paquet instal·lat (no .drv, no source)."""
    store = Path("/nix/store")
    pattern = re.compile(rf"^[a-z0-9]{{32}}-{re.escape(pkg_name)}(?:-[\d\.].*)?$")
    try:
        for p in store.iterdir():
            if p.is_dir() and not p.name.endswith(".drv") and "source" not in p.name:
                if pattern.match(p.name):
                    return str(p)
    except Exception:
        pass
    return None


def resolve_local_closure(store_path):
    """Analitza el closure d'un paquet instal·lat localment via nix-store -qR."""
    out, rc = run_cmd(["nix-store", "-qR", store_path], timeout=60)
    if rc != 0 or not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def get_local_nar_size(store_path):
    """Obté la mida real d'un path del store via nix path-info -s."""
    out, rc = run_cmd(["nix", "path-info", "-s", store_path,
                       "--extra-experimental-features", "nix-command"], timeout=10)
    if rc == 0 and out:
        # format: /nix/store/...\t<bytes>
        parts = out.split()
        if len(parts) >= 2:
            try:
                return int(parts[-1])
            except ValueError:
                pass
    # Fallback: du
    out2, rc2 = run_cmd(["du", "-sb", store_path])
    if rc2 == 0 and out2:
        try:
            return int(out2.split()[0])
        except (ValueError, IndexError):
            pass
    return 0


def get_real_closure_size(attr_name):
    """Utilitza `nix build --dry-run` per obtenir la mida real expandida del closure."""
    try:
        result = subprocess.run(
            ["nix", "build", "--dry-run", f"nixpkgs#{attr_name}"],
            capture_output=True, text=True,
        )
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        m = re.search(
            r'[\d,]+\s+paths will be fetched\s+\(([\d\.]+\s+[KMG]iB)\s+download,\s+([\d\.]+\s+[KMG]iB)\s+unpacked\)',
            combined,
        )
        if not m:
            return None
        value, unit = m.group(2).split()
        units = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
        return float(value.replace(",", "")) * units[unit]
    except Exception:
        return None


# ──────────────────────────────────────────────
#  Detecció del canal/branca real del sistema
# ──────────────────────────────────────────────

_SYSTEM_CHANNEL_CACHE = None

def detect_system_channel() -> str:
    """
    Detecta la branca de nixpkgs que usa el sistema real, en aquest ordre:
      1. nix-channel --list         → font autoritativa per a sistemes sense flakes
      2. /run/current-system        → llegeix el channel del closure
      3. nix registry list          → per a sistemes amb flakes
      4. /etc/os-release            → VERSION_ID per deduir la branca
      5. Fallback: "nixos-unstable"
    Retorna una cadena del tipus "nixos-24.11", "nixos-25.11", "nixos-unstable", etc.
    """
    global _SYSTEM_CHANNEL_CACHE
    if _SYSTEM_CHANNEL_CACHE is not None:
        return _SYSTEM_CHANNEL_CACHE

    # 1. nix-channel --list: font directa i fiable per a sistemes clàssics (no flakes)
    # Format: "nixos https://nixos.org/channels/nixos-25.11"
    out, rc = run_cmd(["nix-channel", "--list"])
    if rc == 0 and out:
        for line in out.splitlines():
            # Busquem la línia que conté el channel del sistema (nixos o nixpkgs)
            m = re.search(r'https?://[^/]+/channels/(nixos-[\w\.\-]+)', line)
            if m:
                branch = m.group(1)
                _SYSTEM_CHANNEL_CACHE = branch
                return _SYSTEM_CHANNEL_CACHE

    # 2. /run/current-system store path
    try:
        link = os.path.realpath("/run/current-system")
        m = re.search(r'nixos-system-[^/]+-(\d+\.\d+)', link)
        if m:
            _SYSTEM_CHANNEL_CACHE = f"nixos-{m.group(1)}"
            return _SYSTEM_CHANNEL_CACHE
    except Exception:
        pass

    # 3. nix registry list (sistemes amb flakes)
    out, rc = run_cmd(
        ["nix", "registry", "list", "--extra-experimental-features", "nix-command flakes"],
        timeout=10,
    )
    if rc == 0 and out:
        for line in out.splitlines():
            m = re.search(r'NixOS/nixpkgs/(nixos-[\w\.\-]+)', line)
            if m:
                branch = m.group(1)
                _SYSTEM_CHANNEL_CACHE = branch
                return _SYSTEM_CHANNEL_CACHE

    # 4. /etc/os-release → VERSION_ID
    try:
        content = Path("/etc/os-release").read_text()
        m = re.search(r'^VERSION_ID="?([^"\n]+)"?', content, re.MULTILINE)
        if m:
            vid = m.group(1).strip()
            if vid and re.match(r'^\d+\.\d+', vid):
                _SYSTEM_CHANNEL_CACHE = f"nixos-{vid}"
                return _SYSTEM_CHANNEL_CACHE
    except Exception:
        pass

    # 5. Fallback
    _SYSTEM_CHANNEL_CACHE = "nixos-unstable"
    return _SYSTEM_CHANNEL_CACHE


def _get_system_nixpkgs_flake_ref() -> str:
    """
    Retorna la referència de nixpkgs del sistema per a 'nix search'.
    Prioritza el path del sistema actual si el podem trobar al registre,
    sinó usa la URL de github amb la branca correcta.
    """
    # Intenta trobar el nixpkgs pinnat al registre del sistema
    out, rc = run_cmd(
        ["nix", "registry", "list", "--extra-experimental-features", "nix-command flakes"],
        timeout=10,
    )
    if rc == 0 and out:
        for line in out.splitlines():
            # Preferim l'entrada de sistema ('system') sobre la global
            if line.startswith("system") and ("nixpkgs" in line or "nixos" in line):
                parts = line.split()
                if len(parts) >= 3:
                    return parts[2]  # ex: "path:/nix/store/xxxx-source"
        # Si no hi ha entrada de sistema, agafem la global
        for line in out.splitlines():
            if line.startswith("global") and "flake:nixpkgs" in line:
                parts = line.split()
                if len(parts) >= 3:
                    ref = parts[2]
                    # Si apunta a github amb branca, la retornem directament
                    if "github:" in ref:
                        return ref

    # Fallback: construïm la ref a partir de la branca detectada
    channel = detect_system_channel()
    return f"github:NixOS/nixpkgs/{channel}"


# ──────────────────────────────────────────────
#  Cerca de paquets
# ──────────────────────────────────────────────

def search_cache_candidates(keyword):
    """Cerca candidats a nixpkgs via search.nixos.org (canal del sistema) o nix search CLI."""
    # Prioritzem l'API web perquè retorna sempre el canal correcte del sistema,
    # sense dependre de la cache local de nix (que pot tenir dades d'unstable).
    results = _search_via_json_index(keyword)
    if results:
        return results
    return _search_via_nix_cli(keyword)


def _search_via_nix_cli(keyword):
    # Usem el nixpkgs real del sistema, no el 'nixpkgs' del registre global
    nixpkgs_ref = _get_system_nixpkgs_flake_ref()
    out, rc = run_cmd(
        ["nix", "search", nixpkgs_ref, keyword, "--json",
         "--extra-experimental-features", "nix-command flakes"],
        timeout=30,
    )
    if rc != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    results = []
    for attr, info in data.items():
        parts = attr.split(".")
        if "legacyPackages" in parts:
            idx = parts.index("legacyPackages")
            attr_clean = ".".join(parts[idx + 2:])
        else:
            attr_clean = parts[-1] if parts else attr
        if keyword.lower() not in attr_clean.lower():
            continue
        results.append({
            "attr": attr_clean or attr,
            "name": attr_clean,
            "version": info.get("version", ""),
            "desc": info.get("description", ""),
        })
        if len(results) >= 40:
            break
    return results


def _search_via_json_index(keyword, channel=None):
    # Si no es passa channel explícit, usem el del sistema
    if channel is None:
        channel = detect_system_channel()
    url = (f"https://search.nixos.org/api/search"
           f"?channel={channel}&query={keyword}&type=packages&size=40")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nixard-claude/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return []
    results = []
    for hit in data.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        pkg_name = src.get("package_attr_name", "")
        if keyword.lower() not in pkg_name.lower():
            continue
        results.append({
            "attr": pkg_name,
            "name": pkg_name,
            "version": src.get("package_version", ""),
            "desc": src.get("package_description", ""),
        })
    return results


# ──────────────────────────────────────────────
#  History management
# ──────────────────────────────────────────────

HISTORY_PATH = Path.home() / ".local" / "share" / "nixard" / "history.json"
NIXARD_TMP_DIR = Path.home() / ".local" / "share" / "nixard" / "tmp"

# Directoris on cercar fitxers .nix
NIX_SEARCH_DIRS = [
    "/etc/nixos",
    str(Path.home() / ".config" / "home-manager"),
    str(Path.home() / ".config" / "nixpkgs"),
    "/root/.config/home-manager",
    "/root/.config/nixpkgs",
]


def find_nix_files_on_system() -> list:
    """Retorna una llista de paths absoluts de fitxers .nix trobats als directoris habituals."""
    found = []
    seen = set()
    # Afegim també el directori del flake si existeix
    flake_dir = find_flake()
    search_dirs = list(NIX_SEARCH_DIRS)
    if flake_dir and flake_dir not in search_dirs:
        search_dirs.insert(0, flake_dir)
    for d in search_dirs:
        p = Path(d)
        try:
            if not p.exists():
                continue
        except Exception:
            continue
        try:
            for f in sorted(p.rglob("*.nix")):
                if f.is_file() and ".nixard-backup" not in f.name:
                    real = str(f.resolve())
                    if real not in seen:
                        seen.add(real)
                        found.append(str(f))
        except Exception:
            pass
    return found


def _generate_nixard_content(marked_list: list) -> str:
    """Genera el contingut d'un fitxer .nixard a partir de la llista de paquets marcats."""
    from datetime import datetime
    pkg_names = [p[0] for p in marked_list]
    pkg_names_clean = []
    for pkg_name, pkg_meta in marked_list:
        if pkg_meta and pkg_meta.get("attr") and pkg_meta["attr"] != pkg_name:
            pkg_names_clean.append(pkg_meta["attr"])
        else:
            base = re.sub(r'-[\d]+[\d\w\.\-]*$', '', pkg_name)
            pkg_names_clean.append(
                base if base and len(base) > 2 and base != pkg_name.split("-")[0] else pkg_name
            )
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# nixard export",
        f"# Generated: {now}",
        f"# Packages: {len(pkg_names)}",
        f"#",
        f"# Copy the relevant section into your NixOS configuration.",
        f"",
        f"# ── environment.systemPackages (configuration.nix or flake nixosConfiguration) ──",
        f"environment.systemPackages = with pkgs; [",
    ]
    for name in pkg_names_clean:
        lines.append(f"  {name}")
    lines += [
        f"];",
        f"",
        f"# ── home.packages (home-manager home.nix) ──",
        f"home.packages = with pkgs; [",
    ]
    for name in pkg_names_clean:
        lines.append(f"  {name}")
    lines += [
        f"];",
        f"",
        f"# ── nix-shell (try without installing) ──",
        f"nix-shell -p {' '.join(pkg_names_clean)}",
        f"",
        f"# ── nix profile install (per-user installation) ──",
        f"nix profile install {' '.join(f'nixpkgs#{n}' for n in pkg_names_clean)}",
        f"",
    ]
    return "\n".join(lines), pkg_names_clean, pkg_names, now


def load_history() -> list:
    """Load export history from JSON. Returns list of entries, newest first."""
    try:
        if HISTORY_PATH.exists():
            with open(HISTORY_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def save_history_entry(entry: dict):
    """Append a new entry to the history JSON file."""
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        history = load_history()
        history.insert(0, entry)
        with open(HISTORY_PATH, "w") as f:
            json.dump(history, f, indent=2)
    except Exception:
        pass


# ──────────────────────────────────────────────
#  Modal d'exportació
# ──────────────────────────────────────────────

class ExportModal(ModalScreen):
    """Modal per revisar paquets marcats i exportar el .nixard."""

    CSS = """
    ExportModal {
        align: center middle;
    }
    #export-dialog {
        width: 80%;
        height: 80%;
        background: #111111;
        border: tall $accent;
        padding: 1 2;
    }
    #export-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #export-summary {
        height: auto;
        margin-bottom: 1;
    }
    #export-pkg-list {
        height: 1fr;
        border: tall $panel;
        padding: 0 1;
        overflow-y: auto;
        margin-bottom: 1;
    }
    #export-totals {
        height: auto;
        margin-bottom: 1;
        color: $text;
    }
    #export-name-row {
        layout: horizontal;
        height: 3;
        margin-bottom: 1;
    }
    #export-path-row {
        layout: horizontal;
        height: 3;
        margin-bottom: 1;
    }
    #export-name-label {
        width: auto;
        padding: 1 1 0 0;
    }
    #export-path-label {
        width: auto;
        padding: 1 1 0 0;
    }
    #export-name-input {
        width: 1fr;
        height: 3;
    }
    #export-path-input {
        width: 1fr;
        height: 3;
    }
    #export-hint {
        color: $text-muted;
    }
    #export-loading {
        height: 3;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Cancel", priority=True),
        Binding("enter", "export", "Export", priority=True),
    ]

    def __init__(self, marked_packages: list, local_store_names: set,
                 local_store_names_only: set, profile_names: set, profile_names_only: set):
        super().__init__()
        self.marked_packages = marked_packages
        self._local_store_names = local_store_names
        self._local_store_names_only = local_store_names_only
        self._profile_names = profile_names
        self._profile_names_only = profile_names_only
        self._closure_data = {}
        self._total_download = 0
        self._total_expanded = 0
        self._default_dir = str(Path(__file__).parent)

    def compose(self) -> ComposeResult:
        with Vertical(id="export-dialog"):
            yield Label("NIXARD EXPORT", id="export-title")
            yield Label(
                f"{len(self.marked_packages)} package(s) selected",
                id="export-summary"
            )
            with ScrollableContainer(id="export-pkg-list"):
                yield Label("[dim]Calculating closures...[/dim]", id="export-pkg-content")
            yield LoadingIndicator(id="export-loading")
            yield Label("", id="export-totals")
            with Horizontal(id="export-name-row"):
                yield Label("Filename:", id="export-name-label")
                yield Input(placeholder="my-packages", id="export-name-input")
            with Horizontal(id="export-path-row"):
                yield Label("Save to: ", id="export-path-label")
                yield Input(value=self._default_dir, id="export-path-input")
            yield Label(
                "Tab → switch fields   Enter → export .nixard   Esc → cancel",
                id="export-hint"
            )

    def on_mount(self):
        self.run_worker(self._calculate_closures(), exclusive=True)

    async def _calculate_closures(self):
        lines = []
        total_dl = 0
        total_exp = 0

        for pkg_name, pkg_meta in self.marked_packages:
            attr_name = pkg_meta.get("attr", pkg_name) if pkg_meta else pkg_name
            remote_version = pkg_meta.get("version", "") if pkg_meta else ""
            base_name = re.sub(r'-[\d][\d\w\.\-]*$', '', pkg_name)

            # Via A: instal·lat localment
            local_store_path = await asyncio.to_thread(find_store_path_for_pkg, pkg_name)
            if not local_store_path:
                local_store_path = await asyncio.to_thread(find_store_path_for_pkg, base_name)

            if local_store_path:
                deps_list = await asyncio.to_thread(resolve_local_closure, local_store_path)
                pkg_exp = 0
                for dep_path in deps_list:
                    pkg_exp += await asyncio.to_thread(get_local_nar_size, dep_path)
                self._closure_data[pkg_name] = {"download": 0, "expanded": pkg_exp}
                total_exp += pkg_exp
                lines.append(
                    f"  [bold]●[/bold] {pkg_name:<40} "
                    f"[dim]already installed  expanded: {human_bytes(pkg_exp)}[/dim]"
                )
            else:
                # Via B: cache remota
                root_hash = await asyncio.to_thread(get_root_hash, attr_name, remote_version, pkg_name)
                if root_hash:
                    closure = await asyncio.to_thread(resolve_remote_closure, root_hash)
                    pkg_dl = pkg_exp = 0
                    for h, data in closure.items():
                        path_name = os.path.basename(data["store_path"])
                        pkg_name_only = "-".join(path_name.split("-")[1:])
                        in_store = (path_name in self._local_store_names or
                                    pkg_name_only in self._local_store_names_only)
                        if not in_store:
                            pkg_dl += data["file_size"]
                            pkg_exp += data["nar_size"]
                    self._closure_data[pkg_name] = {"download": pkg_dl, "expanded": pkg_exp}
                    total_dl += pkg_dl
                    total_exp += pkg_exp
                    lines.append(
                        f"  [bold]●[/bold] {pkg_name:<40} "
                        f"[dim]dl: {human_bytes(pkg_dl):<10} expanded: {human_bytes(pkg_exp)}[/dim]"
                    )
                else:
                    lines.append(f"  [bold]●[/bold] {pkg_name:<40} [dim]could not resolve[/dim]")

            # Actualitzem la llista mentre va calculant
            self.query_one("#export-pkg-content", Label).update("\n".join(lines))

        self._total_download = total_dl
        self._total_expanded = total_exp
        self.query_one("#export-loading").display = False
        self.query_one("#export-totals", Label).update(
            f"  [bold]Total download:[/bold]  {human_bytes(total_dl)}\n"
            f"  [bold]Total on disk:[/bold]   {human_bytes(total_exp)} (expanded)"
        )

    def action_export(self):
        name = self.query_one("#export-name-input", Input).value.strip() or "nixard-export"
        path = self.query_one("#export-path-input", Input).value.strip() or self._default_dir
        self.dismiss((name, path))

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "export-name-input":
            # Tab al path
            self.query_one("#export-path-input", Input).focus()
        else:
            self.action_export()




# ──────────────────────────────────────────────
#  History modal
# ──────────────────────────────────────────────

class HistoryModal(ModalScreen):
    """Modal to browse and restore previous exports."""

    CSS = """
    HistoryModal {
        align: center middle;
    }
    #history-dialog {
        width: 80%;
        height: 80%;
        background: #111111;
        border: tall $accent;
        padding: 1 2;
    }
    #history-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #history-list {
        height: 1fr;
        border: tall $panel;
        margin-bottom: 1;
    }
    #history-detail {
        height: auto;
        min-height: 4;
        padding: 0 1;
        margin-bottom: 1;
        color: $text;
    }
    #history-hint {
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("d", "delete_entry", "Delete entry", priority=True),
    ]

    def __init__(self):
        super().__init__()
        self.history = load_history()
        self._selected_index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="history-dialog"):
            yield Label("NIXARD HISTORY", id="history-title")
            yield DataTable(id="history-list")
            yield Label("", id="history-detail")
            yield Label(
                "Enter → restore   d → delete entry   Esc → close",
                id="history-hint"
            )

    def on_mount(self):
        table = self.query_one("#history-list", DataTable)
        table.add_columns("Date", "Name", "Packages", "Path")
        table.cursor_type = "row"
        table.zebra_stripes = False
        if not self.history:
            table.add_row("—", "No exports yet", "—", "—")
        else:
            for entry in self.history:
                table.add_row(
                    entry.get("date", "—"),
                    entry.get("name", "—"),
                    str(entry.get("package_count", "—")),
                    entry.get("path", "—"),
                )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._selected_index = event.cursor_row
        self._update_detail()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_restore()

    def _update_detail(self):
        detail = self.query_one("#history-detail", Label)
        if not self.history or self._selected_index >= len(self.history):
            detail.update("")
            return
        entry = self.history[self._selected_index]
        pkgs = entry.get("packages", [])
        pkg_str = "  " + "  ".join(pkgs[:10])
        if len(pkgs) > 10:
            pkg_str += f"  [dim]... and {len(pkgs) - 10} more[/dim]"
        detail.update(
            f"  [bold]Packages:[/bold] {pkg_str}\n"
            f"  [bold]File:[/bold]     [dim]{entry.get('path', '—')}[/dim]"
        )

    def action_restore(self):
        if not self.history or self._selected_index >= len(self.history):
            return
        entry = self.history[self._selected_index]
        self.dismiss(("restore", entry))

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_close()

    def action_close(self):
        self.dismiss(("close", None))

    def action_delete_entry(self):
        if not self.history or self._selected_index >= len(self.history):
            return
        self.history.pop(self._selected_index)
        try:
            with open(HISTORY_PATH, "w") as f:
                json.dump(self.history, f, indent=2)
        except Exception:
            pass
        # Rebuild table
        table = self.query_one("#history-list", DataTable)
        table.clear()
        if not self.history:
            table.add_row("—", "No exports yet", "—", "—")
        else:
            for entry in self.history:
                table.add_row(
                    entry.get("date", "—"),
                    entry.get("name", "—"),
                    str(entry.get("package_count", "—")),
                    entry.get("path", "—"),
                )
            # Adjust cursor
            new_idx = min(self._selected_index, len(self.history) - 1)
            table.move_cursor(row=new_idx, animate=False)
            self._selected_index = new_idx
            self._update_detail()


# ──────────────────────────────────────────────
#  Modal: demanar contrasenya per escriure com a root
# ──────────────────────────────────────────────

class SudoPasswordModal(ModalScreen):
    """Demana la contrasenya de sudo per poder escriure un fitxer protegit."""

    CSS = """
    SudoPasswordModal { align: center middle; }
    #sudo-dialog {
        width: 60;
        height: auto;
        background: #111111;
        border: tall $accent;
        padding: 1 2;
    }
    #sudo-title { text-style: bold; margin-bottom: 1; }
    #sudo-hint { color: $text-muted; margin-top: 1; }
    #sudo-confirm-btn {
        margin-top: 1;
        width: 100%;
    }
    """

    BINDINGS = [Binding("escape", "action_cancel", "Cancel", priority=True)]

    def compose(self) -> ComposeResult:
        with Vertical(id="sudo-dialog"):
            yield Label("Root password required", id="sudo-title")
            yield Label("This file requires root permissions to write.", id="sudo-desc")
            yield Input(placeholder="Password", password=True, id="sudo-input")
            yield Button("Confirm", id="sudo-confirm-btn", variant="primary")
            yield Label("Esc → cancel", id="sudo-hint")

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter al camp de password: aturem la propagació i confirmem."""
        event.stop()
        event.prevent_default()
        self._confirm()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sudo-confirm-btn":
            event.stop()
            event.prevent_default()
            self._confirm()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _confirm(self) -> None:
        password = self.query_one("#sudo-input", Input).value
        self.dismiss(password)


# ──────────────────────────────────────────────
#  Modal: guardar o descartar el .nixard temporal
# ──────────────────────────────────────────────

class SaveNixardModal(ModalScreen):
    """Pregunta si es vol guardar el .nixard temporal i, si sí, demana el nom."""

    CSS = """
    SaveNixardModal { align: center middle; }
    #save-dialog {
        width: 60;
        height: auto;
        background: black;
        border: tall white;
        padding: 1 2;
    }
    #save-title { text-style: bold; margin-bottom: 1; color: white; }
    #save-desc { color: #aaaaaa; margin-bottom: 1; }
    #save-hint { color: #666666; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "discard", "Discard", priority=True),
        Binding("f1", "save", "Save", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="save-dialog"):
            yield Label("Save .nixard file?", id="save-title")
            yield Label("Give it a name (or leave blank to discard):", id="save-desc")
            yield Input(placeholder="my-packages", id="save-name-input")
            yield Label("Enter → save   Esc → discard without saving", id="save-hint")

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_discard()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        name = event.value.strip()
        if name:
            self.dismiss(("save", name))
        else:
            self.dismiss(("discard", None))

    def action_discard(self):
        self.dismiss(("discard", None))

    def action_save(self):
        name = self.query_one("#save-name-input", Input).value.strip()
        self.dismiss(("save", name) if name else ("discard", None))


# ──────────────────────────────────────────────
#  Pantalla d'edició dividida: .nixard | .nix
# ──────────────────────────────────────────────

class NixEditScreen(Screen):
    """
    Pantalla dividida verticalment:
      Esquerra: editor del .nixard (referència, editable)
      Dreta:    selector de fitxer .nix + editor del fitxer triat
    """

    CSS = """
    NixEditScreen {
        background: black;
        layout: vertical;
        overflow: hidden hidden;
    }
    #edit-header-bar {
        height: 3;
        min-height: 3;
        max-height: 3;
        background: #1a1a1a;
        color: white;
        padding: 0 1;
        align: left middle;
        border-bottom: solid #444444;
    }
    #edit-header-label {
        width: 1fr;
        text-style: bold;
        color: #aaaaaa;
    }
    #exit-btn {
        padding: 0 2;
        content-align: center middle;
    }
    #exit-btn:hover {
        text-style: bold underline;
    }
    #exit-btn:focus {
        text-style: bold reverse;
    }
    #sudo-bar {
        height: 3;
        min-height: 3;
        max-height: 3;
        background: $warning;
        color: black;
        padding: 0 1;
        align: left middle;
        display: none;
    }
    #sudo-label {
        width: auto;
        text-style: bold;
        color: black;
        margin-right: 1;
    }
    #sudo-inline-input {
        width: 30;
        height: 1;
        border: none;
        background: black;
        color: white;
    }
    #sudo-cancel-btn {
        min-width: 10;
        height: 1;
        margin-left: 1;
        border: none;
    }
    #edit-panels-row {
        height: 1fr;
        overflow: hidden hidden;
    }
    #edit-left-panel {
        width: 1fr;
        border-right: vkey $panel;
        overflow: hidden hidden;
    }
    #edit-right-panel {
        width: 1fr;
        overflow: hidden hidden;
    }
    #edit-left-title {
        height: 1;
        min-height: 1;
        max-height: 1;
        background: $panel;
        padding: 0 1;
        text-style: bold;
    }
    #edit-right-title {
        height: 1;
        min-height: 1;
        max-height: 1;
        background: $panel;
        padding: 0 1;
        text-style: bold;
    }
    #nix-file-selector {
        height: 3;
        min-height: 3;
        max-height: 3;
        margin: 0;
        border: none;
    }
    #nixard-editor {
        height: 1fr;
    }
    #nix-editor {
        height: 1fr;
    }
    #edit-footer {
        height: 1;
        min-height: 1;
        max-height: 1;
        background: $panel;
        padding: 0 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("ctrl+s", "save_nix", "Save .nix", priority=True),
        Binding("ctrl+escape", "exit_editor", "Exit", priority=True),
        Binding("ctrl+left", "focus_left", "Focus left", priority=True),
        Binding("ctrl+right", "focus_right", "Focus right", priority=True),
    ]

    def __init__(self, nixard_content: str, marked_list: list,
                 pkg_names_clean: list, pkg_names: list, generated_at: str):
        super().__init__()
        self._nixard_content = nixard_content
        self._marked_list = marked_list
        self._pkg_names_clean = pkg_names_clean
        self._pkg_names = pkg_names
        self._generated_at = generated_at
        self._nix_files = []
        self._current_nix_path: str | None = None
        self._nix_needs_root = False
        self._pending_nix_content: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="edit-header-bar"):
            yield Label(
                " NIXARD EDITOR   Ctrl+S → save .nix   Ctrl+←/→ → switch panel",
                id="edit-header-label"
            )
            lbl = Label("[white bold][[ Exit ]][/white bold]", id="exit-btn")
            lbl.can_focus = True
            yield lbl
        with Horizontal(id="sudo-bar"):
            yield Label(" Root password: ", id="sudo-label")
            yield Input(placeholder="password...", password=True, id="sudo-inline-input")
            yield Button("Cancel", id="sudo-cancel-btn", variant="warning")
        with Horizontal(id="edit-panels-row"):
            with Vertical(id="edit-left-panel"):
                yield Label(" .nixard (reference)", id="edit-left-title")
                yield TextArea(
                    self._nixard_content,
                    language="css",
                    id="nixard-editor",
                )
            with Vertical(id="edit-right-panel"):
                yield Label(" .nix file editor", id="edit-right-title")
                yield Select(
                    options=[],
                    prompt="Select a .nix file to edit...",
                    id="nix-file-selector",
                )
                yield TextArea(
                    "",
                    language="css",
                    id="nix-editor",
                )
        yield Label(
            " Ctrl+S → save .nix   Ctrl+← → focus left   Ctrl+→ → focus right   Tab → focus Exit btn",
            id="edit-footer"
        )

    def on_mount(self) -> None:
        self.run_worker(self._load_nix_files(), exclusive=False)
        self.query_one("#nixard-editor", TextArea).focus()

    async def _load_nix_files(self):
        files = await asyncio.to_thread(find_nix_files_on_system)
        self._nix_files = files
        selector = self.query_one("#nix-file-selector", Select)
        if files:
            options = [(Path(f).name + "  " + f, f) for f in files]
            selector.set_options(options)
        else:
            selector.set_options([("No .nix files found", "")])

    def on_select_changed(self, event: Select.Changed) -> None:
        path = event.value
        if not path or path == Select.BLANK:
            return
        self._current_nix_path = str(path)
        self._nix_needs_root = not os.access(self._current_nix_path, os.W_OK)
        try:
            content = Path(self._current_nix_path).read_text(errors='replace')
        except Exception as e:
            content = f"# Error reading file: {e}"
        editor = self.query_one("#nix-editor", TextArea)
        editor.load_text(content)
        editor.focus()
        title = self.query_one("#edit-right-title", Label)
        root_tag = "  [dim](root required)[/dim]" if self._nix_needs_root else ""
        title.update(f" {Path(self._current_nix_path).name}{root_tag}")

    def on_label_clicked(self, event) -> None:
        try:
            if event.label.id == "exit-btn":
                self.action_exit_editor()
        except Exception:
            pass

    def on_click(self, event) -> None:
        try:
            if event.widget.id == "exit-btn":
                self.action_exit_editor()
        except Exception:
            pass

    def on_key(self, event) -> None:
        if event.key == "enter":
            try:
                focused = self.focused
                if focused and focused.id == "exit-btn":
                    event.stop()
                    self.action_exit_editor()
            except Exception:
                pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sudo-cancel-btn":
            self._hide_sudo_bar()

    def _show_sudo_bar(self):
        """Mostra la barra de contrasenya inline i hi posa el focus."""
        bar = self.query_one("#sudo-bar")
        bar.display = True
        inp = self.query_one("#sudo-inline-input", Input)
        inp.value = ""
        inp.focus()

    def _hide_sudo_bar(self):
        """Amaga la barra de contrasenya i torna el focus al TextArea."""
        bar = self.query_one("#sudo-bar")
        bar.display = False
        self.query_one("#sudo-inline-input", Input).value = ""
        self._pending_nix_content = None

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Captura l'Enter al camp de contrasenya inline."""
        if event.input.id != "sudo-inline-input":
            return
        event.stop()
        event.prevent_default()
        password = event.value
        # Llegim el contingut ABANS d'amagar la barra (que posa _pending_nix_content a None)
        content = self._pending_nix_content or ""
        self._hide_sudo_bar()
        if not password or not content:
            return
        try:
            self._backup_and_write_sudo(self._current_nix_path, content, password)
            self.notify(f"Saved (sudo): {self._current_nix_path}", severity="information")
        except Exception as e:
            self.notify(f"Error saving (sudo): {e}", severity="error")

    def action_focus_left(self):
        self.query_one("#nixard-editor", TextArea).focus()

    def action_focus_right(self):
        if self._current_nix_path:
            self.query_one("#nix-editor", TextArea).focus()
        else:
            self.query_one("#nix-file-selector", Select).focus()

    def action_save_nix(self):
        if not self._current_nix_path:
            self.notify("No .nix file selected.", severity="warning")
            return
        if self._nix_needs_root:
            # Guardem el contingut ARA per evitar qualsevol interacció posterior
            self._pending_nix_content = self.query_one("#nix-editor", TextArea).text
            self._show_sudo_bar()
        else:
            self._do_save_direct()

    def _do_save_direct(self):
        content = self.query_one("#nix-editor", TextArea).text
        try:
            self._backup_and_write(self._current_nix_path, content)
            self.notify(f"Saved: {self._current_nix_path}", severity="information")
        except Exception as e:
            self.notify(f"Error saving: {e}", severity="error")

    def _backup_and_write(self, path: str, content: str):
        """Fa backup i escriu el fitxer directament."""
        src = Path(path)
        backup = Path(str(path) + ".nixard-backup")
        if src.exists() and not backup.exists():
            import shutil
            shutil.copy2(str(src), str(backup))
        src.write_text(content)

    def _backup_and_write_sudo(self, path: str, content: str, password: str):
        """Fa backup via sudo i escriu el fitxer via sudo tee."""
        # Pas 1: autenticar sudo amb la contrasenya (sudo -S -v valida sense fer res)
        auth = subprocess.run(
            ["sudo", "-S", "-v"],
            input=password + "\n",
            capture_output=True, text=True
        )
        if auth.returncode != 0:
            raise RuntimeError("Incorrect password or sudo failed")

        backup = path + ".nixard-backup"
        # Pas 2: backup si no existeix (sudo ja autenticat, sense -S)
        check = subprocess.run(
            ["sudo", "test", "-f", backup],
            capture_output=True, text=True
        )
        if check.returncode != 0:  # backup no existeix, el creem
            subprocess.run(
                ["sudo", "cp", path, backup],
                capture_output=True, text=True, check=True
            )
        # Pas 3: escriu via sudo tee (sense -S, token ja vàlid, stdin = només contingut)
        result = subprocess.run(
            ["sudo", "tee", path],
            input=content,
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "sudo tee failed")

    def action_exit_editor(self):
        """Surt de l'editor: pregunta si vol guardar el .nixard."""
        self.app.push_screen(
            SaveNixardModal(),
            self._handle_save_nixard
        )

    def _handle_save_nixard(self, result):
        if result is None:
            return
        action, name = result
        if action == "save" and name:
            self._persist_nixard(name)
        # Tant si guarda com si descarta, tornem a l'app principal
        self.app.pop_screen()

    def _persist_nixard(self, filename: str):
        """Desa el .nixard (amb el contingut editat) i l'afegeix a l'historial."""
        from datetime import datetime
        content = self.query_one("#nixard-editor", TextArea).text
        dirpath = HISTORY_PATH.parent
        dirpath.mkdir(parents=True, exist_ok=True)
        filepath = dirpath / f"{filename}.nixard"
        try:
            filepath.write_text(content)
            entry = {
                "date": self._generated_at,
                "name": filename,
                "path": str(filepath),
                "packages": self._pkg_names_clean,
                "packages_original": self._pkg_names,
                "packages_versions": {
                    p[0]: (p[1].get("version", "") if p[1] else "")
                    for p in self._marked_list
                },
                "package_count": len(self._pkg_names_clean),
            }
            save_history_entry(entry)
            self.app.notify(f"Saved: {filepath}", severity="information")
        except Exception as e:
            self.app.notify(f"Error saving .nixard: {e}", severity="error")


# ──────────────────────────────────────────────
#  App Textual
# ──────────────────────────────────────────────

class NixardApp(App):
    """Nixard v2.0.0 – closure real via cache.nixos.org."""

    CSS = """
    Screen {
        background: black;
    }
    Horizontal {
        layout: horizontal;
        width: 100%;
        height: 100%;
        background: black;
    }
    #left-status-panel {
        width: 28;
        padding: 1;
        background: black;
    }
    #center-panel {
        width: 50%;
        padding: 1;
        border-left: vkey $panel;
    }
    DataTable {
        width: 100%;
        height: 1fr;
        background: black;
    }
    DataTable > .datatable--header {
        background: black;
    }
    DataTable > .datatable--body {
        background: black;
    }
    #right-deps-panel {
        width: 1fr;
        padding: 1;
        border-left: vkey $panel;
        background: black;
    }
    #search-container {
        layout: horizontal;
        height: 3;
        margin-bottom: 1;
        width: 100%;
    }
    #search-box {
        width: 55%;
        height: 3;
        border: tall white;
        padding: 0 1;
    }
    #scope-selector {
        width: 45%;
        height: 3;
        layout: horizontal;
        background: transparent;
        border: none;
        margin-left: 1;
        padding: 0;
    }
    #scope-selector RadioButton {
        height: 1;
        margin-right: 1;
        padding: 0;
        background: transparent;
    }
    .panel-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #deps-container {
        padding: 1 2;
        height: 100%;
    }
    #deps-text {
        height: auto;
    }
    LoadingIndicator {
        height: 3;
        color: white;
    }
    #center-loading {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "reload", "Reload local"),
        ("space", "toggle_mark", "Mark/unmark"),
        ("e", "export", "Export .nixard"),
        ("n", "nix_edit", "Edit .nix"),
        ("h", "history", "History"),
        Binding("escape", "reset", "Reset", priority=True),
    ]

    def __init__(self):
        super().__init__()
        self.packages_by_scope = defaultdict(set)
        self.local_master_set = set()
        self.current_results = []
        self.last_selected_pkg = None
        self.current_scope = "Installed"
        self.marked_packages: set = set()
        # Cache del store local (evita re-escanejar en cada selecció)
        self._local_store_names = set()
        self._local_store_names_only = set()
        self._profile_names = set()
        self._profile_names_only = set()
        self._version_map = {}

    # ── Composició UI ──

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left-status-panel"):
                yield Label("PRESENCE", classes="panel-title")
                yield Label("[dim]Select a package...[/dim]", id="status-content")

            with Vertical(id="center-panel"):
                yield Label("NIX PACKAGE EXPLORER", classes="panel-title")
                with Horizontal(id="search-container"):
                    yield Input(placeholder="Search packages...", id="search-box")
                    with RadioSet(id="scope-selector"):
                        yield RadioButton("Installed", value=True)
                        yield RadioButton("Available")
                        yield RadioButton("All")
                yield LoadingIndicator(id="center-loading")
                yield DataTable(id="packages-table")

            with Vertical(id="right-deps-panel"):
                yield Label("DETAILS (real closure)", classes="panel-title")
                yield LoadingIndicator(id="loading-indicator")
                with ScrollableContainer(id="deps-container"):
                    yield Label("[dim]Awaiting selection...[/dim]", id="deps-text")

        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#packages-table", DataTable)
        table.add_column(" ", width=2)
        table.add_column("Package", width=52)
        table.add_column("Version", width=20)
        table.add_column("Status", width=16)
        table.cursor_type = "row"
        table.zebra_stripes = False
        self.query_one("#loading-indicator").display = False
        self.query_one("#center-loading").display = True
        self.query_one("#packages-table").display = False
        self.run_worker(self.load_local_system_data(), exclusive=True)

    # ── Càrrega de dades locals ──

    async def load_local_system_data(self):
        self.query_one("#status-content", Label).update("[dim]Scanning local system...[/dim]")
        self.packages_by_scope.clear()
        self.local_master_set.clear()

        # Store i perfil (per a l'anàlisi de closure)
        self._local_store_names, self._local_store_names_only = get_local_paths()
        self._profile_names, self._profile_names_only = get_profile_paths()

        # Mapa versió activa: nom_base → versió, des de /run/current-system (una sola crida)
        self._version_map = await asyncio.to_thread(build_version_map)

        flake_dir = await asyncio.to_thread(find_flake)

        # ── Scopes actius (el que hi ha ara al sistema) ──
        global_pkgs = pkgs_from_store("/run/current-system")
        if global_pkgs:
            self.packages_by_scope["System (active)"] = global_pkgs

        homes = [f"/home/{u}" for u in os.listdir("/home")] if os.path.exists("/home") else []
        homes.append("/root")
        for home in homes:
            if os.path.isdir(home):
                user = os.path.basename(home)
                u_prof = os.path.join(home, ".local/state/nix/profiles/profile")
                if os.path.exists(u_prof):
                    self.packages_by_scope[f"User profile ({user})"] = pkgs_from_store(u_prof)

                p1 = os.path.join(home, ".local/state/home-manager/gcroots/current-home")
                p2 = os.path.join(home, ".nix-profile")
                hm = p1 if os.path.exists(p1) else (
                    p2 if os.path.exists(p2) and os.path.exists(
                        os.path.join(home, ".config/home-manager")) else ""
                )
                if hm:
                    self.packages_by_scope[f"Home Manager ({user})"] = pkgs_from_store(hm)

        # ── Scopes declarats (el que diu la config) ──
        if flake_dir:
            # Flake pur
            config_sys = await asyncio.to_thread(
                get_packages_from_flake, flake_dir, "environment.systemPackages"
            )
            if config_sys:
                self.packages_by_scope["Config: system pkgs (flake)"] = config_sys

            flake_users = await asyncio.to_thread(get_flake_users, flake_dir)
            for username in flake_users:
                u_pkgs = await asyncio.to_thread(
                    get_packages_from_flake, flake_dir, f"users.users.{username}.packages"
                )
                if u_pkgs:
                    self.packages_by_scope[f"Config: user {username} (flake)"] = u_pkgs

        elif os.path.exists(NIXOS_CONFIG):
            # configuration.nix tradicional
            config_sys = await asyncio.to_thread(
                get_packages_from_nix_config, "environment.systemPackages"
            )
            if config_sys:
                self.packages_by_scope["Config: system pkgs"] = config_sys
            try:
                with open(NIXOS_CONFIG) as f:
                    content = f.read()
                users = set(re.findall(r'users\.users\.([a-zA-Z0-9_-]+)', content))
                for username in users:
                    u_pkgs = await asyncio.to_thread(
                        get_packages_from_nix_config, f"users.users.{username}.packages"
                    )
                    if u_pkgs:
                        self.packages_by_scope[f"Config: user {username}"] = u_pkgs
            except Exception:
                pass

        for pkgs in self.packages_by_scope.values():
            self.local_master_set.update(pkgs)

        self.query_one("#center-loading").display = False
        self.query_one("#packages-table").display = True
        # Mostra el canal real del sistema al panell d'estat
        detected_channel = await asyncio.to_thread(detect_system_channel)
        self.query_one("#status-content", Label).update(
            f"Local store cached.\n\n[dim]Channel:[/dim] [bold]{detected_channel}[/bold]"
        )
        self.update_search_results()
        self.query_one("#packages-table", DataTable).focus()

    def action_reload(self):
        self.query_one("#center-loading").display = True
        self.query_one("#packages-table").display = False
        self.run_worker(self.load_local_system_data(), exclusive=True)

    # ── Marcat de paquets ──

    def action_toggle_mark(self):
        table = self.query_one("#packages-table", DataTable)
        if table.cursor_row is None:
            return
        try:
            row_data = table.get_row_at(table.cursor_row)
        except Exception:
            return
        current_row = table.cursor_row
        pkg_name = row_data[1]
        if pkg_name in self.marked_packages:
            self.marked_packages.discard(pkg_name)
        else:
            self.marked_packages.add(pkg_name)
        # Actualitzem NOMÉS la cel·la del marcador de la fila actual,
        # sense fer clear() per evitar que el cursor salti
        mark = "[bold yellow]\u25cf[/bold yellow]" if pkg_name in self.marked_packages else " "
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            table.update_cell(row_key, table.columns[0].key, mark, update_width=False)
            table.move_cursor(row=current_row, animate=False)
        except Exception:
            # Fallback: re-render complet si update_cell falla
            self.render_table_rows()
            table.move_cursor(row=current_row, animate=False)
        self._update_footer_mark_count()

    def _update_footer_mark_count(self):
        n = len(self.marked_packages)
        if n == 0:
            self.sub_title = ""
        else:
            self.sub_title = f"{n} package{'s' if n != 1 else ''} marked  ·  press e to export"

    # ── Exportació ──

    def action_export(self):
        if not self.marked_packages:
            self.notify("No packages marked. Use Space to mark packages.", severity="warning")
            return

        # Recollim metadades dels paquets marcats
        marked_list = []
        for pkg_name in sorted(self.marked_packages):
            pkg_meta = None
            for row in self.current_results:
                if row[0] == pkg_name and len(row) > 3:
                    pkg_meta = row[3]
                    break
            if not pkg_meta:
                pkg_meta = {"attr": pkg_name, "name": pkg_name, "version": ""}
            marked_list.append((pkg_name, pkg_meta))

        def handle_export(result):
            if result:
                name, path = result
                self.run_worker(
                    self._write_nixard(name, path, marked_list),
                    exclusive=False,
                )

        modal = ExportModal(
            marked_list,
            self._local_store_names,
            self._local_store_names_only,
            self._profile_names,
            self._profile_names_only,
        )
        self.push_screen(modal, handle_export)

    def action_nix_edit(self):
        """Tecla n: obre la pantalla d'edició dividida .nixard | .nix."""
        if not self.marked_packages:
            self.notify("No packages marked. Use Space to mark packages.", severity="warning")
            return

        # Construim la llista de paquets marcats amb metadades
        marked_list = []
        for pkg_name in sorted(self.marked_packages):
            pkg_meta = None
            for row in self.current_results:
                if row[0] == pkg_name and len(row) > 3:
                    pkg_meta = row[3]
                    break
            if not pkg_meta:
                pkg_meta = {"attr": pkg_name, "name": pkg_name, "version": ""}
            marked_list.append((pkg_name, pkg_meta))

        content, pkg_names_clean, pkg_names, generated_at = _generate_nixard_content(marked_list)
        self.push_screen(
            NixEditScreen(
                nixard_content=content,
                marked_list=marked_list,
                pkg_names_clean=pkg_names_clean,
                pkg_names=pkg_names,
                generated_at=generated_at,
            )
        )

    async def _write_nixard(self, filename: str, output_dir: str, marked_list: list):
        """Genera el fitxer .nixard al directori escollit."""
        from datetime import datetime
        from pathlib import Path

        dirpath = Path(output_dir).expanduser()
        dirpath.mkdir(parents=True, exist_ok=True)
        filepath = dirpath / f"{filename}.nixard"
        pkg_names = [p[0] for p in marked_list]
        pkg_names_clean = []
        for pkg_name, pkg_meta in marked_list:
            # Use attr from meta if available (correct nixpkgs name)
            if pkg_meta and pkg_meta.get("attr") and pkg_meta["attr"] != pkg_name:
                pkg_names_clean.append(pkg_meta["attr"])
            else:
                # Only strip version suffix if result still looks like a valid name
                base = re.sub(r'-[\d]+[\d\w\.\-]*$', '', pkg_name)
                # If base is too short or loses meaningful part, keep original
                pkg_names_clean.append(base if base and len(base) > 2 and base != pkg_name.split("-")[0] else pkg_name)

        # Calculem totals (reutilitzem la lògica del modal si ja s'ha calculat)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        lines = [
            f"# nixard export — \"{filename}\"",
            f"# Generated: {now}",
            f"# Packages: {len(pkg_names)}",
            f"#",
            f"# Note: download and disk totals shown in the export panel.",
            f"# Copy the relevant section into your NixOS configuration.",
            f"",
            f"# ── environment.systemPackages (configuration.nix or flake nixosConfiguration) ──",
            f"environment.systemPackages = with pkgs; [",
        ]
        for name in pkg_names_clean:
            lines.append(f"  {name}")
        lines += [
            f"];",
            f"",
            f"# ── home.packages (home-manager home.nix) ──",
            f"home.packages = with pkgs; [",
        ]
        for name in pkg_names_clean:
            lines.append(f"  {name}")
        lines += [
            f"];",
            f"",
            f"# ── nix-shell (try without installing) ──",
            f"nix-shell -p {' '.join(pkg_names_clean)}",
            f"",
            f"# ── nix profile install (per-user installation) ──",
            f"nix profile install {' '.join(f'nixpkgs#{n}' for n in pkg_names_clean)}",
            f"",
        ]

        try:
            with open(filepath, "w") as f:
                f.write("\n".join(lines))
            # Save to history
            entry = {
                "date": now,
                "name": filename,
                "path": str(filepath),
                "packages": pkg_names_clean,
                "packages_original": pkg_names,
                "packages_versions": {
                    p[0]: (p[1].get("version", "") if p[1] else "")
                    for p in marked_list
                },
                "package_count": len(pkg_names_clean),
            }
            save_history_entry(entry)
            self.notify(f"Exported to {filepath}", severity="information")
        except Exception as e:
            self.notify(f"Error writing file: {e}", severity="error")

    # ── Historial ──

    def action_history(self):
        def handle_history(result):
            if not result:
                return
            action, entry = result
            if action == "close":
                self._reset_to_initial()
            elif action == "restore":
                self._restore_from_history(entry)

        self.push_screen(HistoryModal(), handle_history)

    def action_reset(self):
        self._reset_to_initial()

    def _reset_to_initial(self):
        """Reset app to clean initial state."""
        self.marked_packages.clear()
        self.last_selected_pkg = None
        self.sub_title = ""
        self.current_scope = "Installed"
        try:
            radio_set = self.query_one("#scope-selector", RadioSet)
            for button in radio_set.query(RadioButton):
                if str(button.label) == "Installed":
                    button.value = True
                    break
        except Exception:
            pass
        self.query_one("#search-box", Input).value = ""
        self.query_one("#status-content", Label).update("[dim]Select a package...[/dim]")
        self.query_one("#deps-text", Label).update("[dim]Awaiting selection...[/dim]")
        self.query_one("#loading-indicator").display = False
        self.update_search_results()
        self.query_one("#packages-table", DataTable).focus()

    def _restore_from_history(self, entry: dict):
        # Prefer original names (with version) over clean names
        packages = entry.get("packages_original") or entry.get("packages", [])
        if not packages:
            self.notify("No packages found in this entry.", severity="warning")
            return

        self.marked_packages = set(packages)
        self.last_selected_pkg = None  # reset so selection works after restore
        self._update_footer_mark_count()

        # Canviem a scope "All" i filtrem per mostrar només els restaurats
        self.current_scope = "All"
        try:
            radio_set = self.query_one("#scope-selector", RadioSet)
            for button in radio_set.query(RadioButton):
                if str(button.label) == "All":
                    button.value = True
                    break
        except Exception:
            pass

        # Filtrem la taula per mostrar només els paquets restaurats
        search_box = self.query_one("#search-box", Input)
        search_box.value = ""
        self._show_restored_packages(packages, entry.get("name", "export"),
                                      entry.get("packages_versions", {}))

    def _show_restored_packages(self, packages: list, export_name: str, versions: dict = None):
        """Mostra a la taula els paquets restaurats de l'historial."""
        restored_list = []
        for pkg in sorted(packages):
            in_local = pkg in self.local_master_set
            status = "[bold]installed[/bold]" if in_local else "[dim]not installed[/dim]"
            # Use saved version if available, otherwise try regex but avoid false positives
            if versions and pkg in versions and versions[pkg]:
                version = versions[pkg]
            else:
                m = re.match(r'^.+?-([\d]+[\.\d]+[\d\w\.\-]*)$', pkg)
                version = m.group(1) if m else "[dim]—[/dim]"
            restored_list.append((pkg, version, status, None))

        self.current_results = restored_list
        self.render_table_rows()
        self.query_one("#packages-table", DataTable).focus()
        self.sub_title = (
            f"{len(packages)} packages marked  ·  restored from \"{export_name}\"  ·  press e to export"
        )
        self.query_one("#status-content", Label).update(
            f"[bold]Restored from:[/bold] {export_name}\n"
            f"[dim]{len(packages)} packages loaded.[/dim]"
        )

    # ── Canvi d'àmbit ──

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self.current_scope = str(event.pressed.label)
        search_box = self.query_one("#search-box", Input)
        if self.current_scope == "Installed":
            search_box.placeholder = "Search installed packages..."
            self.update_search_results()
        else:
            search_box.placeholder = "Search cache.nixos.org candidates (press Enter)..."

    # ── Cerca ──

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.current_scope == "Installed":
            self.update_search_results()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.current_scope != "Installed":
            keyword = event.value.strip()
            if keyword:
                self.query_one("#status-content", Label).update(
                    f"[dim]Searching '{keyword}' in nixpkgs...[/dim]"
                )
                self.run_worker(self.search_and_show_candidates(keyword), exclusive=True)

    async def search_and_show_candidates(self, keyword: str):
        candidates = search_cache_candidates(keyword)

        # Construïm un índex de noms base (sense versió) dels paquets instal·lats.
        # local_master_set pot contenir "gnome-chess-49.2" però candidates retorna "gnome-chess".
        installed_base_names = set()
        for pkg in self.local_master_set:
            installed_base_names.add(pkg)
            base = re.sub(r'-[\d][\d\w\.\-]*$', '', pkg)
            if base and base != pkg:
                installed_base_names.add(base)

        combined_list = []
        for pkg in candidates:
            name = pkg["name"]
            base_name = re.sub(r'-[\d][\d\w\.\-]*$', '', name)
            is_installed = (name in installed_base_names or base_name in installed_base_names)
            if self.current_scope == "Available" and is_installed:
                continue
            status = "[bold]installed[/bold]" if is_installed else "[dim]not installed[/dim]"
            if is_installed:
                version = self._version_map.get(name) or self._version_map.get(base_name) or pkg.get("version", "") or "[dim]—[/dim]"
            else:
                version = pkg.get("version", "") or "[dim]—[/dim]"
            combined_list.append((name, version, status, pkg))

        self.current_results = sorted(combined_list, key=lambda x: x[0].lower())
        self.render_table_rows()
        self.query_one("#status-content", Label).update(
            f"{len(self.current_results)} candidates found."
        )

    def update_search_results(self) -> None:
        search_value = self.query_one("#search-box", Input).value.lower()
        local_list = []
        for pkg in self.local_master_set:
            if search_value and search_value not in pkg.lower():
                continue
            # Intentem extreure la versió directament del nom del paquet (ex: fuse-2.9.9-man → 2.9.9-man)
            m_ver = re.search(r'-([\d][\d\w\.\-]*)$', pkg)
            if m_ver:
                version = m_ver.group(1)
            else:
                # Fallback: busquem al version_map pel nom base sense versió
                base = re.sub(r'-[\d][\d\w\.\-]*$', '', pkg)
                version = self._version_map.get(pkg) or self._version_map.get(base) or "[dim]—[/dim]"
            local_list.append((pkg, version, "[bold]installed[/bold]", None))
        self.current_results = sorted(local_list, key=lambda x: x[0].lower())
        self.render_table_rows()

    def render_table_rows(self):
        table = self.query_one("#packages-table", DataTable)
        table.clear()
        self.last_selected_pkg = None
        for row in self.current_results:
            pkg_name, version, status = row[0], row[1], row[2]
            ver_short = version[:12] if len(version) > 12 else version
            mark = "[bold yellow]●[/bold yellow]" if pkg_name in self.marked_packages else " "
            table.add_row(mark, pkg_name, ver_short, status)

    # ── Selecció de paquet ──

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter sobre un paquet: actualitza panell esquerre i llança l'anàlisi del closure."""
        try:
            table = self.query_one("#packages-table", DataTable)
            if table.cursor_row is None:
                return
            row_data = table.get_row_at(table.cursor_row)
            if row_data:
                self._on_pkg_highlighted(row_data[1])
                self._on_pkg_closure(row_data[1])
        except Exception:
            pass

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Cursor sobre un paquet: actualitza només el panell esquerre (scopes)."""
        if event.data_table.id != "packages-table":
            return
        try:
            table = self.query_one("#packages-table", DataTable)
            if table.cursor_row is None:
                return
            row_data = table.get_row_at(table.cursor_row)
            if row_data:
                pkg_name = row_data[1]
                if pkg_name != self.last_selected_pkg:
                    self._on_pkg_highlighted(pkg_name)
        except Exception:
            pass

    def _on_pkg_highlighted(self, pkg_name: str):
        """Actualitza el panell esquerre amb la presència del paquet als scopes."""
        self.last_selected_pkg = pkg_name

        status_label = self.query_one("#status-content", Label)
        lines = [f"[bold]{pkg_name.upper()}[/bold]\n"]

        active_scopes = {k: v for k, v in self.packages_by_scope.items()
                         if not k.startswith("Config:")}
        declared_scopes = {k: v for k, v in self.packages_by_scope.items()
                           if k.startswith("Config:")}

        found_locally = False
        if active_scopes:
            lines.append("[dim]─ active ─[/dim]")
            for scope, pkgs in active_scopes.items():
                if pkg_name in pkgs:
                    lines.append(f"  [bold]✓[/bold] {scope}")
                    found_locally = True
                else:
                    lines.append(f"[dim]  · {scope}[/dim]")

        if declared_scopes:
            lines.append("\n[dim]─ declared ─[/dim]")
            for scope, pkgs in declared_scopes.items():
                label = scope.replace("Config: ", "")
                if pkg_name in pkgs:
                    lines.append(f"  [bold]✓[/bold] {label}")
                    found_locally = True
                else:
                    lines.append(f"[dim]  · {label}[/dim]")

        if not found_locally:
            lines.append("\n[dim]Not local. Cached.[/dim]")
        status_label.update("\n".join(lines))

    def _on_pkg_closure(self, pkg_name: str):
        """Llança l'anàlisi del closure al panell dret (només quan l'usuari prem Enter)."""
        # Determinem si el paquet és local per passar-ho a render_nix_deps
        found_locally = any(
            pkg_name in pkgs
            for pkgs in self.packages_by_scope.values()
        )

        # Panell dret: neteja i indicador de càrrega
        self.query_one("#deps-text", Label).update("")
        self.query_one("#loading-indicator").display = True

        # Busquem metadades del paquet (versió, attr) si les tenim
        pkg_meta = None
        for row in self.current_results:
            row_name, _version, _status, meta = row[0], row[1], row[2], row[3] if len(row) > 3 else None
            if row_name == pkg_name and meta:
                pkg_meta = meta
                break
        if not pkg_meta:
            pkg_meta = {"attr": pkg_name, "name": pkg_name, "version": ""}

        self.run_worker(
            self.render_nix_deps(pkg_name, pkg_meta, found_locally),
            exclusive=True,
        )

    # ── Anàlisi de closure real (portada de nix-deps.py) ──

    async def render_nix_deps(self, pkg_name: str, pkg_meta: dict, is_installed: bool):
        deps_label = self.query_one("#deps-text", Label)
        attr_name = pkg_meta.get("attr", pkg_name)
        remote_version = pkg_meta.get("version", "")

        def hide_loading():
            self.query_one("#loading-indicator").display = False

        try:
            # Si pkg_name ja porta versió (ex: "pipewire-1.4.7"), extreiem nom base i versió directament
            base_name = re.sub(r'-[\d][\d\w\.\-]*$', '', pkg_name)
            m_ver = re.search(r'-([\d][\d\w\.\-]*)$', pkg_name)
            if base_name != pkg_name and m_ver:
                # El nom ja porta versió: usem-la directament sense buscar al store
                local_version = m_ver.group(1)
            else:
                _, local_version = await asyncio.to_thread(find_local_installed_version, base_name)
                if not local_version:
                    _, local_version = await asyncio.to_thread(find_local_installed_version, pkg_name)

            pkg_in_profile = bool(self._profile_names) and any(
                pkg_name in n or base_name in n for n in self._profile_names
            )

            # is_installed indica si el paquet és al store local (independent de si té versió al nom)
            if pkg_in_profile and local_version and remote_version and local_version != remote_version:
                status_str = f"[bold]v{local_version}[/bold] [dim]installed / v{remote_version} available[/dim]"
            elif pkg_in_profile and local_version:
                status_str = f"[bold]v{local_version}[/bold] [dim](in profile)[/dim]"
            elif local_version and remote_version and local_version != remote_version:
                status_str = f"[bold]v{local_version}[/bold] [dim]installed / v{remote_version} available[/dim]"
            elif local_version:
                status_str = f"[bold]v{local_version}[/bold] [dim](in store)[/dim]"
            elif is_installed and pkg_in_profile:
                status_str = "[bold]installed[/bold] [dim](in profile, no version)[/dim]"
            elif is_installed:
                status_str = "[bold]installed[/bold] [dim](in store, no version)[/dim]"
            else:
                status_str = "[dim]not installed[/dim]"

            # ── Via A: paquet instal·lat localment → nix-store -qR ──
            local_store_path = await asyncio.to_thread(find_store_path_for_pkg, pkg_name) if is_installed else None
            if not local_store_path and is_installed:
                local_store_path = await asyncio.to_thread(find_store_path_for_pkg, base_name)

            if local_store_path:
                deps_list = await asyncio.to_thread(resolve_local_closure, local_store_path)
                total = len(deps_list)

                total_size = 0
                size_details = []
                for dep_path in deps_list:
                    sz = await asyncio.to_thread(get_local_nar_size, dep_path)
                    total_size += sz
                    name_only = "-".join(os.path.basename(dep_path).split("-")[1:])
                    size_details.append((name_only, sz))

                size_details.sort(key=lambda x: x[1], reverse=True)

                breakdown_lines = []
                for name, sz in size_details[:15]:
                    breakdown_lines.append(
                        f"   [dim]↳[/dim] {name[:36]:<36} "
                        f"[dim]size:[/dim] [bold]{human_bytes(sz)}[/bold]"
                    )
                if len(size_details) > 15:
                    breakdown_lines.append(
                        f"   [dim]... and {len(size_details) - 15} more smaller packages.[/dim]"
                    )
                breakdown = "\n".join(breakdown_lines)

                output = (
                    f"  [bold]SUMMARY FOR {pkg_name.upper()}[/bold]\n\n"
                    f"  Status:             {status_str}\n"
                    f"  [dim]Source: local store (local build, not in public cache)[/dim]\n\n"
                    f"  [bold]Local closure ({total} packages):[/bold]\n"
                    f"    Total expanded size:  [bold]{human_bytes(total_size)}[/bold]\n"
                    f"    In store:            {total} packages\n"
                    f"    Absent:              [dim]0 packages[/dim]\n\n"
                    f"  [bold]Real impact:[/bold]\n"
                    f"    Estimated download:  [dim]0 B (already installed)[/dim]\n"
                    f"    New space on disk:   [dim]0 B[/dim]\n\n"
                    f"  [bold]Closure packages (by size):[/bold]\n"
                    f"{breakdown}\n\n"
                    f"  Net disk cost: [bold]0 B[/bold] [dim]— closure already materialized locally.[/dim]\n"
                )

            # ── Via B: paquet no instal·lat → cache.nixos.org ──
            else:
                root_hash = await asyncio.to_thread(get_root_hash, attr_name, remote_version, pkg_name)
                if not root_hash:
                    hide_loading()
                    deps_label.update(
                        f"[dim]No cache entry. Config file.[/dim]"
                    )
                    return

                closure = await asyncio.to_thread(resolve_remote_closure, root_hash)
                if not closure:
                    hide_loading()
                    deps_label.update(
                        f"Could not fetch closure from cache.nixos.org.\n"
                        f"[dim]This package may be a local build with no entry in the public cache.[/dim]"
                    )
                    return

                in_profile_count = in_store_count = pending_count = 0
                closure_installed = 0
                pending_download = pending_installed = 0
                pending_details = []

                for h, data in closure.items():
                    path_name = os.path.basename(data["store_path"])
                    closure_installed += data["nar_size"]
                    pkg_name_only = "-".join(path_name.split("-")[1:])

                    in_prof = (path_name in self._profile_names or
                               pkg_name_only in self._profile_names_only)
                    in_stor = (path_name in self._local_store_names or
                               pkg_name_only in self._local_store_names_only)

                    if in_prof:
                        in_profile_count += 1
                    elif in_stor:
                        in_store_count += 1
                    else:
                        pending_count += 1
                        pending_download += data["file_size"]
                        pending_installed += data["nar_size"]
                        pending_details.append((pkg_name_only, data["file_size"], data["nar_size"]))

                pending_details.sort(key=lambda x: x[1], reverse=True)

                real_closure_size = await asyncio.to_thread(get_real_closure_size, attr_name) or closure_installed
                installed_expanded = max(0, real_closure_size - pending_installed)

                breakdown_lines = []
                for name, f_size, n_size in pending_details[:15]:
                    breakdown_lines.append(
                        f"   [dim]↳[/dim] {name[:32]:<32} "
                        f"[dim]dl:[/dim] [bold]{human_bytes(f_size)}[/bold]  "
                        f"[dim]expanded:[/dim] {human_bytes(n_size)}"
                    )
                if len(pending_details) > 15:
                    breakdown_lines.append(
                        f"   [dim]... and {len(pending_details) - 15} more smaller packages.[/dim]"
                    )
                breakdown = "\n".join(breakdown_lines) if breakdown_lines else \
                    "   [dim]All dependencies already in local /nix/store.[/dim]"

                output = (
                    f"  [bold]SUMMARY FOR {pkg_name.upper()}[/bold]\n\n"
                    f"  Status:             {status_str}\n"
                    f"  [dim]Source: cache.nixos.org[/dim]\n\n"
                    f"  [bold]Total closure ({len(closure)} packages):[/bold]\n"
                    f"    Real closure size:   [bold]{real_closure_size / 1024 / 1024:.2f} MB[/bold]\n"
                    f"    Already installed:   {human_bytes(installed_expanded)}\n"
                    f"    Pending install:     {human_bytes(pending_installed)}\n"
                    f"    In profile:          [dim]{in_profile_count} packages[/dim]\n"
                    f"    In store (GC-able):  [dim]{in_store_count} packages[/dim]\n"
                    f"    Absent:              {pending_count} packages\n\n"
                    f"  [bold]Real impact ({pending_count} packages pending):[/bold]\n"
                    f"    Estimated download:  [bold]{human_bytes(pending_download)}[/bold]\n"
                    f"    New space on disk:   [bold]{human_bytes(pending_installed)}[/bold] (expanded)\n\n"
                    f"  [bold]Pending packages breakdown:[/bold]\n"
                    f"{breakdown}\n\n"
                    f"  Net disk cost: [bold]{human_bytes(pending_installed)}[/bold] will be added to your system.\n"
                )

        except Exception as e:
            output = f"Error analyzing {pkg_name}:\n[dim]{e}[/dim]"

        hide_loading()
        deps_label.update(output)


if __name__ == "__main__":
    NixardApp().run()
