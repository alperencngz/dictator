# Desirable high-leverage functionalities

**Preconditions:** everything in `IMPLEMENTATION_GUIDE.md` §7 is green — basic
press-talk-to-text works from Terminal *and* Dictate.app, and streaming HUD-preview mode
works. Do not start anything here before that.

Items are ordered by leverage-per-effort. Each has a gate; treat them like the guide's
phases — one at a time, user-confirmed. Constraints that always apply: TR+EN support
(never English-only models), on-device by default, batch mode must never regress.

---

## 1. Transcript history in the HUD (Effort: S, Leverage: L)

**What:** keep the last ~20 utterances; render them in a scrollable list in the HUD
(NSScrollView + NSTableView or a simple text view); click an entry → copy to clipboard.
Persist to `~/.dictate/history.jsonl` (timestamp, text, duration, mode).

**Why high leverage:** it *is* the requested "desktop app view for easy
logging/debugging" fully realized — a failed paste no longer loses the utterance; the
user can audit exactly what was heard vs. inserted; recovery is one click. Also the
substrate for item 4 ("scratch that").

**Gate:** dictate 3 utterances; all 3 visible; clicking the middle one puts exactly that
text on the clipboard; entries survive an app restart (loaded from the jsonl).

## 2. Custom vocabulary via `initial_prompt` (Effort: S, Leverage: L)

**What:** config key `vocabulary: ["Alperen", "Türkiye.ai", "Claude", …]` → joined into
faster-whisper's `initial_prompt` (and RealtimeSTT's equivalent) so proper nouns,
Turkish names, and project jargon transcribe correctly.

**Why:** the cheapest accuracy win Whisper offers; names are the #1 practical error class
in dictation, and the user's world (Turkish names + tech jargon) is exactly the hard case.

**Gate:** with an empty vocabulary, dictate a sentence containing two names it misspells;
add them to `vocabulary`; both come out right on retry.

## 3. Latency: Metal-accelerated backend benchmark (Effort: M, Leverage: XL if it pays off)

**What:** benchmark harness first, backend switch second. Generate a fixed corpus with
`say` (EN) + recorded TR samples, then time: current ctranslate2 CPU `small` vs
`mlx-whisper` (Apple-Silicon GPU) vs `whisper.cpp` (Core ML), for `small` and
`large-v3-turbo`. If a backend gives large-v3-turbo-quality at ≤ current small-latency
(or small at well under 1 s), add it behind `backend: faster-whisper | mlx` in config
with `Transcriber` as the interface — keep faster-whisper as the always-works fallback.

**Why:** release-to-text delay is *the* perceived quality of a dictation tool; GPU
inference could buy either ~2-3× speed or a much more accurate model at equal speed
(better Turkish). Benchmark-first prevents chasing a backend that doesn't actually win on
this machine.

**Gate:** a table of (backend × model → seconds, WER-ish spot check) committed to the
repo; the chosen default demonstrably faster or more accurate; batch mode still passes
the guide's P4 test.

## 4. Spoken punctuation & "scratch that" (Effort: M, Leverage: M-L)

**What:** post-transcription text rules: "new line"/"yeni satır" → `\n`, "comma"/"virgül"
→ `,` (config-toggleable, per-language table). Plus the killer command: "scratch that" /
"sil onu" deletes the previous utterance (emit that many backspaces in `type` mode, or
⌘Z if the last insert was one paste — history from item 1 knows the length).

**Why:** hands-free correction is what separates "toy" from "daily driver"; Whisper's
auto-punctuation is decent but unreliable for dictated commands and Turkish.

**Gate:** "hello new line world" produces two lines; a wrong utterance followed by
"scratch that" leaves the document as it was before it.

## 5. Language control in the menu bar (Effort: S, Leverage: M)

**What:** menu items Auto / English / Türkçe setting `language` live (autodetect →
forced code), persisted to config. Show the active choice with a checkmark; maybe suffix
the menu-bar icon (🎙️ᵀᴿ).

**Why:** autodetect misfires on short utterances (a 1-second Turkish phrase decoded as
English gibberish is a classic Whisper failure). One click to force the language during a
long Turkish session removes a whole error class with ~30 lines of code.

**Gate:** with "Türkçe" forced, a short ambiguous utterance transcribes in Turkish;
setting survives restart.

## 6. Double-tap PTT = toggle lock (Effort: S-M, Leverage: M)

**What:** double-tap Right ⌥ within ~350 ms → lock recording on (same as toggle mode);
single tap while locked → stop. Implement in `HotkeyManager` with a timestamp of the last
release; drive the existing `engine.toggle()`.

**Why:** long dictations (emails, docs) with a physically held key are fatiguing; this
adds hands-free mode without sacrificing the PTT interaction or binding a second key.

**Gate:** double-tap → icon stays 🔴 with hands off; speak two sentences; tap → both
transcribed and inserted. Normal hold-PTT still behaves identically.

## 7. Mic level indicator while recording (Effort: S-M, Leverage: M)

**What:** during recording, compute RMS per audio block (already flowing through
`BufferRecorder._callback`) and render a small level bar (or 3-5 dots) in the HUD via the
existing pending/applied sync.

**Why:** trust + diagnosis: "is it hearing me?" answered at a glance; instantly exposes
wrong-input-device and muted-mic states that today look identical to "transcription is
slow". (This diagnosed-blind failure already happened: the July 1 `final audio length: 0`.)

**Gate:** speaking makes the level move; muting the mic flatlines it while state stays
"recording".

## 8. App-aware insertion profiles (Effort: M, Leverage: M)

**What:** read the frontmost app's bundle id (`NSWorkspace.sharedWorkspace().frontmostApplication()`)
at insert time; per-app overrides in config, e.g. terminals (`com.apple.Terminal`,
`com.googlecode.iterm2`) → `insert_method: type` + no trailing space; chat apps → default
paste. Config: `app_profiles: {<bundle-id>: {insert_method: …, trailing_space: …}}`.

**Why:** paste is blocked or weird in exactly the apps a developer dictates into most;
today that means a global config flip. Per-app profiles make insertion "just work"
everywhere simultaneously.

**Gate:** dictation pastes in TextEdit but types in Terminal, without touching config
between them.

## 9. Optional LLM cleanup pass (Effort: M, Leverage: M-L, **off by default**)

**What:** post-process the final transcript (filler-word removal, punctuation,
capitalization, optional TR→EN translation) through either a local model (ollama) or the
Claude API (Haiku tier — cheap and instant for one sentence). Config:
`cleanup: off | local | claude`, plus a per-utterance bypass (e.g. toggle key held).
Show raw vs. cleaned in the HUD history (item 1).

**Why:** this is the feature that makes tools like Wispr Flow feel "magic" — dictation
becomes *writing*. Kept off by default because it breaks the "100% on-device, no cloud"
promise unless local, and adds latency.

**Gate:** "um so basically the meeting is uh moved to tuesday" → "The meeting is moved to
Tuesday." with `cleanup: claude`; identical raw behavior with `cleanup: off`; no network
calls made when off (assert via log).

## 10. Auto-pause media while recording (Effort: S, Leverage: S-M)

**What:** on record start, if audio is playing, send the system play/pause media key (or
AppleScript to Music/Spotify/Chrome); resume on stop. Config-gated.

**Why:** speaker bleed into the mic measurably degrades Whisper accuracy; the manual
pause-dictate-resume dance is friction on every single use for music listeners.

**Gate:** with music playing, hold PTT → music pauses → release → transcript is clean and
music resumes.

## 11. Streaming live-typing done right (Effort: L, Leverage: L, **risky — last**)

**What:** revisit true type-as-you-speak at the cursor (currently HUD-preview only, per
the guide's Phase 5). Requires solving modifier contamination: either a non-modifier PTT
key (F18 via Karabiner), waiting for modifier release before flushing the buffer, or
posting CGEvents with explicitly zeroed flags — *empirically validated* with the guide's
Phase 5.3 experiment. `LiveTyper` must never backspace while any modifier is held
(⌥⌫ = delete-word).

**Why last:** highest wow-factor, but it's the only feature that can actively *destroy*
user text when it goes wrong, and the HUD preview already delivers most of the perceived
liveness safely.

**Gate:** 3 long dictations into TextEdit with zero corrupted characters and zero lost
pre-existing text; a deliberate mid-sentence correction by the model reconciles correctly.

## 12. Wake word ("hey dictate") (Effort: M, Leverage: S)

**What:** RealtimeSTT supports openwakeword/porcupine backends; add
`wake_word: null | "hey dictate"` config that arms always-listening activation.

**Why ranked last:** PTT is already near-zero friction and strictly more private; a wake
word means an always-open mic (battery + trust cost). Only worth it if the user
explicitly asks for hands-busy scenarios (cooking, driving).

**Gate:** wake word starts recording hands-free; `wake_word: null` provably never opens
the mic stream outside PTT.

---

## Suggested batches

- **Batch A (one sitting):** 1 + 2 + 5 — history, vocabulary, language toggle. Small
  diffs, immediate daily-use payoff.
- **Batch B:** 3 (benchmark) → decide backend; then 7 while models download.
- **Batch C:** 4 + 6 — the "daily driver" interaction upgrades.
- **Batch D (opt-in polish):** 8, 9, 10, then 11/12 only on explicit request.
