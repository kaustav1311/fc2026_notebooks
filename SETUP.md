# Warehouse → PWA automation setup

This repo's GitHub Actions workflow (`.github/workflows/refresh.yml`) runs the
warehouse pipeline on a schedule and pushes the emitted JSON into the PWA repo
(`kaustav1311/fifawc2026 → public/data/`). Vercel auto-redeploys on that push.

To get there from a clean checkout you do four one-time things:

1. Push this repo to GitHub (public).
2. Create a fine-grained Personal Access Token (PAT) scoped to the PWA repo.
3. Add the PAT as a secret on this repo (`PWA_REPO_PAT`).
4. Wait for (or manually trigger) the first run.

---

## 1. Push to GitHub

The git repo is initialized locally. To put it on GitHub:

**Option A — via the GitHub web UI** (easiest if `gh` CLI isn't installed):

1. Go to <https://github.com/new>
2. Owner: `kaustav1311`. Repo name: `fc2026_notebooks`. Visibility: **Public**. Do NOT initialize with README / .gitignore / license (we have our own).
3. Create the repo. Copy the `git remote add origin …` + `git push` commands GitHub shows you.
4. In a PowerShell/Git Bash in `E:/fc2026_notebooks/`:

```sh
git remote add origin https://github.com/kaustav1311/fc2026_notebooks.git
git branch -M main
git push -u origin main
```

**Option B — via `gh` CLI** (if you install it via `winget install --id GitHub.cli`):

```sh
cd E:/fc2026_notebooks
gh auth login                 # one-time browser auth
gh repo create kaustav1311/fc2026_notebooks --public --source=. --push
```

---

## 2. Create the PAT

The workflow needs write access to the PWA repo to push the refreshed JSON.

1. Go to <https://github.com/settings/personal-access-tokens/new>
2. **Token name**: `wc26-warehouse-refresh-bot` (or anything memorable).
3. **Expiration**: 90 days is fine (you'll renew during/after the tournament).
4. **Resource owner**: `kaustav1311`.
5. **Repository access** → "Only select repositories" → pick **`kaustav1311/fifawc2026`** (and ONLY that repo).
6. **Repository permissions**:
   - `Contents`: **Read and write**
   - `Metadata`: **Read-only** (auto-required)
   - (Leave everything else at "No access".)
7. Click "Generate token". **Copy the `github_pat_…` string immediately** — GitHub only shows it once.

---

## 3. Add the PAT as a repo secret on the warehouse repo

1. Go to <https://github.com/kaustav1311/fc2026_notebooks/settings/secrets/actions>
2. Click "New repository secret".
3. **Name**: `PWA_REPO_PAT` (exactly this — the workflow looks for this name).
4. **Value**: paste the `github_pat_…` token from step 2.
5. Click "Add secret".

---

## 4. First run

The cron runs at the top of every hour. Two options:

**Wait for the next `:00` UTC minute** — the workflow fires automatically.

**Or trigger manually right now** to see it work:

1. Go to <https://github.com/kaustav1311/fc2026_notebooks/actions/workflows/refresh.yml>
2. Click "Run workflow" (top right).
3. Leave inputs blank (auto-pick bucket; no force-refresh).
4. Watch the run.

### What to expect on the very first run

- **~15–25 minutes** — cold start, no GHA cache, refresh.py re-fetches the
  full universe from FotMob/FIFA/ESPN/FDH/etc.
- The cache save step at the end of the run populates the cache for next time.
- The "Push JSON to PWA repo" step commits to `kaustav1311/fifawc2026` →
  Vercel rebuilds the PWA → fresh JSON is live within ~2 min after the push.

### What to expect on every subsequent run

- **~2–4 minutes** — cache hits, event-gated logic skips already-fetched data.
- If nothing changed since the last tick (no matches finished), the push step
  skips with "No JSON changes vs PWA HEAD — nothing to commit."

---

## Cadence summary

| Trigger | Runs |
|---|---|
| Hourly cron (`0 * * * *`) | Every hour, always. Inside the tournament window (2026-06-11 → 2026-07-20), refresh.py does the hourly bucket. On the first tick at-or-after 07:00 UTC each tournament day, it ALSO runs the daily bucket (FIFA squad roster + referee panel sweeps). Outside the window, refresh.py exits after ~5 sec with no work. |
| `workflow_dispatch` (manual) | On demand. Use to force a daily/all/frozen rebuild, or to run with `force_refresh=true` if you suspect stale data. |

The per-table refresh logic lives in `refresh.py` and `lib/events.py` —
that's the existing pipeline, untouched. The workflow just calls it on a clock
and ships the output.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Workflow fails on "Push JSON to PWA repo" with `ERROR: PWA_REPO_PAT secret is not set` | Step 3 not done | Add the secret. |
| Push step says `Permission denied (publickey)` or `403` | PAT expired or wrong scope | Regenerate PAT per step 2; update the secret. |
| Workflow takes 20+ min every run, not just the first | Cache eviction (GHA evicts caches not touched for 7 days) | Should self-heal on the next run after eviction. If it keeps happening, check the "Restore warehouse cache" step output. |
| First run after the tournament starts fetches everything and times out at 45 min | Genuine cold start with no cache + large initial data pull | Increase `timeout-minutes` in the workflow to 60 or 90, run once, then revert. |
| FotMob / FIFA / FDH start returning 403 from GHA runner IPs | Rare but possible if upstream rate-limits the GHA IP range | Switch to a self-hosted runner on a VPS, or proxy via Cloudflare Workers. Out of scope for this setup. |

---

## How to roll back

If something breaks and you want to disable the auto-refresh:

- Go to <https://github.com/kaustav1311/fc2026_notebooks/actions/workflows/refresh.yml>
- Click `...` (top right) → "Disable workflow".
- The workflow stays in the repo but stops firing.
- Re-enable from the same menu when ready.

You can always still do the manual local flow (`python refresh.py && python _emit_pwa_json.py && cd ../fifawc2026 && npm run refresh`) — none of this changes that.
