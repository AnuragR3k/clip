# Deploy the clipper to a VPS (step by step)

This is the "no downtime while agents edit" setup: code lives on GitHub, a push
deploys it to a VPS, and saved clips live on persistent volumes.

Vercel / Cloudflare Workers will **not** work here — this is a 24/7 process with
persistent disk, Twitch IRC sockets and ffmpeg. Use a normal VPS.

---

## 1. Get a VPS
Any provider is fine (Hetzner CX21/CX31, DigitalOcean droplet, etc.).
- **Ubuntu 24.04**, at least **2 vCPU / 4 GB RAM** (ffmpeg encoding is heavy).
- Note the server **IP**, and the SSH username (`root` or `ubuntu`).
- Open ports **22** (SSH) and **8765** (review UI) in the firewall.

## 2. Create a deploy SSH key (on this machine)
```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/clip_deploy
# press Enter twice (no passphrase)
cat ~/.ssh/clip_deploy.pub
```
Copy the printed **public** key. On the VPS, paste it into:
```bash
mkdir -p ~/.ssh && echo "PASTE_PUBLIC_KEY_HERE" >> ~/.ssh/authorized_keys
chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys
```
Test from this machine:
```bash
ssh -i ~/.ssh/clip_deploy USER@VPS_IP "echo SSH_OK"
```

## 3. Prepare the VPS
```bash
ssh -i ~/.ssh/clip_deploy USER@VPS_IP
sudo apt update
sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER
# log out and back in so the docker group takes effect
git clone https://github.com/AnuragR3k/clip.git ~/clip
cd ~/clip && chmod +x deploy.sh
docker compose build clipper
REVIEW_PORT=8765 EXTRA_ARGS=--no-publish docker compose up -d clipper
docker compose logs -f clipper
```
Check the UI at `http://VPS_IP:8765`.

## 4. Add GitHub secrets
In the repo → **Settings → Secrets and variables → Actions → New repository secret**,
add these four:

| Secret | Value |
|---|---|
| `VPS_HOST` | the VPS IP address |
| `VPS_USER` | SSH username (`root`/`ubuntu`) |
| `VPS_SSH_KEY` | full contents of `~/.ssh/clip_deploy` (the **private** key) |
| `VPS_APP_DIR` | e.g. `/home/ubuntu/clip` |

Never commit the private key to the repo.

## 5. Deploy by pushing
Any push to `main` runs `.github/workflows/deploy.yml`: it SSHes in, does
`git pull --ff-only`, then `bash deploy.sh` (build + `docker compose up -d`).
Watch it in the **Actions** tab. Saved clips are safe because `data/` is a
persistent bind mount.

Do not edit files directly on the VPS — the workflow uses `--ff-only`, so
manual changes would block future pulls.

## 6. Enable publishing (later)
On the VPS, create `~/clip/.env` (gitignored):
```env
EXTRA_ARGS=
SUPOCLIP_API_KEY=sk_...
```
Then `cd ~/clip && docker compose up -d`. The container must be able to reach
the **SupoClip backend** (not just its frontend tunnel) — expose it securely or
run SupoClip on the same VPS.

## 7. Day-to-day
- Edit code locally, commit, push → auto-deploy.
- `ssh -i ~/.ssh/clip_deploy USER@VPS_IP "cd ~/clip && docker compose logs -f clipper"`
- Review clips at `http://VPS_IP:8765`.
- Restart manually: `cd ~/clip && docker compose restart clipper`.
