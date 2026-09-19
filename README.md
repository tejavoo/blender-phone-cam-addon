# Cam Link Pro — Advanced Real-Time Camera Link & Mixer

Turn a phone into a live handheld virtual camera for Blender. Point the
phone at real space, walk around, and a camera inside your Blender scene
mirrors your exact position and rotation in real time — for blocking
shots, previz, and recording real camera moves as keyframes.

This add-on is the **Blender receiver**. It listens for pose data sent by
a companion phone app (Unity + AR Foundation, Android), drives a camera
live, records takes into keyframes, mixes takes on the NLA, and streams
a live JPEG monitor of the shot back to the phone with remote record
control.

There is also an `llms.txt` file next to this one, written for AI
assistants (Claude, ChatGPT, etc.) helping you work with this add-on or
its companion phone app's source — point an assistant at it instead of
re-explaining the system from scratch.

---

## Install

1. In Blender: **Edit → Preferences → Add-ons → Install...**, pick the
   `cam_link_pro` zip.
2. Enable "Advanced Real-Time Camera Link & Mixer Pro" in the list.
3. Open the **3D Viewport sidebar (N-panel) → Cam Link Pro** tab.

## Quick start

1. **Network Config**: set the camera you want driven under *Target
   Camera* (defaults to the scene's active camera if left empty).
2. **Phone Pairing & Monitor**: click **Show Pairing QR**, scan it with
   the phone app. This hands the phone your IP, both ports, and an
   auth token in one step. (No QR reader handy? Type the IP/ports into
   the phone app's manual-entry fields instead — Network Config shows
   the values you need.)
3. Click **Connect**. This opens the network sockets and pairs with the
   phone, but nothing in the scene moves yet — check the status line
   (see *Live Stream Control* below) to confirm the phone is paired and
   pose data is actually arriving.
4. Click **Start Live Stream**. The camera should now follow the phone.
5. When you're ready to record a take, click **Record Motion** (or the
   phone's own Record button — see *Remote control* below), move the
   phone, then stop. Click **Bake & Mix to NLA** to push the recorded
   Action into its own NLA track so you can layer, mute, or solo takes.

---

## Panel reference

### Network Config
- **IP Address** — local interface to listen on. `0.0.0.0` (default)
  listens on all interfaces; that's almost always what you want.
- **Port** — UDP port for pose data (default `5005`). Must match the
  phone app.
- **Target Camera** — the object the incoming stream drives. Leave
  unset to default to the scene's active camera.

### Data Tuning
- **Wi-Fi Smoothing (Lerp)** — blends the current transform toward each
  incoming packet: `current = current*factor + incoming*(1-factor)`.
  Higher = smoother but laggier; lower = snappier but jitterier over a
  noisy Wi-Fi link.
- **Drive Location / Drive Rotation** — per-channel switches for what
  the live stream is allowed to touch. Turn a channel OFF to leave a
  hand-keyed or NLA-driven animation on that channel completely alone —
  e.g. keep your own dolly move on X/Y/Z and only take rotation from
  the phone. Both default ON (full 6DOF). This also gates what
  **Record Motion** keyframes: an OFF channel is never keyed, so an
  existing curve on it is never touched even while recording.
- **Set Up Delta Rig** — see *Layering a live/recorded pass on top of an
  existing animation* below. Use this once per camera before you
  attempt to combine a phone-driven pass with animation you've already
  made on that same camera.

### Live Stream Control
Two independent toggles, on purpose — connecting to the phone and
actually driving the camera are separate steps, so you can verify the
link before anything in the scene moves:

- **Connect / Disconnect** — opens (or closes) the UDP pose socket and,
  if enabled, the video/command TCP server. Connecting alone never
  moves the camera or allows recording; it only starts *watching* for
  incoming data. If a socket fails to open you'll see an error in the
  Info bar and this reverts to Disconnected.
- **Start Live Stream / Stop Live Stream** — only enabled once
  Connected. Starting actually applies incoming pose data to the
  target camera and allows recording (Record Motion, and the phone's
  own Start button); stopping freezes the camera where it is and
  disables recording again, but leaves the connection open so you can
  go live again instantly without re-pairing. Disconnecting also drops
  out of Live automatically.

**Status** shows a red/yellow/green health indicator plus the two
underlying signals it's built from once connected:
- 🔴 **Disconnected** — sockets are closed.
- 🟡 **Connected** — sockets open, not live yet, camera untouched.
- 🟡 **LIVE -- no pose data** — live, but no valid pose packet has
  arrived in the last ~1.5s (phone not sending, tracking lost, wrong
  IP/port, Wi-Fi dropped, etc.) — the camera is simply holding its last
  applied position, not visibly broken, but check the phone.
- 🟢 **LIVE** — live and a pose packet has arrived within the last
  ~1.5s. This is the only state where you should expect to see smooth,
  continuous motion.

Below that, while connected: **Pose: Receiving/No data** reflects the
UDP pose channel specifically (independent of Live), and **Phone:
Connected/Not connected** (only shown when *Enable Live Monitor* is on)
reflects whether a client has an open TCP video/command connection.

### Capture & Motion Management
- **Take Name** — name given to the next baked NLA track/strip.
- **Blend Type** — how the *next* baked take's NLA strip combines with
  whatever's already on the tracks below it:
  - **Replace** (default) — this take is an alternative to the others;
    only it is seen wherever it plays. Use this for separate takes
    you'll mute/solo/pick between in the NLA editor.
  - **Add** — this take's values are added to the strips below it. Use
    for layering a handheld pass on top of an existing move — see the
    Delta Rig section, because a raw Add strip on *absolute* recorded
    values only combines correctly if the recorded values are small
    deltas around zero to begin with.
  - **Subtract / Multiply / Combine** — the same operations NLA always
    offers; rarely needed here but available.
- **Record Motion** — only enabled while **Live** (Connected alone isn't
  enough — recording before the camera is actually being driven
  wouldn't capture anything meaningful). Bakes the live (smoothed)
  transform to keyframes at the scene's current frame, advancing the
  frame by one each tick. Recording can also be started/stopped from
  the phone (`START`/`STOP` over the command channel) if *Allow Remote
  Record Control* is on — a phone-side Start is silently ignored if the
  stream isn't live yet, same as if remote control were disabled.
  Both controls always agree on the actual state, since the received
  `REC_ON`/`REC_OFF` status is the single source of truth either way.
- **Bake & Mix to NLA** — pushes the just-recorded Action (object
  transform + lens/sensor width) into a brand-new NLA track/strip
  named after *Take Name*, using the current *Blend Type*, and clears
  the active action so the next take starts clean.

### Phone Pairing & Monitor
- **Video/Command Port** — TCP port for the live monitor + remote
  record control (default `5006`).
- **Enable Live Monitor** — streams the target camera's rendered view
  back to the phone as JPEG frames, composited over the phone's own AR
  passthrough. Adds render cost on top of tracking; leave off if you
  only need position/rotation data.
- **Monitor Frame Rate** — how often a frame is captured (5–30fps).
  Expect roughly 10–25fps in practice; the render+encode time is the
  bottleneck, not the network.
- **Allow Remote Record Control** — lets the phone's own Record button
  drive `is_recording` here, in addition to this panel.
- **Token** — included in the pairing QR; a device must have scanned it
  (or been given it manually) to open the video feed or send record
  commands. Click the refresh icon to invalidate any QR shown so far.
- **Show Pairing QR** — opens a QR image (in your OS's default image
  viewer) encoding `camlink://<ip>:<pose_port>:<video_port>?t=<token>`.

---

## Layering a live/recorded pass on top of an existing animation

**The problem:** while streaming, the add-on writes *absolute*
location/rotation values straight onto the target camera every tick.
That write always wins over whatever an Action or NLA stack on that
*same* object would otherwise evaluate to — "add on top of" is an
NLA/playback-time concept, and it doesn't exist yet at the moment a
live packet is applied. So a hand-keyed (or NLA'd) move on the same
object the phone is driving will visibly fight with the incoming
stream — the two write sources fight over the same property every
frame, which looks like a glitch/pop, not a smooth layered combination.

**The fix:** don't put both animations on the same object. Click **Set
Up Delta Rig** in the Data Tuning section with your animated camera set
as Target Camera. This:

1. Creates a new Empty at the camera's current world transform (nothing
   visibly jumps).
2. Moves the camera's existing Action onto that Empty.
3. Resets the camera to `(0, 0, 0)` local and parents it to the Empty.

Now the Empty plays your original base animation independently, and the
camera — a child of the Empty — is what Cam Link Pro drives at full
6DOF. Blender composes parent → child transforms every frame
automatically, so the live-driven local delta rides on top of the
Empty's base motion via ordinary matrix math, live — in the viewport,
in the phone's monitor preview, and while recording. No fighting.

**To use it:** run *Set Up Delta Rig* once, turn both *Drive Location*
and *Drive Rotation* on, tap **Reset Origin** on the phone right before
recording (so the recorded delta starts near zero rather than some
large absolute offset), and record as usual. The Camera's own action
(created by Record Motion) contains only the local delta — bake that to
its own NLA strip same as any other take.

This operator refuses to run if the camera already has a parent, or
already has NLA tracks of its own — in either case, set the rig up by
hand in the Outliner/NLA editor instead, so nothing existing gets
reshuffled automatically.

---

## Troubleshooting

**`Video monitor error: bpy_struct: item.attr = val: enum "JPEG" not
found in ('FFMPEG')`, and the monitor keeps turning itself off.**
Blender 4.0+ splits the Output Properties panel into an Image/Video
*Media Type* toggle; when the scene's output is set to Video,
`file_format`'s own enum shrinks to just `FFMPEG`, and the monitor's
still-frame capture (which needs `JPEG`) fails. Fixed as of this
version — the capture step now forces Media Type to Image for the
instant it grabs a frame, then restores whatever it was. If you still
see this after updating, the running Blender session has the old
module cached in memory; toggling the add-on off/on in Preferences (or
restarting Blender) reloads it.

**Recording overwrites/mixes with an animation I made by hand.**
See *Layering a live/recorded pass on top of an existing animation*
above — use Set Up Delta Rig, or turn off whichever of Drive
Location/Drive Rotation you don't want the phone touching.

**When I move the phone, it feels like the whole scene swings around
instead of just the camera moving through it.**
This is very likely expected, not a bug — see the FAQ in `llms.txt`
("why does the scene appear to move") for the full explanation and a
30-second way to confirm the camera really is moving independently.

**"No 3D Viewport area is open" in the console, monitor shows nothing.**
The live-frame capture path needs a visible 3D Viewport area to render
through (it reuses that viewport's OpenGL context). Make sure a 3D
Viewport is visible somewhere in the current workspace.

**Camera snaps to a totally different spot the moment you start
streaming.** The phone's own tracking origin is independent of
anything in Blender — see `llms.txt` for how "Reset Origin" and Cam
Link Pro's smoothing baseline interact, and consider a Delta Transform
or the Delta Rig setup if you need a fixed starting point that survives
this.

---

## Wire protocol (for reference)

- **Pose (UDP, phone → Blender):** one line of ASCII per packet:
  `sequence_id,pos_x,pos_y,pos_z,rot_x,rot_y,rot_z,focal_length_mm,sensor_width_mm`,
  position in metres and rotation in degrees, already in Blender's
  right-handed Z-up space and XYZ Euler order.
- **Video + command (TCP, both directions):** phone sends
  `AUTH <token>` on connect, then `START` / `STOP` / `PING`; Blender
  replies with `REC_ON` / `REC_OFF` / `REMOTE_DISABLED` / `PONG` /
  `STATE <token>` and interleaves JPEG frames as a 4-byte big-endian
  length prefix followed by that many bytes of JPEG data.
  `STATE <token>` mirrors the panel's own Connect/Live traffic light —
  `<token>` is `CONNECTED`, `LIVE_NODATA`, or `LIVE` — sent whenever it
  changes, and also immediately to a phone that (re)connects mid-session
  so it never has to wait for the next transition to learn where things
  actually stand.
- **Pairing QR:** `camlink://<ip>:<pose_port>:<video_port>?t=<token>`.

Full detail on the phone-side pose pipeline (axis conversion, jump
absorption, origin/heading anchor, horizon levelling, One Euro
filtering, axis freezing/rig presets, dolly offset) lives in the
companion Unity app's own spec and source — see `llms.txt` for a
compact version of the same reference.

---

## Files in this add-on

- `__init__.py` — settings, operators, UI panel, UDP pose server.
- `video_channel.py` — TCP video/command server + JPEG frame capture.
- `qr_gen.py` — pairing QR generation (no external dependencies).
- `README.md` — this file.
- `llms.txt` — compact reference for AI assistants; see below.
