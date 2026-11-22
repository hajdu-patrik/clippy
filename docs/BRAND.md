# Clippy brand

## Mark

`clippy/web/static/brand/mark.svg`: a wine-red rounded tile with a white **C** whose two ends are cut at an angle (the cut), holding a
play triangle (the clip). It is also the favicon, the chat avatar and the sidebar logo (inlined as the `#mark`
symbol in `index.html`).

- `logo.svg`: mark + wordmark for dark backgrounds
- `logo-light.svg`: mark + wordmark for light backgrounds
- Clear space: at least a quarter of the tile width on every side. Smallest size: 16 px.
- Do not recolour, rotate, outline or add effects to the mark; keep it on the tile.

## Colour

| Token | Hex | Use |
|---|---|---|
| wine-900 | `#3d0a17` | deepest shade |
| wine-800 | `#5c1023` | tile gradient end (dark) |
| wine-700 | `#7d1631` | accent on light theme |
| wine-600 | `#9e1d3e` | **primary accent** (buttons, badges) |
| wine-500 | `#bf2c52` | hover, tile gradient start |
| wine-400 | `#d9577a` | progress fills, CLI accent |
| wine-300 | `#eb8da6` | accent text on dark |
| wine-200 | `#f6c3d1` | tints |
| graphite | `#121113` `#1b1a1d` `#262428` / `#5c5762` `#322f35` | dark theme page, surfaces, lines |
| fog | `#f4f3f5` `#ffffff` `#ece9ed` / `#1d1b1f` `#dad6db` | light theme page, surfaces, lines |
| ok / danger | `#4fc58f` / `#ff6b6b` (dark), `#1b7a50` / `#c0293a` (light) | status only |

## Type

- One family, the platform's interface font (Segoe UI Variable on Windows, SF Pro on macOS, system-ui elsewhere), so
  nothing is fetched from the network. Titles 600 weight in sentence case; never all capitals. Logs: Cascadia Mono.

## Components

The app follows the form language the big AI apps share (Claude, ChatGPT, Gemini) in Clippy's black and wine colours:
neutral graphite (dark) or white and fog (light) surfaces, wine only for the filled button, the selection and focus;
soft radii (`--r-sm` 8, `--r-md` 12, `--r-lg` 16, pills for the composer, chips and buttons), 1 px hairlines
(`--line`, `--line-strong`), flat surfaces with `--shadow-float` only on floating layers. The stylesheet
`clippy/web/static/style.css` imports `css/base.css` (tokens, controls), `shell.css` (sidebar, page frame),
`chat.css`, `pages.css` and `overlays.css` (dialogs, toast).

- Buttons (`.btn`, 40 px high, 32 px for `.sm`, 44 px on touch): pills with a hairline border; one filled wine
  `.primary` per view, `.ghost` without a border, `.danger` for removing.
- Surfaces (cards, rows, fields) are `--surface` with a 1 px `--line`; lists are one rounded group with hairline rows.
- Every screen is a page in the main area (Projects, Calendar, Results, Daily picks, Watched channels, Campaigns, Brand
  kit, Settings): a title, one line on what it is for, at most one filled button, then one column of content. Only the
  quick Post and Copy steps are dialogs; on a phone (up to 560 px) the Post dialog fills the screen with its buttons
  pinned at the bottom, and every control is at least 44 px.

## Motion

Ease-out `cubic-bezier(.2,.8,.2,1)` for movement and a light spring `cubic-bezier(.34,1.4,.64,1)` for pops.
Durations stay between 150 and 450 ms. Everything respects `prefers-reduced-motion`.

## Voice

Short, direct, friendly. Say what is happening and what comes next ("Transcript 42% · ≈ 6 min left"), never
blame the user, and keep every label under a few words.
