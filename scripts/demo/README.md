# README demo recording

Regenerates `docs/demo.gif`. Everything is stubbed: isolated throwaway SQLite DB
(`scripts/demo/demo.db`), canned-response LLM, fake Gmail sender and fake web
fetcher - no API key, no network calls to company sites, nothing is ever sent.

Requires `playwright` (`pip install playwright && playwright install chromium`)
and `ffmpeg` on top of the app's own requirements.

```bash
# 1. start the seeded demo server (port 8123)
python scripts/demo/server.py

# 2. in another terminal: drive the UI and record a webm into scripts/demo/video/
python scripts/demo/record.py

# 3. convert to GIF
cd scripts/demo
ffmpeg -y -i video/*.webm \
  -vf "setpts=PTS/1.25,fps=9,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5" \
  -loop 0 ../../docs/demo.gif
```
