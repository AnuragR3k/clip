# Connecting YouTube + Instagram — live setup state

## Done on this machine (2026-11-18)
- cloudflared installed at `~/.local/bin/cloudflared`; quick tunnel running to
  `http://localhost:3107`.
- **Public URL:** `https://find-active-print-costa.trycloudflare.com`
  (verified HTTP 200). This URL changes if the tunnel restarts — if it does,
  update the four `.env` entries below and the redirect URIs in your OAuth apps.
- `~/Desktop/projects/sudoclip/.env` updated:
  - `SOCIAL_PUBLISHING_ACCESS=all`
  - `NEXT_PUBLIC_APP_URL` (both lines) = public URL
  - `SOCIAL_OAUTH_REDIRECT_BASE_URL` = public URL
  - `SOCIAL_PUBLIC_MEDIA_BASE_URL` = public URL
- Docker images rebuilt and recreated (`backend`, `worker`, `frontend`). All
  containers healthy; `/health`, `/health/db`, `/health/worker` = 200;
  `POST /tasks/ingest-rendered` confirmed in the live backend's OpenAPI.

## Still needed from you

### A. OAuth credentials (paste them back to me)
1. **Google Cloud Console** → APIs & Services → enable **YouTube Data API v3**
   → Credentials → **Create Credentials → OAuth client ID** → Application type
   **Web application**.
   - Authorized redirect URI (exact):
     `https://find-active-print-costa.trycloudflare.com/api/social/callback/youtube`
   - Copy the **Client ID** and **Client secret**.
2. **Meta Developers** → create an app → add the **Instagram API** product.
   - Redirect URI (exact):
     `https://find-active-print-costa.trycloudflare.com/api/social/callback/instagram`
   - Copy the **App ID** and **App secret**.
   - The Instagram account you connect must be a **Business** or **Creator**
     account (Reels publishing requirement).

I will put these into `.env` as `YOUTUBE_OAUTH_CLIENT_ID`,
`YOUTUBE_OAUTH_CLIENT_SECRET`, `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET` and
rebuild again. Do not paste them into chat if you would rather edit `.env`
yourself — either way works.

### B. Account + consent (you must do in a browser)
1. Open `https://find-active-print-costa.trycloudflare.com`, register and
   verify a SupoClip account, then log in. (DB currently has 0 users.)
2. **Settings → Social accounts → Connect YouTube** and **Connect Instagram**;
   approve the Google/Meta consent screens with the accounts you want to post to.
3. **Settings → API keys → Create key**. Export it as `SUPOCLIP_API_KEY`.
4. Verify: `GET /social/providers` shows `enabled: true`, both providers
   `configured: true`; `GET /social/connections` shows both with `status: active`.

### C. Start posting
```bash
cd ~/Desktop/projects/clip
source .venv/bin/activate
export SUPOCLIP_API_KEY=sk_...
python supervisor.py          # no --no-publish
```
Approve clips at `http://localhost:8765`. Approved clips are uploaded,
registered with `/tasks/ingest-rendered`, and posted to YouTube Shorts and
Instagram Reels with the post caption from `config.yaml`
(`title` + hashtags + `caption_template`). No burned-in subtitles, per your choice.

## Notes
- `POST /social/captions` (AI caption drafting) would additionally need an
  `ANTHROPIC_API_KEY` on the server; not required for the template caption.
- Quick-tunnel URLs are ephemeral. If it changes, restart with the same command
  and update `.env` + both OAuth apps' redirect URIs.
- Clipping other people's streams for revenue needs their permission / a sound
  fair-use basis and must respect each platform's terms.
