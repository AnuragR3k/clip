# Creating the two OAuth apps (step by step)

These must be created while logged in as **you** — I have no browser session for
your Google/Meta accounts, so I can't do this part. Menu wording may shift
slightly, but the fields and redirect URIs below are what matter.

Public URL to use everywhere:
**`https://find-active-print-costa.trycloudflare.com`**

---

## 1. Google Cloud → OAuth client for YouTube

1. Open https://console.cloud.google.com/ and sign in with the Google account
   that owns (or can manage) the YouTube channel you want to post to.
2. Top-left **project dropdown → New Project**. Name it e.g.
   `supoclip-publishing` → **Create**, and make sure it's selected.
3. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
4. **APIs & Services → OAuth consent screen**:
   - User Type: **External** → **Create**.
   - App name: `SupoClip Local`. User support email: your email. Developer
     contact email: your email.
   - On **Scopes**, add:
     - `https://www.googleapis.com/auth/youtube.upload`
     - `https://www.googleapis.com/auth/youtube.readonly`
   - On **Test users**, add the Google account email(s) you will use. This
     matters: while the app is in *Testing*, only listed test users can consent.
   - Save/continue through the remaining screens. You do **not** need to publish
     the app for personal use.
5. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Web application**.
   - Name: `SupoClip`.
   - **Authorized redirect URIs → Add URI**, paste exactly:
     `https://find-active-print-costa.trycloudflare.com/api/social/callback/youtube`
   - **Create**.
6. Copy the **Client ID** and **Client secret**. Send me these two values.

---

## 2. Meta Developers → app for Instagram Reels

1. Open https://developers.facebook.com/apps/ → **Create app**.
   Choose the **Business** app type (Instagram Graph API needs it). Name it
   e.g. `supoclip-reels`, enter your contact email → **Create app**.
2. In the app dashboard, **Add a product** → **Instagram API** (this is the
   Graph API for professional accounts, which uses the scopes SupoClip asks for:
   `instagram_business_basic`, `instagram_business_content_publish`,
   `instagram_business_manage_insights`).
3. **Instagram API → Configuration** (or Settings) → find **OAuth redirect
   URIs** and add exactly:
   `https://find-active-print-costa.trycloudflare.com/api/social/callback/instagram`
4. **Settings → Basic** → copy the **App ID** and **App secret**. Send me these.
5. Make sure the Instagram account you'll connect is a **Business** or
   **Creator** account (Instagram app → Settings → Account type). If Meta asks
   for a Facebook Page, link the Instagram professional account to one.
6. Add yourself as an **admin/tester** of the app. In development mode you can
   authorize your own account without going through App Review; App Review is
   only needed if other people will connect their accounts.

---

## 3. What I do after you paste the four values

- Write them into `~/Desktop/projects/sudoclip/.env`:
  `YOUTUBE_OAUTH_CLIENT_ID`, `YOUTUBE_OAUTH_CLIENT_SECRET`,
  `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`.
- Rebuild/recreate backend + worker, then verify `GET /social/providers`
  reports `enabled: true` with both providers `configured: true`.

## 4. What you then do in the browser

1. Open `https://find-active-print-costa.trycloudflare.com`, register + verify
   a SupoClip account (DB has 0 users right now), and log in.
2. **Settings → Social accounts → Connect YouTube** → approve Google consent
   (if you see "app hasn't been verified", choose **Advanced → Go to
   <app name> (unsafe)** — that's normal for a Testing app you own).
3. **Connect Instagram** → approve Meta consent.
4. **Settings → API keys → Create key**. Export it as `SUPOCLIP_API_KEY`.
5. Confirm both accounts appear as `status: active` in `GET /social/connections`.

## 5. Finally, start posting

```bash
cd ~/Desktop/projects/clip
source .venv/bin/activate
export SUPOCLIP_API_KEY=sk_...
python supervisor.py          # no --no-publish
```
Approve at `http://localhost:8765`. Approved clips upload, register via
`/tasks/ingest-rendered`, and post to YouTube Shorts + Instagram Reels with the
description caption from `config.yaml`.

---

**If the tunnel URL changes** (quick tunnels are ephemeral), tell me the new one:
`.env` and both OAuth apps' redirect URIs must be updated.
