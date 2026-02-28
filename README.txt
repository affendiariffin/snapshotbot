TTS BATTLEFIELD REPLAY
======================
Automatically captures top-down screenshots of your Tabletop Simulator
battlefield at the end of each turn, then compiles them into a shareable
HTML replay file when your session ends.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIRST-TIME SETUP  (one time only)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Enable TTS External Editor API
   In TTS: Configuration → External Editor API → ON  (port 39998)

2. Add the capture button to TTS
   Right-click any object → Scripting → paste capture_button.lua → Save & Play
   A 📷 CAPTURE button will appear on the object.

3. Calibrate your capture region
   Double-click TTS_Replay.exe → click 🎯 Calibrate Region
   Drag a rectangle over your battlefield area.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EACH SESSION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Click ▶ Start Session  (indicator turns GREEN)
2. Press 📷 CAPTURE in TTS at the end of each turn
3. Click ■ Stop Session
   → replay.html is compiled automatically and the folder opens

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INDICATOR COLOURS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  🔴  Red  = idle / not recording 
  🟢  Green = actively recording — captures will be saved 

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TIPS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• Sessions are saved in "TTS Replay Sessions\" next to the exe.
  Each session is its own folder — old sessions are never deleted.

• After calibrating, a preview window shows you exactly what the
  app will capture. Click Redo if it looks wrong.

• The replay opens as a single replay.html file — share it with anyone,
  it opens in any browser with no install required.
  Use ← → arrow keys or the slider to step through turns.


• If you try to close the app mid-session, it will ask whether you
  want to save the replay first — so you won't lose any frames.

• Windows Defender may warn you the first time (unsigned exe).
  Click "More info" → "Run anyway" — it's safe.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TROUBLESHOOTING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  TTS Replay can't connect to TTS
  → Make sure TTS is running and External Editor API is enabled:
    Configuration → External Editor API → ON  (port 39998).
    TTS Replay will reconnect automatically once TTS is open.

  "Start Session" is greyed out
  → You need to calibrate your capture region first.
    Click 🎯 Calibrate Region in the app window.

  Replay does not compile / "no valid frames found"
  → The session had no captures. Make sure you pressed 📷 CAPTURE
    at least once while the session was active (indicator was green).

  Screenshots are blank or show the wrong area
  → Click 🎯 Calibrate Region and drag again.
    Make sure TTS is NOT in exclusive fullscreen mode.
