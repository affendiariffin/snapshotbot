TTS BATTLEFIELD REPLAY
======================
Automatically captures top-down screenshots of your Tabletop Simulator
battlefield at the end of each turn, then lets you play them back as a
timelapse in your browser.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUICK START
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Double-click TTS_Replay.exe
   → A red icon appears in your system tray (bottom-right of taskbar)
   → A "TTS Replay Sessions" folder is created next to the exe

2. Add the capture button to TTS  (one-time setup)
   → Open capture_button.lua in Notepad (also created next to the exe)
   → In TTS, right-click any small object → Scripting
   → Paste the Lua code → Save & Play
   → A 📷 CAPTURE button appears on the object

3. Enable TTS External Editor API  (one-time)
   → TTS menu → Configuration → External Editor API → Enable
   → Port must be 39998 (the default)

4. Calibrate the capture region
   → Right-click tray icon → Calibrate Region
   → Drag a rectangle over your battlefield area on screen
   → Release to save

5. Start a session
   → Right-click tray icon → Start Session

6. Play your game!
   → Press 📷 CAPTURE in TTS at the end of each turn
   → The app automatically snaps a screenshot

7. Watch the replay
   → Right-click tray icon → Open Replay in Browser
   → Use Microsoft Edge, or whitelist "localhost" in your ad blocker
   → Controls: Space = play/pause, ← → = step, Home/End = first/last frame

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TIPS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• Sessions are saved in "TTS Replay Sessions\" next to the exe.
  Each session is its own folder — old sessions are never deleted.

• The app filters out TTS drawing lines (rulers, measurement circles)
  from screenshots automatically.

• If a screenshot looks wrong, use "Clean Existing Frames" from the
  tray menu to reprocess the current session.

• The replay page auto-refreshes every 10 seconds during a live session,
  so you can have it open on a second monitor while you play.

• Windows Defender may show a warning the first time you run the exe
  (because it's unsigned). Click "More info" → "Run anyway".

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TROUBLESHOOTING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  "Cannot bind port 39998"
  → TTS is already running and has the port open. Start the tray app
    BEFORE launching TTS, or restart TTS after starting the tray app.

  Replay page shows "ERROR"
  → Make sure you opened it via the tray icon (not by double-clicking
    the HTML file). URL must be http://localhost:8080/playback.html

  Screenshots are blank or wrong area
  → Recalibrate: tray icon → Calibrate Region

  Ad blocker blocks images
  → Use Microsoft Edge, or add "localhost" to your ad blocker's whitelist
