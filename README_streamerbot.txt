Streamer.bot Integration (Quickstart)
====================================

Global variables to create (Persisted = Yes)
- deathBotURL  -> Base URL of the bot (e.g., http://127.0.0.1:5000 or https://archi.plagrizr.com)
- authKey      -> Optional shared key; leave blank if auth disabled

Minimum actions to implement
- Twitch Subs     -> GET {deathBotURL}/twitch?user=<Token: User.DisplayName>&qty=<Token: Months or Count>
- Twitch Cheers   -> GET {deathBotURL}/cheer?user=<Token: User.DisplayName>&qty=<Token: Bits>
- TikTok Subs     -> GET {deathBotURL}/tiktok?user=<Token: User.DisplayName>&qty=<Token: Months or Count>
- TikTok Coins    -> GET {deathBotURL}/coins?user=<Token: User.DisplayName>&qty=<Token: Coins>
- Optional Custom -> GET {deathBotURL}/custom?user=<Token: User.DisplayName>&qty=<YourNumber>

Auth (if enabled in config.json):
- Add header  X-Auth-Key: <Global: authKey>
  OR append   &auth_key=<Global: authKey>  to the URL

Example Action: “DL – Twitch Cheers”
------------------------------------
1) Create Action: DL – Twitch Cheers
2) Trigger: Twitch -> On Cheer
3) Subaction: Execute Web Request
   - Method: GET
   - URL:
     {deathBotURL}/cheer?user=<Token: User.DisplayName>&qty=<Token: Bits>
   - If using auth:
     Add header  X-Auth-Key: <Global: authKey>
     (or append &auth_key=<Global: authKey> to the URL)

Notes
-----
- “Shared” vs “non-shared” chat is just which triggers you wire up; the endpoint stays the same.
- The server fires 1 DeathLink immediately and queues the rest based on your config (bits_per_death, coins_per_death, etc.).
- For gift subs, point your “Subs” action at:
  {deathBotURL}/twitch?user=<Token: User.DisplayName>&qty=<Token: Months or GiftCount>
  (Use the token that matches your Streamer.bot trigger setup.)
