# README demo recording

Regenerates `docs/demo.gif`. Everything is stubbed: isolated throwaway SQLite DB
(`scripts/demo/demo.db`), canned-response LLM, fake Gmail sender, fake company
website and a fake technopark directory ("Demo Teknokent") - no API key, no
network calls to real sites, nothing is ever sent. All people, companies and
addresses are fictional (`*.example`).

Requires `playwright` (`pip install playwright && playwright install chromium`)
and `ffmpeg` on top of the app's own requirements.

```bash
# 1. start the seeded demo server (port 8123) - re-seeds a fresh DB every start
python scripts/demo/server.py

# 2. in another terminal: drive the UI and record a webm into scripts/demo/video/
python scripts/demo/record.py

# 3. convert to GIF (1.25x speed, 7 fps, 960px wide - stays under 10 MB)
cd scripts/demo
ffmpeg -y -i video/*.webm \
  -vf "setpts=PTS/1.25,fps=7,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=80:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
  -loop 0 ../../docs/demo.gif
```

The fake LLM picks its answer by the caller's system prompt (see `DemoLLM` in
`server.py`); if a prompt's opening sentence changes, update the matching key
there or that step falls back to an empty answer.
