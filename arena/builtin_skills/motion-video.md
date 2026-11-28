---
name: motion-video
description: Make a short video as code - brief, shot plan (MOTION.md), one animated HTML composition, a draft render, then the final MP4 with render_video. Use for explainers, product teasers, title cards, social clips and animated demos.
tools: write_file, edit_file, read_file, render_video, ask_user, set_todos
---

# Motion video

A video here is an HTML page with animations, rendered frame by frame to MP4 by `render_video`. Nothing plays in real time, so timing is exact and the same file always renders the same video.

## 1. Brief

Settle these before writing anything; ask once with `ask_user` only for what you cannot sensibly choose:
- purpose and audience; the one idea the viewer should leave with
- length (default 10-20 s) and shape: 16:9 at 1280x720 (default), 9:16 at 720x1280, 1:1 at 1080x1080
- every line of text that must appear, and the brand colours, fonts and logo files in the folder

## 2. Shot plan: `video/MOTION.md`

Write the plan before the code. One row per shot:

| Shot | From - to (s) | On screen | Motion | Transition out | Sound |
|---|---|---|---|---|---|
| 1 Hook | 0.0 - 2.0 | Headline, 2 lines | words rise in, 0.08 s stagger | quick cut | hit on 0.4 |

Rules for the plan:
- Open with a hook in the first 2 s; end on the message or call to action, held at least 1.5 s.
- Each text block stays readable: at least 1 s, plus about 0.3 s per word.
- One idea per shot. Repeat a motion style instead of inventing a new one per shot.
- Note the design values (palette, type, spacing) at the top so the composition reuses them.

## 3. The composition: `video/<name>.html`

- A fixed stage: `html, body { margin: 0 }` and one root element exactly the render size, `overflow: hidden`.
- All timing in one system, so `render_video` can set any moment exactly:
  - CSS `@keyframes` with `animation-delay` per shot (simplest), or
  - the Web Animations API (`el.animate(...)`), or
  - one GSAP timeline (load GSAP from a file in the folder or a CDN), or
  - for canvas, WebGL or custom drawing: `window.__seek = (t) => { draw the frame for time t }`.
- Set `window.__duration = <seconds>` so the length lives in the file.
- No `setTimeout`/`setInterval` animation and no `Math.random()` per frame: they are not frame-exact. Seed any randomness.
- Fonts and images live in the project folder (or Google Fonts); the page can only read the project folder.
- Keep text inside a safe area (about 5% from each edge) and never smaller than about 28 px at 720p.

## 4. Render

1. Draft: `<tool name="render_video"><path>video/intro.html</path><fps>10</fps></tool>` to check timing cheaply.
2. Read the result line (frames, length). Fix what the plan and the render disagree on.
3. Final: the same call at `<fps>30</fps>` (60 only for fast motion), with `<audio>` if there is a soundtrack.

The MP4 lands in `.arena-artifacts/videos/`. Report its path, length and size.

## 5. Review before saying it is done

- The rendered length matches `window.__duration` and the plan.
- Every shot in `MOTION.md` exists in the composition at its planned time.
- No text is cut off, overlapping, or shown for less than its reading time.
- The last frame holds the final message.

## What this is not

It does not generate images, footage, music or voice. Use files the user provides (or the user's own generators) and place them in the folder first.
