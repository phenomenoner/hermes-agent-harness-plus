# Docs & Website Update Guide

Use this guide whenever you touch `README.md`, anything in `docs/`, or the
GitHub Pages site (`docs/index.html`). It exists so that any future maintainer —
human or agent — can extend the public surfaces without breaking the design
language or letting the facts drift.

The short version: **cute on the outside, careful on the inside.** Marketing
energy comes from copy, color, and emoji. The facts stay boring, verifiable, and
identical across every surface.

---

## 1. Brand system

### Identity

- Name: **Hermes Agent Harness Plus** (never abbreviate to "HAHP" in public copy;
  "Harness Plus" is the approved short form).
- Brand mark: `🧸✨` (used in the README title and as the emotional sign-off pair).
- Core metaphor: **"a memory, a map, and a health plan"** — reuse this phrase,
  don't invent competing metaphors.
- The three habits (canonical order): **Keep the map · Find it again · Share
  cleanly.**
- Sign-off line: *"May your agent never lose a clue again. 🧸✨"*

### Voice

- Cheerful, warm, a little playful — but every sentence under the sugar is
  technically precise.
- Benefit-led headlines ("Give your agent a memory"), short sentences, active
  voice.
- Playful naming for problems is encouraged ("the scrollback graveyard 🪦");
  playful naming for features is not — features keep their real names.
- **Honest marketing only.** No invented user counts, no fake testimonials, no
  fabricated benchmarks, no urgency tricks. Verifiable component counts,
  local-state defaults, "0 changes to Hermes core", and "MIT" are acceptable;
  provider-backed components must say so explicitly.
- Public surfaces are English. Keep commands copy-pasteable and tested.

### Emoji lexicon

Use these consistently; one emoji per heading, at the start:

| Concept | Emoji | Concept | Emoji |
|---|---|---|---|
| Task Canvas / map | 🗺️ | Recall / search | 🔎 |
| MCP sidecar | 🛰️ | Autopilot archive | 🗃️ |
| Watchdog / health | 🩺 | Skills / guides | 📚 |
| Checklists / ops | ✅ | Install / package | 📦 |
| Components / catalog | 🧩 | Website | 🌈 |
| Local state | 🏠 | Lightweight | 🪶 |
| Failure posture | 🛟 | Evidence | 🧾 |
| Share / gift | 🎁 | Principles / compass | 🧭 |

---

## 2. Visual system (website)

`docs/index.html` is a **single self-contained file**: inline CSS, inline JS, no
external fonts, scripts, images, or analytics. Keep it that way.

### Color tokens

Defined in `:root`; reuse tokens instead of inventing new colors:

| Token | Hex | Role |
|---|---|---|
| `--cream` | `#fff8ea` | page base |
| `--ink` | `#202033` | text |
| `--muted` | `#62637a` | secondary text |
| `--pink` | `#ff7ab6` | accent 1 |
| `--peach` | `#ffb36b` | accent 2 |
| `--mint` | `#68d8b2` | accent 3 |
| `--sky` | `#6bbcff` | accent 4 |
| `--violet` | `#8e7dff` | accent 5 |

Signature gradients:

- Headline rainbow: `#ff6aa9 → #ff9f45 → #31c7a2 → #6b7cff` (class `.rainbow`).
- Primary button: `#f0529c → #6f63ea`.
- Code blocks: dark panel `#23233b`, comment `#9aa0c3`, command `#7ee7c0`,
  argument `#ffa9d2`.

### Shape & texture

- Rounded everything: cards 24–34px radius, buttons/pills `999px`.
- Glassmorphism: translucent white + `backdrop-filter: blur(...)`.
- Soft violet shadows (`rgba(87, 77, 150, 0.16)` family), never hard black.
- Pastel radial-gradient background with two fixed blurred blobs.
- Font stack: `ui-rounded, "SF Pro Rounded", "Segoe UI", system-ui, ...` —
  rounded, friendly, zero downloads. Headings use tight letter-spacing
  (−0.03em to −0.075em) and `clamp()` sizing.

### Motion & accessibility (non-negotiable)

- Animations are decorative only: `floaty` (bobbing emoji), `drift` (blobs),
  hover lift on cards/buttons.
- `prefers-reduced-motion: reduce` must disable all animation. Never remove
  this block.
- Keep `:focus-visible` outlines, `aria-label`s on sections, semantic headings
  (one `h1`, then `h2` per section), and readable contrast (`--ink` on light
  panels; `--muted` only for secondary text).
- Responsive: three breakpoints (~900px grid collapse, ~860px nav/hero stack,
  ~520px small-screen padding). Test each when layout changes.

---

## 3. Surface anatomy

### `docs/index.html` — section order is part of the design

1. `nav` — brand + anchors (`#features`, `#quickstart`, `#faq`) + ⭐ GitHub link.
2. `.hero` — pill badge, `h1` with `.rainbow` span, lead, 3 CTAs, showcase panel
   (mac-dots window with three `.note` rows) with floating emoji.
3. `.statbar` — exactly 4 honest facts. If the active component count changes,
   update the `7+` tile; archived source does not count as an active component.
4. Problem section — kicker `🪦`, three `.ouch` pain cards.
5. `#features` — seven `.card`s, one per active component; emoji tile + `h3` + ≤2
   sentences. New component ⇒ new card (and consider the grid still balancing
   at 3 columns).
6. `#quickstart` — three `.step`s with dark `.code` blocks. Each block carries
   the full command in a `data-copy` attribute for the copy button; the visible
   version may use `\` line continuations for narrow columns.
7. Promises ribbon — 5 pledge pills. Only add a pledge the code actually keeps.
8. `#faq` — native `<details>` accordions, question in `summary`, honest answer.
9. `.banner` — emotional close + 3 CTAs.
10. `footer` — attribution to Hermes Agent + NOTICE / CONTRIBUTING / SECURITY
    links.

**Link rule:** from the website, never link to raw `.md` paths (GitHub Pages
serves them as plain text). Always use the GitHub blob URL, e.g.
`https://github.com/phenomenoner/hermes-agent-harness-plus/blob/main/docs/install.md`.
In-page anchors (`#features`) and full URLs only.

### `README.md` — section order

1. Centered hero: `# 🧸✨ Hermes Agent Harness Plus`, tagline `###`, bold
   one-sentence pitch.
2. Badge row — real endpoints only. CI badge points at the actual workflow;
   decorative badges use the palette hexes (`ff7ab6`, `6bbcff`, `8e7dff`,
   `68d8b2`, `ffb36b`). A badge that can break (CI) must link to its source.
3. Quick-links row: 🌈 website · 📦 install · 🧩 catalog · 🐛 issues.
4. 😵‍💫 The problem → 💡 The fix (habits table).
5. 🎁 What's in the box — directory tree + component "superpower" table.
6. ⚡ Quick start — the same three steps as the website, then the MCP YAML
   snippet.
7. 🧭 Design principles — the five promises.
8. 📚 Learn more — table of doc links.
9. 🤝 Contributing → 📜 License → centered sign-off.

### `docs/*.md` and deeper pages

- `install.md` / `catalog.md` / `release-manifest.md` / `technical/*` stay in
  plain, calm documentation voice — emoji-light, precision-first. The marketing
  layer is README + website only; depth lives here (see
  `public-release-checklist.md` §5: story up front, details in
  `docs/technical/`).

---

## 4. Content sync map

When you change one thing, these are the surfaces that must stay in agreement.
"Same fact, three places" is the design — drift is a bug:

| You changed… | Also update |
|---|---|
| Added / renamed / removed a component | README box tree + superpower table · `docs/catalog.md` · website feature card · statbar count · `docs/release-manifest.md` |
| Install or quick-start commands | `docs/install.md` · README ⚡ Quick start · website `#quickstart` (visible text **and** `data-copy`) — all three must run the same commands |
| MCP tool names or config keys | `docs/install.md` · `docs/catalog.md` · README superpower table · website MCP card |
| New doc page | README 📚 Learn more table · website links where relevant |
| New third-party dependency | `NOTICE` (source, version, license) — see `public-release-checklist.md` §4 |
| Tested-against versions (Hermes, Qdrant, mcp, fastembed) | `NOTICE` |
| New ops-rule or skill | `docs/catalog.md` · README box tree stays directory-level (no change) |
| Brand claim / principle / tagline | This guide's copy bank + every surface that repeats it |
| Repo About box (description / homepage / topics) | `gh repo edit phenomenoner/hermes-agent-harness-plus --description ... --homepage ... --add-topic ...` |

---

## 5. Update workflow

1. **Edit** with the tokens and anatomy above. Facts must match the code as it
   exists in the same commit.
2. **Preview locally.** Open `docs/index.html` in a browser and check: hero,
   feature cards, quick-start blocks (and their copy buttons), FAQ toggles, a
   ~800px-wide window, and reduced-motion if animations changed. Preview
   README in a Markdown renderer; confirm badges resolve.
3. **Check links.** Relative links exist in-repo; website links use blob URLs;
   no raw `.md` hrefs on the site.
4. **Run tests** if any code moved: `python -m pytest -q`.
5. **Run the release checklist** (`ops-rules/public-release-checklist.md`)
   before anything new becomes public.
6. **Commit** in repo style: `docs:` / `feat:` / `fix:` prefix + imperative
   subject ≤ 72 chars (see `git log` for the house pattern).
7. **Push to `main`.** GitHub Pages auto-deploys from `main` `/docs` — there is
   no separate deploy step and no build config to maintain.
8. **Verify live** with a cache-busted fetch, e.g.
   `curl -s "https://phenomenoner.github.io/hermes-agent-harness-plus/?cb=$RANDOM" | grep "health plan"`
   (deploys typically land in under two minutes).

### Verification checklist

- [ ] Same commands in install.md, README, and website (visible + `data-copy`).
- [ ] Statbar numbers and component counts still true.
- [ ] No external assets crept into `docs/index.html`.
- [ ] `prefers-reduced-motion` block intact; focus outlines intact.
- [ ] Website `.md` links are blob URLs, not raw paths.
- [ ] Badges render; CI badge points at the real workflow.
- [ ] No invented numbers, testimonials, or claims anywhere.
- [ ] Live site shows the new content after push.

---

## 6. Copy bank (approved phrases)

Reuse before rewriting; edit here first if the brand language evolves.

- "Give your agent a memory, a map, and a health plan."
- "A cheerful companion toolbox for Hermes Agent."
- "Keep the receipts."
- "Your agent works hard. Its best clues deserve better than line 4,000."
- "The scrollback graveyard."
- "Seven little helpers, one happy harness." (update the number with the count)
- "Cute on the outside, careful on the inside."
- "Take one piece or take them all — Hermes Agent itself is never modified."
- "Local state by default; provider-backed calls stay explicit."
- "Evidence or it didn't happen." / "'Trust me' is not a citation."
- "Pick the pieces you need, leave the rest."
- "May your agent never lose a clue again. 🧸✨"

CTA labels: "🚀 Install in minutes" · "🧩 See what's inside" · "📖 Read the
README" · "⭐ Star on GitHub" · "📦 Install guide" · "🧩 Component catalog".

---

## 7. Don'ts

- Don't add external fonts, CDNs, trackers, or analytics to the website.
- Don't fabricate numbers, users, quotes, or logos. Ever.
- Don't remove the reduced-motion or focus-visible accessibility blocks.
- Don't switch the palette, font stack, or border-radius language casually — a
  restyle is a deliberate decision, recorded by updating §2 of this guide first.
- Don't let README, install.md, and the website give different commands.
- Don't link the website to raw `.md` files.
- Don't put marketing voice into `docs/technical/*` or checklist files.
- Don't publish anything that fails `ops-rules/public-release-checklist.md`.
