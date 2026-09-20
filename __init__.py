"""
================================================================================
 Advanced Real-Time Camera Link & Mixer Pro
 Blender 4.x Add-on -- UDP Receiver / Recorder / NLA Mixer

 Companion receiver for CameraLinkSender.cs (Unity + AR Foundation, Android).

 PACKET PROTOCOL (UTF-8, comma-separated, one line per packet)
 --------------------------------------------------------------
     sequence_id,x,y,z,rx,ry,rz,focal_length,sensor_width

     sequence_id   : integer, monotonically increasing (may wrap)
     x,y,z         : floats, position already in BLENDER space (meters)
     rx,ry,rz      : floats, Euler rotation already in BLENDER space (DEGREES)
     focal_length  : float, mm
     sensor_width  : float, mm

 The sender is responsible for converting Unity's left-handed Y-up space
 into Blender's right-handed Z-up space before the packet is sent -- this
 add-on assumes the incoming x/y/z/rx/ry/rz are already Blender-space.
================================================================================
"""

import socket
import math
import time

import bpy
import bpy.utils.previews
from bpy.props import (
    StringProperty,
    IntProperty,
    FloatProperty,
    BoolProperty,
    EnumProperty,
    PointerProperty,
)
from bpy.types import (
    PropertyGroup,
    Operator,
    Panel,
)

from . import video_channel

bl_info = {
    "name": "Advanced Real-Time Camera Link & Mixer Pro",
    "author": "Cam Link Pro",
    "version": (2, 0, 0),
    "blender": (4, 0, 0),
    "location": "3D Viewport > Sidebar > Cam Link Pro",
    "description": (
        "Streams real-time camera transforms, focal length and sensor width "
        "from an external Wi-Fi device over UDP, records them to keyframes, "
        "mixes takes into NLA tracks, and streams a live JPEG monitor of the "
        "camera's view back to the device with remote record control."
    ),
    "category": "Animation",
}

# Target modal-timer frequency (seconds). ~60Hz.
TIMER_INTERVAL = 1.0 / 60.0

# Number of comma-separated fields expected in a valid packet.
EXPECTED_FIELD_COUNT = 9


def _live_state_token(settings):
    """The single source of truth for "how connected/live are we", shared
    by the panel's traffic light and the outbound STATE line sent to the
    phone (WM_OT_camera_stream_server._drain_commands) so the two can
    never disagree.

    Returns "CONNECTED", "LIVE_NODATA" or "LIVE", or None if not even
    connected. None is deliberately not one of the three wire tokens --
    there is no STATE line for the disconnected case, since the phone's
    own TCP connection dropping already conveys that unambiguously.
    """
    if not settings.is_streaming:
        return None
    if not settings.is_live:
        return "CONNECTED"
    return "LIVE" if settings.pose_receiving else "LIVE_NODATA"


# ------------------------------------------------------------------------
# Data Structures
# ------------------------------------------------------------------------
class CamLinkProSettings(PropertyGroup):
    """Stores UI and network state for the Cam Link Pro add-on."""

    ip_address: StringProperty(
        name="IP Address",
        description="Local interface to listen on (0.0.0.0 = all interfaces)",
        default="0.0.0.0",
    )

    port: IntProperty(
        name="Port",
        description="UDP port to listen on",
        default=5005,
        min=1,
        max=65535,
    )

    target_camera: PointerProperty(
        name="Target Camera",
        description="Camera object driven by the incoming stream",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == "CAMERA",
    )

    smoothing_factor: FloatProperty(
        name="Wi-Fi Smoothing (Lerp)",
        description=(
            "Interpolation factor between the current value and the "
            "incoming value. Higher = smoother but more latency"
        ),
        default=0.8,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )

    drive_location: BoolProperty(
        name="Drive Location",
        description=(
            "Let the incoming stream write the camera's X/Y/Z location every "
            "tick. Turn OFF to leave a hand-keyed or NLA-driven position "
            "animation (and any Delta Transform offset) completely alone and "
            "only take rotation (and lens, if enabled) from the phone"
        ),
        default=True,
    )

    drive_rotation: BoolProperty(
        name="Drive Rotation",
        description=(
            "Let the incoming stream write the camera's rotation every tick. "
            "Turn OFF to leave hand-keyed or NLA-driven rotation alone"
        ),
        default=True,
    )

    is_streaming: BoolProperty(
        name="Is Streaming",
        description="Sockets are open (Connect/Disconnect) -- independent of whether the camera is actually being driven",
        default=False,
    )

    is_live: BoolProperty(
        name="Is Live",
        description=(
            "The connected stream is actually driving the target camera "
            "(and recording is allowed). Requires Is Streaming to already "
            "be on -- toggled by Start/Stop Live Stream, separate from "
            "Connect/Disconnect so you can verify the connection before "
            "anything in the scene moves"
        ),
        default=False,
    )

    phone_connected: BoolProperty(
        name="Phone Connected",
        description="Read-only status: a client is connected on the video/command TCP channel. Updated live by the modal server -- not meant to be set by hand",
        default=False,
    )

    phone_client_count: IntProperty(
        name="Connected Clients",
        description=(
            "Read-only status: number of authenticated clients on the video/command "
            "TCP channel (mirrors video_server.client_count). Updated live by the "
            "modal server -- not meant to be set by hand"
        ),
        default=0,
        min=0,
    )

    pose_receiving: BoolProperty(
        name="Pose Receiving",
        description="Read-only status: a valid pose packet has arrived within the last ~1.5s. Updated live by the modal server -- not meant to be set by hand",
        default=False,
    )

    is_recording: BoolProperty(
        name="Is Recording",
        default=False,
    )

    active_take_name: StringProperty(
        name="Take Name",
        description="Name given to the next baked NLA take",
        default="Take_01",
    )

    take_blend_type: EnumProperty(
        name="Blend Type",
        description=(
            "How the next baked take's NLA strip combines with whatever is "
            "already on the tracks below it"
        ),
        items=[
            (
                "REPLACE",
                "Replace",
                "This take is an alternative to the others -- only it is seen "
                "wherever it plays. Use this for separate takes you'll mute/"
                "solo/pick between in the NLA editor (the default)",
            ),
            (
                "ADD",
                "Add",
                "This take's values are added on top of the strips below it. "
                "Use this to layer a handheld pass on top of an existing "
                "keyframed move -- tap 'Reset Origin' on the phone right "
                "before recording so the added values are small deltas "
                "around zero, not a large absolute offset",
            ),
            (
                "SUBTRACT",
                "Subtract",
                "This take's values are subtracted from the strips below it",
            ),
            (
                "MULTIPLY",
                "Multiply",
                "This take's values multiply the strips below it",
            ),
            (
                "COMBINE",
                "Combine",
                "Blender picks addition, multiplication or quaternion math "
                "per channel, whichever is correct for that channel's type",
            ),
        ],
        default="REPLACE",
    )

    # -- Video / command channel (the "virtual camera monitor") -------------
    video_port: IntProperty(
        name="Video/Command Port",
        description="TCP port the phone connects to for the live monitor and remote record control",
        default=5006,
        min=1,
        max=65535,
    )

    video_enabled: BoolProperty(
        name="Enable Live Monitor",
        description=(
            "Stream the target camera's view back to the phone as JPEG frames. "
            "Adds render load on top of the pose stream -- leave off if you "
            "only need tracking, no monitor"
        ),
        default=False,
    )

    video_fps: IntProperty(
        name="Monitor Frame Rate",
        description="How often a frame is captured and sent. Higher costs more render time per second",
        default=15,
        min=5,
        max=30,
    )

    remote_control_enabled: BoolProperty(
        name="Allow Remote Record Control",
        description="Let the phone's Record button start/stop recording here, in addition to this panel",
        default=True,
    )

    pairing_token: StringProperty(
        name="Pairing Token",
        description="Included in the QR code; a device must have scanned it to control recording or see the monitor",
        default="",
    )


# ------------------------------------------------------------------------
# Modal Operator: UDP Streaming Server
# ------------------------------------------------------------------------
class WM_OT_camera_stream_server(Operator):
    """Non-blocking UDP modal server that drives the target camera live."""

    bl_idname = "wm.camera_stream_server"
    bl_label = "Toggle Camera Live Stream"
    bl_description = "Start/stop the non-blocking UDP camera stream server"

    _timer = None
    sock = None

    # Sequencing / out-of-order protection.
    last_packet_id = -1

    # Wall-clock time (time.time()) a valid pose packet was last parsed --
    # drives the "Pose Receiving" status independent of whether the stream
    # is actually live, so Connect alone can show whether data is arriving.
    last_packet_wall_time = 0.0
    POSE_TIMEOUT_S = 1.5

    # Cached "current" values used as the base for Lerp smoothing so that
    # jitter is filtered even across missed / dropped packets.
    current_loc = (0.0, 0.0, 0.0)
    current_rot_deg = (0.0, 0.0, 0.0)
    current_focal = 50.0
    current_sensor_width = 36.0

    # Video/command channel. Opened on every Connect (see invoke()) so
    # pairing and remote record control work whether or not the JPEG
    # monitor itself is enabled; stays None only if the port failed to bind.
    video_server = None
    _video_tick_counter = 0

    # Tracks what STATE line was last broadcast and how many clients were
    # attached last tick, purely to decide when to (re-)send STATE -- see
    # _drain_commands. Both live on the main thread; _accept_loop and
    # _client_reader (video_channel.py) run on background threads and must
    # never touch these or any bpy/settings.* property directly.
    _last_sent_state = None
    _last_client_count = 0

    def invoke(self, context, event):
        settings = context.scene.cam_link_pro

        if settings.is_streaming:
            # Already streaming -> this invocation means "stop".
            settings.is_streaming = False
            return {"FINISHED"}

        if not self._open_socket(settings):
            self.report({"ERROR"}, "Failed to open UDP socket. See console.")
            settings.is_streaming = False
            return {"CANCELLED"}

        cam_obj = self._resolve_camera(context, settings)
        if cam_obj is not None:
            self.current_loc = tuple(cam_obj.location)
            self.current_rot_deg = tuple(math.degrees(a) for a in cam_obj.rotation_euler)
            self.current_focal = cam_obj.data.lens
            self.current_sensor_width = cam_obj.data.sensor_width

        self.last_packet_id = -1
        self.last_packet_wall_time = 0.0
        self._last_sent_state = None
        self._last_client_count = 0

        # A fresh Connect always starts not-live and with clean status,
        # even if these were left on from a previous session somehow.
        settings.is_live = False
        settings.phone_connected = False
        settings.phone_client_count = 0
        settings.pose_receiving = False

        # Always opens, independent of video_enabled: this is the same TCP
        # channel the phone uses for pairing and remote record control, not
        # just the JPEG monitor feed, so a phone should be able to pair and
        # start/stop recording even with "Enable Live Monitor" off. Only
        # frame *capture* (_maybe_capture_frame, gated on video_enabled in
        # modal()) is actually tied to that checkbox.
        if not settings.pairing_token:
            settings.pairing_token = video_channel.new_pairing_token()
        try:
            self.video_server = video_channel.VideoCommandServer(
                settings.ip_address, settings.video_port, settings.pairing_token
            )
            self.video_server.start()
        except OSError as exc:
            self.report({"ERROR"}, f"Video/command port failed to open: {exc}")
            self.video_server = None
            # Pose streaming still works without the monitor -- don't
            # abort the whole operator over this.
        self._video_tick_counter = 0

        wm = context.window_manager
        self._timer = wm.event_timer_add(TIMER_INTERVAL, window=context.window)
        wm.modal_handler_add(self)

        settings.is_streaming = True
        self.report({"INFO"}, f"Camera Link streaming on {settings.ip_address}:{settings.port}")
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        settings = context.scene.cam_link_pro

        # Allow the UI toggle to request a stop even without an ESC/click on
        # this operator directly (e.g. user unchecks the panel toggle).
        if not settings.is_streaming:
            return self._finish(context)

        if event.type == "TIMER":
            try:
                self._drain_and_apply(context, settings)
            except Exception as exc:  # noqa: BLE001 - never let the modal crash Blender
                self.report({"ERROR"}, f"Camera Link stream error: {exc}")
                settings.is_streaming = False
                return self._finish(context)

            if self.video_server is not None:
                try:
                    self._drain_commands(context, settings)
                    if settings.video_enabled:
                        self._maybe_capture_frame(context, settings)
                except Exception as exc:  # noqa: BLE001 - monitor issues must not kill tracking
                    self.report({"WARNING"}, f"Video monitor error: {exc}")
                    settings.video_enabled = False

        return {"PASS_THROUGH"}

    def cancel(self, context):
        self._finish(context)

    # -- Internal helpers -------------------------------------------------

    def _open_socket(self, settings):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setblocking(False)
            self.sock.bind((settings.ip_address, settings.port))
            return True
        except OSError as exc:
            print(f"[Cam Link Pro] Socket bind failed: {exc}")
            self.sock = None
            return False

    def _resolve_camera(self, context, settings):
        """Returns the target camera, defaulting to the active scene camera."""
        if settings.target_camera is not None and settings.target_camera.type == "CAMERA":
            return settings.target_camera
        return context.scene.camera

    def _drain_and_apply(self, context, settings):
        """Drains the UDP buffer fully, keeping only the newest valid packet."""
        newest_payload = None

        while True:
            try:
                data, _addr = self.sock.recvfrom(4096)
            except (BlockingIOError, socket.error):
                # No more datagrams currently queued -- buffer is drained.
                break

            if data:
                newest_payload = data

        parsed = self._parse_packet(newest_payload) if newest_payload is not None else None

        # "Pose Receiving" is a decaying status, not a per-tick flag: it
        # has to stay True across the gaps between individual packets (the
        # phone sends at 24-60Hz, well under the 60Hz tick rate) and only
        # flip False once packets genuinely stop for a while. Updated
        # whether or not the stream is live, so Connect alone can already
        # show whether data is arriving before anything drives the camera.
        if parsed is not None:
            self.last_packet_wall_time = time.time()
        settings.pose_receiving = (time.time() - self.last_packet_wall_time) < self.POSE_TIMEOUT_S

        if parsed is None:
            return

        seq_id, x, y, z, rx, ry, rz, focal_length, sensor_width = parsed

        # Out-of-order packet safety: drop anything older than what we've
        # already applied so late Wi-Fi packets can't cause backward hitches.
        if seq_id < self.last_packet_id:
            return
        self.last_packet_id = seq_id

        cam_obj = self._resolve_camera(context, settings)
        if cam_obj is None or cam_obj.type != "CAMERA":
            return

        smoothing = settings.smoothing_factor

        # Lerp filtering: current_val = current*smoothing + incoming*(1-smoothing)
        new_loc = tuple(
            self.current_loc[i] * smoothing + (x, y, z)[i] * (1.0 - smoothing)
            for i in range(3)
        )
        new_rot_deg = tuple(
            self.current_rot_deg[i] * smoothing + (rx, ry, rz)[i] * (1.0 - smoothing)
            for i in range(3)
        )
        new_focal = self.current_focal * smoothing + focal_length * (1.0 - smoothing)
        new_sensor_width = (
            self.current_sensor_width * smoothing + sensor_width * (1.0 - smoothing)
        )

        self.current_loc = new_loc
        self.current_rot_deg = new_rot_deg
        self.current_focal = new_focal
        self.current_sensor_width = new_sensor_width

        # Smoothing state (current_loc etc.) keeps updating above even
        # while not live, so it's already settled to the incoming data by
        # the time Start Live Stream is pressed -- no catch-up jump from a
        # stale baseline. But the camera itself is only ever touched while
        # actually live: Connect alone must never move anything.
        if not settings.is_live:
            return

        # Apply spatial transform (convert degrees -> radians for Blender).
        # Gated per-channel so a hand-keyed or NLA-driven position (or
        # rotation) animation can be left alone -- e.g. rig the camera's
        # move yourself and only take rotation from the phone, or vice
        # versa. Delta Transform is never touched here either way, so a
        # fixed offset stored there always survives regardless of these
        # toggles.
        if settings.drive_location:
            cam_obj.location.x = new_loc[0]
            cam_obj.location.y = new_loc[1]
            cam_obj.location.z = new_loc[2]
        if settings.drive_rotation:
            cam_obj.rotation_euler = tuple(math.radians(a) for a in new_rot_deg)

        # Apply optical lens properties.
        cam_obj.data.lens = new_focal
        cam_obj.data.sensor_width = new_sensor_width

        # Recording: bake the smoothed live values straight to keyframes.
        if settings.is_recording:
            self._insert_keyframes(context, cam_obj, settings)

    def _drain_commands(self, context, settings):
        """Applies Start/Stop commands that arrived from the phone. Mirrors
        exactly what ticking the panel's own checkbox does, so the two
        controls can never disagree about what state Blender is in."""
        client_count = self.video_server.client_count
        settings.phone_connected = client_count > 0
        settings.phone_client_count = client_count

        # STATE <token> (CONNECTED / LIVE_NODATA / LIVE) is sent on the
        # main thread here -- never from _accept_loop/_client_reader in
        # video_channel.py, which run on background threads and can't
        # safely touch settings.*/bpy. Two independent reasons to
        # (re-)send: the token itself changed, or a client just joined and
        # needs to learn the current state immediately rather than wait
        # for the next transition (e.g. reconnecting mid-session to an
        # already-live Blender). No line is sent for "disconnected" --
        # the TCP connection dropping already conveys that on its own.
        new_client_joined = client_count > self._last_client_count
        self._last_client_count = client_count

        token = _live_state_token(settings)
        if token is not None and (token != self._last_sent_state or new_client_joined):
            self.video_server.send_status(f"STATE {token}")
            self._last_sent_state = token

        for cmd in self.video_server.drain_commands():
            if cmd == "PING":
                self.video_server.send_status("PONG")

            elif cmd == "START":
                if not settings.remote_control_enabled:
                    # The phone has no other way to learn why its Record
                    # tap did nothing, so it gets a status line instead of
                    # pure silence -- a new addition to the wire contract,
                    # coordinated with the phone app (see llms.txt).
                    self.video_server.send_status("REMOTE_DISABLED")
                    continue
                if not settings.is_live:
                    # Recording without the stream being live would just
                    # bake whatever the camera's non-live transform already
                    # is, frame after frame -- not what a phone-side Record
                    # tap means. Silently ignored (remote control itself is
                    # fine, the phone just needs to wait for Live).
                    continue
                settings.is_recording = True
                self.video_server.send_status("REC_ON")

            elif cmd == "STOP":
                if not settings.remote_control_enabled:
                    self.video_server.send_status("REMOTE_DISABLED")
                    continue
                if settings.is_recording:
                    settings.is_recording = False
                    self.video_server.send_status("REC_OFF")
                    # Deliberately does NOT auto-bake to NLA here. Stopping
                    # from the phone now behaves exactly like toggling
                    # "Record Motion" off in the panel: the take stays as a
                    # plain active Action until you click "Bake & Mix to
                    # NLA" yourself, so you always get to check/change Take
                    # Name and Blend Type first -- baking immediately on
                    # remote stop meant a phone-triggered take got pushed
                    # into an NLA strip under whatever name/blend type
                    # happened to already be set, with no chance to review.

    def _maybe_capture_frame(self, context, settings):
        """Throttles video capture to settings.video_fps independent of the
        60Hz pose loop -- the render is the expensive part, not the pose math,
        so this is the knob that actually controls how much load streaming
        adds to Blender's own responsiveness."""
        self._video_tick_counter += 1
        ticks_per_frame = max(1, round(60.0 / max(1, settings.video_fps)))
        if self._video_tick_counter < ticks_per_frame:
            return
        self._video_tick_counter = 0

        if self.video_server.client_count == 0:
            return  # no one watching -- don't spend render time on it

        cam_obj = self._resolve_camera(context, settings)
        if cam_obj is None or cam_obj.type != "CAMERA":
            return

        try:
            jpeg = video_channel.capture_frame_jpeg(context, cam_obj)
        except RuntimeError as exc:
            # Most likely "no 3D Viewport area is open" -- report once via
            # the console rather than spamming an operator error every tick.
            print(f"[Cam Link Pro] Frame capture skipped: {exc}")
            return

        self.video_server.push_frame(jpeg)

    @staticmethod
    def _parse_packet(raw_bytes):
        """Parses a raw UDP payload into its 9 numeric fields, or None."""
        try:
            text = raw_bytes.decode("utf-8").strip()
            fields = text.split(",")
            if len(fields) != EXPECTED_FIELD_COUNT:
                return None

            seq_id = int(float(fields[0]))
            x, y, z, rx, ry, rz, focal_length, sensor_width = (
                float(v) for v in fields[1:]
            )
            return seq_id, x, y, z, rx, ry, rz, focal_length, sensor_width
        except (UnicodeDecodeError, ValueError, IndexError):
            return None

    @staticmethod
    def _insert_keyframes(context, cam_obj, settings):
        scene = context.scene
        frame = scene.frame_current

        # Only key the channels this session is actually driving -- if
        # Drive Location is off, a hand-keyed/NLA position track is left
        # completely untouched, even while recording a rotation-only take.
        if settings.drive_location:
            cam_obj.keyframe_insert(data_path="location", frame=frame)
        if settings.drive_rotation:
            cam_obj.keyframe_insert(data_path="rotation_euler", frame=frame)
        cam_obj.data.keyframe_insert(data_path="lens", frame=frame)
        cam_obj.data.keyframe_insert(data_path="sensor_width", frame=frame)

        # Advance the timeline so recording moves forward in real time.
        scene.frame_current = frame + 1

    def _finish(self, context):
        settings = context.scene.cam_link_pro

        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None

        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

        if self.video_server is not None:
            self.video_server.stop()
            self.video_server = None

        # Restore safe UI states so the panel never gets stuck mid-stream.
        settings.is_streaming = False
        settings.is_live = False
        settings.is_recording = False
        settings.phone_connected = False
        settings.phone_client_count = 0
        settings.pose_receiving = False

        return {"FINISHED"}


# ------------------------------------------------------------------------
# Operator: Start/Stop Live (separate from Connect/Disconnect)
# ------------------------------------------------------------------------
class WM_OT_camlink_toggle_live(Operator):
    """Starts or stops driving the target camera from an already-open
    connection. Deliberately does not touch any socket -- Connect/Disconnect
    (WM_OT_camera_stream_server) owns those. This only flips settings.is_live,
    which is what the running modal server's own tick checks before it ever
    writes to the camera or allows recording, so verifying a connection (via
    Connect) never has side effects on the scene until this is used."""

    bl_idname = "wm.cam_link_toggle_live"
    bl_label = "Toggle Live"
    bl_description = "Start or stop applying the connected stream to the target camera"

    def execute(self, context):
        settings = context.scene.cam_link_pro

        if not settings.is_streaming:
            self.report({"ERROR"}, "Not connected -- click Connect first.")
            return {"CANCELLED"}

        settings.is_live = not settings.is_live
        if not settings.is_live:
            # Recording only ever makes sense while live -- see
            # _drain_commands' own guard on the phone-triggered path.
            settings.is_recording = False

        return {"FINISHED"}


# ------------------------------------------------------------------------
# Operator: Bake & Mix recorded Action into NLA
# ------------------------------------------------------------------------
class ANIM_OT_bake_take_to_nla(Operator):
    """Pushes the current recorded Action down into a new, named NLA track."""

    bl_idname = "anim.cam_link_bake_to_nla"
    bl_label = "Bake & Mix to NLA"
    bl_description = "Push the current recorded take into a new NLA track for mixing"

    def execute(self, context):
        settings = context.scene.cam_link_pro
        cam_obj = settings.target_camera or context.scene.camera

        if cam_obj is None or cam_obj.type != "CAMERA":
            self.report({"ERROR"}, "No valid target camera set.")
            return {"CANCELLED"}

        take_name = settings.active_take_name.strip() or "Take_01"
        blend_type = settings.take_blend_type

        baked_object = self._push_down(cam_obj, take_name, is_object_level=True, blend_type=blend_type)

        baked_data = False
        if cam_obj.data is not None:
            baked_data = self._push_down(
                cam_obj.data, take_name + "_Lens", is_object_level=False, blend_type=blend_type
            )

        if not baked_object and not baked_data:
            self.report({"WARNING"}, "No active recorded action found to bake.")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Baked take '{take_name}' into NLA track(s).")
        return {"FINISHED"}

    @staticmethod
    def _push_down(id_data, take_name, is_object_level, blend_type="REPLACE"):
        """
        Renames the active Action on id_data.animation_data to take_name,
        clears it from the active slot, and pushes it into a brand-new NLA
        track as a strip, so each take can be muted, soloed, sliced or
        cross-faded against the others in the NLA editor.
        """
        anim_data = id_data.animation_data
        if anim_data is None or anim_data.action is None:
            return False

        action = anim_data.action
        action.name = take_name if is_object_level else f"{take_name}_Data"

        # Create a fresh NLA track so each take lives on its own layer.
        track = anim_data.nla_tracks.new()
        track.name = take_name

        start_frame = int(action.frame_range[0])
        strip = track.strips.new(name=take_name, start=start_frame, action=action)

        # Default is REPLACE: takes are alternatives to one another, not
        # layers to be summed -- two stacked position strips under ADD would
        # put the camera at twice its distance from the set. REPLACE lets you
        # mute and solo takes in the NLA editor and see exactly one of them
        # at a time. Pick ADD (via the panel's Blend Type dropdown) when you
        # do want to layer -- a handheld shake pass over a clean dolly move
        # is the case where that earns its keep. See that property's
        # description for the rest of the enum's meanings.
        strip.blend_type = blend_type

        # NOTHING keeps a strip silent outside its own frame range, so an
        # earlier take doesn't hold its last pose over the top of a later one.
        strip.extrapolation = "NOTHING"

        # Blender 4.4 and later store animation in action "slots": one action
        # can carry channels for several datablocks, so a strip has to be told
        # which slot it is playing. Older versions have no such field, hence
        # the guard.
        if hasattr(strip, "action_slot") and strip.action_slot is None:
            slots = getattr(action, "slots", None)
            if slots:
                strip.action_slot = slots[0]

        # Clear the active action so the viewport is ready for a new take
        # without editing on top of the one we just baked down.
        anim_data.action = None

        return True


class ANIM_OT_camlink_setup_delta_rig(Operator):
    """Splits a hand-made (or otherwise pre-existing) camera animation off
    onto a new parent Empty, freeing the camera object itself for Cam Link
    Pro to drive at full 6DOF.

    This is the fix for a real limitation: the live stream writes absolute
    location/rotation values straight onto the target camera every tick,
    which always wins over whatever an Action or NLA stack on that SAME
    object evaluates to -- there is no live "add" mode, because "add" is an
    NLA/playback-time concept and the live write happens before any of that
    stack is considered. Splitting the base motion onto a parent object
    sidesteps the conflict entirely: the parent's animation (however it's
    authored) evaluates independently every frame, and the camera's locally
    driven transform is composed on top of it by ordinary parent/child
    math -- live, in the monitor preview, and while recording, with no
    fighting and no glitch.
    """

    bl_idname = "anim.cam_link_setup_delta_rig"
    bl_label = "Set Up Delta Rig"
    bl_description = (
        "Move the target camera's existing animation onto a new parent Empty "
        "and reset the camera to identity, so Cam Link Pro's live/recorded "
        "6DOF motion rides on top of that base animation instead of "
        "overwriting it"
    )

    def execute(self, context):
        settings = context.scene.cam_link_pro
        cam_obj = settings.target_camera or context.scene.camera

        if cam_obj is None or cam_obj.type != "CAMERA":
            self.report({"ERROR"}, "No valid target camera set.")
            return {"CANCELLED"}

        if cam_obj.parent is not None:
            self.report(
                {"ERROR"},
                f"'{cam_obj.name}' already has a parent ('{cam_obj.parent.name}'). "
                "Set up the rig by hand so an existing hierarchy isn't disturbed.",
            )
            return {"CANCELLED"}

        if cam_obj.animation_data is not None and len(cam_obj.animation_data.nla_tracks) > 0:
            self.report(
                {"ERROR"},
                f"'{cam_obj.name}' already has NLA tracks. Move the base take(s) onto a "
                "new parent Empty by hand in the NLA editor rather than risk this "
                "operator reshuffling strips automatically.",
            )
            return {"CANCELLED"}

        # The rig starts exactly where the camera currently is, so nothing
        # visibly jumps the moment this operator runs.
        rig = bpy.data.objects.new(f"{cam_obj.name}_BaseRig", None)
        rig.empty_display_type = "PLAIN_AXES"
        rig.empty_display_size = 0.5
        context.collection.objects.link(rig)
        rig.matrix_world = cam_obj.matrix_world.copy()

        # Move the existing active action (the hand-made base animation)
        # from the camera to the rig -- the camera keeps its lens/sensor
        # data untouched, only its object-level transform animation moves.
        moved_action = False
        if cam_obj.animation_data is not None and cam_obj.animation_data.action is not None:
            action = cam_obj.animation_data.action
            action_slot = getattr(cam_obj.animation_data, "action_slot", None)
            rig.animation_data_create()
            rig.animation_data.action = action
            # Blender 4.4+ "layered" actions key channels by slot, not by
            # owning ID -- carrying the action over without also carrying
            # its slot leaves the rig with .action set but nothing actually
            # evaluating (the base motion would silently freeze). Reuse the
            # exact slot the camera was using; that's safe because the
            # camera's own action reference is cleared right below, so
            # nothing else is left pointing at it.
            if hasattr(rig.animation_data, "action_slot"):
                if action_slot is not None:
                    rig.animation_data.action_slot = action_slot
                else:
                    slots = getattr(action, "slots", None)
                    if slots:
                        rig.animation_data.action_slot = slots[0]
            cam_obj.animation_data.action = None
            moved_action = True

        # The camera itself becomes the live-driven delta: identity local
        # transform relative to the rig, so Cam Link Pro's absolute writes
        # are effectively "on top of the rig" the instant it starts driving.
        cam_obj.location = (0.0, 0.0, 0.0)
        cam_obj.rotation_euler = (0.0, 0.0, 0.0)
        cam_obj.parent = rig
        # A fresh object's matrix_parent_inverse is already identity, so no
        # explicit correction is needed here for a camera that had no prior
        # parent (checked above).

        self.report(
            {"INFO"},
            f"Created '{rig.name}'."
            + (" Base animation moved onto it." if moved_action else " No base animation found to move.")
            + f" '{cam_obj.name}' reset to (0,0,0) local and ready -- turn on both Drive "
            "Location and Drive Rotation, tap Reset Origin on the phone right before "
            "recording, and record as usual.",
        )
        return {"FINISHED"}


class WM_OT_camlink_new_token(Operator):
    """Generates a fresh pairing token, invalidating any QR code already shown."""

    bl_idname = "wm.cam_link_new_token"
    bl_label = "New Pairing Token"
    bl_description = "Generate a new pairing token (any previously scanned QR stops working)"

    def execute(self, context):
        settings = context.scene.cam_link_pro
        settings.pairing_token = video_channel.new_pairing_token()
        self.report({"INFO"}, "New pairing token generated -- re-scan the QR on the phone")
        return {"FINISHED"}


class WM_OT_camlink_show_qr(Operator):
    """Writes the pairing QR to a temp PNG and opens it with the OS's default
    image viewer -- simpler and more reliable than drawing it inline in the
    Blender UI, and it's full-screen, which is easier to scan from anyway."""

    bl_idname = "wm.cam_link_show_qr"
    bl_label = "Show Pairing QR"
    bl_description = "Open the phone-pairing QR code in your image viewer"

    def execute(self, context):
        import os
        import tempfile

        settings = context.scene.cam_link_pro
        if not settings.pairing_token:
            settings.pairing_token = video_channel.new_pairing_token()

        ip = video_channel.local_lan_ip()
        try:
            png = video_channel.pairing_qr_png_bytes(
                ip, settings.port, settings.video_port, settings.pairing_token, scale=10
            )
        except ValueError as exc:
            self.report({"ERROR"}, f"Could not build pairing QR: {exc}")
            return {"CANCELLED"}

        path = os.path.join(tempfile.gettempdir(), "cam_link_pro_pairing_qr.png")
        with open(path, "wb") as fh:
            fh.write(png)

        try:
            bpy.ops.wm.path_open(filepath=path)
        except Exception as exc:  # noqa: BLE001 - OS-dependent, never fatal
            self.report(
                {"WARNING"},
                f"Saved to {path} but couldn't auto-open it ({exc}). Open it manually.",
            )
            return {"FINISHED"}

        self.report({"INFO"}, f"Pairing QR for {ip}:{settings.port}/{settings.video_port}")
        return {"FINISHED"}


# Custom-icon preview collection backing the in-Blender QR popup below. Kept
# as a module-level singleton (rather than on the operator instance) because
# Blender frees preview collections independently of operator lifetime --
# this must be torn down explicitly in unregister() or it leaks.
_qr_preview_collection = None


def _get_qr_preview_collection():
    global _qr_preview_collection
    if _qr_preview_collection is None:
        _qr_preview_collection = bpy.utils.previews.new()
    return _qr_preview_collection


class WM_OT_camlink_show_qr_popup(Operator):
    """Shows the pairing QR as a popup inside Blender itself, for scanning
    without alt-tabbing out to the OS image viewer. The OS-viewer version
    (WM_OT_camlink_show_qr) stays the primary/most-reliable option since
    Blender's preview icons cap out at a fairly low resolution."""

    bl_idname = "wm.cam_link_show_qr_popup"
    bl_label = "Show QR in Blender"
    bl_description = "Show the phone-pairing QR code in a popup inside Blender"

    _ip = ""
    _port = 0
    _video_port = 0

    def execute(self, context):
        return {"FINISHED"}

    def invoke(self, context, event):
        import os
        import tempfile

        settings = context.scene.cam_link_pro
        if not settings.pairing_token:
            settings.pairing_token = video_channel.new_pairing_token()

        ip = video_channel.local_lan_ip()
        try:
            png = video_channel.pairing_qr_png_bytes(
                ip, settings.port, settings.video_port, settings.pairing_token, scale=8
            )
        except ValueError as exc:
            self.report({"ERROR"}, f"Could not build pairing QR: {exc}")
            return {"CANCELLED"}

        path = os.path.join(tempfile.gettempdir(), "cam_link_pro_pairing_qr_popup.png")
        with open(path, "wb") as fh:
            fh.write(png)

        pcoll = _get_qr_preview_collection()
        # ImagePreviewCollection.load() raises KeyError on a name that's
        # already in the collection regardless of force_reload -- that flag
        # only controls whether Blender re-reads pixels for an existing key,
        # it doesn't bypass the "already registered" guard. Since this popup
        # can be reopened after the token (and so the QR image) changed, the
        # stale entry has to be dropped explicitly first.
        if "qr" in pcoll:
            del pcoll["qr"]
        pcoll.load("qr", path, "IMAGE", force_reload=True)

        self._ip = ip
        self._port = settings.port
        self._video_port = settings.video_port

        return context.window_manager.invoke_popup(self, width=300)

    def draw(self, context):
        layout = self.layout
        pcoll = _get_qr_preview_collection()
        qr = pcoll.get("qr")
        if qr is not None:
            layout.template_icon(icon_value=qr.icon_id, scale=15)

        settings = context.scene.cam_link_pro
        layout.label(text=f"{self._ip}:{self._port}/{self._video_port}")
        layout.label(text=f"Token: {settings.pairing_token}")


def _open_bundled_doc(operator, filename):
    """Shared by the two doc-opening operators below: resolves a file
    sitting next to this add-on's own __init__.py (so it's whatever
    shipped in the installed/zipped package, not a stray copy elsewhere)
    and opens it with the OS's default handler -- a separate window/tab
    outside Blender, same approach already used for the pairing QR image."""
    import os

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(path):
        operator.report({"ERROR"}, f"{filename} not found next to the add-on ({path}).")
        return {"CANCELLED"}

    try:
        bpy.ops.wm.path_open(filepath=path)
    except Exception as exc:  # noqa: BLE001 - OS-dependent, never fatal
        operator.report({"WARNING"}, f"Couldn't auto-open it ({exc}). File is at: {path}")
        return {"FINISHED"}

    operator.report({"INFO"}, f"Opened {path}")
    return {"FINISHED"}


class WM_OT_camlink_open_readme(Operator):
    """Opens this add-on's README.md -- full documentation and a How To
    Use walkthrough -- with the OS's default markdown/text viewer."""

    bl_idname = "wm.cam_link_open_readme"
    bl_label = "Open README / How To Use"
    bl_description = "Open this add-on's README.md (full docs + how to use) in your default markdown/text viewer"

    def execute(self, context):
        return _open_bundled_doc(self, "README.md")


class WM_OT_camlink_open_llms_txt(Operator):
    """Opens this add-on's llms.txt -- a compact, structured reference
    written for an AI assistant helping with this add-on or the
    companion phone app's source, not for human reading."""

    bl_idname = "wm.cam_link_open_llms_txt"
    bl_label = "Open llms.txt (for AI Assistants)"
    bl_description = (
        "Open this add-on's llms.txt -- a compact reference meant to be handed to an "
        "AI assistant helping you with this add-on, not for reading yourself"
    )

    def execute(self, context):
        return _open_bundled_doc(self, "llms.txt")


# ------------------------------------------------------------------------
# UI Panel
# ------------------------------------------------------------------------
class VIEW3D_PT_cam_link_pro(Panel):
    """Sidebar panel exposing Cam Link Pro's network, tuning and capture UI."""

    bl_label = "Cam Link Pro"
    bl_idname = "VIEW3D_PT_cam_link_pro"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cam Link Pro"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.cam_link_pro

        # --- Section 1: Network Config -----------------------------------
        box = layout.box()
        box.label(text="Network Config", icon="URL")
        box.prop(settings, "ip_address")
        box.prop(settings, "port")

        row = box.row()
        row.prop(settings, "target_camera")
        if settings.target_camera is None:
            box.label(
                text=f"Defaulting to: {context.scene.camera.name if context.scene.camera else 'None'}",
                icon="INFO",
            )

        # --- Section 2: Data Tuning ---------------------------------------
        box = layout.box()
        box.label(text="Data Tuning", icon="MOD_SMOOTH")
        box.prop(settings, "smoothing_factor", slider=True)

        drive_row = box.row(align=True)
        drive_row.prop(settings, "drive_location", toggle=True)
        drive_row.prop(settings, "drive_rotation", toggle=True)
        if not settings.drive_location or not settings.drive_rotation:
            box.label(
                text="Untoggled channel(s) stay on their existing animation",
                icon="INFO",
            )

        rig_row = box.row()
        rig_row.operator(ANIM_OT_camlink_setup_delta_rig.bl_idname, icon="CON_CHILDOF")
        box.label(
            text="Want full 6DOF layered on an existing move? Use this once first.",
            icon="INFO",
        )

        # --- Section 3: Live Stream Control --------------------------------
        box = layout.box()
        box.label(text="Live Stream Control", icon="ANTIALIASED")

        # Red/yellow/green at-a-glance health, on top of the plain state
        # labels below: red means not connected at all; yellow means
        # connected but either not live yet, or live without pose data
        # actually arriving (a phone that stopped sending, tracking lost,
        # etc.); green means live and genuinely receiving data right now.
        # Same _live_state_token() the STATE wire line to the phone uses,
        # so the panel and the phone can never disagree about this.
        _STATUS_DISPLAY = {
            None: ("COLOR_RED", "Disconnected"),
            "CONNECTED": ("ERROR", "Connected"),
            "LIVE_NODATA": ("ERROR", "LIVE -- no pose data"),
            "LIVE": ("COLOR_GREEN", "LIVE"),
        }
        status_icon, status_text = _STATUS_DISPLAY[_live_state_token(settings)]

        status_row = box.row()
        status_row.label(text=f"Status: {status_text}", icon=status_icon)

        if settings.is_streaming:
            detail_row = box.row(align=True)
            detail_row.label(
                text="Pose: Receiving" if settings.pose_receiving else "Pose: No data",
                icon="CHECKMARK" if settings.pose_receiving else "CANCEL",
            )
            # Shown regardless of video_enabled: this is the same
            # video/command TCP connection either way, and a user may want
            # to know a phone is connected even with the monitor off.
            detail_row.label(
                text="Phone: Connected" if settings.phone_connected else "Phone: Not connected",
                icon="CHECKMARK" if settings.phone_connected else "CANCEL",
            )

        connect_row = box.row()
        connect_label = "Disconnect" if settings.is_streaming else "Connect"
        connect_icon = "UNLINKED" if settings.is_streaming else "LINKED"
        connect_row.operator(
            WM_OT_camera_stream_server.bl_idname,
            text=connect_label,
            icon=connect_icon,
            depress=settings.is_streaming,
        )

        live_row = box.row()
        live_row.enabled = settings.is_streaming
        live_label = "Stop Live Stream" if settings.is_live else "Start Live Stream"
        live_icon = "PAUSE" if settings.is_live else "PLAY"
        live_row.operator(
            WM_OT_camlink_toggle_live.bl_idname,
            text=live_label,
            icon=live_icon,
            depress=settings.is_live,
        )

        # --- Section 4: Capture & Motion Management ------------------------
        box = layout.box()
        box.label(text="Capture & Motion Management", icon="REC")
        box.prop(settings, "active_take_name")
        box.prop(settings, "take_blend_type")

        record_row = box.row()
        record_row.enabled = settings.is_live
        record_row.prop(
            settings,
            "is_recording",
            text="Recording..." if settings.is_recording else "Record Motion",
            toggle=True,
            icon="REC",
        )

        bake_row = box.row()
        bake_row.operator(ANIM_OT_bake_take_to_nla.bl_idname, icon="NLA_PUSHDOWN")

        # --- Section 5: Phone Pairing / Live Monitor ------------------------
        box = layout.box()
        box.label(text="Phone Pairing & Monitor", icon="CAMERA_DATA")
        box.prop(settings, "video_port")
        box.prop(settings, "video_enabled")
        if settings.video_enabled:
            box.prop(settings, "video_fps", slider=True)
        box.prop(settings, "remote_control_enabled")

        # A blank token would just show an empty field the user has to
        # remember to click "New Pairing Token" for -- generate one up
        # front instead, same call the refresh button already makes.
        if not settings.pairing_token:
            settings.pairing_token = video_channel.new_pairing_token()

        token_row = box.row(align=True)
        token_row.prop(settings, "pairing_token", text="Token")
        token_row.operator(WM_OT_camlink_new_token.bl_idname, text="", icon="FILE_REFRESH")

        qr_row = box.row(align=True)
        qr_row.operator(WM_OT_camlink_show_qr.bl_idname, icon="LIGHT")
        qr_row.operator(WM_OT_camlink_show_qr_popup.bl_idname, text="", icon="IMAGE_DATA")

        if settings.video_enabled:
            box.label(
                text=f"Clients connected: {settings.phone_client_count}",
                icon="CHECKMARK" if settings.phone_client_count else "CANCEL",
            )
            if settings.phone_client_count > 1:
                box.label(
                    text="Multiple devices connected -- generate a new token if unexpected",
                    icon="ERROR",
                )

        # --- Section 6: Help & Docs -----------------------------------------
        box = layout.box()
        box.label(text="Help & Docs", icon="HELP")
        box.operator(WM_OT_camlink_open_readme.bl_idname, icon="TEXT")
        box.operator(WM_OT_camlink_open_llms_txt.bl_idname, icon="COPY_ID")


# ------------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------------
classes = (
    CamLinkProSettings,
    WM_OT_camera_stream_server,
    WM_OT_camlink_toggle_live,
    ANIM_OT_bake_take_to_nla,
    ANIM_OT_camlink_setup_delta_rig,
    WM_OT_camlink_new_token,
    WM_OT_camlink_show_qr,
    WM_OT_camlink_show_qr_popup,
    WM_OT_camlink_open_readme,
    WM_OT_camlink_open_llms_txt,
    VIEW3D_PT_cam_link_pro,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.cam_link_pro = PointerProperty(type=CamLinkProSettings)


def unregister():
    # Safety: if the modal operator is somehow still running when the
    # add-on is disabled, there is no live Python reference to stop it from
    # here (Blender's own operator lifecycle handles cleanup on scene/window
    # teardown), so we simply make sure the property group is removed
    # cleanly and classes unregister without raising.
    del bpy.types.Scene.cam_link_pro

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    global _qr_preview_collection
    if _qr_preview_collection is not None:
        bpy.utils.previews.remove(_qr_preview_collection)
        _qr_preview_collection = None


if __name__ == "__main__":
    register()


# ==============================================================================
# EXTERNAL TEST CLIENT (reference only -- NOT part of the add-on registration)
# ==============================================================================
#
# Run this on your phone (e.g. via a Python-on-Android environment) or on a
# PC on the same LAN to sanity-check the Blender receiver without needing
# the Unity app running yet. It sends a slowly orbiting, slowly zooming
# camera move so you can confirm the pipeline end-to-end.
#
# import socket
# import time
# import math
#
# BLENDER_HOST = "192.168.1.42"   # <-- replace with your Blender machine's LAN IP
# BLENDER_PORT = 5005              # <-- must match the port set in Cam Link Pro
#
# def run_test_client():
#     sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
#     sock.setblocking(False)
#
#     sequence_id = 0
#     start_time = time.time()
#
#     try:
#         while True:
#             t = time.time() - start_time
#
#             # Simple orbiting camera path.
#             x = 3.0 * math.cos(t * 0.5)
#             y = 3.0 * math.sin(t * 0.5)
#             z = 1.5
#
#             rx = 75.0
#             ry = 0.0
#             rz = math.degrees(t * 0.5) + 90.0
#
#             # Slowly breathing zoom between 24mm and 85mm.
#             focal_length = 54.5 + 30.5 * math.sin(t * 0.3)
#             sensor_width = 36.0
#
#             payload = (
#                 f"{sequence_id},{x:.5f},{y:.5f},{z:.5f},"
#                 f"{rx:.5f},{ry:.5f},{rz:.5f},"
#                 f"{focal_length:.5f},{sensor_width:.5f}"
#             )
#
#             sock.sendto(payload.encode("utf-8"), (BLENDER_HOST, BLENDER_PORT))
#
#             sequence_id += 1
#             time.sleep(1.0 / 60.0)  # 60Hz
#     except KeyboardInterrupt:
#         pass
#     finally:
#         sock.close()
#
# if __name__ == "__main__":
#     run_test_client()
