"""Drives the game's own FMOD 2.02 DLLs (fmod.dll / fmodstudio.dll, 64-bit) from Python via ctypes.

Used by dictation_bank.py to decode the original host audio and to check a built bank offline.
Set JACKBOX_PP6 if the Party Pack 6 is not in the default Steam folder.
"""
import array, ctypes as C, os, tempfile, time

GAME = os.environ.get("JACKBOX_PP6", r"C:\Program Files (x86)\Steam\steamapps\common\The Jackbox Party Pack 6")
TD2 = os.path.join(GAME, "games", "TriviaDeath2")

OUTPUT_NOSOUND_NRT, OUTPUT_WAVWRITER = 4, 3
STUDIO_INIT_SYNCHRONOUS_UPDATE = 0x2
MODE_OPENONLY, MODE_ACCURATETIME = 0x2000, 0x4000
TIMEUNIT_PCM = 2
FORMAT_PCM16, FORMAT_PCMFLOAT = 2, 5
CB_TIMELINE_MARKER = 0x800
PLAYBACK_STOPPED = 2
LOADING_LOADED, LOADING_ERROR = 3, 4

_dlls = None


def dlls():
    global _dlls
    if _dlls is None:
        os.add_dll_directory(GAME)
        _dlls = C.WinDLL(os.path.join(GAME, "fmod.dll")), C.WinDLL(os.path.join(GAME, "fmodstudio.dll"))
    return _dlls


def check(res, what):
    if res != 0:
        raise RuntimeError(f"{what} failed: FMOD error {res}")


def header_version():
    """FMOD 2.x insists the caller's header version matches the DLL, so find it by probing."""
    core, _ = dlls()
    sysp = C.c_void_p()
    for v in (0x00020000 | minor << 8 | dev for minor in range(4) for dev in range(0x40)):
        if core.FMOD5_System_Create(C.byref(sysp), C.c_uint(v)) == 0:
            core.FMOD5_System_Release(sysp)
            return v
    raise RuntimeError("no matching FMOD header version")


def decode_fsb(fsb):
    """FSB5 bytes -> [(name, rate, mono PCM16 bytes)] for every subsound."""
    core, _ = dlls()
    tmp = tempfile.NamedTemporaryFile(suffix=".fsb", delete=False)
    tmp.write(fsb)
    tmp.close()
    sysp, snd = C.c_void_p(), C.c_void_p()
    check(core.FMOD5_System_Create(C.byref(sysp), C.c_uint(header_version())), "System_Create")
    check(core.FMOD5_System_SetOutput(sysp, OUTPUT_NOSOUND_NRT), "SetOutput")
    check(core.FMOD5_System_Init(sysp, 8, 0, None), "System_Init")
    try:
        check(core.FMOD5_System_CreateSound(sysp, tmp.name.encode(), MODE_OPENONLY | MODE_ACCURATETIME, None,
                                            C.byref(snd)), "CreateSound")
        n = C.c_int()
        check(core.FMOD5_Sound_GetNumSubSounds(snd, C.byref(n)), "GetNumSubSounds")
        out = []
        for i in range(n.value):
            sub = C.c_void_p()
            check(core.FMOD5_Sound_GetSubSound(snd, i, C.byref(sub)), "GetSubSound")
            typ, fmt, ch, bits = C.c_int(), C.c_int(), C.c_int(), C.c_int()
            check(core.FMOD5_Sound_GetFormat(sub, C.byref(typ), C.byref(fmt), C.byref(ch), C.byref(bits)), "GetFormat")
            if ch.value != 1:
                raise RuntimeError("only mono sounds are supported")
            freq, prio, length, got = C.c_float(), C.c_int(), C.c_uint(), C.c_uint()
            core.FMOD5_Sound_GetDefaults(sub, C.byref(freq), C.byref(prio))
            check(core.FMOD5_Sound_GetLength(sub, C.byref(length), TIMEUNIT_PCM), "GetLength")
            buf = C.create_string_buffer(length.value * bits.value // 8)
            core.FMOD5_Sound_SeekData(sub, 0)
            res = core.FMOD5_Sound_ReadData(sub, buf, len(buf), C.byref(got))
            if res not in (0, 16):  # OK / FILE_EOF
                check(res, "ReadData")
            raw = buf.raw[:got.value]
            if fmt.value == FORMAT_PCMFLOAT:
                raw = array.array("h", (max(-32768, min(32767, round(x * 32767))) for x in array.array("f", raw))).tobytes()
            elif fmt.value != FORMAT_PCM16:
                raise RuntimeError(f"unexpected decoded format {fmt.value}")
            name = C.create_string_buffer(256)
            core.FMOD5_Sound_GetName(sub, name, 256)
            out.append((name.value.decode(), int(freq.value), raw))
        return out
    finally:
        if snd:
            core.FMOD5_Sound_Release(snd)
        core.FMOD5_System_Release(sysp)
        os.remove(tmp.name)


class MarkerProps(C.Structure):
    _fields_ = [("name", C.c_char_p), ("position", C.c_int)]


def play_events(host_bank, events, wav_path=None):
    """Play events from host_bank (with the game's other TMP2 banks loaded) and return
    {event: (length ms, [(marker ms, marker name)])}. With wav_path the mix is recorded in real time."""
    core, studio = dlls()
    st, cs = C.c_void_p(), C.c_void_p()
    check(studio.FMOD_Studio_System_Create(C.byref(st), C.c_uint(header_version())), "Studio_System_Create")
    check(studio.FMOD_Studio_System_GetCoreSystem(st, C.byref(cs)), "GetCoreSystem")
    check(core.FMOD5_System_SetOutput(cs, OUTPUT_WAVWRITER if wav_path else OUTPUT_NOSOUND_NRT), "SetOutput")
    extra = C.c_char_p(wav_path.encode()) if wav_path else None
    check(studio.FMOD_Studio_System_Initialize(st, 64, STUDIO_INIT_SYNCHRONOUS_UPDATE, 0, extra), "Initialize")
    tick = (lambda: time.sleep(0.01)) if wav_path else (lambda: None)
    results = {}
    try:
        for path in (os.path.join(TD2, "TMP2Music.strings.bank"), os.path.join(TD2, "TMP2Music.bank"), host_bank):
            bank = C.c_void_p()
            check(studio.FMOD_Studio_System_LoadBankFile(st, path.encode(), 0, C.byref(bank)), f"LoadBankFile {path}")
        for ev in events:
            desc, inst = C.c_void_p(), C.c_void_p()
            check(studio.FMOD_Studio_System_GetEvent(st, ("event:/" + ev).encode(), C.byref(desc)), f"GetEvent {ev}")
            length, state = C.c_int(), C.c_int(-1)
            studio.FMOD_Studio_EventDescription_GetLength(desc, C.byref(length))
            check(studio.FMOD_Studio_EventDescription_LoadSampleData(desc), "LoadSampleData")
            for _ in range(3000):
                studio.FMOD_Studio_System_Update(st)
                studio.FMOD_Studio_EventDescription_GetSampleLoadingState(desc, C.byref(state))
                if state.value in (LOADING_LOADED, LOADING_ERROR):
                    break
                tick()
            if state.value != LOADING_LOADED:
                raise RuntimeError(f"{ev}: sample data did not load (state {state.value})")
            check(studio.FMOD_Studio_EventDescription_CreateInstance(desc, C.byref(inst)), "CreateInstance")
            markers = []

            @C.WINFUNCTYPE(C.c_int, C.c_uint, C.c_void_p, C.c_void_p)
            def on_event(kind, _inst, params):
                if kind == CB_TIMELINE_MARKER:
                    m = C.cast(params, C.POINTER(MarkerProps)).contents
                    markers.append((m.position, m.name.decode("utf-8", "replace")))
                return 0

            check(studio.FMOD_Studio_EventInstance_SetCallback(inst, on_event, CB_TIMELINE_MARKER), "SetCallback")
            check(studio.FMOD_Studio_EventInstance_Start(inst), "Start")
            for n in range(100000):
                check(studio.FMOD_Studio_System_Update(st), "Update")
                tick()
                studio.FMOD_Studio_EventInstance_GetPlaybackState(inst, C.byref(state))
                if state.value == PLAYBACK_STOPPED and n > 2:
                    break
            studio.FMOD_Studio_EventInstance_Release(inst)
            results[ev] = (length.value, markers)
    finally:
        studio.FMOD_Studio_System_Release(st)
    return results
