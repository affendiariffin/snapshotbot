TTS BATTLEFIELD REPLAY
======================
Automatically captures top-down screenshots of your Tabletop Simulator
battlefield at the end of each turn, then compiles them into a timelapse
video (replay.mp4) when your session ends.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUICK START
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Double-click TTS_Replay.exe
   → A red icon appears in your system tray (bottom-right of taskbar)
   → The Setup Guide opens automatically on first run — just follow it!

   The guide walks you through every step. You only need to do it once.
   To open it again any time: right-click the tray icon → Setup Guide.

2. Play your game
   → Right-click tray icon → ▶ Start Session  (icon turns GREEN)
   → Press 📷 CAPTURE in TTS at the end of each turn
   → The frame count in the tray menu updates as you go

3. Get your video
   → Right-click tray icon → ■ Stop Session
   → replay.mp4 is compiled automatically
   → Your session folder opens in Explorer — just double-click the video!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TRAY ICON COLOURS
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

• The replay video plays at 2 frames per second (each turn = 0.5s).
  To change speed: open capture.pyw in Notepad, find  fps = 2  and
  change the number.

• If screenshots show ruler lines or measurement circles, use
  "🔧 Fix Screenshot Glitches" from the tray menu.

• If you try to close the app mid-session, it will ask whether you
  want to save the video first — so you won't lose any frames.

• Windows Defender may warn you the first time (unsigned exe).
  Click "More info" → "Run anyway" — it's safe.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TROUBLESHOOTING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  "Cannot bind port 39998"
  → TTS is already open and owns the port. Start TTS Replay FIRST,
    then launch TTS. Or restart TTS after starting the tray app.

  "Start Session" is greyed out
  → You need to calibrate your capture region first.
    Right-click the tray icon → 🎯 Calibrate Region.

  Video does not compile / "no valid frames found"
  → The session had no captures. Make sure you pressed 📷 CAPTURE
    at least once while the session was active (icon was green).

  Screenshots are blank or show the wrong area
  → Right-click tray → 🎯 Calibrate Region and drag again.
    Make sure TTS is NOT in exclusive fullscreen mode.

  replay.mp4 won't open
  → Install VLC (free). Windows Media Player may not support the
    codec used by the app. https://www.videolan.org/vlc/
