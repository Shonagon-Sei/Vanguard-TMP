"""Helper for voicing questions with TTSMaker (https://ttsmaker.com/).

Automatic (TTSMaker PRO API key)
  Set your key for the current terminal only (it is never written to disk):
      PowerShell:  $env:TTSMAKER_API_KEY = "your-key"
      Git Bash:    export TTSMAKER_API_KEY="your-key"

  python tools/tts_import.py voices [language]       list voice IDs (default language: en)
  python tools/tts_import.py status                  show your remaining character quota
  python tools/tts_import.py generate --voice ID [--speed 1.0] [--pitch 1.0] [--volume 1.0]
                                      [--only 300063,400055] [--force]
      Voices every pending line and drops it into the right folder in the game's format.
      Finished lines are remembered in tools/tts_done.json, so re-running only does new or
      changed lines (or everything with --force).

Manual (website)
  python tools/tts_import.py checklist
      Writes tools/tts_checklist.txt: every line that still needs a real voice, and the file
      name to save each TTSMaker download as (e.g. 300063.mp3, 300059_intro.mp3).
  python tools/tts_import.py import "C:/path/to/downloads"
      Converts each file to the game's format (Ogg Vorbis, 24 kHz, mono, 48 kbps) and puts it
      in the right TDQuestion/TDFinalRound folder, creating data.jet if it is missing.
"""
import json, os, re, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # voice names can contain emoji

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRLF = "\r\n"
API ="https://api.ttsmaker.com/v2"
DONE_FILE = os.path.join(ROOT, "tools", "tts_done.json")

# Lines that currently use placeholder (Windows TTS) audio or have no audio at all.
PENDING = (
    ["300059", "300060", "300061", "300062"]
    + [str(i) for i in range(300063, 300147)]
    + ["400020", "400023", "400024"]
    + [str(i) for i in range(400036, 400061)]
)


def load(name):
    return {c["id"]: c for c in json.load(open(os.path.join(ROOT, name), encoding="utf-8"))["content"]}


def lines_for(entry_id):
    """(file stem, text to speak, destination path) for every audio file of an entry."""
    if entry_id.startswith("3"):
        q = load("TDQuestion.jet")[entry_id]
        folder = os.path.join(ROOT, "TDQuestion", entry_id)
        out = []
        if q.get("introAudio"):
            out.append((entry_id + "_intro", q["introAudio"], os.path.join(folder, "introAudio.ogg")))
        out.append((entry_id, q["text"], os.path.join(folder, "questionAudio.ogg")))
        return out
    f = load("TDFinalRound.jet")[entry_id]
    return [(entry_id, f["text"], os.path.join(ROOT, "TDFinalRound", entry_id, "question.ogg"))]


def speakable(text):
    """Text sent to TTS: on-screen text without symbols that break the narrator's pacing."""
    t = re.sub(r"\[/?[a-z]+\]", "", text)                     # [i]...[/i] markup
    t = t.replace("Cardfight!!", "Cardfight")
    t = t.replace('"', "")                                    # quotes
    t = re.sub(r"\((RC|VC|GC|R|V|G)\)", lambda m: " ".join(m.group(1)), t)  # (RC) -> R C
    t = re.sub(r"\s*\(", ", ", t).replace(")", ",")           # other brackets -> pauses
    t = re.sub(r"(\d)-(\d)", r"\1 to \2", t)                  # 2011-2012 -> 2011 to 2012
    t = re.sub(r"(\d)/(\d)", r"\1 \2", t)                     # 25/26 -> 25 26
    t = re.sub(r"\+(?=\d)", "plus ", t)                       # +100,000,000
    t = t.replace("&", " and ")
    t = re.sub(r"[-+=_/]", lambda m: " blank " if m.group(0) == "_" else " ", t)
    t = t.replace(":", ",")
    t = re.sub(r"!+(?=\s+[A-Z])", ".", t)                     # quoted "...!" ending a sentence
    t = re.sub(r"!+(?=\s*[\w,.?])", "", t)                    # other mid-sentence !
    t = re.sub(r"\s+([,.?!])", r"\1", t)                      # tidy punctuation, keep "..."
    t = re.sub(r",\s*,", ",", t)
    t = re.sub(r",\s*(\.\.\.|[.?!])", r"\1", t)
    t = re.sub(r"(?<!\.)\.\s*,", ".", t)
    return re.sub(r"\s{2,}", " ", t).strip(" ,")


def write_data_jet(entry_id):
    """Create data.jet for an entry that has no folder yet (same layout as the existing ones)."""
    if entry_id.startswith("3"):
        q = load("TDQuestion.jet")[entry_id]
        folder = os.path.join(ROOT, "TDQuestion", entry_id)
        fields = [
            {"t": "B", "v": "true" if q.get("introAudio") else "false", "n": "HasIntro"},
            {"t": "A", "v": "introAudio", "n": "Intro"},
            {"t": "B", "v": "true", "n": "HasQ"},
            {"t": "A", "v": "questionAudio", "n": "Q", "s": q["text"]},
            {"t": "B", "v": "false", "n": "HasPic"},
            {"t": "G", "v": "image", "n": "Pic"},
            {"t": "B", "v": "false", "n": "HasVamp"},
            {"t": "A", "v": "pictureVamp", "n": "Vamp"},
        ]
    else:
        f = load("TDFinalRound.jet")[entry_id]
        folder = os.path.join(ROOT, "TDFinalRound", entry_id)
        fields = [
            {"t": "B", "v": "true", "n": "HasQ"},
            {"t": "A", "v": "question", "n": "Q", "s": f["text"]},
        ]
    path = os.path.join(folder, "data.jet")
    if not os.path.exists(path):
        os.makedirs(folder, exist_ok=True)
        text = json.dumps({"fields": fields}, indent=1, ensure_ascii=False)
        open(path, "wb").write((text.replace("\n", CRLF) + CRLF).encode("utf-8"))


def to_game_format(src, dest):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src,
                    "-ar", "24000", "-ac", "1", "-c:a", "libvorbis", "-b:a", "48k", dest], check=True)


def checklist():
    out_path = os.path.join(ROOT, "tools", "tts_checklist.txt")
    rows = [line for entry_id in PENDING for line in lines_for(entry_id)]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"{len(rows)} lines to voice. Save each TTSMaker download as the name on the left.\n\n")
        for stem, text, _ in rows:
            f.write(f"{stem}.mp3\n    {speakable(text)}\n\n")
    print(f"Wrote {len(rows)} lines to {os.path.relpath(out_path, ROOT)}")


def import_dir(src):
    targets = {stem: (entry_id, dest) for entry_id in PENDING for stem, _, dest in lines_for(entry_id)}
    done, unknown = 0, []
    for name in sorted(os.listdir(src)):
        stem, ext = os.path.splitext(name)
        if stem not in targets:
            unknown.append(name)
            continue
        entry_id, dest = targets[stem]
        write_data_jet(entry_id)
        to_game_format(os.path.join(src, name), dest)
        print("imported", name, "->", os.path.relpath(dest, ROOT))
        done += 1
    print(f"{done} file(s) imported.")
    if unknown:
        print("Skipped (name not in the checklist):", ", ".join(unknown))


# ---------------------------------------------------------------- TTSMaker API

def api_key():
    key = os.environ.get("TTSMAKER_API_KEY", "").strip()
    if not key:
        sys.exit("Set TTSMAKER_API_KEY in this terminal first (see the top of this file).")
    return key


def api_call(path, params=None, body=None):
    """GET with query params, or POST a JSON body. Retries on HTTP 429 (limit: 1 request/second)."""
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Vanguard-TMP tts helper"}
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
            return json.load(urllib.request.urlopen(req, timeout=120))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                time.sleep(2 * (attempt + 1))
                continue
            sys.exit(f"TTSMaker API error: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:300]}")


def check(resp):
    if resp.get("error_code", 0) != 0:
        sys.exit(f"TTSMaker API error {resp.get('error_code')}: {resp.get('error_summary') or resp.get('msg')}")
    return resp


def show_quota(resp):
    acc = resp.get("account_status") or {}
    if acc:
        print(f"Quota: {acc.get('available_quota')} characters left "
              f"({acc.get('characters_used')} of {acc.get('quota_characters')} used)")


def voices(language="en"):
    resp = check(api_call("get-voice-list", {"language": language, "api_key": api_key()}))
    for v in resp.get("voices_detailed_list", []):
        print(f"{v.get('id'):>6}  {v.get('name')}  [{v.get('language')}]  limit {v.get('text_characters_limit')} chars")
    print(f"{resp.get('voices_count')} voices.")


def status():
    show_quota(check(api_call("get-token-status", {"api_key": api_key()})))


def generate(args):
    def opt(name, default=None):
        return args[args.index(name) + 1] if name in args else default
    if "--voice" not in args:
        sys.exit("Usage: generate --voice ID [--speed 1.0] [--pitch 1.0] [--volume 1.0] [--only ID,ID] [--force]")
    settings = {"voice_id": int(opt("--voice")), "audio_speed": float(opt("--speed", 1.0)),
                "audio_pitch": float(opt("--pitch", 1.0)), "audio_volume": float(opt("--volume", 1.0))}
    only = set(opt("--only").split(",")) if "--only" in args else None
    key = api_key()

    done = json.load(open(DONE_FILE, encoding="utf-8")) if os.path.exists(DONE_FILE) else {}
    todo = []
    for entry_id in PENDING:
        if only and entry_id not in only:
            continue
        for stem, text, dest in lines_for(entry_id):
            record = {"text": speakable(text), **settings}
            if "--force" in args or done.get(stem) != record:
                todo.append((entry_id, stem, record, dest))
    chars = sum(len(r["text"]) for _, _, r, _ in todo)
    print(f"{len(todo)} line(s) to generate, about {chars} characters.")

    for n, (entry_id, stem, record, dest) in enumerate(todo, 1):
        resp = check(api_call("create-tts-order", body={"api_key": key, "text": record["text"], "audio_format": "wav", **settings}))
        tmp = tempfile.mktemp(suffix=".wav")
        for url in (resp.get("audio_download_url"), resp.get("audio_download_backup_url")):
            if not url:
                continue
            try:
                urllib.request.urlretrieve(url, tmp)
                break
            except Exception:
                continue
        else:
            sys.exit(f"Could not download the audio for {stem}.")
        write_data_jet(entry_id)
        to_game_format(tmp, dest)
        os.remove(tmp)
        done[stem] = record
        json.dump(done, open(DONE_FILE, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        print(f"[{n}/{len(todo)}] {stem} -> {os.path.relpath(dest, ROOT)}")
        if n == 1 or n == len(todo):
            show_quota(resp)
        time.sleep(1.2)  # API limit: 1 request per second
    print("Done.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "checklist":
        checklist()
    elif cmd == "import" and len(sys.argv) >= 3:
        import_dir(sys.argv[2])
    elif cmd == "voices":
        voices(sys.argv[2] if len(sys.argv) > 2 else "en")
    elif cmd == "status":
        status()
    elif cmd == "generate":
        generate(sys.argv[2:])
    else:
        print(__doc__)
