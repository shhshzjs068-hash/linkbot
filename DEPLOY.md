# Deploy on Railway

1. **Make the session string (once, on your PC)**
   `pip install telethon && python gen_session.py` -> log in with the spare account -> copy the string.

2. **Push to GitHub** (private repo). `.gitignore` already keeps secrets/DB out.

3. **Railway -> New Project -> Deploy from GitHub repo.** It builds automatically; `railway.json` sets the start command.

4. **Variables tab** (Service -> Variables):
   - `BOT_TOKEN`, `API_ID`, `API_HASH`
   - `SESSION_STRING` (from step 1)
   - `ADMIN_IDS` (your Telegram user ID; leave empty = anyone can use the bot)

5. **Add a Volume** (Service -> right-click canvas / `+ New` -> Volume), mount path `/data`.
   The bot picks it up through `RAILWAY_VOLUME_MOUNT_PATH`, so cache, settings and stats survive redeploys.
   Without a volume everything still works, but the cache/settings reset on each deploy.

6. **Deploy.** Logs should show `Checker account: ...` then polling starts. Send the bot a link.

## Rules to avoid breaking things
- Never run the bot locally with the same `SESSION_STRING` while Railway is running. Two places using one session can get it revoked by Telegram.
- Keep it to **one replica**.
- If you see `SESSION_STRING is invalid or expired`, run `gen_session.py` again and update the variable.
