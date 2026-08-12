"""GitHub clone + HEAD-SHA helpers for Phase 1 ingestion.

Two entry points:

* :func:`clone_to_tempdir` clones a repo into a tempdir scoped to a context
  manager; the directory is removed on exit even if indexing fails.
* :func:`remote_head_sha` does a lightweight ``git ls-remote`` (no clone)
  used by the revisit-staleness check.

The clone is shallow and single-branch. We never need history beyond HEAD in
v1 — TODO/FIXME archaeology (Lane B) uses ``git log`` on a fresh full clone
when activated.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import git
import httpx
import structlog

log = structlog.get_logger(__name__)


GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$"
)


@dataclass(frozen=True, slots=True)
class CloneResult:
    repo_url: str
    head_sha: str
    path: Path
    owner: str
    name: str

    @property
    def repo_id(self) -> str:
        """Canonical primary key used across the schema: ``owner/name@sha``."""
        return f"{self.owner}/{self.name}@{self.head_sha}"


def parse_github_url(repo_url: str) -> tuple[str, str]:
    """Return ``(owner, name)`` for a public GitHub URL.

    Raises ``ValueError`` for anything that is not a recognised public-GitHub
    ``https://github.com/<owner>/<name>`` URL. The hard scope fence in
    ``docs/01_PROBLEM_AND_SOLUTION.md`` rules out other hosts in v1.
    """
    match = GITHUB_URL_RE.match(repo_url.strip())
    if not match:
        raise ValueError(
            f"not a public GitHub URL: {repo_url!r}. "
            "v1 supports https://github.com/<owner>/<name> only."
        )
    return match.group("owner"), match.group("name")


def remote_head_sha(repo_url: str) -> str:
    """Return the current default-branch HEAD SHA via ``git ls-remote HEAD``.

    Used by the revisit-staleness check — cheap, no clone. Raises
    ``RuntimeError`` if ls-remote does not return a HEAD ref (e.g. private repo,
    bad URL, transient network failure).
    """
    output = git.cmd.Git().ls_remote(repo_url, "HEAD")
    # `git ls-remote <url> HEAD` prints "<sha>\tHEAD" — first 40 chars is the SHA.
    for line in str(output).splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == "HEAD" and len(sha) == 40:
            return sha
    raise RuntimeError(f"ls-remote returned no HEAD for {repo_url!r}: {output!r}")


def remote_repo_size_kb(repo_url: str, *, github_pat: str | None = None) -> int | None:
    """Repository size in KiB from the GitHub API, or ``None`` if unknown.

    Asked before cloning: the clone is the one stage with no size bound, and on
    a small host a large repository dies inside ``git clone`` rather than at the
    line-count cap, which is checked after the scan. Unknown (rate limit, API
    outage) means "do not block indexing" — the caller proceeds.
    """
    owner, name = parse_github_url(repo_url)
    headers = {"Accept": "application/vnd.github+json"}
    if github_pat:
        headers["Authorization"] = f"Bearer {github_pat}"
    try:
        response = httpx.get(
            f"https://api.github.com/repos/{owner}/{name}",
            headers=headers,
            timeout=10.0,
        )
        if response.status_code != 200:
            return None
        size = response.json().get("size")
    except (httpx.HTTPError, ValueError):
        return None
    return int(size) if isinstance(size, int) else None


@contextmanager
def clone_to_tempdir(repo_url: str, *, root: Path | None = None) -> Iterator[CloneResult]:
    """Shallow-clone ``repo_url`` into a tempdir; clean up on exit.

    The yielded :class:`CloneResult` carries the path, the HEAD SHA, and a
    canonical ``repo_id`` derived from owner/name/sha. The directory is removed
    when the ``with`` block ends, success or failure.
    """
    owner, name = parse_github_url(repo_url)

    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
    tempdir = Path(tempfile.mkdtemp(prefix=f"repopilot-{owner}-{name}-", dir=root))
    try:
        log.info("clone.start", repo_url=repo_url, dest=str(tempdir))
        repo = git.Repo.clone_from(repo_url, tempdir, depth=1, single_branch=True)
        head_sha = repo.head.commit.hexsha
        log.info("clone.done", repo_url=repo_url, head_sha=head_sha)
        yield CloneResult(
            repo_url=repo_url,
            head_sha=head_sha,
            path=tempdir,
            owner=owner,
            name=name,
        )
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)


__all__ = [
    "CloneResult",
    "clone_to_tempdir",
    "parse_github_url",
    "remote_head_sha",
    "remote_repo_size_kb",
]
