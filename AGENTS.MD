# Silent Hunter Monitor Agent & Contributor Guide

> **Golden Rule**: If the work is not tracked as a Gitea issue, it does not exist. No exceptions.

This document defines how work is discovered, planned, executed, and closed in the Silent Hunter Monitor repo. It applies to human contributors and autonomous agents equally.

A **story** is the smallest unit of trackable work. It has an ID (the Gitea issue number), a status, and a single clear outcome. Stories live exclusively in Gitea. Everything else — code, specs, tests, docs — is produced *in service of* a story.

Stories answer three questions:
1. **What problem are we solving?**
2. **How will we know it is solved?** (acceptance criteria)
3. **What is the current state?** (status)

---

## 1. Gitea Issues Are the Source of Truth

We previously maintained story files in `stories/` alongside Gitea issues. This created confusion by splitting state across two systems. **That practice is deprecated.** The canonical story record is always the Gitea issue.

- The Gitea issue number is the story ID (e.g., `#43`). Reference it in commits as `#43`.
- Status is tracked via labels and open/closed state.
- There is no local index file to maintain. Use `tea issues ls` to find work.

---

## 2. When to Create a Story

Create a story **before** starting work if any of the following are true:

| Criterion | Examples |
|---|---|
| Touches more than 2 files | Refactoring a service interface |
| Changes user-visible behavior | New LLM prompt strategy, TTS feature |
| Requires a design decision | "Should we submodule NeMo or vendor it?" |
| Risk of breaking existing tests | State machine changes, pipeline refactoring |
| Estimated effort > 30 minutes | If it is not a one-line fix, track it |
| Discovered during another story | File a new story; do not scope-creep the current one |
| Cleanup / hygiene work | "Delete duplicate image-handling specs", "Move xmpp-bridge docs" |

**Do NOT create a story for:**
- One-line typo fixes in comments (fix and commit directly)
- Purely reactive debugging that is resolved in the same session with no code change
- Updating this guide or other meta-changes

**Rule of thumb**: If you would need to explain what you did in a stand-up, create a story first.

---

## 3. Story Lifecycle

```
Draft → Ready → In Progress → Done
                     ↓
                 Abandoned
```

We model these states with Gitea labels on open issues. Closed issues are either Done or Abandoned.

| State | Label | Gitea State | Meaning |
|---|---|---|---|
| Draft | `status/draft` | open | Problem identified, acceptance criteria not yet written |
| Ready | `status/ready` | open | Acceptance criteria written; story is small enough to complete in one session |
| In Progress | `status/in-progress` | open | You are actively working on it |
| In Review | `status/in-review` | open | PR is open and awaiting human review |
| Done | `status/done` | closed | All acceptance criteria met and code is merged |
| Abandoned | `status/abandoned` | closed | No longer relevant or solved by another story |

**Label hygiene**: When moving a story to a new state, remove the old status label and add the new one. Only one status label should be present at a time.

---

## 4. How to Create a Story

All stories start as Gitea issues. Use the `tea` CLI.

### Step 1: Write the description

Use the template in §12. Fill in:

- **Title**: A clear, concise problem statement.
- **Problem**: What is broken or missing?
- **Acceptance Criteria**: A checklist of specific, verifiable outcomes.
- **Dependencies**: Any blocking stories or prerequisites.

> **Search first.** Before creating a new issue, run `tea issues ls -k "<keyword>"` to confirm the work is not already tracked.

### Step 2: Create the issue

```bash
tea issues create -t "<title>" -d "<description>"
```

The returned issue index is your story ID.

### Step 3: Label it

```bash
tea issues edit <index> --add-labels status/draft
```

If the status labels do not yet exist in the repo, create them once:

```bash
tea labels create -n "status/draft"       -c "#cccccc"
tea labels create -n "status/ready"       -c "#0e8a16"
tea labels create -n "status/in-progress" -c "#fbca04"
tea labels create -n "status/in-review"   -c "#5319e7"
tea labels create -n "status/done"        -c "#0052cc"
tea labels create -n "status/abandoned"   -c "#b60205"
```

---

## 5. Working on a Story

An agent may be invoked to pick up work that a human (or another agent) already started. **Always search for existing stories before creating a new one.** Duplicates fragment context and waste effort.

### Picking up a story

1. Find a Ready story:
   ```bash
   tea issues ls --labels status/ready
   ```
2. Move it to In Progress and assign yourself:
   ```bash
   tea issues edit <index> \
     --add-labels status/in-progress \
     --remove-labels status/ready \
     -a <your-username>
   ```

### While in progress

- **Only one story should be In Progress per contributor at a time.**
- Reference the story ID in commit messages (see §8).
- If the scope changes, update the issue description:
  ```bash
  tea issues edit <index> -d "<updated description>"
  ```
  Or add a comment:
  ```bash
  tea comment <index> "Scope change: ..."
  ```

### Commit and push discipline

Commit whenever you finish a coherent slice of work — a single fix, a refactor, a passing test. Do not batch an entire day's work into one commit.

- **Commit message**: Every commit that touches story-related code must reference the story ID (see §8).
- **Push**: Push at the end of every work session, if not more frequently. Never leave unpushed work on your local machine overnight.
- **Branching**: Work on a feature branch named for the story, e.g. `story-7-silence-gate`. Open a pull request when the change is ready for review (or open a draft PR early if you want feedback mid-flight).
- **Direct to main**: Truly trivial changes (single-line typo fixes, README updates) may skip review and go direct to `main`, but only after the story exists in Gitea. Everything else goes through a PR.

### Opening a pull request

When your branch is ready:

```bash
# Push your branch
git push -u origin story-<index>-<short-name>

# Create the PR
tea pr create -t "<title>" -d "Closes #<index>. <description>"
```

The PR description should reference the story issue so Gitea links them.

After creating the PR, update the story labels and stop:

```bash
tea issues edit <index> \
  --add-labels status/in-review \
  --remove-labels status/in-progress
tea comment <index> "PR opened: <url>. Awaiting human review."
```

**Agents stop here.** The story is now in `status/in-review` and waits for a human reviewer. Self-reviews are not permitted — an agent cannot approve or merge its own PR.

### Reviewing a pull request

All non-trivial changes must be reviewed before merge.

- **List open PRs**: `tea pr ls`
- **Check out a PR locally**: `tea pr checkout <pr-index>`
- **Review interactively**: `tea pr review <pr-index>`
- **Approve**: `tea pr approve <pr-index> "LGTM"`
- **Request changes**: `tea pr reject <pr-index> "<reason>"`

Reviewers should verify acceptance criteria, test coverage, and spec consistency. Review is performed by humans; agents do not review their own PRs.

If changes are requested, the author updates the branch and pushes new commits. The story stays in `status/in-review` until approved.

### Merging

Once approved and CI passes, a human merges the PR:

```bash
tea pr merge <pr-index>
```

Then close the story issue (see §6).

### Stacked diffs (advanced)

If a story is large enough to split into dependent PRs, you can stack them by creating each branch from the previous one and using `--base`:

```bash
tea pr create --base story-7-part-1 -t "Part 2: ..."
```

This is supported but not the default workflow. Gitea will not automatically retarget stacked PRs when the base merges, so use this sparingly and only when the alternative (one giant PR) is worse.

---

## 6. Definition of Done

A story is **Done** only when **all** of the following are true:

1. **Code is reviewed and merged to `main`.**
   - For non-trivial changes: approved via pull request and merged with `tea pr merge`.
   - The merge commit (or final commit) references the story ID: `feat(audio): add silence gate, closes #12`.
2. **Tests pass.**
   - New behavior has tests. Existing tests still pass. If tests were deliberately removed, the commit message explains why.
3. **Specs are updated if the contract changed.**
   - If you modified an HTTP endpoint, a state machine transition, or an external source schema, the relevant file in `specs/` is updated to match the implementation. The spec and the code must not contradict each other.
4. **The Gitea issue is closed with a resolution comment.**
   - Add a closing comment linking to the merge commit and summarizing what was done:
     ```bash
     tea comment <index> "Closed by commit <hash>. Resolution: ..."
     tea issues close <index>
     ```
   - Update the labels: remove `status/in-progress`, add `status/done`.
5. **No orphaned TODOs remain.**
   - If this story was spawned from a root-level TODO file (e.g., `video-fix.md`), that file is either deleted or moved to `archive/` with a note pointing to the story.

**A story is NOT done when:**
- The code works on your machine but is not pushed.
- You wrote the code but the spec still describes the old behavior.
- You fixed the bug but left a `FIXME` in the source referencing the story.

---

## 7. Abandoning a Story

If a story is no longer relevant:

```bash
tea comment <index> "Abandoned: <reason>"
tea issues edit <index> \
  --add-labels status/abandoned \
  --remove-labels status/draft,status/ready,status/in-progress
tea issues close <index>
```

---

## 8. Commit Convention

Reference the story ID in the commit subject or body so `git log` connects to the tracker.

```
feat(stt): add FinalizingSTT state, closes #7

- Removes isFinalizingStt boolean flag
- Adds state machine transition Idle → FinalizingSTT → Responding
```

If a commit is part of a story but does not close it:

```
fix(pipeline): reduce poll interval to 100ms

Relates to #7.
```

---

## 9. File & Directory Conventions

```
stories/
  TEMPLATE.md         ← Template for new issue descriptions. Copy and paste into Gitea.
  archive/            ← Historical stories only. Do not create new files here.
```

- **Do not create new `stories/<id>_title.md` files.** All new stories are Gitea issues.
- **Existing `stories/*.md` files are deprecated** and will be removed in a separate cleanup. Do not update them.
- **No root-level TODO/FIXME/PLAN files.** If you find one, convert it to a Gitea issue or delete it.

---

## 10. Relationship Between Stories and Specs

| | Story | Spec |
|---|---|---|
| **Purpose** | Track work | Document design |
| **Audience** | Contributors | Future maintainers |
| **Lifecycle** | Created → Done → Archived | Draft → Review → Active → Superseded |
| **Location** | Gitea issues | `specs/` |

- A **P1 story usually needs a spec first.** If the change is architectural, write or update the spec in the same branch as the story.
- A spec without an associated story is documentation; it does not represent committed work.
- If a spec is superseded, mark it as such in its frontmatter and point to the replacement. Do not delete specs — they are historical record.

---

## 11. Prohibited Patterns

These patterns created the mess we are cleaning up. Do not reintroduce them.

1. **Dual tracking** — Do not maintain a story in both Gitea and a `stories/*.md` file.
2. **Stories without acceptance criteria** — If you cannot define "done," the story stays in `status/draft`.
3. **Mega-stories** — If a story touches every subsystem, split it. A story should be completable in one session.
4. **Ghost dependencies** — If Story 9 depends on Story 2, mention `#2` in the description and/or add a comment.
5. **Ad-hoc root TODOs** — `FIXES_TODO.md`, `Text-messages-TODO.md`, etc. Use the Gitea issue tracker.

---

## 12. Quick Reference

| Action | Command |
|---|---|
| List open stories | `tea issues ls` |
| List Ready stories | `tea issues ls --labels status/ready` |
| List In Progress stories | `tea issues ls --labels status/in-progress` |
| List In Review stories | `tea issues ls --labels status/in-review` |
| View a story | `tea issues <index>` |
| Create a story | `tea issues create -t "<title>" -d "<description>"` |
| Update title / description | `tea issues edit <index> -t "<title>" -d "<description>"` |
| Change status label | `tea issues edit <index> --add-labels status/<new> --remove-labels status/<old>` |
| Assign yourself | `tea issues edit <index> -a <username>` |
| Add a comment | `tea comment <index> "<text>"` |
| Close a story | `tea issues close <index>` |
| Reopen a story | `tea issues reopen <index>` |
| List open PRs | `tea pr ls` |
| Create a PR | `tea pr create -t "<title>" -d "<description>"` |
| Check out a PR | `tea pr checkout <pr-index>` |
| Review a PR | `tea pr review <pr-index>` |
| Approve a PR | `tea pr approve <pr-index> "<comment>"` |
| Request changes | `tea pr reject <pr-index> "<reason>"` |
| Merge a PR | `tea pr merge <pr-index>` |

### Posting Long Comments with `tea comment`

The `tea comment` command does **not** support `@filename` syntax (verified by testing — it sends the literal string `@/tmp/file.md` as the comment body).

For long or multi-line comments (audit reports, scope changes, etc.), write the text to a file first and use shell command substitution:

```bash
# Write comment to temp file
cat > /tmp/comment.md << 'EOF'
## Audit of Story #51

### Completed
- Item 1
- Item 2

### Remaining
- Item 3
EOF

# Post it — single-quoted heredoc prevents shell expansion
tea comment 51 "$(cat /tmp/comment.md)"
```

**Why this matters:** Inline multi-line strings with backticks, pipes, dollar signs, or unescaped quotes will cause shell parsing errors or silent truncation. Always use a heredoc and `$(cat file)` for comments longer than one line.

### Issue Description Template

Paste this into the description when creating a new issue:

```markdown
## Problem

[What is broken or missing?]

## Acceptance Criteria

- [ ] [Specific, verifiable outcome 1]
- [ ] [Specific, verifiable outcome 2]

## Dependencies

- [ ] #[other-story] (if any)

## Notes

[Any additional context, design decisions, or links to specs]
```

---

**Version**: 2.0
**Last Updated**: 2026-06-01
