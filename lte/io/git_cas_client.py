"""
lte/io/git_cas_client.py -- SIDE-EFFECT BOUNDARY. The exclusive owner of
`git` subprocess calls inside lte/ (CAS reads, CAS writes, index reads).

Every call is an argv vector, never a shell string: paths and author names
containing spaces stay one argv element (see the eval note at the top of
config/build_config.yaml for the production failure this rules out).

WRITES USE A PRIVATE TEMPORARY INDEX (GIT_INDEX_FILE), never a work tree.
A CAS commit is: read-tree <parent> into the temp index -> one
`update-index --index-info` batch (mode-0 records delete, 100644 records
add) -> write-tree -> commit-tree -> update-ref with compare-and-swap on the
old value. No checkout, no nested mktree walk, and a concurrent writer that
moved the branch makes update-ref fail instead of silently losing a commit.

READS OF MANY FILES USE ONE `cat-file --batch` PROCESS, not one process per
file. Object names may be `<rev>:<path>` (a commit's tree) or `:<path>`
(the index), so the same reader serves the CAS compile path and the
pre-commit hook.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

REGULAR_FILE_MODE = "100644"
_FORBIDDEN_PATH_CHARS = ("\t", "\n", "\r", "\0")


class GitError(RuntimeError):
    """A git invocation exited non-zero. Carries the argv and stderr."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            "git exited %d: %s\n%s" % (returncode, " ".join(self.argv), stderr.strip())
        )


@dataclass(frozen=True)
class StagedChange:
    """One entry of `git diff --cached --name-status`.

    `old_path` is the path at HEAD; `path` is the path in the index. They
    differ only for renames and copies.
    """

    status: str
    path: str
    old_path: str


@dataclass(frozen=True)
class CommitResult:
    changed: bool
    commit: str | None
    parent: str | None
    tree: str | None
    added: tuple = field(default_factory=tuple)
    removed: tuple = field(default_factory=tuple)


def _check_index_path(path: str) -> None:
    if not path or path.startswith("/") or any(ch in path for ch in _FORBIDDEN_PATH_CHARS):
        raise ValueError("refusing unsafe repository path: %r" % (path,))
    if ".." in Path(path).parts:
        raise ValueError("refusing path with '..' segment: %r" % (path,))


def _matches(path: str, prefix: str, suffixes: Sequence[str]) -> bool:
    if prefix and not path.startswith(prefix):
        return False
    return not suffixes or path.endswith(tuple(suffixes))


class GitClient:
    """
    Thin argv wrapper around the `git` binary.

    git_dir         -- set for a bare CAS repository (--git-dir).
    cwd             -- working directory for a non-bare repository.
    env             -- base environment; defaults to os.environ at call time.
    safe_directory  -- pass `-c safe.directory=*`. Needed when a container
                       reads a bind-mounted repository owned by another uid.
    """

    def __init__(self, git_dir=None, cwd=None, env: Mapping[str, str] | None = None,
                 safe_directory: bool = False, git_binary: str = "git"):
        self.git_dir = str(git_dir) if git_dir is not None else None
        self.cwd = str(cwd) if cwd is not None else None
        self._env = dict(env) if env is not None else None
        self.safe_directory = safe_directory
        self.git_binary = git_binary

    # ------------------------------------------------------------ plumbing

    def _argv(self, args: Sequence[str]) -> list:
        argv = [self.git_binary]
        if self.safe_directory:
            argv += ["-c", "safe.directory=*"]
        if self.git_dir is not None:
            argv += ["--git-dir", self.git_dir]
        argv += [str(a) for a in args]
        return argv

    def _environ(self, extra: Mapping[str, str] | None) -> dict:
        env = dict(os.environ) if self._env is None else dict(self._env)
        # Never let a caller's GIT_DIR/GIT_WORK_TREE silently redirect us.
        if self.git_dir is not None:
            env.pop("GIT_DIR", None)
            env.pop("GIT_WORK_TREE", None)
        if extra:
            env.update(extra)
        return env

    def run(self, *args, input_bytes: bytes | None = None,
            extra_env: Mapping[str, str] | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
        argv = self._argv(args)
        proc = subprocess.run(
            argv,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=self._environ(extra_env),
        )
        if check and proc.returncode != 0:
            raise GitError(argv, proc.returncode, proc.stderr.decode("utf-8", "replace"))
        return proc

    def output(self, *args, **kwargs) -> str:
        """Single-value commands only (rev-parse, write-tree, config)."""
        return self.run(*args, **kwargs).stdout.decode("utf-8").strip()

    # ---------------------------------------------------------------- reads

    def is_repository(self) -> bool:
        return self.run("rev-parse", "--git-dir", check=False).returncode == 0

    def rev_parse(self, rev: str) -> str | None:
        """Commit id for `rev`, or None when it does not resolve."""
        proc = self.run("rev-parse", "--verify", "--quiet", rev + "^{commit}", check=False)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8").strip() or None

    def tree_of(self, rev: str) -> str | None:
        proc = self.run("rev-parse", "--verify", "--quiet", rev + "^{tree}", check=False)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8").strip() or None

    def config_value(self, key: str) -> str | None:
        proc = self.run("config", "--get", key, check=False)
        value = proc.stdout.decode("utf-8").strip()
        return value if proc.returncode == 0 and value else None

    def list_paths(self, rev: str, prefix: str = "",
                   suffixes: Sequence[str] = ()) -> list:
        """Sorted blob paths in `rev`'s tree, optionally filtered."""
        args = ["ls-tree", "-r", "-z", "--name-only", "--full-tree", rev]
        if prefix:
            args += ["--", prefix]
        raw = self.run(*args).stdout.decode("utf-8")
        return sorted(p for p in raw.split("\0") if p and _matches(p, prefix, suffixes))

    def blob_ids(self, rev: str, prefix: str = "", suffixes: Sequence[str] = ()) -> dict:
        """{path: blob id} in `rev`'s tree, optionally filtered."""
        args = ["ls-tree", "-r", "-z", "--full-tree", rev]
        if prefix:
            args += ["--", prefix]
        ids = {}
        for record in self.run(*args).stdout.decode("utf-8").split("\0"):
            if not record:
                continue
            meta, path = record.split("\t", 1)
            _mode, kind, oid = meta.split(" ")
            if kind == "blob" and _matches(path, prefix, suffixes):
                ids[path] = oid
        return ids

    def list_index_paths(self, prefix: str = "", suffixes: Sequence[str] = ()) -> list:
        """Sorted paths in the index (non-bare repositories)."""
        args = ["ls-files", "-z", "--cached"]
        if prefix:
            args += ["--", prefix]
        raw = self.run(*args).stdout.decode("utf-8")
        return sorted(p for p in raw.split("\0") if p and _matches(p, prefix, suffixes))

    def read_blobs(self, object_names: Iterable[str]) -> dict:
        """
        {object_name: bytes | None} through ONE `cat-file --batch`.

        None means missing or not a blob. Object names containing a newline
        cannot be expressed in this protocol and are rejected up front.
        """
        names = list(dict.fromkeys(object_names))
        if not names:
            return {}
        for name in names:
            if "\n" in name:
                raise ValueError("object name contains a newline: %r" % (name,))
        stdin = ("\n".join(names) + "\n").encode("utf-8")
        out = self.run("cat-file", "--batch", input_bytes=stdin).stdout
        result, pos = {}, 0
        for name in names:
            nl = out.index(b"\n", pos)
            header = out[pos:nl]
            pos = nl + 1
            if header.endswith((b" missing", b" ambiguous")):
                result[name] = None
                continue
            parts = header.split(b" ")
            if len(parts) != 3:
                raise GitError(["cat-file", "--batch"], 0,
                               "unparseable batch header: %r" % (header,))
            size = int(parts[2])
            data = out[pos:pos + size]
            pos += size + 1  # payload is followed by one LF
            result[name] = data if parts[1] == b"blob" else None
        return result

    def read_texts(self, rev: str, paths: Iterable[str]) -> dict:
        """
        {path: text | None}. `rev` of "" reads the INDEX (`:path`).
        Decoded as strict UTF-8: an undecodable corpus file is an error,
        matching lte.io.corpus_reader.
        """
        paths = list(paths)
        names = {("%s:%s" % (rev, p)): p for p in paths}
        blobs = self.read_blobs(names)
        texts = {}
        for name, path in names.items():
            data = blobs.get(name)
            texts[path] = None if data is None else data.decode("utf-8")
        return texts

    def show(self, rev: str, path: str) -> str | None:
        return self.read_texts(rev, [path]).get(path)

    def empty_tree(self) -> str:
        return self.output("hash-object", "-t", "tree", "--stdin", input_bytes=b"")

    def staged_changes(self, prefix: str = "", suffixes: Sequence[str] = ()) -> list:
        """
        Every staged change against HEAD (or the empty tree before the first
        commit), with rename detection. Filtered on either side of a rename.
        """
        base = self.rev_parse("HEAD") or self.empty_tree()
        args = ["diff", "--cached", "--name-status", "-z", "-M", base]
        if prefix:
            args += ["--", prefix]
        tokens = self.run(*args).stdout.decode("utf-8").split("\0")
        changes, i = [], 0
        while i < len(tokens) and tokens[i]:
            status = tokens[i][0]
            if status in ("R", "C"):
                old, new = tokens[i + 1], tokens[i + 2]
                i += 3
            else:
                old = new = tokens[i + 1]
                i += 2
            if _matches(old, prefix, suffixes) or _matches(new, prefix, suffixes):
                changes.append(StagedChange(status=status, path=new, old_path=old))
        return changes

    # --------------------------------------------------------------- writes

    @classmethod
    def init_bare(cls, path, initial_branch: str = "main") -> "GitClient":
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "--quiet", str(path)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        client = cls(git_dir=path)
        client.run("symbolic-ref", "HEAD", "refs/heads/" + initial_branch)
        return client

    def hash_blobs(self, payloads: Sequence[bytes], write: bool = True) -> list:
        """Blob ids through ONE `hash-object --stdin-paths`; writes them unless write=False."""
        if not payloads:
            return []
        with tempfile.TemporaryDirectory(prefix="lte-cas-blobs-") as tmp:
            files = []
            for index, data in enumerate(payloads):
                target = os.path.join(tmp, "%06d" % index)
                with open(target, "wb") as handle:
                    handle.write(data)
                files.append(target)
            stdin = ("\n".join(files) + "\n").encode("utf-8")
            flags = ["-w"] if write else []
            out = self.run("hash-object", *flags, "--no-filters", "--stdin-paths",
                           input_bytes=stdin).stdout.decode("utf-8").split()
        if len(out) != len(payloads):
            raise GitError(["hash-object"], 0, "expected %d ids, got %d" % (len(payloads), len(out)))
        return out

    def commit_paths(self, branch: str, additions: Mapping[str, bytes], *,
                     message: str, author_name: str, author_email: str,
                     removals: Iterable[str] = (),
                     replace_prefix: str | None = None,
                     replace_suffixes: Sequence[str] = ()) -> CommitResult:
        """
        One commit on `branch` that writes `additions` and deletes `removals`.

        replace_prefix mirrors a directory: every existing path under it
        (optionally limited to replace_suffixes) that is NOT in `additions`
        is deleted. Returns changed=False, and writes nothing, when the
        resulting tree equals the parent's.
        """
        for path in additions:
            _check_index_path(path)
        ref = "refs/heads/" + branch
        parent = self.rev_parse(ref)
        parent_tree = self.tree_of(ref) if parent else None
        existing = set(self.list_paths(parent)) if parent else set()

        remove = set(removals)
        if replace_prefix is not None:
            remove |= {p for p in existing if _matches(p, replace_prefix, replace_suffixes)}
        remove = (remove & existing) - set(additions)

        ordered = sorted(additions)
        oids = self.hash_blobs([additions[p] for p in ordered])
        null_oid = "0" * len(oids[0]) if oids else "0" * len(parent_tree or "0" * 40)

        records = []
        for path in sorted(remove):
            records.append("0 %s\t%s\0" % (null_oid, path))
        for path, oid in zip(ordered, oids):
            records.append("%s %s\t%s\0" % (REGULAR_FILE_MODE, oid, path))

        with tempfile.TemporaryDirectory(prefix="lte-cas-index-") as tmp:
            index_env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
            if parent:
                self.run("read-tree", parent, extra_env=index_env)
            else:
                self.run("read-tree", "--empty", extra_env=index_env)
            if records:
                self.run("update-index", "-z", "--index-info",
                         input_bytes="".join(records).encode("utf-8"), extra_env=index_env)
            tree = self.output("write-tree", extra_env=index_env)

        if parent and tree == parent_tree:
            return CommitResult(changed=False, commit=parent, parent=parent, tree=tree)

        identity = {
            "GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email,
        }
        args = ["commit-tree", tree, "-F", "-"]
        if parent:
            args[2:2] = ["-p", parent]
        commit = self.output(*args, input_bytes=message.encode("utf-8"), extra_env=identity)
        # Compare-and-swap: fails if another writer moved the ref meanwhile.
        self.run("update-ref", "-m", "lte ingest", ref, commit, parent or null_oid)
        return CommitResult(changed=True, commit=commit, parent=parent, tree=tree,
                            added=tuple(ordered), removed=tuple(sorted(remove)))
