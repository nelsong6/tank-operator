#!/usr/bin/env python3
# fetch-skills.py — pull SKILL.md files from GitHub via the local
# github MCP and lay them down in ~/.claude/skills/. Runs from
# tank-bootstrap.sh on every session boot so skill updates land
# without rebuilding this image.
#
# Why MCP rather than `git clone`? The session pod has no GitHub auth
# of its own (no PAT, no App key); the in-cluster github MCP server
# uses an App installation token that the mcp-auth-proxy sidecar
# forwards via fresh SA-token bearer auth. Going through the MCP keeps
# auth out of this pod entirely.
#
# Soft failures only — a transient MCP issue should not block the
# session bootstrap. Errors print to stderr; the bootstrap shell
# continues on.

import json
import os
import sys
import urllib.error
import urllib.request

GITHUB_MCP_URL = "http://127.0.0.1:9992/"
SKILLS_DIR = os.path.expanduser("~/.claude/skills")

# Each entry: (owner, repo, path-in-repo, dest-skill-name).
# The dest-skill-name is the directory under ~/.claude/skills/; claude
# Code discovers SKILL.md inside it.
SKILLS = [
    ("nelsong6", "tank-operator", "claude-container/skills/done/SKILL.md", "done"),
]


def post(body, session_id=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json,text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(
        GITHUB_MCP_URL,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.headers.get("Mcp-Session-Id"), r.read().decode()


def parse_sse(body):
    for line in body.splitlines():
        if line.startswith("data: "):
            yield json.loads(line[6:])


def extract_file_content(body):
    for msg in parse_sse(body):
        if msg.get("error"):
            return None, msg["error"]
        result = msg.get("result", {})
        if result.get("isError"):
            return None, json.dumps(result.get("content", []))[:300]
        for c in result.get("content", []):
            if c.get("type") == "text":
                inner = json.loads(c["text"])
                if inner.get("kind") == "file":
                    return inner.get("content", ""), None
    return None, "no file content in response"


def main():
    try:
        sid, _ = post({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "fetch-skills", "version": "0"},
            },
        })
        post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    except (urllib.error.URLError, OSError) as exc:
        print(f"github MCP unreachable, skipping skill sync: {exc}", file=sys.stderr)
        return 0

    os.makedirs(SKILLS_DIR, exist_ok=True)

    req_id = 2
    installed = []
    for owner, name, path, skill_name in SKILLS:
        try:
            _, body = post({
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {
                    "name": "get_file_contents",
                    "arguments": {"owner": owner, "name": name, "path": path},
                },
            }, sid)
        except Exception as exc:
            print(f"skill {skill_name}: fetch failed: {exc}", file=sys.stderr)
            req_id += 1
            continue
        req_id += 1

        content, err = extract_file_content(body)
        if content is None:
            print(f"skill {skill_name}: {err}", file=sys.stderr)
            continue

        target_dir = os.path.join(SKILLS_DIR, skill_name)
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, "SKILL.md")
        with open(target_path, "w") as f:
            f.write(content)
        installed.append(skill_name)

    if installed:
        print(f"installed {len(installed)} skill(s): {', '.join(installed)}")
    else:
        print("no skills installed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
