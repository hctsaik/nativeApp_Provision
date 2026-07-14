"""Work out the answers the operator should never have to type.

The prebuilt shell, the portable runtime and the output folder are the same on
every build from a given machine — asking for them is noise that invites typos.
The entrypoint and the app name follow from the project itself.

Kept out of the GUI so the same detection can be unit-tested and reused by a CLI.
Every finder returns None rather than guessing wildly: a wrong silent default is
worse than an honest "not found" with instructions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Where a prebuilt cim-light.exe realistically lives. First hit wins.
SHELL_CANDIDATES = (
    Path("apps/host-tauri/prebuilt/cim-light.exe"),
    Path("apps/host-tauri/src-tauri/target/release/cim-light.exe"),
    Path("cim-light.exe"),
)
# Sibling repos to look in for the shell (native_Provision ships no shell of its own).
SIBLING_REPOS = ("nativeApp", "native_app", "cim-light")

RUNTIME_CANDIDATES = (
    Path(".runtime-cache/python311"),
    Path("runtime/python311"),
    Path("../nativeApp/runtime/python311"),
)

ENTRY_CANDIDATES = ("app.py", "main.py", "streamlit_app.py", "Home.py")

# Directories an entry point never lives in. Searching them produces confident,
# wrong answers (a spike script or a test fixture that also imports streamlit).
SKIPPED_DIRS = (
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
    "node_modules", "dist", "build", "site-packages",
    "tests", "test", "spike", "spikes", "docs", "examples", "scripts",
)


@dataclass(frozen=True)
class Detected:
    value: Path | None
    source: str          # how we found it — shown to the operator, never hidden
    hint: str = ""       # what to do when value is None

    @property
    def found(self) -> bool:
        return self.value is not None


def find_shell(repo_root: Path | None = None) -> Detected:
    """The prebuilt Tauri shell. Never built here — WDAC blocks compiling one."""
    root = (repo_root or REPO_ROOT).resolve()

    override = os.environ.get("CIM_TAURI_EXE")
    if override and Path(override).is_file():
        return Detected(Path(override), "環境變數 CIM_TAURI_EXE")

    for sibling in SIBLING_REPOS:
        base = root.parent / sibling
        for candidate in SHELL_CANDIDATES:
            path = base / candidate
            if path.is_file():
                return Detected(path, f"自動偵測:{sibling}")

    for candidate in SHELL_CANDIDATES:
        path = root / candidate
        if path.is_file():
            return Detected(path, "自動偵測:本 repo")

    return Detected(None, "找不到", hint=(
        "找不到預建的 cim-light.exe。請把它放到 nativeApp\\apps\\host-tauri\\prebuilt\\,"
        "或設定環境變數 CIM_TAURI_EXE。（本機 WDAC 擋 Rust 重編，只能沿用既有的殼。）"
    ))


def find_runtime(repo_root: Path | None = None) -> Detected:
    """A relocatable CPython with pip + venv (python-build-standalone)."""
    root = (repo_root or REPO_ROOT).resolve()

    override = os.environ.get("CIM_PORTABLE_PYTHON")
    if override and (Path(override) / "python.exe").is_file():
        return Detected(Path(override), "環境變數 CIM_PORTABLE_PYTHON")

    for candidate in RUNTIME_CANDIDATES:
        path = (root / candidate).resolve()
        if (path / "python.exe").is_file():
            return Detected(path, "自動偵測")

    return Detected(None, "找不到", hint=(
        "還沒有可攜 Python runtime。按「下載可攜 Python」自動取得（需連網，只需做一次）。"
    ))


def default_output(repo_root: Path | None = None) -> Path:
    return (repo_root or REPO_ROOT) / "dist" / "streamlit-apps"


def _searchable_py(project: Path, max_depth: int = 3):
    """Project .py files worth considering as an entry point. Skips build junk
    and the places an entry point never hides (tests, spikes, docs).

    Prunes as it walks instead of listing everything and filtering afterwards.
    `rglob("*.py")` visits every file it is asked to discard: 9343 of AI4BI's .py
    files live in `.venv`, and the old version spent 2.4–3.2 s (measured, warm cache)
    listing and sorting them all before throwing away everything but 103 — with the Tk
    main thread blocked, because this runs the moment the operator picks a folder.
    Same 103 candidates, in the same order, in 4–6 ms. `dirnames[:] = ...` is the
    difference between "descend into 20k files" and "never open the door": filtering
    after the walk is not the same as not walking.
    """
    project = Path(project)
    skip = set(SKIPPED_DIRS)
    base = len(project.parts)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(project):   # os.walk swallows OSError by design
        here = Path(dirpath)
        depth = len(here.parts) - base                      # 0 == the project root itself
        # In-place, or os.walk has already committed to descending.
        dirnames[:] = [] if depth >= max_depth else [
            d for d in dirnames if d not in skip and not d.startswith(".")
        ]
        found.extend(here / name for name in filenames if name.endswith(".py"))
    # Callers compare candidates by path; keep the old global ordering so that
    # "shallowest, then alphabetical" stays deterministic across machines.
    yield from sorted(found)


# The GUI's two tabs and the three fields a rescue can send someone to, spelled
# exactly as they appear on screen. A hint that says "the other tab" and makes them
# go looking for it is half a rescue; a hint that names a field which then REJECTS
# the value is worse than none — it costs a round trip and the operator's trust.
CIM_TAB = "CIM 平台模組（需 plugin.yaml）"
STREAMLIT_TAB = "Streamlit 專案 → 桌面 App"
MODULE_ROOT_FIELD = "Module 資料夾"      # CIM tab: the layer holding the module folders
PLATFORM_FIELD = "平台專案"              # CIM tab: the platform itself — see the warning below
SD_PROJECT_FIELD = "專案資料夾"          # Streamlit tab

# 「平台專案」 is NOT "wherever your code is": PlatformGateway requires
# <it>\sidecar\python-engine\engine.py and raises GatewayError otherwise. A module
# collection (ANnoTation) has no engine — telling its owner to put it there, as this
# module did until now, is advice that fails 100% of the time. The modules go in
# 「Module 資料夾」; 「平台專案」 keeps pointing at the platform.
PLATFORM_FIELD_IS_THE_PLATFORM = (
    f"「{PLATFORM_FIELD}」要留給 CIM 平台本身"
    "(底下有 sidecar\\python-engine\\engine.py 的那一層,例如 C:\\code\\claude\\nativeApp),"
    "不是這個資料夾——填錯會直接失敗。"
)

PLUGIN_MANIFEST = "plugin.yaml"
# ANnoTation keeps its 18 plugin.yaml files at modules/module_XXX/plugin.yaml, so
# a top-level-only look finds nothing and the operator is told, with total
# confidence, to pick one of 18 CIM module scripts as a Streamlit entry point.
_PLUGIN_GLOBS = (PLUGIN_MANIFEST, f"*/{PLUGIN_MANIFEST}", f"*/*/{PLUGIN_MANIFEST}",
                 f"*/*/*/{PLUGIN_MANIFEST}")


def find_plugin_manifests(project: Path, limit: int = 50) -> list[Path]:
    """The plugin.yaml files under `project`: the signature of a CIM module
    collection. Bounded (globs, a few levels, a cap) because this runs on every
    folder the operator picks, including a 40 GB one they picked by mistake.

    SKIPPED_DIRS is load-bearing, not hygiene: a Streamlit project with a
    `tests/fixtures/plugin.yaml` is still a Streamlit project. Counting that file
    as a module makes the tool declare the whole project a CIM module collection
    and swallow the Streamlit rescue entirely — the operator is then told to point
    「Module 資料夾」 at their `tests` folder."""
    project = Path(project)
    if not project.is_dir():
        return []
    found: list[Path] = []
    for pattern in _PLUGIN_GLOBS:
        try:
            for hit in sorted(project.glob(pattern)):
                if any(part in SKIPPED_DIRS or part.startswith(".")
                       for part in hit.relative_to(project).parts[:-1]):
                    continue
                found.append(hit)
                if len(found) >= limit:
                    return found
        except OSError:
            break
    return found


@dataclass(frozen=True)
class ModuleCollection:
    """What a folder is, in CIM terms — and where its modules actually live.

    One implementation, because "where do the modules live" was computed in two
    places (here and the GUI) and the two answers had already drifted apart.
    """

    folder: Path                       # what the operator pointed at
    manifests: tuple[Path, ...]        # the plugin.yaml files under it (skip list applied)
    module_root: Path | None           # the layer that DIRECTLY holds the module folders

    @property
    def found(self) -> bool:
        return bool(self.manifests)

    @property
    def is_single_module(self) -> bool:
        """They pointed at one module, not at the layer above it. `module_root`
        is then the parent — the folder that holds this module and its siblings."""
        return any(m.parent == self.folder for m in self.manifests)

    @property
    def already_correct(self) -> bool:
        """The folder IS the module root. Say nothing: the fault is elsewhere, and
        「請把欄位改成它現在的值」 is the kind of advice that ends support calls badly."""
        return self.found and self.module_root == self.folder

    def example(self) -> str:
        if not self.manifests:
            return ""
        try:
            return self.manifests[0].relative_to(self.folder).as_posix()
        except ValueError:  # pragma: no cover — manifests always sit under folder
            return self.manifests[0].name

    def count(self) -> str:
        return f"{len(self.manifests)} 個" if len(self.manifests) < 50 else "很多"


def find_module_collection(folder: Path, limit: int = 50) -> ModuleCollection:
    """The one answer to "is this a CIM module collection, and where do I point
    「Module 資料夾」?" — call this from the GUI instead of keeping a second copy.

    `module_root` is the layer that directly contains the module folders, which is
    what 「Module 資料夾」 wants (`source_pack.discover_source_modules` globs
    `*/plugin.yaml` from it). Ties go to the layer holding the MOST modules, so one
    stray manifest cannot drag the operator away from the folder holding the other 18.
    """
    folder = Path(folder)
    manifests = tuple(find_plugin_manifests(folder, limit=limit))
    root: Path | None = None
    if manifests:
        counts: dict[Path, int] = {}
        for hit in manifests:
            # hit.parent == the module folder → hit.parent.parent == the layer above it.
            # For a plugin.yaml sitting directly in `folder`, that lands on folder.parent:
            # correct, and exactly what the "you pointed at a single module" rescue needs.
            counts[hit.parent.parent] = counts.get(hit.parent.parent, 0) + 1
        root = sorted(counts, key=lambda p: (-counts[p], len(p.parts), str(p)))[0]
    return ModuleCollection(folder=folder, manifests=manifests, module_root=root)


def hint_for_streamlit_tab(project: Path) -> str:
    """The operator is on the Streamlit tab with a folder it cannot build.

    "這裡沒有 Streamlit 入口" is true and useless when what they have is a CIM module
    collection. Name what the folder IS, and name a field that will ACCEPT it — this
    hint used to say 「平台專案」, which rejects it every single time.
    """
    collection = find_module_collection(project)
    if not collection.found or collection.module_root is None:
        return ""
    return (f"\n這個資料夾底下有 {collection.count()} plugin.yaml(例:{collection.example()}),"
            f"看起來是 CIM 平台模組集合,不是 Streamlit 專案。\n"
            f"請改用「{CIM_TAB}」分頁,把「{MODULE_ROOT_FIELD}」指到:{collection.module_root}\n"
            f"{PLATFORM_FIELD_IS_THE_PLATFORM}")


def hint_for_cim_tab(folder: Path) -> str:
    """The operator is on the CIM tab (the tab the GUI opens on) with a folder that
    failed to scan. Three different truths, and the wrong one is a dead end:

    * the modules are there, just one layer down  → name the layer;
    * this is a Streamlit project                 → name the other tab;
    * they already point at the module root       → say nothing, the fault is elsewhere.

    A single module folder gets nothing here on purpose: `discover_source_modules`
    raises a better-worded error naming its parent, and printing both would show the
    operator the same paragraph twice.
    """
    folder = Path(folder)
    try:
        collection = find_module_collection(folder)
        if collection.found:
            if collection.already_correct or collection.is_single_module:
                return ""
            return ("\n\n這個資料夾底下其實有 plugin.yaml"
                    f"(例:{collection.example()}),只是不在掃描的那一層。\n"
                    f"請把「{MODULE_ROOT_FIELD}」改指到:{collection.module_root}\n"
                    f"(「{MODULE_ROOT_FIELD}」= 直接裝著各個模組資料夾的那一層。)")
        if not looks_like_streamlit(folder):
            return ""
        entry = find_entrypoint(folder)
    except OSError:
        return ""

    which = f"(入口:{entry.value.name})" if entry.found else "(入口有多個候選,屆時可自行指定)"
    return (f"\n\n這看起來是一個 Streamlit 專案{which},不是 CIM 平台模組"
            "(本頁需要 plugin.yaml)。\n"
            f"請改用上方的「{STREAMLIT_TAB}」分頁——"
            "它會把這個專案打成 User 可直接執行的資料夾。\n"
            f"(那一頁的「{SD_PROJECT_FIELD}」填:{folder})")


def _is_streamlit_app(path: Path) -> bool:
    """Imports streamlit AND looks like a page, not a helper module: a Streamlit
    entry script calls st.<something> at module level (title/write/set_page_config…)."""
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return False
    if "import streamlit" not in text:
        return False
    return any(marker in text for marker in
               ("st.set_page_config", "st.title(", "st.write(", "st.header(",
                "st.markdown(", "st.sidebar", "st.columns("))


def find_entrypoint(project: Path) -> Detected:
    """The Streamlit entry script. Conventional names first (nearest to the root
    wins), then any file that both imports streamlit and renders something.
    Two equally plausible candidates = we ask. Guessing wrong is a build that
    fails minutes later, or worse, one that ships the wrong page."""
    project = Path(project)
    if not project.is_dir():
        return Detected(None, "專案資料夾不存在")

    candidates = list(_searchable_py(project))

    by_name = [p for p in candidates if p.name in ENTRY_CANDIDATES]
    by_name.sort(key=lambda p: (len(p.relative_to(project).parts), p.name))
    if by_name:
        # A conventional name at the shallowest level; ties broken by name order.
        best = by_name[0]
        depth = len(best.relative_to(project).parts)
        rivals = [p for p in by_name
                  if len(p.relative_to(project).parts) == depth and p.name == best.name]
        if len(rivals) > 1:
            names = "、".join(p.relative_to(project).as_posix() for p in rivals[:4])
            return Detected(None, "有多個候選",
                            hint=_ask_or_rescue(project,
                                                f"找到多個同名入口({names}…),請自行指定。"))
        return Detected(best, f"自動偵測:{best.relative_to(project).as_posix()}")

    apps = [p for p in candidates if _is_streamlit_app(p)]
    if len(apps) == 1:
        rel = apps[0].relative_to(project).as_posix()
        return Detected(apps[0], f"自動偵測:{rel}(唯一會算繪畫面的 Streamlit 檔案)")
    if len(apps) > 1:
        # ANnoTation lands HERE, not in the "no streamlit at all" branch below: its
        # 18 CIM modules each import streamlit, so we confidently offered the
        # operator four of them to choose an entry point from — on the wrong tab,
        # for a project that has no single app to build. Both dead ends need the
        # way out, not just the empty one.
        names = "、".join(p.relative_to(project).as_posix() for p in apps[:4])
        return Detected(None, "有多個候選",
                        hint=_ask_or_rescue(
                            project, f"找到多個可能的入口({names}…),請用「瀏覽…」自行指定。"))
    # The empty dead end. Here the diagnosis is not harmful advice, so it stays and the
    # rescue is appended to it.
    return Detected(None, "找不到",
                    hint="這個資料夾裡沒有 import streamlit 的 .py,請確認選對專案。"
                         + hint_for_streamlit_tab(project))


def _ask_or_rescue(project: Path, ask: str) -> str:
    """「請自行指定」 is the right answer for a Streamlit project with two plausible
    pages. It is the WRONG answer for a CIM module collection: those "candidates" are
    18 CIM modules, and inviting the operator to pick one as a Streamlit entry point is
    the mistake the rescue exists to prevent — offering it and then explaining they are
    on the wrong tab just leaves both doors open. When the rescue fires, it replaces
    the invitation instead of trailing after it."""
    rescue = hint_for_streamlit_tab(project)
    return rescue.lstrip("\n") if rescue else ask


def suggest_name(project: Path) -> str:
    """CV_Viewer must stay "CV Viewer", not become "Cv Viewer" — .title() mangles
    every acronym, and this string ends up in the manifest, the README, the
    window title and the tool dropdown the operator reads on the factory floor."""
    words = Path(project).name.replace("-", " ").replace("_", " ").split()
    return " ".join(w if any(c.isupper() for c in w) else w.capitalize() for w in words)


def looks_like_streamlit(project: Path) -> bool:
    """A weak signal, on purpose: enough to say "you are on the wrong tab" even
    when we cannot pin down which file is the entry point."""
    project = Path(project)
    if not project.is_dir():
        return False
    if (project / ".streamlit").is_dir():
        return True
    for name in ("requirements.txt", "requirements.lock.txt", "pyproject.toml"):
        path = project / name
        if path.is_file() and "streamlit" in path.read_text("utf-8", errors="replace").lower():
            return True
    for path in _searchable_py(project):
        try:
            if "import streamlit" in path.read_text("utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False
