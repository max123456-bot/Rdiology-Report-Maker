# Deploying

Three different things, with three different answers. Do them in order — each one
builds on the last.

| Who | Answer | Patient data goes |
|---|---|---|
| **Clinic PCs** | This PC serves the LAN | Nowhere. Stays in the building. |
| **You, from home** | Tailscale — a private network | Encrypted, device to device |
| **Other clients** | Not yet. Read the last section. | — |

---

## 1. Clinic PCs — do this first

One PC runs the app; the rest open it in a browser. Nothing to install anywhere else.

### On the PC that will serve (this one)

**Once, as Administrator:** right-click `allow_clinic_access.bat` → *Run as administrator*.

Windows Firewall blocks incoming connections by default, so without this the app works
on this PC and every other PC just times out. The rule only opens port 8501 on
**private and domain** networks, never public — so on a hotel or cafe Wi-Fi the app
stays invisible.

**Then, to start it:** double-click `serve.bat`. Leave the window open — closing it
stops the app for everyone.

It prints the addresses to use and moves your templates into a shared SQLite database
the first time, because several people editing at once needs transactions rather than
loose files.

### On every other clinic PC

Open a browser and go to:

```
http://AmitPC:8501
```

**Use the computer name, not the IP.** This PC currently has `192.168.1.23`, but that
address is handed out by DHCP and will change after a router reboot — at which point
every bookmark in the clinic breaks. The name survives.

Bookmark it on each PC. Nothing gets installed on them.

### Verified working

Tested from the network address, not just localhost:

```
localhost   -> HTTP 200
LAN IP      -> HTTP 200
hostname    -> HTTP 200
health      -> ok
```

### Keep it running

The server PC must be on and awake. Two things worth doing:

- **Stop it sleeping:** Settings → System → Power → Screen and sleep → *Sleep: Never*
  when plugged in.
- **Start on boot:** Task Scheduler → Create Task → *Run whether user is logged on or
  not* → Trigger: *At startup* → Action: `serve.bat`. Then nobody has to remember.

### Back it up

Everything lives in `data\reports.db` — every doctor template, every learned word.
Copy that file somewhere else weekly. A one-line scheduled task is enough:

```bat
copy /Y "data\reports.db" "\\clinic-nas\backups\reports-%date:~-4%%date:~3,2%%date:~0,2%.db"
```

---

## 2. You, remotely

**Do not port-forward this to the internet.** Port forwarding puts an app that holds
patient reports on the public internet, where it will be found by scanners within
hours, and it currently has no sign-in.

Use **Tailscale** instead. It builds a private encrypted network between your devices;
the app is never publicly reachable at all.

1. Install Tailscale on the clinic PC and on your laptop/phone. Sign in with the same
   account on both — https://tailscale.com/download (free for personal use).
2. Nothing else. No firewall changes, no router changes, no public address.
3. From anywhere, open `http://amitpc:8501` — the same URL as inside the clinic.

Tailscale gives you the private network. It does **not** log you in to the app, so
also turn on the access gate below.

### Turn on the access gate

In `.streamlit/secrets.toml`:

```toml
ACCESS_CODE = "pick something long that is not the clinic name"
```

Restart the app. Everyone now needs that code, and the sidebar gains a **Lock** button.

This is deliberately basic: one shared code, so the activity log can record *that*
someone acted but not *who*. For names in the log, configure real sign-in instead —
the `[auth]` block in `secrets.toml.example` works with any OIDC provider (Google
included) and fills the "Who" column in the activity log.

---

## 3. Other clients — not yet, and here is exactly why

Two problems, both real, neither a deployment setting.

### Every client would share one set of templates

All templates live in one store. Point a second clinic at this app and they see your
doctors' templates, their letterheads, and everything the app learned about how your
radiologists write. Your clients would see each other.

Fixing this properly means scoping every record to a tenant — the storage layer is
already pluggable, so it is a contained change (a tenant column, a tenant from the
signed-in user, and every query filtered by it). It is a day of work, not a setting,
and it must be done *before* a second client touches the system, not after.

### Patient data would leave your control

Right now nothing is stored server-side: a report is formatted and handed back. The
moment other clinics use this, you are processing their patient data on your
infrastructure, and that brings obligations you should get advice on rather than take
from me — retention, breach notification, what your contract with them says, and what
Indian data protection law requires of you as the processor.

### What I would do

1. Get it working for your own clinic. Weeks, not days — find the rough edges with
   users you can walk over to.
2. Add real sign-in, so the activity log has names.
3. Add tenant separation, and test that clinic A genuinely cannot see clinic B.
4. Only then take a paying client, with a written agreement about their data.

Selling it is a good instinct. Doing it before steps 2 and 3 is how a small clinic
tool becomes somebody's data breach.

---

## Hosting it on Render

`render.yaml` is in the repo. Render reads it, builds the app, creates the database and
wires them together, so no connection string is ever copied by hand.

1. **[render.com](https://render.com)** → sign in with GitHub → **New** → **Blueprint**.
2. Pick `Rdiology-Report-Maker`. Render finds `render.yaml` and shows what it will create:
   one web service and one Postgres database, both free, Singapore region (closest to India).
3. It asks for the two secrets that are deliberately not in the file:
   - `GEMINI_API_KEY` — your key
   - `ACCESS_CODE` — **required.** This is a public URL holding patient reports.
4. Apply. First build takes a few minutes.
5. Move your existing templates across, from this PC:
   ```
   python migrate_storage.py --to "<the External Database URL from Render>" --dry-run
   ```
   Check the list, then run it again without `--dry-run`.

### Free tier: two things that will catch you out

**The database may be deleted.** Render's free Postgres has historically been removed
after a fixed period (30 days at the time of writing). If that is still their policy,
**every doctor template and everything the app has learned goes with it.** Check the
current terms on the database page before you rely on it. Either upgrade that one
component, or set a calendar reminder to run:

```
python migrate_storage.py --from "<render url>" --to files
```

That pulls everything back to this PC as JSON, and you can push it to a fresh database.
Do it monthly regardless — it is your backup.

**The app sleeps.** A free Render web service spins down after about 15 minutes of no
traffic, and the next visit waits roughly a minute for it to wake. Fine for you; it
looks broken to a doctor mid-clinic. The paid tier keeps it warm.

### If the database is unreachable

The app does not hang or crash: it falls back to JSON files within 8 seconds and shows a
red banner in the sidebar explaining why. Work continues — but anything saved during that
window lands on the server's temporary disk and will not survive a restart, so fix the
database before carrying on.

## Deploying to Streamlit Cloud instead

Possible, but understand the trade: it is a public URL, and the filesystem is wiped on
every restart.

- You **must** set `STORAGE_URL` to a Postgres database or lose every template on the
  next redeploy. Migrate with `python migrate_storage.py --to "postgresql://..."`.
- You **must** set `ACCESS_CODE` or configure `[auth]`, or the URL is open to anyone.
- Patient dictation and reports would travel to a US-hosted service. For an Indian
  clinic that is a decision to make deliberately, not by accident.

For a single clinic, the LAN setup above is better in every way that matters.
