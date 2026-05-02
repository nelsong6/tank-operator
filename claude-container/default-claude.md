<!--
  Default global CLAUDE.md primer for tank-operator session pods.

  Baked into the claude-container image; copied to ~/.claude/CLAUDE.md
  at first connect by tank-bootstrap.sh. Anything written here loads as
  user-scope context into every prompt of every session pod, regardless
  of /workspace contents.

  Use this for orienting facts the agent always needs (ephemeral k8s
  pod, git-push disabled, MCP servers wired in, etc.). Project-specific
  guidance belongs in the project's own CLAUDE.md, not here.
-->

TODO: replace this placeholder with the default session-pod primer.
