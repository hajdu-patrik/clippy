# Command line

## The `clippy` command

The `clippy` command works from any terminal (PowerShell or cmd) and from any folder:

```bat
clippy start          :: start the web app in the background and open it in the browser (Opera GX if installed)
clippy down           :: stop it completely (running renders too) and free the GPU   (--all: Ollama too)
clippy status         :: is it running, what is it doing, how much VRAM the model holds
clippy restart | open | logs
clippy restart --lan  :: phones and tablets on the same Wi-Fi too (after pairing)
clippy restart --force  :: now, even while jobs run (they start again after the restart)
clippy pair           :: one-time pairing link + QR code   (--reset: forget every device)
clippy clip <link>    :: make clips from the command line (clippy clip -h: every option)
clippy install        :: set up the command and tab completion (PATH, PowerShell profile, Clink)
```

Bare `clippy` shows the banner, the live status and the command list.
**Tab completion** works in PowerShell and in cmd through [Clink](https://chrisant996.github.io/clink/):
`clippy st<Tab>` → `start` / `status`, `clippy clip --layout <Tab>` → `auto face center blur stream`.
The completion scripts are generated from one command spec (`clippy/shell.py`), so they never drift from the
help text.

## Clipping: `clippy clip`

```bat
clippy clip "https://www.youtube.com/watch?v=..."                    :: 5 clips, everything automatic
clippy clip "https://..." -n 8 --lang hu --cta "Follow for part 2!"  :: Hungarian podcast, CTA at the end
clippy clip "https://..." --vocab "DESH, Young Fly"                  :: spell names right
clippy clip "https://twitch.tv/videos/..." --layout stream           :: streamer: facecam on top, game below
clippy clip gameplay.mp4 --layout blur --no-tighten --audio natural  :: gameplay / music
```

Finished clips go to the video library, `<CLIPPY_LIBRARY>\<video title>\` (`--out` picks another library):
- `clip_01_....mp4`: the finished 1080×1920 video, ready to post
- `clip_01_....jpg`: cover image with the hook text
- `clip_01_....txt`: title, hashtags, hook, timestamps, length, score and the reasoning

The plan (`clips.json`) and `render_report.json` are kept in `data\projects\<video title>\`.
To pick first and render later, after editing by hand:

```bat
clippy clip "https://..." --only-select                          :: pick only, no render
:: edit data\projects\...\clips.json: change hooks or titles, adjust times, delete clips
clippy clip --from-json "data\projects\...\clips.json"           :: render the edited plan
```

Every stage is cached, so re-runs are fast.

## Main options

| Option | What it does |
|---|---|
| `-n 5` | number of clips |
| `--min 20 --max 60` | clip length in seconds before the tight edit (20-45 s is a good start; TikTok Creator Rewards needs 60+) |
| `--wish "..."` | what kind of moments to pick, e.g. "the funniest parts" |
| `--lang hu` | spoken language (default: detected, the video's own language) |
| `--captions en` | language of the captions and the hook; a different one than the spoken language is translated |
| `--vocab "DESH, Young Fly"` | names and slang to spell right; the video title is added automatically |
| `--whisper large-v3-turbo` | faster transcription (default: `auto` = large-v3 on the GPU) |
| `--llm ollama/openai/none` | hook picker (also settable in `.env`) |
| `--layout auto/face/center/blur/stream` | `auto`: decides per shot (speaking face → tracking, otherwise blur) |
| `--facecam x,y,w,h` | facecam for the stream layout (estimated automatically otherwise) |
| `--no-tighten` | keep dead air and filler words |
| `--hook` / `--punch` | burn the suggested opening text / punch-in zoom on cuts (both off by default: a clip is the moment, framed, with captions) |
| `--audio natural` | loudness normalisation only, no speech EQ or compressor (music, gameplay) |
| `--isolate auto/on/off`, `--no-align`, `--duck 6` | voice isolation, word alignment, music ducking (dB) |
| `--encoder x264` | CPU x264 slow CRF 16 instead of NVENC |
| `--cta "..."`, `--highlight green`, `--no-upper`, `--font "Impact"` | CTA and caption style |
| `--cookies-from-browser firefox` | when YouTube asks for a sign-in |

## Where files go

| Folder | Holds | Removed |
|---|---|---|
| `<CLIPPY_LIBRARY>\<video title>\` | finished clips: mp4 + cover jpg + post text | never automatically |
| `data/cache/downloads`, `audio`, `transcripts` | the source video, voice/music stems, transcripts (shared by chats) | when the last chat using that video is deleted |
| `data/chats/<id>/` | every version of a chat's clips and its scratch files | when the chat is deleted |
| `data/work/` | render scratch of the command line | after every render |
| `data/projects/<video title>/` | `clips.json` plans and render reports of the command line | by hand |
| `data/web/` | chat database, server log | by hand |

Web clips are named `chat<id>_clip_<nn>_<title>.mp4` in the library, so two chats on the same video never collide.
