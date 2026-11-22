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
| graphite | `#121113` `#18171a` `#211f23` `#322f35` | dark surfaces and borders |
| fog | `#f4f3f5` `#ffffff` `#dad6db` | light surfaces and borders |
| ok / danger | `#4fc58f` / `#ff6b6b` | status only |

## Type

- Display and wordmark: Segoe UI Variable Display, 700, tracking −2 %.
- Interface: Segoe UI Variable Text (system-ui fallback). Logs: Cascadia Mono.

## Components

The app (`clippy/web/static/style.css`) and the project page (`docs/assets/site.css`) share the colour tokens above and
the same scale: sizes `--fs-2xs` to `--fs-2xl` (11.5, 12.5, 14, 15.5, 18, 20, 24 px), radii `--radius-sm` 10,
`--radius-md` 14, `--radius` 16 and `--radius-pill`, spacing `--sp-1` to `--sp-5`.

- Buttons are pills (`.btn`, 44 px high, 36 px for `.sm` on a mouse, 44 px on touch): outlined by default, one filled
  `.primary` per view, `.danger` for removing.
- Surfaces (cards, rows, fields) are `--bg-elev` with a `--border` line and `--radius-md`.
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
