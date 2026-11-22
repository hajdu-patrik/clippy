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
| paper / ink | `#f3ede2` `#fbf8f1` `#eae2d3` / `#151313` | light theme page, surfaces, lines |
| night | `#131110` `#1d1a18` `#26221f` / `#a1988b` | dark theme page, surfaces, lines |
| ok / danger | `#7fd7a8` / `#ff9d8f` (dark), `#1b6b45` / `#a3210f` (light) | status only |

## Type

- Titles and the wordmark: Archivo Black, in capitals, one weight (`--font-display`).
- Interface: Space Grotesk (`--font`). Both ship in `clippy/web/static/fonts/` under the SIL Open Font License
  (`OFL.txt`), so nothing is fetched from the network. Logs: Cascadia Mono.

## Components

The app follows the poster style of makoncert.hu with Clippy's wine as the accent. Its stylesheet is
`clippy/web/static/style.css`, which imports one file per area from `css/`: `base.css` (tokens, fonts, controls),
`shell.css` (sidebar, page frame), `chat.css`, `pages.css`, `overlays.css` (dialogs, toast). Square corners, 2 px ink
lines (`--line`), hard offset shadows (`--shadow-press` 4 px, `--shadow-card` 5 px) and buttons that sink into their
shadow when pressed; sizes `--fs-xs` to `--fs-2xl` (12.8, 14.4, 16, 18, 24, 30 px), spacing `--sp-1` to `--sp-6`.

- Buttons (`.btn`, 48 px high, 44 px for `.sm`): square, 2 px ink border and an offset shadow; one filled `.primary`
  per view, `.ghost` without a border, `.danger` for removing.
- Surfaces (cards, rows, fields) are `--surface` with a 2 px `--line`; dense lists are one card with rows divided by
  `--line-soft`.
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
