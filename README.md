# Google Home → Bring! connector

Say **"Hey Google, add milk to my shopping list"** and have it show up in your
shared **Bring!** list again.

```
 "Hey Google, add milk        Google Keep             this connector            Bring!
  to my shopping list"   ──►  "Shopping list"  ──►  (checks every minute)  ──►  "Milk" 🥛
                                    │                                       (everyone sharing
                                    └──── item is removed from Keep ◄────     the list sees it)
```

## Why this exists

In June 2023 Google stopped letting Google Home / Google Assistant send
shopping-list items to other apps such as Bring!, AnyList or Any.do. Google
Keep is now the only list app Google Home (and Gemini for Home) can add to.
[Bring! confirmed](https://www.getbring.com/blog-posts/google-assistant-no-more-support-for-third-party-list-apps)
it can't change that. Google may never restore it.

This connector works around that. It lets Google Home keep adding items to
your Google Keep **Shopping list** as usual. It watches that list, copies each
new item into Bring!, and then deletes it from Keep. You and your partner keep
using Bring! exactly as before.

What it does for you:

- **Proper Bring! items.** "milk" becomes Bring!'s own **Milk** item, with its
  icon and aisle sorting, not a plain text entry. "apple" matches **Apples**,
  "tomato" matches **Tomatoes**, and so on.
- **Amounts.** "2 gallons of milk" becomes **Milk** with the note
  *2 gallons*. "a dozen eggs" becomes **Eggs** with *1 dozen*.
- **No duplicates.** If milk is already on the Bring! list, it isn't added
  again. A note your partner added (e.g. *2% organic*) is kept.
- **Two people.** With Voice Match, each person's voice goes to their *own*
  Keep list. Add both Google accounts and both lists get copied.
- **Nothing gets lost.** An item is removed from Keep only after Bring!
  confirms it has it. If the internet or Bring! is down, the item waits in
  Keep and goes through later.
- **Tells you when it needs attention.** If Google ever cancels the saved
  login, a reminder item **"⚠ Voice list: fix Google login"** appears on
  your Bring! list. It disappears once the login is fixed.

---

## What you need

1. **Your Bring! email and password.** If you sign in to Bring! with
   Google or Apple, you don't have a Bring! password yet. Create one in the
   Bring! app: **Profile → Settings → Change password**, or use
   **Forgot password** on the Bring! login screen. You can keep signing in
   with Google in the app afterwards.
2. **The Google account(s) your Google Home uses.** For example your Gmail
   address, plus your partner's if they use Voice Match.
3. **A computer that is always on** to run the connector, such as:
   - a Raspberry Pi (use the 64-bit Raspberry Pi OS),
   - a NAS with Docker (Synology, QNAP, Unraid…), a home server, or
   - a Windows/Mac/Linux PC that doesn't go to sleep.

   No always-on computer? See [Option C](#option-c-github-actions-no-computer-needed).
   It runs for free on GitHub, but items take up to ~30 minutes to arrive.
4. About 20 minutes.

---

## Step 1: Check that Google Home adds to Google Keep

1. Open the **Google Home** app → tap your profile picture → **Home
   settings** → **Gemini for Home voice assistant** (called **Google
   Assistant** on older setups) → **Notes & lists**. Select **Google Keep**.
   (On most accounts this is already the default.) In August 2026 a
   Google Home bug showed this page as disabled. Google fixed it in September
   2026, so update the Google Home app if it still looks greyed out.
2. Say **"Hey Google, add test to my shopping list."**
3. Open **Google Keep** on your phone or at <https://keep.google.com>.
   You should see a checklist called **Shopping list** containing *test*.

If your list has another name (for example *Grocery list*), note it. Setup
will ask for it.

## Step 2: Install and set up the connector

Download this project to the always-on computer. Either use
`git clone https://github.com/speedyrulz/Google-home-bring-connector.git`,
or on GitHub click **Code → Download ZIP** and unzip it. Then pick one option.

### Option A: Docker (recommended)

Use this for a Raspberry Pi, NAS or home server. [Install Docker](https://docs.docker.com/engine/install/)
first if needed. On a Raspberry Pi: `curl -fsSL https://get.docker.com | sh`.

In a terminal, inside the project folder:

```bash
mkdir -p data                                    # where settings are stored
docker compose build                             # builds the connector (a few minutes)
docker compose run --rm keep-to-bring setup      # answer the questions
docker compose up -d                             # start it; it also restarts after reboots
docker compose logs -f                           # watch it work (Ctrl+C stops watching, not the connector)
```

### Option B: Plain Python (Windows, Mac or Linux)

Install [Python 3.11 or newer](https://www.python.org/downloads/). On Windows,
tick "Add Python to PATH" in the installer. Then, in a terminal inside the
project folder:

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# Mac / Linux:  source .venv/bin/activate
pip install .
keep-to-bring setup        # answer the questions
keep-to-bring run          # keep this window open; Ctrl+C stops it
```

To start it automatically on Windows, open **Task Scheduler → Create
Basic Task**. Choose **When I log on**, action **Start a program**. Set
*program* to the full path of `.venv\Scripts\keep-to-bring.exe`, *arguments*
to `run`, and *start in* to the project folder. Also turn off sleep in
Windows power settings.

### What the setup asks

`setup` walks you through three things and tests each one:

1. **Bring!**: your email and password, and which Bring! list to use.
2. **Keep list name**: press Enter to keep the default, `Shopping list`.
3. **Google account(s)**: Google doesn't allow scripts to sign in with a
   password any more. Instead you sign in once in a web browser and copy a
   one-time code. The setup prints these steps:
   1. Open a **private/incognito** browser window and go to
      <https://accounts.google.com/EmbeddedSetup>.
   2. Sign in with the Google account. Click **I agree** if asked. The page
      may then load forever; that's normal.
   3. Press **F12** (Mac: **Cmd+Option+I**) to open developer tools.
   4. **Chrome/Edge:** **Application** tab → **Cookies** →
      `https://accounts.google.com`. **Firefox:** **Storage** tab →
      **Cookies**.
   5. Find the row **oauth_token**, copy its **Value** (it starts with
      `oauth2_4/`), and paste it into the setup within a few minutes.

   The setup swaps that code for a long-lasting login token and checks that
   it can see your Keep list. Repeat this for your partner's account if
   needed. They sign in with *their* Google account in the private window.

Settings are saved to `data/.env` (Docker) or `.env` (plain Python). **Keep
that file private.** It contains your Bring! password and Google login tokens.

## Step 3: Try it

Say **"Hey Google, add bananas to my shopping list."** Within about a minute,
**Bananas** appears in Bring! and disappears from Google Keep.

You can test the setup at any time without changing anything:

```bash
docker compose run --rm keep-to-bring check   # Docker
keep-to-bring check                           # plain Python
```

---

## When the Google login stops working

Google occasionally cancels saved logins, for example after you change your
Google password or after a security check. When that happens:

- new voice items **stay safely in Google Keep** (nothing is lost), and
- **"⚠ Voice list: fix Google login"** appears on your Bring! list.

To fix it, get a new login token (same browser steps as during setup):

```bash
docker compose run --rm keep-to-bring get-token   # Docker
keep-to-bring get-token                           # plain Python
```

The running connector picks up the new token within a minute or two. No
restart is needed. Everything that piled up in Keep is then copied over, and
the reminder disappears from Bring!.

## Settings

`setup` covers the essentials. To change anything else, edit the settings
file (`data/.env` or `.env`), then restart: `docker compose restart`, or stop
and start `keep-to-bring run`.

| Setting | Default | What it does |
| --- | --- | --- |
| `BRING_EMAIL`, `BRING_PASSWORD` | (required) | Your Bring! login. |
| `BRING_LIST_NAME` | your default list | Which Bring! list items go to. |
| `GOOGLE_EMAIL`, `GOOGLE_MASTER_TOKEN` | (required) | Google account and its login token (from `get-token`). |
| `GOOGLE_EMAIL_2`, `GOOGLE_MASTER_TOKEN_2`, … | – | More Google accounts (up to 10). |
| `KEEP_LIST_TITLES` | `Shopping list` | Name(s) of the Keep checklist(s) to watch, comma separated. |
| `POLL_INTERVAL_SECONDS` | `60` | How often to check Keep (minimum 15). |
| `KEEP_ACTION` | `delete` | After copying: `delete` the item from Keep, or `check` it off. |
| `PARSE_QUANTITIES` | `true` | "2 gallons of milk" → **Milk** with the note *2 gallons*. |
| `SPLIT_ITEMS` | `false` | Split "milk, eggs and bread" into separate items. Google usually does this already. Common names like "mac and cheese" are never split. |
| `ALERT_IN_BRING` | `true` | Show the "fix Google login" reminder on the Bring! list. |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows much more detail for troubleshooting. |

## Troubleshooting

| Problem | What to do |
| --- | --- |
| Items appear in Keep but never in Bring! | Run `check` (see Step 3). It tells you which part fails. |
| `check` says no matching Keep list | Say "Hey Google, add test to my shopping list" once. If your list is named differently, set `KEEP_LIST_TITLES`. |
| My partner's items don't come through | Their voice probably goes to *their* Keep list. Run `get-token` with their Google account; it is added as an extra account. |
| "Bring! rejected the login" | Check the email/password by logging in at <https://web.getbring.com>. Google/Apple sign-in users must set a Bring! password first (see "What you need"). |
| "Google refused the code" during setup | The browser code works once and expires within minutes. Repeat the browser steps and paste the new code right away. Make sure the email matches the account you signed in with. |
| Docker: "permission denied" writing `/data` | Run `mkdir -p data` *before* the first `docker compose` command. If your user id isn't 1000 (`id -u`), start with `PUID=$(id -u) PGID=$(id -g) docker compose up -d`. |
| Item names look odd | Turn off `PARSE_QUANTITIES` if amounts are being split off wrongly. |

To update to a newer version: download the new files, then run
`docker compose build --pull && docker compose up -d`, or `pip install .`
again for plain Python.

## Security and privacy

- The Google login token gives broad access to that Google account, like a
  password. It is stored only in your settings file on your own computer.
  It is sent only to Google. Your Bring! password is sent only to Bring!.
- To cancel a token: change your Google password, or remove the device at
  <https://myaccount.google.com/device-activity>.
- **Extra-safe option:** create a separate Google account just for the
  connector. In Google Keep, share your Shopping list with it (open the list →
  **Collaborator**), then give the connector that account instead of your
  own. It can then only see the lists you share with it.
- The connector reads only the Keep list(s) you name. It changes nothing
  else in your Google account.
- Neither Google Keep nor Bring! has an official public API for personal
  accounts. This project uses the same community libraries as Home Assistant
  integrations: [gkeepapi](https://github.com/kiwiz/gkeepapi) and
  [bring-api](https://github.com/miaucl/bring-api). If Google or Bring!
  change something, an update to those libraries may be needed.

---

## Option C: GitHub Actions (no computer needed)

GitHub can run the connector for free every 30 minutes. It's slower (items
arrive within about 30 minutes, sometimes later). Google is also more likely
to cancel a login used from GitHub's servers than one used from your home.
Use it only if you have no always-on computer.

1. Get your tokens on any computer with Python (Option B steps), using
   `keep-to-bring get-token --print`.
2. On GitHub, open this repository → **Settings → Secrets and variables →
   Actions**:
   - **Secrets:** add `GOOGLE_EMAIL`, `GOOGLE_MASTER_TOKEN`, `BRING_EMAIL`
     and `BRING_PASSWORD`. Optionally add `GOOGLE_EMAIL_2` and
     `GOOGLE_MASTER_TOKEN_2`.
   - **Variables:** optionally add `BRING_LIST_NAME` and `KEEP_LIST_TITLES`.
3. Open `deploy/github-actions/scheduled-sync.yml` on GitHub, click the ✏️
   (edit), and change the file name at the top to
   `.github/workflows/scheduled-sync.yml`. Commit.
4. Under **Actions → Scheduled sync → Run workflow** you can trigger it once
   to test. GitHub emails you if a run fails, for example when the Google
   login needs renewing.

On a free GitHub plan with a private repository, this uses about 1,500 of the
2,000 free minutes per month.

## Alternative: Home Assistant

If you already run [Home Assistant](https://www.home-assistant.io/), you can
build the same bridge there. Use the official
[Bring! integration](https://www.home-assistant.io/integrations/bring/), the
community [Google Keep Sync](https://github.com/watkins-matt/home-assistant-google-keep-sync)
integration (installed via HACS), and an automation that moves new Keep items
to the Bring! to-do list. This connector is for everyone who doesn't run
Home Assistant.

## For developers

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
pytest
```

The tests run the real `gkeepapi` and `bring-api` libraries against
in-memory fake Google Keep and Bring! servers. No accounts are needed.

| File | Purpose |
| --- | --- |
| `src/keep_to_bring/sync.py` | The sync loop (at-least-once: removes from Keep only after Bring! confirms). |
| `src/keep_to_bring/keep_source.py` | Reads/clears the Google Keep list (gkeepapi). |
| `src/keep_to_bring/bring_sink.py` | Adds items to Bring! (bring-api). |
| `src/keep_to_bring/catalog.py` | Matches spoken names to Bring!'s item catalogue. |
| `src/keep_to_bring/parsing.py` | Splits off quantities ("2 lbs chicken"). |
| `src/keep_to_bring/google_token.py` | Turns the browser `oauth_token` cookie into a master token. |
| `src/keep_to_bring/net.py` | Timeouts for the Google libraries (which set none) and shutdown-safe worker threads. |
| `src/keep_to_bring/cli.py` | `setup`, `get-token`, `check`, `once`, `run`, `health`. |
