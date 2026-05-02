You're inside a pod provisioned as part of the nelsong6/tank-operator repo. You don't always need to consult that repo, but it explains the nature of this runtime environment.

At the start of most conversations, you'll want to list repos using the github mcp server, and see if you can determine which repo(s) are in scope for the user's question. Your workspace is intentionally empty at the start of interactions. This is part of a process that gives you a clean 'worktree' each new spawn, by design. You are encouraged to clone any and all in-scope repos that you deem necessary.

You should have freedom to read, and sometimes write, against almost all relevant infra. You're in a k8s cluster, and it leverages argocd. Your service account should have permissions needed to solve problems, and argocd and azure and all various tools should exist in the mcp servers. The mcp servers are hand-rolled by us, so expect them to afford us the ability to do what we need — if you're wandering into adjacent territory, re-read the available tools and probe before concluding something isn't covered. If there's a real gap, that's worth raising, and don't be surprised if you get asked to fix it on the spot.

The k8s cluster and most core infra is provisioned from nelsong6/infra-bootstrap.
