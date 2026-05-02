from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .github_client import GitHubClient


def _is_404(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404


def register_tools(mcp: FastMCP, gh: GitHubClient) -> None:
    @mcp.tool()
    def list_installation_repos() -> list[dict[str, Any]]:
        """List all repositories the GitHub App is installed on."""
        body = gh.get("/installation/repositories", params={"per_page": 100})
        return [
            {"full_name": r["full_name"], "private": r["private"], "default_branch": r["default_branch"]}
            for r in body.get("repositories", [])
        ]

    @mcp.tool()
    def mint_clone_token(repos: list[str]) -> dict[str, str]:
        """Mint a short-lived (~1h) GitHub App installation token scoped
        read-only over the given repos, suitable for `git clone` / `fetch` /
        `pull` of private repos from a session container.

        The token is scoped down to {contents: read, metadata: read} so it
        cannot be used to push, comment, or otherwise mutate. All write
        operations (commit, branch, PR, issue mutation) must go through the
        other MCP tools — those route through the no-caller-SHA write
        surface that's the project's contract for safe automated writes.
        See tank-operator/CLAUDE.md → "mcp-github write surface".

        Use from a session container as:
            TOKEN=<value of `token` from this call>
            git clone https://x-access-token:${TOKEN}@github.com/owner/name.git

        Args:
            repos: list of "owner/name" strings to scope the token to.
                Required — blanket-scoped tokens are intentionally not
                offered; pass exactly the repos you need.

        Returns: {"token": "...", "expires_at": "<iso8601>"}.
        """
        if not repos:
            raise ValueError("mint_clone_token: pass at least one repo (e.g. ['nelsong6/glimmung'])")
        repo_names: list[str] = []
        for r in repos:
            if "/" not in r:
                raise ValueError(f"repo must be 'owner/name', got: {r}")
            repo_names.append(r.split("/", 1)[1])
        token, expires_at = gh.mint_scoped_token(
            repositories=repo_names,
            permissions={"contents": "read", "metadata": "read"},
        )
        return {"token": token, "expires_at": expires_at}

    @mcp.tool()
    def get_repo(owner: str, name: str) -> dict[str, Any]:
        """Return metadata for a single repo."""
        r = gh.get(f"/repos/{owner}/{name}")
        return {k: r.get(k) for k in ("full_name", "description", "default_branch", "language", "stargazers_count", "open_issues_count", "updated_at")}

    @mcp.tool()
    def create_repository(
        name: str,
        description: str | None = None,
        private: bool = False,
        auto_init: bool = True,
        org: str | None = None,
    ) -> dict[str, Any]:
        """Create a new repository.

        If `org` is given, creates under that organization via
        POST /orgs/{org}/repos. Works with App installation tokens when the
        installation has 'Administration: Write' on the org.

        Otherwise creates under the authenticated user's account via
        POST /user/repos. GitHub does not document App installation tokens as
        a supported auth method for that endpoint, so this path will likely
        return 403; create personal-account repos with `gh repo create` or a
        PAT in that case.

        auto_init=True seeds an initial README so the repo has a default
        branch you can immediately commit to via create_or_update_file or
        commit_to_branch. Set False if you'll push initial content yourself.
        """
        payload: dict[str, Any] = {
            "name": name,
            "private": private,
            "auto_init": auto_init,
        }
        if description is not None:
            payload["description"] = description
        path = f"/orgs/{org}/repos" if org else "/user/repos"
        r = gh.post(path, json=payload)
        return {
            "full_name": r["full_name"],
            "html_url": r["html_url"],
            "default_branch": r["default_branch"],
            "private": r["private"],
        }

    @mcp.tool()
    def get_file_contents(owner: str, name: str, path: str, ref: str | None = None) -> dict[str, Any]:
        """Fetch a file's contents from a repo. Base64-decoded; binary files are returned as-is."""
        params = {"ref": ref} if ref else None
        r = gh.get(f"/repos/{owner}/{name}/contents/{path}", params=params)
        if isinstance(r, list):
            return {"kind": "directory", "entries": [{"name": e["name"], "type": e["type"]} for e in r]}
        if r.get("encoding") == "base64":
            import base64
            try:
                content = base64.b64decode(r["content"]).decode("utf-8")
            except UnicodeDecodeError:
                content = f"<binary {r.get('size', 0)} bytes>"
            return {"kind": "file", "path": r["path"], "size": r.get("size"), "content": content}
        return {"kind": "file", "path": r["path"], "size": r.get("size"), "content": r.get("content", "")}

    @mcp.tool()
    def list_issues(owner: str, name: str, state: str = "open") -> list[dict[str, Any]]:
        """List issues on a repo. state: open|closed|all."""
        body = gh.get(f"/repos/{owner}/{name}/issues", params={"state": state, "per_page": 50})
        return [{"number": i["number"], "title": i["title"], "state": i["state"], "user": i["user"]["login"]} for i in body if "pull_request" not in i]

    @mcp.tool()
    def get_issue(owner: str, name: str, number: int) -> dict[str, Any]:
        """Return issue details including body and labels."""
        r = gh.get(f"/repos/{owner}/{name}/issues/{number}")
        return {
            "number": r["number"],
            "title": r["title"],
            "state": r["state"],
            "user": r["user"]["login"],
            "body": r.get("body", ""),
            "labels": [l["name"] for l in r.get("labels", [])],
        }

    @mcp.tool()
    def list_pull_requests(owner: str, name: str, state: str = "open") -> list[dict[str, Any]]:
        """List PRs on a repo. state: open|closed|all."""
        body = gh.get(f"/repos/{owner}/{name}/pulls", params={"state": state, "per_page": 50})
        return [{"number": p["number"], "title": p["title"], "state": p["state"], "user": p["user"]["login"], "head": p["head"]["ref"]} for p in body]

    @mcp.tool()
    def get_pull_request(owner: str, name: str, number: int) -> dict[str, Any]:
        """Return PR details including body and head/base."""
        r = gh.get(f"/repos/{owner}/{name}/pulls/{number}")
        return {
            "number": r["number"],
            "title": r["title"],
            "state": r["state"],
            "user": r["user"]["login"],
            "body": r.get("body", ""),
            "head": r["head"]["ref"],
            "base": r["base"]["ref"],
            "merged": r.get("merged", False),
        }

    @mcp.tool()
    def search_code(query: str) -> list[dict[str, Any]]:
        """Search code across repos the App has access to. Returns top 30."""
        body = gh.get("/search/code", params={"q": query, "per_page": 30})
        return [{"path": i["path"], "repo": i["repository"]["full_name"], "html_url": i["html_url"]} for i in body.get("items", [])]

    @mcp.tool()
    def list_commits(owner: str, name: str, sha: str | None = None) -> list[dict[str, Any]]:
        """List recent commits on a repo."""
        params: dict[str, Any] = {"per_page": 30}
        if sha:
            params["sha"] = sha
        body = gh.get(f"/repos/{owner}/{name}/commits", params=params)
        return [{"sha": c["sha"], "message": c["commit"]["message"].splitlines()[0], "author": c["commit"]["author"]["name"], "date": c["commit"]["author"]["date"]} for c in body]

    @mcp.tool()
    def create_issue(owner: str, name: str, title: str, body: str | None = None, labels: list[str] | None = None) -> dict[str, Any]:
        """Open a new issue. Returns the created issue's number and URL."""
        payload: dict[str, Any] = {"title": title}
        if body is not None:
            payload["body"] = body
        if labels:
            payload["labels"] = labels
        r = gh.post(f"/repos/{owner}/{name}/issues", json=payload)
        return {"number": r["number"], "html_url": r["html_url"], "state": r["state"]}

    @mcp.tool()
    def update_issue(owner: str, name: str, number: int, title: str | None = None, body: str | None = None, state: str | None = None, labels: list[str] | None = None) -> dict[str, Any]:
        """Edit an issue. Pass state='closed' to close, 'open' to reopen. Works on PRs too."""
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        if labels is not None:
            payload["labels"] = labels
        r = gh.patch(f"/repos/{owner}/{name}/issues/{number}", json=payload)
        return {"number": r["number"], "state": r["state"], "html_url": r["html_url"]}

    @mcp.tool()
    def comment_on_issue(owner: str, name: str, number: int, body: str) -> dict[str, Any]:
        """Add a comment to an issue or PR (same endpoint for both)."""
        r = gh.post(f"/repos/{owner}/{name}/issues/{number}/comments", json={"body": body})
        return {"id": r["id"], "html_url": r["html_url"]}

    @mcp.tool()
    def add_labels(owner: str, name: str, number: int, labels: list[str]) -> list[str]:
        """Add labels to an issue or PR. Returns the full label set after the add."""
        r = gh.post(f"/repos/{owner}/{name}/issues/{number}/labels", json={"labels": labels})
        return [l["name"] for l in r]

    @mcp.tool()
    def remove_label(owner: str, name: str, number: int, label: str) -> list[str]:
        """Remove a single label from an issue or PR. Returns remaining labels."""
        r = gh.delete(f"/repos/{owner}/{name}/issues/{number}/labels/{label}")
        return [l["name"] for l in r] if isinstance(r, list) else []

    @mcp.tool()
    def create_label(
        owner: str,
        name: str,
        label: str,
        color: str = "cccccc",
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a label on a repo so it's available to add_labels.

        Pairs with add_labels: GH 422s when add_labels is called with a
        name the repo doesn't have, and there was no MCP path to create
        one — sessions had to fall back to a direct API call with the
        env-injected App credentials. This tool closes that gap.

        `label`: the label name. GH rejects names containing commas (and
        a few other characters); the 422 comes back as "name invalid"
        with no further detail. Use spaces / hyphens / colons instead.
        Max 50 chars.
        `color`: 6-char hex without leading '#'. Default neutral gray.
        `description`: max 100 chars; longer descriptions 422 with a
        clear "description is too long" error.

        Returns the created label record. Existing labels with the same
        name 422 (caller should treat that as success-equivalent if the
        intent is "ensure this label exists")."""
        payload: dict[str, Any] = {"name": label, "color": color}
        if description is not None:
            payload["description"] = description
        r = gh.post(f"/repos/{owner}/{name}/labels", json=payload)
        return {
            "id": r["id"],
            "name": r["name"],
            "color": r["color"],
            "description": r.get("description"),
            "url": r["url"],
        }

    @mcp.tool()
    def create_pull_request(owner: str, name: str, title: str, head: str, base: str, body: str | None = None, draft: bool = False) -> dict[str, Any]:
        """Open a PR. head='branch' or 'fork-owner:branch'. base is the target branch."""
        payload: dict[str, Any] = {"title": title, "head": head, "base": base, "draft": draft}
        if body is not None:
            payload["body"] = body
        r = gh.post(f"/repos/{owner}/{name}/pulls", json=payload)
        return {"number": r["number"], "html_url": r["html_url"], "state": r["state"]}

    @mcp.tool()
    def merge_pull_request(owner: str, name: str, number: int, merge_method: str = "merge", commit_title: str | None = None, commit_message: str | None = None) -> dict[str, Any]:
        """Merge a PR. merge_method: merge|squash|rebase."""
        payload: dict[str, Any] = {"merge_method": merge_method}
        if commit_title is not None:
            payload["commit_title"] = commit_title
        if commit_message is not None:
            payload["commit_message"] = commit_message
        r = gh.put(f"/repos/{owner}/{name}/pulls/{number}/merge", json=payload)
        return {"merged": r.get("merged", False), "sha": r.get("sha"), "message": r.get("message")}

    @mcp.tool()
    def request_review(owner: str, name: str, number: int, reviewers: list[str] | None = None, team_reviewers: list[str] | None = None) -> dict[str, Any]:
        """Request reviewers on a PR. Pass user logins in reviewers, team slugs in team_reviewers."""
        payload: dict[str, Any] = {}
        if reviewers:
            payload["reviewers"] = reviewers
        if team_reviewers:
            payload["team_reviewers"] = team_reviewers
        r = gh.post(f"/repos/{owner}/{name}/pulls/{number}/requested_reviewers", json=payload)
        return {
            "requested_users": [u["login"] for u in r.get("requested_reviewers", [])],
            "requested_teams": [t["slug"] for t in r.get("requested_teams", [])],
        }

    @mcp.tool()
    def create_or_update_file(owner: str, name: str, path: str, content: str, message: str, branch: str | None = None) -> dict[str, Any]:
        """Create a file or update one. Commits directly to `branch` (default branch
        if omitted). content is plain text; encoded to base64 for the API.

        The current blob sha is resolved server-side immediately before the write,
        so a caller-cached sha can't be reused stale. The mutation is rejected by
        GitHub with 409 if the file changed concurrently between the resolve and
        the write."""
        import base64
        params = {"ref": branch} if branch else None
        sha: str | None = None
        try:
            existing = gh.get(f"/repos/{owner}/{name}/contents/{path}", params=params)
            if isinstance(existing, dict) and "sha" in existing:
                sha = existing["sha"]
        except httpx.HTTPStatusError as exc:
            if not _is_404(exc):
                raise
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if branch is not None:
            payload["branch"] = branch
        if sha is not None:
            payload["sha"] = sha
        r = gh.put(f"/repos/{owner}/{name}/contents/{path}", json=payload)
        return {
            "path": r["content"]["path"],
            "sha": r["content"]["sha"],
            "commit_sha": r["commit"]["sha"],
            "html_url": r["content"]["html_url"],
        }

    @mcp.tool()
    def delete_file(owner: str, name: str, path: str, message: str, branch: str | None = None) -> dict[str, Any]:
        """Delete a file. The current blob sha is resolved server-side immediately
        before the delete; the call fails if the file does not exist on `branch`
        (default branch if omitted)."""
        params = {"ref": branch} if branch else None
        existing = gh.get(f"/repos/{owner}/{name}/contents/{path}", params=params)
        if not isinstance(existing, dict) or "sha" not in existing:
            raise RuntimeError(f"{path} is not a file or does not exist on {branch or 'default branch'}")
        payload: dict[str, Any] = {"message": message, "sha": existing["sha"]}
        if branch is not None:
            payload["branch"] = branch
        r = gh.delete(f"/repos/{owner}/{name}/contents/{path}", json=payload)
        return {"commit_sha": r["commit"]["sha"]}

    @mcp.tool()
    def create_branch(owner: str, name: str, branch: str, base: str = "main") -> dict[str, Any]:
        """Create a new branch pointing at the current HEAD of `base` (default 'main').
        The base sha is resolved server-side at call time — there is intentionally
        no `from_sha` parameter, because a caller-cached sha is exactly the
        affordance that lets a subsequent commit revert previous work by being
        based on a stale view of `base`."""
        base_branch = gh.get(f"/repos/{owner}/{name}/branches/{base}")
        base_sha = base_branch["commit"]["sha"]
        r = gh.post(f"/repos/{owner}/{name}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha})
        return {"ref": r["ref"], "sha": r["object"]["sha"], "base": base, "base_sha": base_sha}

    @mcp.tool()
    def delete_branch(owner: str, name: str, branch: str) -> dict[str, Any]:
        """Delete a branch ref via DELETE /repos/{owner}/{name}/git/refs/heads/{branch}.

        Use for cleaning up stale branches — e.g. a working branch left behind
        when its work landed on main via direct push (so PR-merge auto-delete
        didn't fire), or an abandoned branch with no PR.

        GitHub rejects deleting the repo's default branch (422) and any branch
        covered by a 'restrict deletions' protection rule (422), so no extra
        guard is needed here — those failures surface as HTTPStatusError.
        Missing branches 422 with 'Reference does not exist'."""
        gh.delete(f"/repos/{owner}/{name}/git/refs/heads/{branch}")
        return {"deleted": True, "branch": branch}

    @mcp.tool()
    def commit_to_branch(
        owner: str,
        name: str,
        branch: str,
        files: list[dict[str, Any]],
        message: str,
        base: str = "main",
        deletes: list[str] | None = None,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> dict[str, Any]:
        """Land a single commit covering one or more file changes (and optional
        deletes) on `branch`. If `branch` doesn't exist on the remote, it is
        created from the current HEAD of `base` (default 'main') and the commit
        is the new branch's first commit. If `branch` exists, the commit is
        appended to its current HEAD; `base` is ignored.

        Both branch HEAD and base HEAD are resolved server-side at call time, so
        no caller-cached sha can introduce staleness. This is the preferred path
        for any multi-file change — using it instead of multiple
        create_or_update_file calls keeps the change atomic and gives the PR a
        single coherent commit.

        files: [{"path": "src/foo.py", "content": "<plain text>", "mode"?: "100644"}].
            Mode defaults to 100644; use "100755" for executables. Binary files
            are not supported (content is utf-8 encoded before base64).
        deletes: ["old/file.txt", ...] — paths to remove in the same commit.
        author_name / author_email: override commit author. If omitted, attributes
            to the App's bot identity (same as every other write tool).

        Returns: {branch, commit_sha, tree_sha, parent_sha, ref, html_url}."""
        import base64
        if not files and not deletes:
            raise ValueError("commit_to_branch needs at least one file or one delete")

        branch_existed = True
        try:
            b = gh.get(f"/repos/{owner}/{name}/branches/{branch}")
            parent_sha = b["commit"]["sha"]
        except httpx.HTTPStatusError as exc:
            if not _is_404(exc):
                raise
            branch_existed = False
            b = gh.get(f"/repos/{owner}/{name}/branches/{base}")
            parent_sha = b["commit"]["sha"]

        parent_commit = gh.get(f"/repos/{owner}/{name}/git/commits/{parent_sha}")
        base_tree_sha = parent_commit["tree"]["sha"]

        tree_entries: list[dict[str, Any]] = []
        for f in files or []:
            if "path" not in f or "content" not in f:
                raise ValueError("each file entry needs 'path' and 'content'")
            blob = gh.post(
                f"/repos/{owner}/{name}/git/blobs",
                json={
                    "content": base64.b64encode(f["content"].encode("utf-8")).decode("ascii"),
                    "encoding": "base64",
                },
            )
            tree_entries.append({
                "path": f["path"],
                "mode": f.get("mode", "100644"),
                "type": "blob",
                "sha": blob["sha"],
            })
        for path in deletes or []:
            # sha=None on a tree entry deletes the path from the new tree.
            tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

        new_tree = gh.post(
            f"/repos/{owner}/{name}/git/trees",
            json={"base_tree": base_tree_sha, "tree": tree_entries},
        )

        commit_payload: dict[str, Any] = {
            "message": message,
            "tree": new_tree["sha"],
            "parents": [parent_sha],
        }
        if author_name and author_email:
            author_block = {"name": author_name, "email": author_email}
            commit_payload["author"] = author_block
            commit_payload["committer"] = author_block
        new_commit = gh.post(f"/repos/{owner}/{name}/git/commits", json=commit_payload)

        if branch_existed:
            ref = gh.patch(
                f"/repos/{owner}/{name}/git/refs/heads/{branch}",
                json={"sha": new_commit["sha"]},
            )
        else:
            ref = gh.post(
                f"/repos/{owner}/{name}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": new_commit["sha"]},
            )

        return {
            "branch": branch,
            "commit_sha": new_commit["sha"],
            "tree_sha": new_tree["sha"],
            "parent_sha": parent_sha,
            "ref": ref["ref"],
            "html_url": new_commit.get("html_url", ""),
        }

    @mcp.tool()
    def list_workflow_runs(owner: str, name: str, workflow: str, branch: str | None = None, status: str | None = None, per_page: int = 10) -> list[dict[str, Any]]:
        """List recent workflow runs. workflow is the file name ('tofu.yml') or numeric ID. status: queued|in_progress|completed|success|failure|... Useful for checking whether a CI job actually ran on a given commit."""
        params: dict[str, Any] = {"per_page": per_page}
        if branch:
            params["branch"] = branch
        if status:
            params["status"] = status
        body = gh.get(f"/repos/{owner}/{name}/actions/workflows/{workflow}/runs", params=params)
        return [
            {
                "id": r["id"],
                "head_sha": r["head_sha"],
                "head_branch": r.get("head_branch"),
                "event": r["event"],
                "status": r["status"],
                "conclusion": r["conclusion"],
                "created_at": r["created_at"],
                "html_url": r["html_url"],
                "title": r.get("display_title"),
            }
            for r in body.get("workflow_runs", [])
        ]

    @mcp.tool()
    def get_workflow_run(owner: str, name: str, run_id: int) -> dict[str, Any]:
        """Return a single workflow run's status, conclusion, and metadata."""
        r = gh.get(f"/repos/{owner}/{name}/actions/runs/{run_id}")
        return {
            "id": r["id"],
            "head_sha": r["head_sha"],
            "head_branch": r.get("head_branch"),
            "event": r["event"],
            "status": r["status"],
            "conclusion": r["conclusion"],
            "created_at": r["created_at"],
            "updated_at": r.get("updated_at"),
            "html_url": r["html_url"],
            "title": r.get("display_title"),
        }

    @mcp.tool()
    def list_workflow_run_jobs(owner: str, name: str, run_id: int) -> list[dict[str, Any]]:
        """List the jobs (and per-step results) of a workflow run. Pair with
        get_workflow_job_logs to look up a job_id then download its log text."""
        body = gh.get(f"/repos/{owner}/{name}/actions/runs/{run_id}/jobs", params={"per_page": 50})
        return [
            {
                "id": j["id"],
                "name": j["name"],
                "status": j["status"],
                "conclusion": j.get("conclusion"),
                "started_at": j.get("started_at"),
                "completed_at": j.get("completed_at"),
                "html_url": j.get("html_url"),
                "steps": [
                    {
                        "number": s.get("number"),
                        "name": s.get("name"),
                        "status": s.get("status"),
                        "conclusion": s.get("conclusion"),
                        "started_at": s.get("started_at"),
                        "completed_at": s.get("completed_at"),
                    }
                    for s in j.get("steps", [])
                ],
            }
            for j in body.get("jobs", [])
        ]

    @mcp.tool()
    def get_workflow_job_logs(owner: str, name: str, job_id: int, max_chars: int = 200_000) -> dict[str, Any]:
        """Fetch the log text for a single workflow run job. The Actions logs
        endpoint 302s to a presigned blob URL; get_text follows the redirect
        and returns plain text. Truncated to the LAST `max_chars` characters
        because failures surface near the end of the log; if you need earlier
        output, raise max_chars or look at the GH UI for live streaming.

        Use list_workflow_run_jobs to find a job_id."""
        try:
            text = gh.get_text(f"/repos/{owner}/{name}/actions/jobs/{job_id}/logs")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise RuntimeError(f"job {job_id} not found in {owner}/{name}")
            if exc.response.status_code == 410:
                raise RuntimeError(f"job {job_id} logs have expired and are no longer downloadable")
            raise
        truncated = len(text) > max_chars
        if truncated:
            text = text[-max_chars:]
        return {"job_id": job_id, "chars": len(text), "truncated": truncated, "text": text}

    @mcp.tool()
    def list_workflow_run_artifacts(owner: str, name: str, run_id: int) -> list[dict[str, Any]]:
        """List the artifacts produced by a workflow run. Pair with
        get_workflow_run_artifact_files to download and inspect one."""
        body = gh.get(f"/repos/{owner}/{name}/actions/runs/{run_id}/artifacts", params={"per_page": 100})
        return [
            {
                "id": a["id"],
                "name": a["name"],
                "size_in_bytes": a["size_in_bytes"],
                "created_at": a.get("created_at"),
                "expires_at": a.get("expires_at"),
                "expired": a.get("expired", False),
            }
            for a in body.get("artifacts", [])
        ]

    @mcp.tool()
    def get_workflow_run_artifact_files(
        owner: str,
        name: str,
        artifact_id: int,
        path_glob: str = "*",
        max_total_chars: int = 200_000,
    ) -> dict[str, Any]:
        """Download a workflow run artifact (a zip), extract it, and return
        matching file contents. Files matching path_glob are decoded as UTF-8
        when valid (encoding='text'), else returned as base64
        (encoding='base64'). Files are returned in name-sorted order; once the
        cumulative character budget exceeds max_total_chars, the rest are
        dropped and `truncated_at` names the first omitted file. Use to read
        log lines or jsonl event streams from a CI run without leaving chat.

        Use list_workflow_run_artifacts to find an artifact_id."""
        import base64
        import fnmatch
        import io
        import zipfile
        try:
            zip_bytes = gh.get_bytes(f"/repos/{owner}/{name}/actions/artifacts/{artifact_id}/zip")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise RuntimeError(f"artifact {artifact_id} not found in {owner}/{name}")
            if exc.response.status_code == 410:
                raise RuntimeError(f"artifact {artifact_id} has expired and is no longer downloadable")
            raise
        files = []
        used = 0
        truncated_at: str | None = None
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for info in sorted(zf.infolist(), key=lambda i: i.filename):
                if info.is_dir():
                    continue
                if not fnmatch.fnmatch(info.filename, path_glob):
                    continue
                data = zf.read(info.filename)
                try:
                    payload = data.decode("utf-8")
                    encoding = "text"
                except UnicodeDecodeError:
                    payload = base64.b64encode(data).decode("ascii")
                    encoding = "base64"
                if used + len(payload) > max_total_chars and files:
                    truncated_at = info.filename
                    break
                used += len(payload)
                files.append({
                    "path": info.filename,
                    "bytes": info.file_size,
                    "encoding": encoding,
                    "content": payload,
                })
        return {
            "artifact_id": artifact_id,
            "zip_bytes": len(zip_bytes),
            "file_count": len(files),
            "truncated_at": truncated_at,
            "files": files,
        }

    @mcp.tool()
    def list_repo_variables(owner: str, name: str) -> list[dict[str, Any]]:
        """List repository-level GitHub Actions variables. Requires the App to
        have 'variables: read' permission on its installation; without it this
        returns 403."""
        body = gh.get(f"/repos/{owner}/{name}/actions/variables", params={"per_page": 100})
        return [
            {"name": v["name"], "value": v["value"], "created_at": v.get("created_at"), "updated_at": v.get("updated_at")}
            for v in body.get("variables", [])
        ]

    @mcp.tool()
    def get_repo_variable(owner: str, name: str, variable_name: str) -> dict[str, Any]:
        """Get a single repository Actions variable by name. Requires the App to
        have 'variables: read' permission. Raises on 404 if the variable is
        unset, which is a clean way to verify whether tofu wrote it."""
        v = gh.get(f"/repos/{owner}/{name}/actions/variables/{variable_name}")
        return {"name": v["name"], "value": v["value"], "created_at": v.get("created_at"), "updated_at": v.get("updated_at")}
