# Side repo — git history purge runbook
**Status: PREPARED, NOT RUN. Needs Stephane's explicit go.**
**Written 2026-07-28.**

## Why

`github.com/sboghossian/side` is **public**. Its git history contains
`.gstack/browse-audit.jsonl` (67 KB) across **7 commits / 20 objects**, plus
`bin/__pycache__/*.pyc`.

A pattern scan of one of those blobs found **2 occurrences of `sk-ant-`** —
Anthropic API key material. I did not extract or read the key (the safety
classifier blocked it, correctly). The count is enough to act on.

HEAD is already clean: the files were removed and gitignored in `2ded46b`.
**Removing from HEAD does not remove from history.** Anyone can still run
`git log -p` on a clone and read it.

## Do this FIRST, before any git work

1. **Rotate the Anthropic key.** console.anthropic.com -> API keys -> revoke the
   old one, issue a new one. Assume the old key is compromised: the repo is
   public and has been since 2026-07-15.
2. **Rotate the OpenRouter key** pasted into the 2026-07-28 session
   (`sk-or-v1-f0ef...`). It is not in the repo, but it is in a transcript.
3. Check Anthropic usage for anything you do not recognise.

Rotation is what actually fixes this. The purge below is cleanup, and it is
worthless if the key is still live.

## The purge

Requires `git-filter-repo` (preferred over BFG — actively maintained):
`brew install git-filter-repo`

```bash
# 0. BACKUP FIRST. This is a destructive, history-rewriting operation.
cd ~/Documents/Code
cp -R side side-backup-prepurge-$(date +%Y%m%d)
cd side
git bundle create ../side-prepurge-$(date +%Y%m%d).bundle --all   # second belt

# 1. Confirm the working tree is clean and everything is pushed
git status --short          # must be empty
git log origin/main..HEAD   # must be empty

# 2. Purge the paths from ALL history
git filter-repo --invert-paths \
  --path .gstack/browse-audit.jsonl \
  --path .gstack \
  --path bin/__pycache__ \
  --force

# 3. Verify nothing survives
git rev-list --all --objects | grep -E "\.gstack|__pycache__"   # must print nothing

# 4. filter-repo removes the remote. Put it back.
git remote add origin https://github.com/sboghossian/side.git

# 5. Force-push the rewritten history.  <-- THE IRREVERSIBLE STEP
git push origin --force --all
git push origin --force --tags
```

## What step 5 breaks — read before running

- **Every existing clone and fork breaks.** Anyone who cloned must re-clone.
  Low impact here (small OSS project), but it is not zero.
- **Every commit SHA changes.** Links to commits in Linear, memories, notes and
  the plan docs in `tasks/` will 404. The two v0.9 commits (`c18de9f`,
  `f8a4c5a`) will get new hashes.
- **GitHub keeps unreferenced objects for a while.** Even after a force-push the
  old blobs can remain reachable via the API until GitHub GCs them. To be
  certain, open a GitHub support request to purge cached views, or delete and
  recreate the repo.
- This conflicts with the standing rule "never force push to main". It is being
  made as a deliberate, one-off exception for a credential leak, and only with
  explicit approval.

## Recommended order

1. Rotate keys. **Today.** This is the part that matters.
2. Then decide on the purge. Given the key will already be dead, the purge is
   hygiene rather than emergency — which means it can wait for a moment when a
   broken-clone blast radius is convenient.
3. If you would rather not rewrite history at all, the honest alternative is:
   rotate, leave history alone, and accept that a dead key sits in the log.
   That is a legitimate choice and it costs nothing operationally.
