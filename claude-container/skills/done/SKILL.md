---
name: done
description: Review the session, capture intent and decisions as a GitHub issue, audit auto-memory and docs
---

# /done — Finalize a session

When the user invokes `/done`, perform a session retrospective. The goal is not just to document what changed — it's to capture the **intent** behind the session so future conversations get proper steering.

## 1. Review the session

The source of truth is the **conversation itself**, not git. Review what you did, what the user asked for, what decisions were made, and what you learned about their intentions. Git diffs can supplement this, but the conversation is primary.

Ask yourself:
- What did the user want to accomplish?
- What decisions were made (and why)?
- What feedback or corrections did the user give?
- Were any new preferences, patterns, or rules established?
- Did I write anything to auto-memory that should be migrated to the config chain?

## 2. Audit and migrate auto-memory

Scan all `~/.claude/projects/*/memory/` directories for files that violate the auto-memory policy in `setup/claude/CLAUDE.md`. The policy: these directories must contain **only** a stub `MEMORY.md` that points back to the config chain. Any real memory content is a violation.

For each violation found:
- **Check** whether its content is already captured in a source-controlled CLAUDE.md. If not, migrate it to the appropriate level (global, profile, or repo-specific)
- **Delete** the offending file
- **Reset** the `MEMORY.md` index back to a stub if it was modified

If no violations are found, skip this step.

## 3. File a session-log issue

Record the session as a GitHub issue in the current repo, labelled `session-log`. Use whatever GitHub access is available in the session — don't worry about which tool, the relevant ones will be obvious from how the session has gone.

1. **Identify the repo.** If the working directory isn't inside a GitHub-backed git repo, abort step 3 and surface that to the user — the rest of the skill can still run.
2. **Build the issue body** from your session review (step 1):
   - Lead bullets that summarize **what** changed, **why**, and the user's intent — not a mechanical diff dump
   - Capture corrections, preferences, and decisions explicitly so a future session can use them as steering
   - If commits landed during the session, link them with `org/repo@sha` or full URLs
   - Note any follow-ups or unresolved threads at the bottom
3. **Pick a concise title.** Imperative or noun-phrase summary, prefixed with the date. Examples:
   - `2026-04-28: ArgoCD MCP + CI identity untangle`
   - `2026-04-28: fix /done skill to file GitHub issues`
4. **Create the issue with the `session-log` label.** Capture the URL — you'll surface it in step 5.
5. **Close the issue immediately on creation.** The session-log issue is a timestamped record for later reference, not actionable work. Closing it on creation keeps the open backlog focused on real work and signals "history, not todo." Surface the URL in the report (step 5) regardless — it remains discoverable via `state=closed` issue queries.

## 4. Audit documented descriptions

For each file modified during the session (as recalled from conversation context, supplemented by git diff if helpful), check the corresponding documentation in the repo's CLAUDE.md. Verify that documented behavior still matches actual code:

- **Interfaces/types**: If fields were added, removed, or changed, update the documented interface
- **Component props**: If a component's props changed, update the props description
- **Behavioral descriptions**: If how a component or module works changed, update the prose
- **Data flow descriptions**: If the data flow between components changed, update the relevant section

Only update sections that are actually stale. Do not rewrite sections that are already accurate.

## 5. Report

Output a table of only the documentation/config files that `/done` itself edited (e.g. CLAUDE.md, memory stubs), NOT the source files changed during the session. Qualify each by repo name or location:

| Location | File | What changed |
|----------|------|--------------|
| {repo or context} | {file path} | {brief description} |

Below the table:
- The session-log issue URL from step 3.
- Any sections that look stale but you weren't confident enough to update (flag for user review).
