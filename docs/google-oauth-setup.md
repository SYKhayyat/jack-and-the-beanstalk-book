# Google sign-in — the click-by-click walkthrough

This is the operator's walkthrough for turning on "Continue with Google".
It takes about ten minutes in the Google Cloud console and needs no code
changes: the app reads four environment variables and switches itself over.
When both `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set, Google is
the **only** way in — the shared password form disappears entirely.

---

## Before you start

- You need a Google account, and a Google Cloud project (one is created in
  step 1 below; the free tier of Google Cloud is enough — OAuth itself costs
  nothing).
- Know your deployment URL. For the Render site it is
  `https://jack-and-the-beanstalk-book.onrender.com`.

## 1. Create the project and the OAuth client

1. Go to <https://console.cloud.google.com/apis/credentials>.
2. If you have no project yet: **Create project**, name it (for example
   `jack-and-the-beanstalk`), no organisation needed.
3. **Create credentials → OAuth client ID.** If it offers to *configure the
   consent screen* first, that is normal — follow it and continue below.
   Choose **Web application** as the type.

### The consent screen — the one gotcha in this whole document

The consent screen wizard asks about the **publishing status**. Google's
default suggestion for a new project is **"Testing"**, and it looks like a
saving: no verification needed, works immediately.

**Do not stay in "Testing".** A consent screen in Testing mode issues
sessions that **hard-expire after 7 days** — every user is signed out within
a week, with no way to extend it. Publish the app:

1. Finish the wizard (the rest of the fields don't matter for this app).
2. On **OAuth consent screen**, press **Publish app** and confirm.
3. It will warn "your app is unverified". That is fine: it only uses the
   `openid`, `email` and `profile` scopes, which Google classifies as low-risk,
   and users will see a "unverified app" notice once on first sign-in and
   simply tick "advanced → continue". No sandboxes, no verification review
   needed at this scope set.

### Redirect URIs — add these three

On the OAuth client's page, under **Authorised redirect URIs**, add **exactly**
(the string must match what the app sends, character for character):

```
https://jack-and-the-beanstalk-book.onrender.com/auth/callback
http://localhost:8000/auth/callback
```

If you also wire up the custom domain `book.siachshai.online`
(CNAME `book` → `jack-and-the-beanstalk-book.onrender.com`, proxied through
Cloudflare), add its callback as well **and** use it as `GOOGLE_REDIRECT_URI`
below — the redirect URI must be exactly the address people see in their
browser bar:

```
https://book.siachshai.online/auth/callback
```

Nothing else is authorised — no JavaScript origins are needed for this flow.

## 2. Copy the client id and secret

The OAuth client page shows a **Client ID** (a long string ending in
`apps.googleusercontent.com`) and a **Client secret**. Copy both.

Neither of these ever goes into the repo, a log, or a committed file. They
go straight into the environment:

**On Render** (Environment tab of the service):

```
GOOGLE_CLIENT_ID      = <the client id>
GOOGLE_CLIENT_SECRET  = <the client secret>
GOOGLE_REDIRECT_URI   = https://jack-and-the-beanstalk-book.onrender.com/auth/callback
SESSION_SECRET        = <a long random string — see below>
```

Then **Manual deploy → Deploy latest commit** so the new variables are picked
up (env var changes are not hot; the service needs a restart).

**Locally** (`.env`, which is git-ignored): paste the same four lines. Leave
`GOOGLE_REDIRECT_URI` empty when developing — the app then derives the
redirect from whatever host the request actually came in on, which is wrong
on a server but exactly right on `http://localhost:8000`.

### `SESSION_SECRET` — do not skip it

This is the key that signs the session cookie. If it is unset, the app
invents one at startup, which means **every restart signs every user out** —
and on Render a restart happens at least once a day. Paste any long random
string, e.g. generate one:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 3. Check it worked

1. Open the site. You should be bounced to a login page offering
   **"Continue with Google"** and *no password form*.
2. Click it, choose your Google account. Google shows the consent screen
   once ("unverified" is expected — see above). After approving you land
   back on the app, signed in, with your name in the masthead.
3. In the Render logs, look for the line
   `auth.google_login_ok sub=… email=s***@…` — that is the sign-in
   succeeding, with the email masked.

The `/healthz` endpoint never requires a sign-in, so platform monitoring
keeps working while the app itself is locked.

## What each variable means, in one line each

| Variable | Meaning |
| --- | --- |
| `GOOGLE_CLIENT_ID` | The id Google issued for this app. |
| `GOOGLE_CLIENT_SECRET` | The key used when trading a sign-in code for an identity. Lives only in the environment. |
| `GOOGLE_REDIRECT_URI` | The exact callback address registered in the console. Empty only in local development. |
| `SESSION_SECRET` | Signs the session cookie; set it or restarts sign everyone out. |

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `Error 400: redirect_uri_mismatch` from Google | The `GOOGLE_REDIRECT_URI` env var and the console list disagree. They must be byte-identical (https, no trailing slash). |
| Everyone signed out again within a week | The consent screen is in "Testing" mode. Publish it (step 1). |
| Everyone signed out after every deploy | `SESSION_SECRET` is not set. |
| `That sign-in link had expired` on return | The sign-in waited more than 15 minutes between redirect and callback, or the browser dropped the `jb_oauth_state` cookie. Start again. |
| Session cookie silently missing in Safari | Third-party-cookie blocking; this is a first-party cookie so it should not happen — check you are not proxying through a different host than `GOOGLE_REDIRECT_URI`. |
