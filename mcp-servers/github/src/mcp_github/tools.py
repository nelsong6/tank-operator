from typing import Any

from mcp.server.fastmcp import FastMCP

from .github_client import GitHubClient


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
    def get_repo(owner: str, name: str) -> dict[str, Any]:
        """Return metadata for a single repo."""
        r = gh.get(f"/repos/{owner}/{name}")
        return {k: r.get(k) for k in ("full_name", "description", "default_branch", "language", "stargazers_count", "open_issues_count", "updated_at")}

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
    def create_or_update_file(owner: str, name: str, path: str, content: str, message: str, branch: str | None = None, sha: str | None = None) -> dict[str, Any]:
        """Create a file (omit sha) or update one (pass the existing blob sha from get_file_contents). Commits directly to branch (default branch if omitted). content is plain text; encoded to base64 for the API."""
        import base64
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
    def delete_file(owner: str, name: str, path: str, message: str, sha: str, branch: str | None = None) -> dict[str, Any]:
        """Delete a file. sha is the existing blob sha (from get_file_contents)."""
        payload: dict[str, Any] = {"message": message, "sha": sha}
        if branch is not None:
            payload["branch"] = branch
        r = gh.delete(f"/repos/{owner}/{name}/contents/{path}", json=payload)
        return {"commit_sha": r["commit"]["sha"]}

    @mcp.tool()
    def create_branch(owner: str, name: str, branch: str, from_sha: str) -> dict[str, Any]:
        """Create a new branch pointing at from_sha."""
        r = gh.post(f"/repos/{owner}/{name}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": from_sha})
        return {"ref": r["ref"], "sha": r["object"]["sha"]}
