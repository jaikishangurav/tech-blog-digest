# tech-blog-digest

A daily email digest of engineering blogs (Netflix, Uber, Cloudflare, Stripe, ...), summarized by an LLM and published to a searchable archive site.

It runs entirely on free services: GitHub Actions for the schedule, the Gemini API free tier for summaries, Gmail for sending, and GitHub Pages for the archive.

## How it works

Once a day, a GitHub Actions workflow runs `python digest.py all`, which:

1. **fetch:** reads every RSS feed in `config.yaml`, downloads new articles, and asks Gemini for a summary, key takeaways and 1–3 tags. Hiring posts, press releases and marketing content are filtered out.
2. **send:** emails the unsent articles whose tags are due. Each tag is sent `daily`, `weekly` or never (`off`).
3. **build:** regenerates `docs/index.html`, a static archive page with search and tag filters.

The workflow then commits `data/articles.db` (a SQLite file that tracks what's been seen and sent) and `docs/` back to the repo, so state carries over between runs.

## Set up your own copy

You'll need a GitHub account, a Gmail account to send from, and a Google account for a Gemini API key. Setup takes about 15 minutes.

### 1. Fork the repo

Click **Fork** on GitHub. Keep the fork **public**: GitHub Pages is free only for public repos. Your secrets stay private either way.

### 2. Clear the original owner's data

Your fork comes with the original owner's article history. Delete it so your first run starts fresh:

```bash
git clone https://github.com/<you>/tech-blog-digest.git
cd tech-blog-digest
git rm data/articles.db
git commit -m "Reset article history"
git push
```

The database is created again on the first run. `docs/index.html` is rebuilt every run.

### 3. Edit `config.yaml`

- **`archive_url`:** change it to `https://<your-username>.github.io/tech-blog-digest/`. This link goes at the bottom of each email.
- **`feeds`:** add or remove blogs. Each entry needs a `name` and an RSS/Atom `url`.
- **`tags`:** choose how often each topic is emailed: `daily`, `weekly` or `"off"` (archive only). Keep `"off"` in quotes, because YAML reads a bare `off` as false. `default` sets the frequency for articles that don't match any tag.
- **`model`:** the Gemini model to use (see step 4).
- **`max_new_per_run`:** the maximum number of articles summarized per run. Keep it at or below your model's free daily request limit.
- **`backfill_days`:** on the first run, posts older than this are ignored.
- **`request_delay`:** seconds to wait between Gemini calls, to stay under the per-minute limit.

Commit and push your changes.

### 4. Get a Gemini API key

1. Create a key at https://aistudio.google.com/apikey.
2. Check that the `model` in `config.yaml` is on the free tier. The [pricing page](https://ai.google.dev/gemini-api/docs/pricing) has a "Free Tier" column, and AI Studio's usage/rate-limit page shows your per-model limits. Free models and limits change often, so if `model` isn't free for you, choose one that is.

To list the models your key can use (the `read -s` keeps the key out of your shell history):

```bash
read -s "KEY?Gemini key: " && curl -s "https://generativelanguage.googleapis.com/v1beta/models?pageSize=100" -H "x-goog-api-key: $KEY" | grep '"name"'; unset KEY
```

`read -s "KEY?..."` is zsh syntax. In bash, use `read -sp "Gemini key: " KEY`.

### 5. Create a Gmail app password

A separate Gmail account just for sending is recommended. It keeps the app password away from your personal mailbox.

1. Sign in to the sending account and turn on **2-Step Verification** at https://myaccount.google.com/security.
2. Create an app password at https://myaccount.google.com/apppasswords. Give it any name, such as `tech-blog-digest`.
3. Copy the 16-character password. Google shows it only once.

The first few digests may land in spam. Mark one as "Not spam" and add the sending address to your contacts.

### 6. Add repository secrets

In your fork, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
| --- | --- |
| `GEMINI_API_KEY` | your Gemini API key |
| `SMTP_USER` | the Gmail address you send from |
| `SMTP_PASS` | the app password from step 5 (not your Gmail password) |
| `EMAIL_TO` | the address that receives the digest |

Or use the [GitHub CLI](https://cli.github.com/) from your clone, which prompts for each value:

```bash
gh secret set GEMINI_API_KEY
gh secret set SMTP_USER
gh secret set SMTP_PASS
gh secret set EMAIL_TO
```

Gmail is the default. For another provider, also add an `SMTP_HOST` secret, add it to the workflow's `env:`, and check that the provider supports SMTP over SSL on port 465.

### 7. Enable GitHub Actions

GitHub turns off workflows on forks by default. Open your fork's **Actions** tab and click **I understand my workflows, go ahead and enable them**.

The workflow needs write access to commit the database. It requests this itself with `permissions: contents: write`. If your account or organization restricts that, go to **Settings → Actions → General → Workflow permissions** and choose **Read and write permissions**.

### 8. Enable GitHub Pages

Go to **Settings → Pages**. Under **Build and deployment**, choose **Deploy from a branch**, then branch `main` and folder `/docs`. Save.

The archive appears at `https://<your-username>.github.io/tech-blog-digest/` a minute or two after the first run.

### 9. Run it once

Start the workflow from **Actions → digest → Run workflow**, or with:

```bash
gh workflow run digest
gh run watch
```

Check the log:

- `[warn] no entries: <name>`: that feed returned nothing. Its URL may have changed, or the site blocks bots. Fix or remove it in `config.yaml`.
- `[warn] rate limited, stopping fetch`: you reached Gemini's quota. The remaining articles are picked up on the next run.
- `[error] send: ...`: the email failed, usually because of a wrong app password. The archive is still built, and the run is marked as failed so you notice.

After this, it runs every day on its own.

## Change the schedule

The schedule is the `cron` line in `.github/workflows/digest.yml`. It uses **UTC**. The default `30 2 * * *` is 02:30 UTC, which is 08:00 in India. Use https://crontab.guru to work out other times.

GitHub may delay scheduled runs by a few minutes, and pauses them on repos with no activity for 60 days. The bot commits whenever new articles arrive, which normally keeps the schedule active. If GitHub does pause it, re-enable it from the Actions tab.

## Run locally

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Then set the variables and run any step:

```bash
export GEMINI_API_KEY=...  SMTP_USER=...  SMTP_PASS=...  EMAIL_TO=...
python digest.py fetch   # read feeds and summarize new articles
python digest.py send    # email what's due
python digest.py build   # rebuild docs/index.html
python digest.py all     # all three (the default)
```

`fetch` and `build` don't need the SMTP variables. A local run writes to `data/articles.db`, the same file the workflow uses. If you commit and push it, the scheduled run picks up from where you stopped.

## Good to know

- **Each article is emailed once,** under the first of its tags that is due.
- **Failed summaries are retried** on up to 3 runs, then skipped. Articles too short to summarize are skipped at once. A Gemini quota error doesn't count as a failure.
- **Weekly tags** are sent 7 days after their last send, not on a fixed weekday.
- **To re-send or re-summarize** an article, delete its row from the `articles` table in `data/articles.db`.
