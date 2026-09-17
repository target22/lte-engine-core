"""
lte/engine/layout.py -- LEAF MODULE. The corpus layout: the ONE place in
lte/ that interprets directory shapes, filename conventions, scopes and
compile targets. Compiled from config/corpus_layout.yaml.

Every other module asks this one: "which partition is this path in?",
"which locale is this file?", "which directory prefixes does target X
read?", "which prefixes must never appear in artifact X?". No module may
split a path on '/' and index into the result, and no module may build a
'.<locale>.md' suffix by hand.

PURE. Paths are repo-relative POSIX strings, never pathlib objects; this
module performs no filesystem access and imports nothing from lte.io or
lte.cli.

IGNORE PATTERNS are globs over paths relative to `root`, with explicit
segment semantics (unlike fnmatch, whose `*` crosses '/'):
  *   any characters except '/'        ?   one character except '/'
  **  any number of whole segments     '**/x' also matches 'x' at the root
Character classes are not supported; '[' and ']' match literally.

VOCABULARY
  root        directory under the repository that holds the corpus
              ('docs', 'governance-os', or '.' for the repository root).
  scope       a visibility class with a numeric `level`. A reference from a
              lower level to a higher level is a boundary violation. Level 0
              is publishable. A scope either owns a directory under root
              (docs/public/) or has none (path: null), in which case its
              partitions sit directly under root next to other scopes'.
  partition   a Tier-1 unit. Its id, anchor domain and scope id come from
              config/taxonomy.yaml; its directory comes from here.
  zone role   a partition may declare `default_role` (the role of a file
              whose name carries no role segment) and `pairs_with` (its
              files join the documents of another partition, matched by
              relative path). This is how a right-pane directory of debates
              attaches to a left-pane directory of policies.
  target      a compile target: the scopes it reads, the artifact it
              writes, and whether that artifact may be served publicly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping, Sequence

SUPPORTED_LAYOUT_VERSION = "1.0.0"
LOCALE_MODE_SUFFIX = "suffix"
LOCALE_MODE_NONE = "none"
LOCALE_MODES = (LOCALE_MODE_SUFFIX, LOCALE_MODE_NONE)
PUBLIC_EXPOSURE = "public"
EXPOSURES = (PUBLIC_EXPOSURE, "restricted")
PUBLISHABLE_LEVEL = 0
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_EXTENSION_RE = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]*$")
_TOP_KEYS = {"layout_version", "root", "scopes", "partitions", "filenames",
             "targets", "ignore", "staging"}


class LayoutError(ValueError):
    """config/corpus_layout.yaml is malformed or inconsistent with the taxonomy."""


# ----------------------------------------------------------------- paths

def normalize_rel(path: str, label: str = "path") -> str:
    """'./a//b/' -> 'a/b'; '.' -> ''. Rejects absolute paths, '..' and backslashes."""
    if not isinstance(path, str):
        raise LayoutError("%s must be a string, got %r" % (label, path))
    if path.startswith("/") or "\\" in path:
        raise LayoutError("%s must be a relative POSIX path: %r" % (label, path))
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise LayoutError("%s must not contain '..': %r" % (label, path))
    return "/".join(parts)


def join(*parts: str) -> str:
    return "/".join(p for p in parts if p not in ("", "."))


def is_within(path: str, directory: str) -> bool:
    """Component-wise prefix test. Every path is within the '' directory."""
    return directory == "" or path == directory or path.startswith(directory + "/")


def relative_to(path: str, directory: str) -> str:
    if not is_within(path, directory):
        raise LayoutError("%r is not under %r" % (path, directory))
    return path[len(directory):].lstrip("/") if directory else path


def compile_glob(pattern: str) -> re.Pattern:
    """Glob with segment semantics (see module docstring) -> anchored regex."""
    if not isinstance(pattern, str) or not pattern or pattern.startswith("/"):
        raise LayoutError("ignore pattern must be a non-empty relative glob: %r" % (pattern,))
    out, i, n = [], 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == n:
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _alternation(values: Sequence[str]) -> str:
    return "|".join(re.escape(v) for v in sorted(values, key=len, reverse=True))


# ----------------------------------------------------------------- specs

@dataclass(frozen=True)
class ScopeSpec:
    id: str
    path: str | None   # repo-relative directory, or None for a directory-less scope
    level: int


@dataclass(frozen=True)
class PartitionSpec:
    id: str
    scope: str
    path: str      # repo-relative directory
    default_role: str | None
    pairs_with: str | None


@dataclass(frozen=True)
class TargetSpec:
    id: str
    scopes: tuple
    artifact: str  # repo-relative file
    exposure: str
    aliases: tuple


@dataclass(frozen=True)
class FileInfo:
    """Everything the layout knows about one corpus file."""

    rel_path: str
    partition: str
    scope: str
    locale: str
    role: str | None
    document_id: str
    tractate: tuple
    group_partition: str
    extension: str

    @property
    def group_key(self) -> tuple:
        """Tier-3 grouping key: files sharing it form one logical document."""
        return (self.group_partition, self.tractate, self.document_id)


@dataclass(frozen=True)
class CorpusLayout:
    root: str
    scopes: tuple
    partitions: tuple             # taxonomy order; this is emission order
    locale_mode: str
    locales: tuple                # also the artifact's top-level key order
    extensions: tuple
    document_roles: tuple
    targets: tuple
    ignore: tuple                 # compiled patterns
    staging_root: str | None
    filename_pattern: re.Pattern
    _scopes: Mapping = field(repr=False)
    _partitions: Mapping = field(repr=False)
    _targets: Mapping = field(repr=False)

    @property
    def locales_suffixed(self) -> bool:
        """True when filenames carry a locale segment (mode: suffix)."""
        return self.locale_mode == LOCALE_MODE_SUFFIX

    # -- lookups -------------------------------------------------------
    def scope(self, scope_id: str) -> ScopeSpec:
        try:
            return self._scopes[scope_id]
        except KeyError:
            raise LayoutError("unknown scope %r" % scope_id) from None

    def partition(self, partition_id: str) -> PartitionSpec:
        try:
            return self._partitions[partition_id]
        except KeyError:
            raise LayoutError("unknown partition %r" % partition_id) from None

    def target(self, name: str) -> TargetSpec:
        """Canonical target for an id or alias."""
        try:
            return self._targets[name]
        except KeyError:
            raise LayoutError("unknown compile target %r (known: %s)"
                              % (name, ", ".join(self.target_names()))) from None

    def target_names(self) -> list:
        return sorted(self._targets)

    def target_accepts(self, name: str) -> tuple:
        """Every name that designates the same target as `name`."""
        spec = self.target(name)
        return (spec.id,) + spec.aliases

    # -- directory prefixes ------------------------------------------
    def root_prefix(self) -> str:
        """Listing prefix for the whole corpus: 'docs/', or '' for the repository root."""
        return self.root + "/" if self.root else ""

    def root_relative(self, rel_path: str) -> str:
        """A repo-relative corpus path -> relative to the corpus root."""
        return relative_to(rel_path, self.root)

    def partition_prefix(self, partition_id: str) -> str:
        """Listing prefix with a trailing '/', or '' for the repository root."""
        path = self.partition(partition_id).path
        return path + "/" if path else ""

    def scope_prefixes(self, scope_id: str) -> tuple:
        """The scope's directory prefix, or its partitions' prefixes if it has none."""
        spec = self.scope(scope_id)
        if spec.path is not None:
            return (spec.path + "/" if spec.path else "",)
        return tuple(self.partition_prefix(p.id) for p in self.partitions if p.scope == scope_id)

    def walk_directories(self) -> list:
        """Directories an ingest walk covers: scope directories, or root for directory-less scopes."""
        dirs = {s.path if s.path is not None else self.root for s in self.scopes}
        return sorted(d for d in dirs if not any(o != d and is_within(d, o) for o in dirs))

    def partitions_for_target(self, name: str) -> list:
        """Partition ids a target reads, in taxonomy (emission) order."""
        scopes = set(self.target(name).scopes)
        return [p.id for p in self.partitions if p.scope in scopes]

    def emitted_partitions_for_target(self, name: str) -> list:
        """As partitions_for_target, minus zones absorbed via pairs_with."""
        return [pid for pid in self.partitions_for_target(name)
                if self.partition(pid).pairs_with is None]

    def restricted_prefixes(self, name: str) -> tuple:
        """Scope directory prefixes whose content must never reach this target."""
        included = set(self.target(name).scopes)
        return tuple(sorted({prefix for s in self.scopes if s.id not in included
                             for prefix in self.scope_prefixes(s.id)}))

    def restricted_source_prefixes(self) -> tuple:
        """Prefixes of every scope above the publishable level."""
        return tuple(sorted({prefix for s in self.scopes if s.level != PUBLISHABLE_LEVEL
                             for prefix in self.scope_prefixes(s.id)}))

    def default_target(self, restricted: bool = False) -> str:
        """First declared target of the requested exposure."""
        for spec in self.targets:
            if (spec.exposure != PUBLIC_EXPOSURE) == restricted:
                return spec.id
        raise LayoutError("no %s compile target is declared" % ("restricted" if restricted else "public"))

    def restricted_target_names(self) -> tuple:
        return tuple(sorted(n for n, t in self._targets.items() if t.exposure != PUBLIC_EXPOSURE))

    def artifact_path(self, name: str) -> str:
        return self.target(name).artifact

    # -- classification ----------------------------------------------
    def is_ignored(self, rel_path: str) -> bool:
        if not self.ignore or not is_within(rel_path, self.root):
            return False
        local = relative_to(rel_path, self.root)
        return any(pattern.match(local) for pattern in self.ignore)

    def partition_of_path(self, rel_path: str) -> PartitionSpec | None:
        for spec in self.partitions:
            if is_within(rel_path, spec.path) and rel_path != spec.path:
                return spec
        return None

    def parse_filename(self, name: str) -> tuple | None:
        """-> (slug, role | None, locale, extension), or None if non-conforming."""
        match = self.filename_pattern.match(name)
        if not match:
            return None
        groups = match.groupdict()
        locale = groups.get("locale") or self.locales[0]
        return groups["slug"], groups.get("role"), locale, groups["ext"]

    def classify(self, rel_path: str) -> FileInfo | None:
        """
        None when the path is ignored, outside every partition, or its name
        does not follow the filename convention. Never raises for such a
        path: callers decide whether "not a corpus file" is an error.
        """
        if self.is_ignored(rel_path):
            return None
        spec = self.partition_of_path(rel_path)
        if spec is None:
            return None
        local = relative_to(rel_path, spec.path).split("/")
        parsed = self.parse_filename(local[-1])
        if parsed is None:
            return None
        slug, role, locale, extension = parsed
        return FileInfo(
            rel_path=rel_path,
            partition=spec.id,
            scope=spec.scope,
            locale=locale,
            role=role if role is not None else spec.default_role,
            document_id=slug,
            tractate=tuple(local[:-1]),
            group_partition=spec.pairs_with or spec.id,
            extension=extension,
        )

    def locale_of(self, rel_path: str) -> str | None:
        """Locale of a corpus file; None for anything classify() rejects."""
        info = self.classify(rel_path)
        return info.locale if info else None

    def locale_of_name(self, rel_path: str) -> str | None:
        """
        Locale from the file NAME alone, ignoring partition membership and
        ignore rules. For whole-tree linting, which must still inspect a
        conforming file placed outside every partition directory.
        """
        parsed = self.parse_filename(rel_path.rsplit("/", 1)[-1])
        return parsed[2] if parsed else None

    def has_corpus_extension(self, rel_path: str) -> bool:
        return rel_path.endswith(self.extensions)

    def listing_suffixes(self, locale: str | None = None) -> tuple:
        """Coarse filename suffixes for git/glob pre-filtering; classify() is exact."""
        if self.locale_mode == LOCALE_MODE_NONE:
            return self.extensions
        locales = self.locales if locale is None else (locale,)
        return tuple(".%s%s" % (loc, ext) for loc in locales for ext in self.extensions)

    def discovery_globs(self, locale: str | None = None) -> tuple:
        return tuple("*" + suffix for suffix in self.listing_suffixes(locale))

    # -- policy -------------------------------------------------------
    def is_boundary_violation(self, source_scope: str, target_scope: str) -> bool:
        """A reference may not point from a lower-level scope into a higher one."""
        return self.scope(source_scope).level < self.scope(target_scope).level

    def staging_target(self, path: str) -> str | None:
        """'.../<staging_root>/<a>/<b>.md' -> '<root>/<a>/<b>.md'."""
        if not self.staging_root:
            return None
        parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
        if self.staging_root not in parts:
            return None
        tail = parts[parts.index(self.staging_root) + 1:]
        return join(self.root, *tail) if tail else None

    def corpus_path(self, root_relative: str) -> str:
        """A path relative to the corpus root -> repo-relative."""
        return join(self.root, normalize_rel(root_relative))


# --------------------------------------------------------------- compile

def _row(row) -> tuple:
    if hasattr(row, "get"):
        return row.get("id"), row.get("scope")
    return row[0], row[4]


def _require_mapping(value, label) -> Mapping:
    if not isinstance(value, Mapping):
        raise LayoutError("%s must be a mapping" % label)
    return value


def _require_list(value, label, allow_empty=False) -> list:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise LayoutError("%s must be a %slist" % (label, "" if allow_empty else "non-empty "))
    return value


def _token(value, label) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.match(value):
        raise LayoutError("%s must match %s, got %r" % (label, _TOKEN_RE.pattern, value))
    return value


def _check_disjoint(paths: Mapping, what: str, label: str) -> None:
    items = sorted(paths.items())
    for i, (a_id, a) in enumerate(items):
        for b_id, b in items[i + 1:]:
            if is_within(a, b) or is_within(b, a):
                raise LayoutError("%s: %s %r (%r) and %r (%r) overlap; directories must be disjoint"
                                  % (label, what, a_id, a or ".", b_id, b or "."))


def compile_layout(data: Mapping, partition_rows: Sequence, document_roles: Sequence[str],
                   label: str = "config/corpus_layout.yaml") -> CorpusLayout:
    """
    Validates the layout document against the taxonomy rows and the
    grammar's document roles, and compiles it. Fail-fast: every problem is
    a LayoutError at load time.
    """
    data = _require_mapping(data, label)
    version = data.get("layout_version")
    if version != SUPPORTED_LAYOUT_VERSION:
        raise LayoutError("%s: layout_version %r is not supported (expected %r)"
                          % (label, version, SUPPORTED_LAYOUT_VERSION))
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise LayoutError("%s: unknown top-level key(s): %s" % (label, ", ".join(unknown)))

    root = normalize_rel(data.get("root", "."), label + ": root")

    # scopes
    scopes = []
    scope_by_id = {}
    for i, entry in enumerate(_require_list(data.get("scopes"), label + ": scopes")):
        entry = _require_mapping(entry, "%s: scopes[%d]" % (label, i))
        sid = _token(entry.get("id"), "%s: scopes[%d].id" % (label, i))
        if sid in scope_by_id:
            raise LayoutError("%s: duplicate scope id %r" % (label, sid))
        level = entry.get("level")
        if not isinstance(level, int) or isinstance(level, bool) or level < 0:
            raise LayoutError("%s: scope %r needs an integer level >= 0" % (label, sid))
        raw_path = entry.get("path", sid)
        path = None if raw_path is None else join(root, normalize_rel(raw_path, "scope %r path" % sid))
        spec = ScopeSpec(sid, path, level)
        scopes.append(spec)
        scope_by_id[sid] = spec
    _check_disjoint({s.id: s.path for s in scopes if s.path is not None}, "scopes", label)

    # partitions: identity from the taxonomy, directories from here
    roles = tuple(document_roles)
    overrides = _require_mapping(data.get("partitions") or {}, label + ": partitions")
    rows = [_row(r) for r in partition_rows]
    known_ids = {pid for pid, _ in rows}
    stray = sorted(set(overrides) - known_ids)
    if stray:
        raise LayoutError("%s: partitions not in config/taxonomy.yaml: %s" % (label, ", ".join(stray)))
    partitions = []
    for pid, scope_id in rows:
        if scope_id not in scope_by_id:
            raise LayoutError("%s: taxonomy partition %r uses scope %r, which is not declared here"
                              % (label, pid, scope_id))
        entry = _require_mapping(overrides.get(pid) or {}, "%s: partitions.%s" % (label, pid))
        extra = sorted(set(entry) - {"path", "default_role", "pairs_with"})
        if extra:
            raise LayoutError("%s: partitions.%s: unknown key(s) %s" % (label, pid, ", ".join(extra)))
        default_role = entry.get("default_role")
        if default_role is not None and default_role not in roles:
            raise LayoutError("%s: partitions.%s.default_role %r is not a document role (%s)"
                              % (label, pid, default_role, ", ".join(roles)))
        base = scope_by_id[scope_id].path
        path = join(root if base is None else base, normalize_rel(entry.get("path", pid), "partition %r path" % pid))
        if path == root:
            raise LayoutError("%s: partitions.%s resolves to the corpus root itself" % (label, pid))
        for other in scopes:
            if other.path and other.id != scope_id and is_within(path, other.path):
                raise LayoutError("%s: partition %r (scope %s) lies inside scope %r's directory %s"
                                  % (label, pid, scope_id, other.id, other.path))
        partitions.append(PartitionSpec(pid, scope_id, path, default_role, entry.get("pairs_with")))
    partition_by_id = {p.id: p for p in partitions}
    _check_disjoint({p.id: p.path for p in partitions}, "partitions", label)
    for p in partitions:
        if p.pairs_with is None:
            continue
        other = partition_by_id.get(p.pairs_with)
        if other is None or other.id == p.id:
            raise LayoutError("%s: partitions.%s.pairs_with %r is not another partition"
                              % (label, p.id, p.pairs_with))
        if other.pairs_with is not None:
            raise LayoutError("%s: partitions.%s pairs with %r, which itself pairs; chains are not allowed"
                              % (label, p.id, other.id))
        if other.scope != p.scope:
            raise LayoutError("%s: partitions.%s (scope %s) cannot pair with %r (scope %s): grouping "
                              "across scopes would mix visibility levels in one document"
                              % (label, p.id, p.scope, other.id, other.scope))

    # filenames
    names = _require_mapping(data.get("filenames"), label + ": filenames")
    locale_block = _require_mapping(names.get("locale"), label + ": filenames.locale")
    mode = locale_block.get("mode")
    if mode not in LOCALE_MODES:
        raise LayoutError("%s: filenames.locale.mode must be one of %s" % (label, ", ".join(LOCALE_MODES)))
    if mode == LOCALE_MODE_SUFFIX:
        locales = tuple(_token(v, label + ": filenames.locale.values[]")
                        for v in _require_list(locale_block.get("values"), label + ": filenames.locale.values"))
    else:
        locales = (_token(locale_block.get("default"), label + ": filenames.locale.default"),)
    if len(set(locales)) != len(locales):
        raise LayoutError("%s: filenames.locale.values contains duplicates" % label)
    extensions = tuple(_require_list(names.get("extensions"), label + ": filenames.extensions"))
    for ext in extensions:
        if not isinstance(ext, str) or not _EXTENSION_RE.match(ext):
            raise LayoutError("%s: filenames.extensions entry %r must look like '.md'" % (label, ext))

    pattern = r"^(?P<slug>[^/]+?)"
    if roles:
        pattern += r"(?:\.(?P<role>%s))?" % _alternation(roles)
    if mode == LOCALE_MODE_SUFFIX:
        pattern += r"\.(?P<locale>%s)" % _alternation(locales)
    pattern += r"(?P<ext>%s)$" % _alternation(extensions)
    filename_pattern = re.compile(pattern)

    # targets
    targets = []
    target_by_name = {}
    for i, entry in enumerate(_require_list(data.get("targets"), label + ": targets")):
        entry = _require_mapping(entry, "%s: targets[%d]" % (label, i))
        tid = _token(entry.get("id"), "%s: targets[%d].id" % (label, i))
        aliases = tuple(_token(a, "%s: targets.%s.aliases[]" % (label, tid))
                        for a in _require_list(entry.get("aliases") or [], label, allow_empty=True))
        target_scopes = tuple(_require_list(entry.get("scopes"), "%s: targets.%s.scopes" % (label, tid)))
        for sid in target_scopes:
            if sid not in scope_by_id:
                raise LayoutError("%s: targets.%s reads unknown scope %r" % (label, tid, sid))
        exposure = entry.get("exposure")
        if exposure not in EXPOSURES:
            raise LayoutError("%s: targets.%s.exposure must be one of %s" % (label, tid, ", ".join(EXPOSURES)))
        if exposure == PUBLIC_EXPOSURE:
            above = [s for s in target_scopes if scope_by_id[s].level != PUBLISHABLE_LEVEL]
            if above:
                raise LayoutError("%s: targets.%s is public but reads scope(s) above level %d: %s"
                                  % (label, tid, PUBLISHABLE_LEVEL, ", ".join(above)))
        artifact = normalize_rel(entry.get("artifact", ""), "targets.%s.artifact" % tid)
        if not artifact:
            raise LayoutError("%s: targets.%s.artifact is required" % (label, tid))
        spec = TargetSpec(tid, target_scopes, artifact, exposure, aliases)
        for name in (tid,) + aliases:
            if name in target_by_name:
                raise LayoutError("%s: target name %r is declared twice" % (label, name))
            target_by_name[name] = spec
        targets.append(spec)
    artifacts = [t.artifact for t in targets]
    if len(set(artifacts)) != len(artifacts):
        raise LayoutError("%s: two targets write the same artifact" % label)
    for t in targets:
        for p in partitions:
            if is_within(t.artifact, p.path):
                raise LayoutError("%s: targets.%s writes its artifact inside partition %r (%s)"
                                  % (label, t.id, p.id, p.path))

    ignore = tuple(compile_glob(p) for p in
                   _require_list(data.get("ignore") or [], label + ": ignore", allow_empty=True))
    staging = _require_mapping(data.get("staging") or {}, label + ": staging")
    staging_root = staging.get("root")
    if staging_root is not None:
        staging_root = _token(staging_root, label + ": staging.root")

    return CorpusLayout(
        root=root, scopes=tuple(scopes), partitions=tuple(partitions), locale_mode=mode,
        locales=locales, extensions=extensions, document_roles=roles, targets=tuple(targets),
        ignore=ignore, staging_root=staging_root, filename_pattern=filename_pattern,
        _scopes=scope_by_id, _partitions=partition_by_id, _targets=target_by_name,
    )
