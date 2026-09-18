#!/usr/bin/env python3
"""
lte/cli/hooks/pre_commit.py -- ORCHESTRATOR. Git pre-commit entry point.

REPLACES scripts/core_pipeline/git_pre_commit_hook.py. Enforces, on the
staged docs/*.md changes:

  1. the ref-gated payload lock            (lte.engine.payload_lock)
  2. the atomic deprecation guardrail      (lte.engine.payload_lock +
                                            lte.engine.state_machine)

TWO HALVES, ONE FILE.

  Host half (module top level, main, dispatch): Python 3.5 floor, stdlib
  only, NO lte.engine / lte.io import at module level. Git executes this
  file directly on the 3.6.8 host, where lte.engine cannot even be parsed.
  It re-executes the checks inside the lte-cas container.

  Container half (_run_checks): imports lte.engine / lte.io lazily and
  runs only when LTE_HOOK_IN_CONTAINER=1 or LTE_HOOK_NO_DOCKER=1.

  The five payload_lock calls below no longer pass an admonition->kind map.
  A dependent block's role comes from the configured alias table, which
  reaches payload_lock on the Grammar it already takes, so the parameter had
  become a second path for information already present.

Install (symlink resolves to the code checkout, so the hook follows updates):
  ln -sf ../../lte/cli/hooks/pre_commit.py .git/hooks/pre-commit

Environment:
  LTE_HOOK_IMAGE      container image (default: lte-cas)
  LTE_HOOK_NO_DOCKER  run the checks in-process (interpreter must be 3.9+)
  LTE_CODE_ROOT       checkout holding lte/ and config/ (default: derived
                      from this file's real path)

Mounts: code root -> /lte-src (ro), work tree -> /work (ro). They are
separate so a fixture repository with no lte/ of its own can be checked.

Which staged paths are corpus files, and their locales, come from
config/corpus_layout.yaml inside the container; the host half knows nothing
about the corpus layout.

Exit codes: 0 allow, 1 block. Infrastructure failures (docker missing,
image missing) also block: the hook fails closed. Bypass deliberately with
`git commit --no-verify`.
"""

import os
import subprocess
import sys

IN_CONTAINER_ENV = "LTE_HOOK_IN_CONTAINER"
NO_DOCKER_ENV = "LTE_HOOK_NO_DOCKER"
IMAGE_ENV = "LTE_HOOK_IMAGE"
CODE_ROOT_ENV = "LTE_CODE_ROOT"
DEFAULT_IMAGE = "lte-cas"
CODE_MOUNT = "/lte-src"
WORK_MOUNT = "/work"
MIN_CHECK_PYTHON = (3, 9)
EXIT_ALLOW = 0
EXIT_BLOCK = 1
TAG = "[pre-commit]"


def _say(message):
    sys.stderr.write("{0} {1}\n".format(TAG, message))


# --------------------------------------------------------------- host half

def code_root():
    """<root>/lte/cli/hooks/pre_commit.py -> <root>, following symlinks."""
    explicit = os.environ.get(CODE_ROOT_ENV)
    if explicit:
        return os.path.realpath(explicit)
    here = os.path.realpath(__file__)
    for _ in range(4):
        here = os.path.dirname(here)
    return here


def work_root():
    proc = subprocess.Popen(["git", "rev-parse", "--show-toplevel"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError("not inside a git work tree: {0}".format(err.strip()))
    return os.path.realpath(out.strip())


def translate_index_file(value, host_root):
    """
    Git exports GIT_INDEX_FILE to hooks (e.g. .git/next-index-*.lock during
    `commit -a`). A relative value is relative to the work tree root and is
    valid unchanged under WORK_MOUNT; an absolute one must be rebased.
    """
    if not value:
        return None
    if not os.path.isabs(value):
        return value
    real = os.path.realpath(value)
    if real == host_root or real.startswith(host_root + os.sep):
        return WORK_MOUNT + real[len(host_root):].replace(os.sep, "/")
    return None


def docker_argv(image, host_code_root, host_work_root, hook_args, index_file):
    argv = [
        "docker", "run", "--rm",
        "-v", "{0}:{1}:ro".format(host_code_root, CODE_MOUNT),
        "-v", "{0}:{1}:ro".format(host_work_root, WORK_MOUNT),
        "-w", WORK_MOUNT,
        "-e", "PYTHONPATH={0}".format(CODE_MOUNT),
        "-e", "SRKH_REPO_ROOT={0}".format(CODE_MOUNT),
        "-e", "SRKH_CONFIG_DIR={0}/config".format(CODE_MOUNT),
        "-e", "GIT_OPTIONAL_LOCKS=0",
        "-e", "{0}=1".format(IN_CONTAINER_ENV),
    ]
    if index_file:
        argv += ["-e", "GIT_INDEX_FILE={0}".format(index_file)]
    argv += [image, "python", "-m", "lte.cli.hooks.pre_commit"]
    return argv + list(hook_args)


def dispatch_to_container(hook_args):
    try:
        host_work = work_root()
    except RuntimeError as exc:
        _say(str(exc))
        return EXIT_BLOCK
    host_code = code_root()
    if not os.path.isdir(os.path.join(host_code, "lte")):
        _say("code root {0} has no lte/ package; set {1}".format(host_code, CODE_ROOT_ENV))
        return EXIT_BLOCK
    index_file = translate_index_file(os.environ.get("GIT_INDEX_FILE"), host_work)
    if os.environ.get("GIT_INDEX_FILE") and index_file is None:
        _say("GIT_INDEX_FILE is outside the work tree; cannot expose it to the container")
        return EXIT_BLOCK
    argv = docker_argv(os.environ.get(IMAGE_ENV, DEFAULT_IMAGE), host_code, host_work,
                       hook_args, index_file)
    try:
        rc = subprocess.call(argv)
    except OSError as exc:
        _say("cannot run docker ({0}); commit blocked. Bypass deliberately with "
             "`git commit --no-verify`.".format(exc))
        return EXIT_BLOCK
    if rc in (125, 126, 127):
        _say("container launch failed (docker exit {0}); commit blocked. Is the '{1}' "
             "image built?".format(rc, os.environ.get(IMAGE_ENV, DEFAULT_IMAGE)))
        return EXIT_BLOCK
    return EXIT_ALLOW if rc == 0 else EXIT_BLOCK


def main(argv=None):
    hook_args = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get(IN_CONTAINER_ENV) == "1" or os.environ.get(NO_DOCKER_ENV) == "1":
        if sys.version_info < MIN_CHECK_PYTHON:
            _say("in-process checks need Python {0}.{1}+, found {2}.{3}; unset {4}".format(
                MIN_CHECK_PYTHON[0], MIN_CHECK_PYTHON[1],
                sys.version_info[0], sys.version_info[1], NO_DOCKER_ENV))
            return EXIT_BLOCK
        # Executed by Git as a file, sys.path[0] is lte/cli/hooks/, not the
        # code root; `import lte` needs the root on the path.
        root = code_root()
        if root not in sys.path:
            sys.path.insert(0, root)
        return _run_checks(hook_args)
    return dispatch_to_container(hook_args)


# ---------------------------------------------------------- container half

def _run_checks(hook_args):
    import argparse

    from lte.engine import payload_lock
    from lte.engine.grammar import ConfigError
    from lte.engine.layout import LayoutError
    from lte.io import config_reader
    from lte.io.git_cas_client import GitClient, GitError

    parser = argparse.ArgumentParser(prog="pre-commit")
    parser.add_argument("--lock-on-staged", action="store_true",
                        help="compute locks from references in the index instead of HEAD")
    parser.add_argument("--work-tree", default=os.getcwd())
    args = parser.parse_args(hook_args)

    try:
        config = config_reader.load()
    except (ConfigError, LayoutError, OSError) as exc:
        _say("CONFIG ERROR: {0}".format(exc))
        return EXIT_BLOCK
    grammar, layout = config.grammar, config.layout
    prefix, suffixes = layout.root_prefix(), layout.listing_suffixes()
    client = GitClient(cwd=args.work_tree, safe_directory=True)

    def corpus_only(paths):
        return [p for p in paths if layout.classify(p) is not None]

    try:
        if client.rev_parse("HEAD") is None:
            _say("OK -- initial commit, nothing is locked yet")
            return EXIT_ALLOW
        changes = [payload_lock.Change(c.status, c.path, c.old_path)
                   for c in client.staged_changes(prefix, suffixes)
                   if layout.classify(c.old_path) is not None or layout.classify(c.path) is not None]
        if not changes:
            return EXIT_ALLOW
        head_images = client.read_texts("HEAD", corpus_only(client.list_paths("HEAD", prefix, suffixes)))
        index_images = client.read_texts("", corpus_only(client.list_index_paths(prefix, suffixes)))
    except (GitError, UnicodeDecodeError) as exc:
        _say("GIT ERROR: {0}".format(exc))
        return EXIT_BLOCK

    head_refs = payload_lock.reference_map(grammar, head_images, layout=layout)
    lock_refs = (payload_lock.reference_map(grammar, index_images, layout=layout)
                 if args.lock_on_staged else head_refs)

    policy = grammar.payload_lock
    mutable = tuple(policy.get("mutable_frontmatter_keys") or ())
    lock_violations = []
    for change in changes:
        if change.status not in ("M", "D", "R"):
            continue
        lock_violations += payload_lock.check_payload_lock(
            grammar, change,
            head_images.get(change.old_path),
            None if change.status == "D" else index_images.get(change.path),
            lock_refs, mutable, layout=layout)

    transitions = payload_lock.newly_invalidating(grammar, changes,
                                                  head_images, index_images, layout=layout)
    by_head_path = dict((c.old_path, c) for c in changes)
    atomic_violations = payload_lock.check_atomic_deprecation(
        grammar, transitions, head_refs, by_head_path, index_images, layout=layout)

    enforced = bool(policy.get("enforced", True))
    blocking = list(atomic_violations) + (lock_violations if enforced else [])
    if lock_violations and not enforced:
        _say("WARN -- payload_lock.enforced is false in linter_rules.json; "
             "{0} lock violation(s) reported, not blocking:".format(len(lock_violations)))
        for line in lock_violations:
            _say("  ~ " + line)
    if not blocking:
        _say("OK -- {0} staged corpus change(s) checked".format(len(changes)))
        return EXIT_ALLOW

    _say("BLOCKED -- {0} violation(s):".format(len(blocking)))
    for line in blocking:
        _say("  - " + line)
    _say("Fix the staged files, or bypass deliberately with `git commit --no-verify`.")
    return EXIT_BLOCK


if __name__ == "__main__":
    sys.exit(main())
