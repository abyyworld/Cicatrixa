# YC Demo Video — 60s voiceover script

Record your voice over `demo/yc_demo.mp4` (raw product footage, already cut to
these beats). Plain delivery, no music — YC explicitly prefers unpolished and
real. One take is fine if it's honest.

| time | on screen (real footage) | say this |
|------|--------------------------|----------|
| 0:00–0:08 | repo picker: two repos checked, name typed, Deploy clicked | "This is Cicatrixa. I'm deploying two GitHub repos — a frontend and a backend. Neither has a Dockerfile. Watch." |
| 0:08–0:22 | live deploy log: AI writes Dockerfile, detects port, smoke test, verdict | "The AI reads the code, writes the Dockerfile itself, detects the port, and won't call it live until it's verified the app actually serves. That log is real and streaming." |
| 0:22–0:30 | both services LIVE, subdomains, /api bridge line | "Both apps get their own subdomain with HTTPS, wired together automatically — no CORS config, no env files." |
| 0:30–0:40 | crash take: watchdog restart line appears in the log | "Now the important part. I just killed the backend container. Nobody's watching — and the watchdog notices, restarts it, and it's healthy again. That's real." |
| 0:40–0:55 | chat medic: typed bug report, diagnosis, diff, Apply, pushed sha | "Here's a real production bug. I describe it in chat. The AI reads the live logs, finds the ZeroDivisionError, writes the fix, and — this is the part I love — it verifies the patch builds before offering it. One click: real commit, pushed to GitHub, redeployed. The endpoint went from 500 to 200." |
| 0:55–1:00 | pricing card / end | "It's $2.99 a month — hosting where the apps fix themselves. cicatrixa.com." |

Recording tips:
- Do it in one sitting; if you flub a line, pause 2s and repeat the sentence —
  easy to cut.
- Record audio with `ffmpeg -f pulse -i default yc_vo.wav` while watching the
  video, or record on your phone and I'll sync it.
- When done, drop the audio file in `demo/` and I'll mux it:
  `ffmpeg -i yc_demo.mp4 -i yc_vo.wav -c:v copy -map 0:v -map 1:a -shortest yc_demo_final.mp4`
