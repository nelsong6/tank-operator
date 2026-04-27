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
