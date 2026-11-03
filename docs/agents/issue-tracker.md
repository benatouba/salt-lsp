# Issue tracker: GitHub

Issues and specs live in `benatouba/salt-lsp`. Use the `gh` CLI.

## Repository targeting

Always pass `--repo benatouba/salt-lsp` to issue, PR, and label commands.
For API calls, use `repos/benatouba/salt-lsp/...`.
Bare `gh` commands currently default to upstream `dcermak/salt-lsp`.

Before the first issue creation in a session, verify the target with:
`gh repo view benatouba/salt-lsp --json nameWithOwner`.

## Conventions

The commands below require the repository flag above.

- Create: `gh issue create --title "..." --body "..."`.
  For multiline bodies, use a heredoc with `--body-file -`.
- Read: `gh issue view <number> --json number,title,body,labels,comments`.
- List: `gh issue list --state open --json number,title,body,labels,comments`,
  adding label and state filters as needed.
- Comment: `gh issue comment <number> --body "..."`.
- Label: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`.
- Close: post the explanation with `gh issue comment`, then `gh issue close`.
  Comment separately even if the issue is already closed.
- Sub-issues: with gh 2.94+, use `gh issue create --parent <parent> ...`
  or `gh issue edit <parent> --add-sub-issue <child>`.
  Older clients can POST to `repos/benatouba/salt-lsp/issues/<parent>/sub_issues`
  with `sub_issue_id=<child-database-id>`.
  If unavailable, put `Part of #<parent>` atop the child body.
- Blocking: use native issue dependencies. With gh 2.94+, use
  `gh issue edit <child> --add-blocked-by <blocker>`.
  Older clients can POST to
  `repos/benatouba/salt-lsp/issues/<child>/dependencies/blocked_by`
  with `issue_id=<blocker-database-id>`.
  Obtain database IDs using `gh api repos/benatouba/salt-lsp/issues/<n> --jq .id`.
  If unavailable, put `Blocked by: #<n>, #<n>` atop the child body.
- Count open blockers:
  `gh api repos/benatouba/salt-lsp/issues/<n> --jq .issue_dependencies_summary.blocked_by`.
  A ticket is startable when all blockers are closed.

## Pull requests as a triage surface

**PRs as a request surface: no.**

If changed to yes, use `gh pr view`, `gh pr diff`, `gh pr comment`,
`gh pr edit`, and `gh pr close`, always with the repository flag above.
Read the PR body and comments separately. External PRs are those whose
author_association is neither OWNER, MEMBER, nor COLLABORATOR.
Issues and PRs share a number space; resolve bare numbers accordingly.

## Skill operations

- "Publish to the issue tracker": create a GitHub issue.
- "Fetch the relevant ticket": read the GitHub issue.

## Wayfinding operations

- Map: one issue labelled `wayfinder:map`, containing Notes,
  Decisions-so-far, and Fog.
- Children: link tickets as sub-issues; otherwise add them to the map's
  task list and put `Part of #<map>` atop each child.
  Label each `wayfinder:<type>`: research, prototype, grilling, or task.
- Frontier: choose the first open child in map order with no open blocker
  and no assignee.
- Claim: assign the ticket to `@me` as the session's first write.
- Resolve: comment with the answer, close the ticket, then append a gist
  and link to the map's Decisions-so-far.
