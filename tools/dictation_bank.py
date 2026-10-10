"""Builds Trivia Murder Party 2's host voice bank (TMP2Host.bank) with the mod's Dictation letters.

The Dictation minigame does not use the text in TDDictation.jet. It plays the FMOD event named by
`eventName` (HOST/DictationVO/<slot>, 18 slots) and shows and grades that event's timeline markers.
So every letter in TDDictation.jet is voiced line by line, joined into one clip and written into
its slot with one marker per line. Slots that no letter uses are left empty, since the game only
plays the letters listed in TDDictation.jet. To share the mod, ship TMP2Host.bank (it goes in
games/TriviaDeath2) together with TDDictation.jet (it goes in the content folder).

  python tools/dictation_bank.py build                 writes TMP2Host.bank in the repo root
  python tools/dictation_bank.py check                 plays each letter from the built bank and lists its markers
  python tools/dictation_bank.py install [--dev-console]
      Copies TMP2Host.bank to games/TriviaDeath2 and TDDictation.jet to its content folder, after backing up
      the game's own files once (*.vanilla). --dev-console also turns on the game's developer console
      (open it with the ` key; runTest("Dictation") locks the killing floor to Dictation).
  python tools/dictation_bank.py restore               puts the backed-up vanilla files back

Line audio comes from tools/dictation_audio/<id>/<n>.ogg (line n of letter <id>, counting from 1),
made by tts_import.py generate/import. Lines without a file, or whose text changed since they were
voiced, get a Windows TTS placeholder voice. Needs ffmpeg and the game's FMOD DLLs.
Set JACKBOX_PP6 if the Party Pack 6 is not in the default Steam folder.
"""
import array, json, os, shutil, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fmodbank import Bank, FSB5, FSB_PCM16, FSB_VORBIS, TIMELINE_RATE, pcm16_sample  # noqa: E402
from tts_import import DONE_FILE, speakable  # noqa: E402
import fmod_runtime  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TD2 = fmod_runtime.TD2
AUDIO_DIR = os.path.join(ROOT, "tools", "dictation_audio")
OUT_BANK = os.path.join(ROOT, "TMP2Host.bank")
SLOT_PREFIX = "HOST/DictationVO/"

LEAD_IN = 0.25        # seconds of silence before the first line
GAP = 0.9             # pause between lines, so players can keep typing
TAIL = 0.6            # silence after the last line
MARKER_EARLY = 0.15   # show each line slightly before it is spoken
SILENCE = 300         # PCM16 level treated as silence when trimming each line


def letters():
    return json.load(open(os.path.join(ROOT, "TDDictation.jet"), encoding="utf-8"))["content"]


def vanilla_bank():
    for path in (os.path.join(TD2, "TMP2Host.bank.vanilla"), os.path.join(TD2, "TMP2Host.bank")):
        if os.path.exists(path):
            bank = Bank(path)
            if bank.fsbs[0].mode == FSB_VORBIS:
                return bank
    sys.exit("No vanilla TMP2Host.bank found (the game's copy is already modded and has no .vanilla backup).\n"
             "Verify the game files in Steam to get it back.")


def to_pcm48(path):
    return subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(TIMELINE_RATE), "-"],
                          check=True, capture_output=True).stdout


def placeholder_voice(lines, folder):
    """Windows TTS for each line -> [wav path]."""
    paths = [os.path.join(folder, f"{n}.wav") for n in range(1, len(lines) + 1)]
    job = os.path.join(folder, "lines.json")
    json.dump([{"text": t, "path": p} for t, p in zip(lines, paths)], open(job, "w", encoding="utf-8"))
    script = ("Add-Type -AssemblyName System.Speech; "
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Rate = -1; "
              f"foreach ($l in (Get-Content -Raw -Encoding UTF8 '{job}' | ConvertFrom-Json)) "
              "{ $s.SetOutputToWaveFile($l.path); $s.Speak($l.text) }; $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
    return paths


def trim(pcm):
    a = array.array("h", pcm)
    loud = [i for i in range(len(a)) if abs(a[i]) > SILENCE]
    if not loud:
        return b""
    pad = TIMELINE_RATE // 50  # keep 20 ms around the speech
    return a[max(0, loud[0] - pad):loud[-1] + pad].tobytes()


def assemble(letter, tmp):
    """One letter -> (mono PCM16 at 48 kHz, [(marker position, line)], number of placeholder lines)."""
    lines = letter["text"]
    files = [os.path.join(AUDIO_DIR, letter["id"], f"{n}.ogg") for n in range(1, len(lines) + 1)]
    done = json.load(open(DONE_FILE, encoding="utf-8")) if os.path.exists(DONE_FILE) else {}

    def voiced(n):
        record = done.get(f"{letter['id']}_{n + 1}")
        return os.path.exists(files[n]) and (record is None or record["text"] == speakable(lines[n]))

    missing = [n for n in range(len(lines)) if not voiced(n)]
    if missing:
        folder = os.path.join(tmp, letter["id"])
        os.makedirs(folder, exist_ok=True)
        spoken = placeholder_voice([lines[i] for i in missing], folder)
        for i, path in zip(missing, spoken):
            files[i] = path
    silence = lambda seconds: b"\0\0" * int(seconds * TIMELINE_RATE)
    pcm, markers = bytearray(silence(LEAD_IN)), []
    for n, (line, path) in enumerate(zip(lines, files)):
        if n:
            pcm += silence(GAP)
        start = len(pcm) // 2
        markers.append((0 if n == 0 else max(0, start - int(MARKER_EARLY * TIMELINE_RATE)), line))
        pcm += trim(to_pcm48(path))
    pcm += silence(TAIL)
    return bytes(pcm), markers, len(missing)


def build():
    todo = letters()
    for letter in todo:
        if not letter["eventName"].startswith(SLOT_PREFIX):
            sys.exit(f"Letter {letter['id']}: eventName must be one of the {SLOT_PREFIX}<slot> events.")
    slots = [l["eventName"] for l in todo]
    if len(set(slots)) != len(slots):
        sys.exit("Two letters use the same eventName slot.")

    bank = vanilla_bank()
    print("Decoding the original host audio...")
    samples = {name: (rate, pcm) for name, rate, pcm in fmod_runtime.decode_fsb(bank.fsb_bytes())}
    with tempfile.TemporaryDirectory() as tmp:
        for letter in todo:
            slot = letter["eventName"][len(SLOT_PREFIX):]
            if slot not in samples:
                sys.exit(f"Letter {letter['id']}: there is no {letter['eventName']} event in the bank.")
            pcm, markers, placeholders = assemble(letter, tmp)
            chunk, timeline, k = bank.timeline_for_sample(slot)
            timeline.set_instrument_length(k, len(pcm) // 2)
            timeline.set_markers(markers)
            chunk.data = timeline.build()
            samples[slot] = (TIMELINE_RATE, pcm)
            note = f", {placeholders} line(s) with the placeholder voice (run tts_import.py generate)" if placeholders else ""
            print(f"{letter['id']} -> {letter['eventName']}: {len(markers)} lines, {len(pcm) / 2 / TIMELINE_RATE:.1f} s{note}")
    # The game only plays letters listed in TDDictation.jet, so the other slots are never heard:
    # empty them instead of shipping the original host's recordings.
    used = {l["eventName"][len(SLOT_PREFIX):] for l in todo}
    unused = sorted(s for s in samples if s.isdigit() and s not in used)
    for slot in unused:
        chunk, timeline, k = bank.timeline_for_sample(slot)
        silence = b"\0\0" * (TIMELINE_RATE // 10)
        timeline.set_instrument_length(k, len(silence) // 2)
        timeline.set_markers([])
        chunk.data = timeline.build()
        samples[slot] = (TIMELINE_RATE, silence)
    print(f"{len(unused)} unused slot(s) left empty.")
    old = bank.fsbs[0]
    bank.fsbs[0] = FSB5(FSB_PCM16, old.extra, [pcm16_sample(s.name, samples[s.name][1], samples[s.name][0])
                                               for s in old.samples])
    size = bank.save(OUT_BANK)
    print(f"Wrote {os.path.relpath(OUT_BANK, ROOT)} ({size / 1e6:.1f} MB)")


def check():
    if not os.path.exists(OUT_BANK):
        sys.exit("Run build first.")
    todo = letters()
    results = fmod_runtime.play_events(OUT_BANK, [l["eventName"] for l in todo])
    bad = 0
    for letter in todo:
        length, markers = results[letter["eventName"]]
        ok = [name for _, name in markers] == letter["text"]
        bad += not ok
        print(f"{letter['id']} {letter['eventName']} ({length / 1000:.1f} s) {'OK' if ok else 'MISMATCH'}")
        for ms, name in markers:
            print(f"  {ms / 1000:6.2f} s  {name}")
    sys.exit(1 if bad else 0)


def backup(path):
    if not os.path.exists(path + ".vanilla"):
        shutil.copy2(path, path + ".vanilla")


def install(dev_console):
    if not os.path.exists(OUT_BANK):
        sys.exit("Run build first.")
    targets = [(OUT_BANK, os.path.join(TD2, "TMP2Host.bank")),
               (os.path.join(ROOT, "TDDictation.jet"), os.path.join(TD2, "content", "TDDictation.jet"))]
    for src, dest in targets:
        backup(dest)
        shutil.copy2(src, dest)
        print("installed", os.path.relpath(dest, os.path.dirname(TD2)))
    if dev_console:
        config = os.path.join(TD2, "jbg.config.jet")
        text = open(config, encoding="utf-8").read()
        if '"dev-console"' not in text:
            backup(config)
            newline = "\r\n" if "\r\n" in text else "\n"
            open(config, "w", encoding="utf-8", newline="").write(text.replace("{" + newline, "{" + newline + '\t"dev-console":"true",' + newline, 1))
        print('developer console on: press ` in game, then run  runTest("Dictation")')


def restore():
    restored = 0
    for path in (os.path.join(TD2, "TMP2Host.bank"), os.path.join(TD2, "content", "TDDictation.jet"),
                 os.path.join(TD2, "jbg.config.jet")):
        if os.path.exists(path + ".vanilla"):
            shutil.copy2(path + ".vanilla", path)
            os.remove(path + ".vanilla")
            print("restored", os.path.relpath(path, os.path.dirname(TD2)))
            restored += 1
    print(f"{restored} file(s) restored.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "build":
        build()
    elif cmd == "check":
        check()
    elif cmd == "install":
        install("--dev-console" in sys.argv)
    elif cmd == "restore":
        restore()
    else:
        print(__doc__)
