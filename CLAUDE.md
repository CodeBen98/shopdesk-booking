# ShopDesk booking service

FastAPI service the Vapi voice agent calls mid-call to book an appointment (`POST /book`). It writes to the shared Supabase database, creates the Google Calendar event, and sends SMS/email confirmations. The owner dashboard is the sibling repo `shopdesk-dashboard`, which also owns the database migrations.

# How we work

`main` deploys to production on Render (shopdesk-booking.onrender.com) on every merge, and live phone calls depend on it. Nothing reaches `main` without going through this flow.

## Branch → PR → CI → merge

1. Branch off an up-to-date `main`: `git switch main && git pull && git switch -c <type>/<short-name>`
   (types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`).
2. Make one logical change. Unrelated changes go on separate branches.
3. Before every push: `python -m compileall -q . && python -m pytest -q`
   Code must stay compatible with Python 3.9 (CI runs 3.9 and 3.12): use `Optional[...]`, not `X | None`.
4. Push and open a PR with `gh pr create`. The body says what changed, why, how it was tested, and any manual step (Render env var, database migration).
5. Merge only after both CI jobs (`test (3.9)`, `test (3.12)`) are green: `gh pr merge --squash --delete-branch`.
6. After merge, confirm the Render deploy is live and `GET /health` returns `{"database": "ok"}`.

`main` is protected: direct pushes are rejected and CI must pass.

## Commits

- One logical change per commit, conventional style (`fix: …`, `feat: …`), subject under ~72 characters.
- The body explains *why*. Write multi-line messages to a file and use `git commit -F <file>`.
- Never commit secrets; `.env` is gitignored and production values live in Render.

## Rules specific to this service

- It runs inside a live phone call: keep `/book` fast, never let a failing side effect (calendar, SMS, email) crash the request, and log failures.
- Every request is resolved to exactly one shop from the `shop_id` query parameter. Never add a fallback to a default or global shop.
- Times are parsed and stored in the shop's own `timezone` (`shop_timezone()`); never assume Eastern.
- Database schema changes are made as migrations in `shopdesk-dashboard`, applied before any code here that depends on them is merged.
- Bug fixes come with a test in `tests/`.
