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

## Motion

Ease-out `cubic-bezier(.2,.8,.2,1)` for movement and a light spring `cubic-bezier(.34,1.4,.64,1)` for pops.
Durations stay between 150 and 450 ms. Everything respects `prefers-reduced-motion`.

## Voice

Short, direct, friendly. Say what is happening and what comes next ("Transcript 42% · ≈ 6 min left"), never
blame the user, and keep every label under a few words.
