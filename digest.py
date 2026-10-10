"""Tech blog digest: fetch -> summarize -> email -> static archive.
Usage: python digest.py [fetch|send|build|all]"""
import calendar, datetime as dt, html, json, os, smtplib, sqlite3, sys, time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import feedparser, trafilatura, yaml
from anthropic import Anthropic

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "articles.db")
CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml")))
# YAML 1.1 reads a bare `off` as False; map it back so INTERVAL_DAYS lookups work
CFG["tags"] = {k: "off" if v is False else v for k, v in CFG["tags"].items()}
MODEL = "claude-haiku-4-5-20251001"
ALLOWED = [t for t in CFG["tags"] if t != "default"]
INTERVAL_DAYS = {"daily": 1, "weekly": 7, "off": None}
MAX_ATTEMPTS = 3  # summarize failures before an article is recorded as skipped


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS articles(url TEXT PRIMARY KEY, company TEXT, title TEXT,
      published TEXT, summary TEXT, takeaways TEXT, tags TEXT, sent INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tag_state(tag TEXT PRIMARY KEY, last_sent TEXT);
    CREATE TABLE IF NOT EXISTS failures(url TEXT PRIMARY KEY, attempts INTEGER);""")
    return c


# ---------- fetch + summarize ----------
def summarize(client, title, text):
    prompt = f"""Article title: {title}

Article text:
{text[:12000]}

Return ONLY a JSON object, no other text:
{{"technical": true/false,
 "summary": "2-3 sentence summary",
 "takeaways": ["3 short key takeaways"],
 "tags": ["1-3 tags chosen ONLY from {ALLOWED}"]}}
Set technical=false for hiring posts, press releases, funding, culture or marketing content."""
    r = client.messages.create(model=MODEL, max_tokens=600,
                               messages=[{"role": "user", "content": prompt}])
    t = r.content[0].text
    return json.loads(t[t.find("{"): t.rfind("}") + 1])


def skip(c, url, company, title):
    """Mark an article done without archiving or emailing it, so it is not retried."""
    c.execute("INSERT OR IGNORE INTO articles VALUES(?,?,?,?,?,?,?,?)",
              (url, company, title, dt.datetime.now(dt.timezone.utc).isoformat(),
               "", "[]", "[]", 1))
    c.execute("DELETE FROM failures WHERE url=?", (url,))
    c.commit()


def fetch():
    c, client = db(), Anthropic()
    cutoff = time.time() - CFG.get("backfill_days", 14) * 86400
    budget = CFG.get("max_new_per_run", 30)
    for src in CFG["feeds"]:
        try:
            feed = feedparser.parse(src["url"], agent="TechBlogDigest/1.0 (personal reader)")
            if not feed.entries:
                print(f"[warn] no entries: {src['name']}")
            for e in feed.entries:
                url = e.get("link")
                if not url or c.execute("SELECT 1 FROM articles WHERE url=?", (url,)).fetchone():
                    continue
                ts = e.get("published_parsed") or e.get("updated_parsed")
                if ts and calendar.timegm(ts) < cutoff:
                    continue
                if budget <= 0:
                    break
                budget -= 1
                try:
                    page = trafilatura.fetch_url(url)
                    text = (trafilatura.extract(page) if page else None) or e.get("summary", "")
                    if len(text) < 200:
                        skip(c, url, src["name"], e.get("title", url))
                        continue
                    r = summarize(client, e.get("title", ""), text)
                    published = (dt.datetime.fromtimestamp(calendar.timegm(ts), dt.timezone.utc)
                                 if ts else dt.datetime.now(dt.timezone.utc)).isoformat()
                    tags = [t for t in r.get("tags", []) if t in ALLOWED] or ["general"]
                    ok = bool(r.get("technical"))
                    c.execute("INSERT INTO articles VALUES(?,?,?,?,?,?,?,?)",
                              (url, src["name"], e.get("title", url), published,
                               r["summary"] if ok else "", json.dumps(r.get("takeaways", [])),
                               json.dumps(tags), 0 if ok else 1))
                    c.commit()
                    time.sleep(1)
                except Exception as ex:  # one bad article never stops the run
                    print(f"[error] {url}: {ex}")
                    c.execute("INSERT INTO failures VALUES(?,1) ON CONFLICT(url) "
                              "DO UPDATE SET attempts=attempts+1", (url,))
                    c.commit()
                    n = c.execute("SELECT attempts FROM failures WHERE url=?", (url,)).fetchone()[0]
                    if n >= MAX_ATTEMPTS:
                        print(f"[skip] {url}: failed {n} times")
                        skip(c, url, src["name"], e.get("title", url))
        except Exception as ex:
            print(f"[error] feed {src['name']}: {ex}")


# ---------- email ----------
def freq(tag):
    return CFG["tags"].get(tag, CFG["tags"]["default"])


def is_due(c, tag, now):
    days = INTERVAL_DAYS[freq(tag)]
    if days is None:
        return False
    row = c.execute("SELECT last_sent FROM tag_state WHERE tag=?", (tag,)).fetchone()
    if not row:
        return True
    last = dt.datetime.fromisoformat(row["last_sent"])
    return (now - last).total_seconds() >= days * 86400 - 3600


def send():
    c, now = db(), dt.datetime.now(dt.timezone.utc)
    due = {t for t in ALLOWED + ["general"] if is_due(c, t, now)}
    rows = c.execute("SELECT * FROM articles WHERE sent=0 AND summary!='' "
                     "ORDER BY published DESC").fetchall()
    groups = {}
    for r in rows:  # each article is emailed once, under its first due tag
        first = next((t for t in json.loads(r["tags"]) if t in due), None)
        if first:
            groups.setdefault(first, []).append(r)
    if not groups:
        print("Nothing due to send.")
        return
    e = html.escape
    body = ["<div style='font-family:system-ui,sans-serif;max-width:680px'>"]
    for tag, arts in groups.items():
        body.append(f"<h2 style='margin:24px 0 8px'>{e(tag)}</h2>")
        for a in arts:
            lis = "".join(f"<li>{e(t)}</li>" for t in json.loads(a["takeaways"]))
            body.append(f"<p><a href='{e(a['url'])}'><b>{e(a['title'])}</b></a><br>"
                        f"<small>{e(a['company'])}</small><br>{e(a['summary'])}</p><ul>{lis}</ul>")
    body.append(f"<hr><p><a href='{e(CFG['archive_url'])}'>Browse the full archive</a></p></div>")
    n = sum(len(v) for v in groups.values())
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Tech Blog Digest: {n} new articles"
    msg["From"], msg["To"] = os.environ["SMTP_USER"], os.environ["EMAIL_TO"]
    msg.attach(MIMEText("".join(body), "html"))
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
    c.executemany("UPDATE articles SET sent=1 WHERE url=?",
                  [(a["url"],) for v in groups.values() for a in v])
    for tag in groups:
        c.execute("INSERT OR REPLACE INTO tag_state VALUES(?,?)", (tag, now.isoformat()))
    c.commit()
    print(f"Sent {n} articles.")


# ---------- static archive ----------
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Tech Blog Digest</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mu:#666;--bd:#e3e3e3;--ac:#1a56db}
@media(prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--mu:#999;--bd:#333;--ac:#7aa7ff}}
body{font:16px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:760px;margin:0 auto;padding:16px}
input{width:100%;padding:10px;font:inherit;border:1px solid var(--bd);border-radius:8px;background:var(--bg);color:var(--fg);box-sizing:border-box}
.chips{margin:12px 0}.chip{display:inline-block;margin:0 6px 6px 0;padding:2px 10px;border:1px solid var(--bd);border-radius:99px;font-size:13px;cursor:pointer}
.chip.on{background:var(--ac);color:#fff;border-color:var(--ac)}
h2{font-size:14px;color:var(--mu);margin:28px 0 8px}article{border-bottom:1px solid var(--bd);padding:12px 0}
a{color:var(--ac)}.meta{font-size:13px;color:var(--mu)}
</style></head><body><h1>Tech Blog Digest</h1>
<input id="q" placeholder="Search title, summary, company..."><div class="chips" id="chips"></div><div id="list"></div>
<script>
const D=__DATA__;let tag=null;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const tags=[...new Set(D.flatMap(a=>a.tags))].sort();
function render(){
 const q=document.getElementById('q').value.toLowerCase();
 document.getElementById('chips').innerHTML=tags.map(t=>`<span class="chip${t==tag?' on':''}" data-t="${esc(t)}">${esc(t)}</span>`).join('');
 let day='',h='';
 for(const a of D){
  if(tag&&!a.tags.includes(tag))continue;
  if(q&&!(a.title+a.summary+a.company+a.takeaways.join(' ')).toLowerCase().includes(q))continue;
  const d=a.published.slice(0,10);
  if(d!==day){day=d;h+=`<h2>${esc(d)}</h2>`}
  h+=`<article><a href="${esc(a.url)}"><b>${esc(a.title)}</b></a><div class="meta">${esc(a.company)} · ${a.tags.map(esc).join(', ')}</div><p>${esc(a.summary)}</p><ul>${a.takeaways.map(t=>`<li>${esc(t)}</li>`).join('')}</ul></article>`}
 document.getElementById('list').innerHTML=h||'<p>No matches.</p>'}
document.getElementById('q').oninput=render;
document.getElementById('chips').onclick=e=>{const t=e.target.dataset.t;if(t){tag=tag==t?null:t;render()}};
render();
</script></body></html>"""


def build():
    rows = [dict(r) for r in db().execute(
        "SELECT url,company,title,published,summary,takeaways,tags FROM articles "
        "WHERE summary!='' ORDER BY published DESC")]
    for r in rows:
        r["takeaways"], r["tags"] = json.loads(r["takeaways"]), json.loads(r["tags"])
    os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)
    data = json.dumps(rows).replace("</", "<\\/")
    open(os.path.join(ROOT, "docs", "index.html"), "w").write(PAGE.replace("__DATA__", data))
    print(f"Archive built: {len(rows)} articles.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("fetch", "all"): fetch()
    send_failed = False
    if cmd in ("send", "all"):
        try:
            send()
        except Exception as ex:  # still rebuild the archive if email fails
            print(f"[error] send: {ex}")
            send_failed = True
    if cmd in ("build", "all"): build()
    if send_failed: sys.exit(1)  # fail the job so a broken email setup is noticed
