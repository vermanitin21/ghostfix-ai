# Presentation layer v1: judge-facing repair experience

A single page that shows one recorded GhostFix repair: bug reproduced, Candidate 1
executed and rejected, execution evidence fed back, Candidate 2 verified, and the
Proof of Fix. It renders recorded evidence only. It never runs repairs, models or
Docker.

## Start

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app
```

Open <http://127.0.0.1:8000/>. Everything is served by the existing FastAPI app on
one port. No Node, build step or extra dependency is needed. Fonts are local
system fonts, and the page makes no external requests.

## Architecture

```
recorded RepairRun / ProofOfFix (artifacts/hackathon-demo, gitignored)
   -> scripts/build_replay_bundle.py     select + cross-check, fail closed
   -> app/presentation/data/canonical-self-correction.json   (tracked, sanitized)
   -> app/presentation/bundle.py         strict schema + unsafe-content scan
   -> app/presentation/view.py           adapter: evidence -> view model
   -> GET /api/replay                    view model only
   -> app/presentation/static/           HTML/CSS/vanilla JS renderer
```

- **Outcomes come from structured fields only.** VERIFIED requires the
  ProofOfFix status `VERIFIED`, `verified=true` and no evidence issues. PASS and
  FAIL come from the recorded `passed` and exit-code fields.
- **Display-only text parsing.** Test counts ("4/4 passed") and
  observed/expected values come from pytest's output. They are shown only when
  they agree with the structured result, and are hidden otherwise.
- **Locked benchmark values.** v1 1/7, v2 3/7 and v3 4/7 are constants in the
  adapter, never recomputed. The demo is not part of them.
- **Static renderer.** The browser receives the view model and nothing else. The
  JS builds the DOM with `textContent`; there is no `innerHTML` or inline script,
  and a strict Content-Security-Policy applies.
- **No route to repair execution.** Importing `app.main` cannot load the
  orchestrator, repair loop, verifier, LLM client, Docker runner or evaluation
  code. A test checks this.

## Evidence source

The canonical artifacts are gitignored, so a fresh checkout cannot rely on them.
`scripts/build_replay_bundle.py` derives a small tracked bundle from them. It is
read-only and refuses to overwrite an existing bundle without `--replace`. The
bundle contains:

- the recorded, schema-valid ProofOfFix, unchanged;
- per-attempt provenance from the RepairRun (proposal model label, whether
  correction evidence was supplied, recovery/retries, the diff applied to the
  original source);
- the correction record (model, request SHA-256, evidence fields), cross-checked
  against the model audit;
- the SHA-256 of each source file and the canonical run ID.

It contains no prompts, raw model responses, credentials or host paths. The
builder fails closed if run IDs, attempt counts, proof status or the correction
hash disagree, if a needed field was withheld, or if unsafe content appears.

When the local canonical artifacts are present, the server compares their bytes
with the bundle's recorded hashes. The footer then shows "hash-matched",
"DOES NOT MATCH" or "not present". A missing, malformed, schema-invalid or unsafe
bundle returns `503 evidence_unavailable`, and the page shows "Recorded evidence
unavailable" instead of any result.

## Replay: recorded, never live

**Replay demo** reveals the already-rendered recorded evidence in nine steps, about
19 seconds in total. Controls are Pause/Resume and Reset. The status pill reads
`REPLAYING RECORDED EVIDENCE - STEP n/9`, then `REPLAY COMPLETE`, then
`RECORDED VERIFIED RUN`. Replay only toggles visibility; it never changes content
and makes no network calls beyond the initial `/api/replay` fetch. There is no live
mode, so nothing is ever labelled live.

## Security boundary

- Only fields chosen by the adapter reach the browser. Arbitrary JSON is never
  dumped.
- The bundle is scanned at build time and at load time for Windows, UNC and POSIX
  host paths, verification-workspace names, credential patterns and the
  configured API key.
- Error responses are generic: no paths, exception text or partial evidence.
- CSP is `default-src 'self'`, with no inline scripts or styles and no framing.
  `nosniff` and `no-referrer` are also set.

## Accessibility and layout

- Status is never shown by color alone: each status has an icon and a text label
  (e.g. ✕ FAILED, ✓ PASSED, ◆ model, ▣ evidence).
- Buttons and disclosure summaries have visible focus outlines.
- Code text is at least 13 px.
- Programming ligatures are disabled, so code shows exact characters (`->`, not
  an arrow glyph).
- `prefers-reduced-motion` disables the transitions.
- Tuned for 1920×1080 and checked at 1366×768.

## Limitations

- One recorded run on one small fixture. Candidate 1 was deliberately injected
  by the demo script. A passing test suite is not formal correctness.
- Test counts depend on pytest's summary line being in the bounded output. When
  it isn't, the page says "count unavailable".
- The page proves that evidence was *supplied* to the correction call. It cannot
  show how the model used that evidence internally.
- To regenerate the bundle you need the local canonical artifacts:
  `python scripts/build_replay_bundle.py --replace`.
