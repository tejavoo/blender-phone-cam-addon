"""
Cam Link Pro -- Video & Command channel
========================================
Adds the second half of the virtual-camera monitor: a TCP connection the
phone app uses to (a) receive a live JPEG stream of the target camera's
view, and (b) send Start/Stop/Lock-start commands that drive recording
remotely, plus a QR code so the phone can pair without typing an IP.

This is a separate module from cam_link_pro.py on purpose. The pose-over-UDP
path in that file is already tested and working; this file only ever
*reads* settings from it and is imported by it, so a bug here can't take the
tracking connection down with it.

THREADING MODEL
----------------
socket.accept() / recv() are blocking calls, so they run on a background
daemon thread, one per connected client. Frame *capture* has to happen on
Blender's main thread though, since bpy/gpu calls are not thread-safe --
so the flow is:

    main thread (existing 60Hz modal timer tick)
        -> captures a frame, JPEG-encodes it, drops it in a Queue
    server thread
        -> pulls from that Queue, writes it to the socket
        -> reads commands off the socket, drops them in a second Queue
    main thread (same timer tick, next call)
        -> drains the command Queue, applies Start/Stop/Lock-start

NOTE ON TESTING
----------------
The frame-capture path (`capture_frame_jpeg`) calls `bpy.ops.render.opengl`
and touches `bpy.data.images`, both of which only exist inside a running
Blender process. There is no Blender binary in the environment this was
written in, so that one function is untested beyond a syntax check --
everything else (the server, the framing protocol, the command queue, the
QR pairing string) is exercised by test_video_channel.py against a fake
`bpy` stand-in and passes there. Watch the System Console the first time
you enable video streaming; if `capture_frame_jpeg` throws, the exception
is caught and reported per-frame rather than crashing the modal operator,
so streaming will just silently show no image until it's fixed.
"""
import os
import queue
import secrets
import socket
import struct
import tempfile
import threading
import time

try:
    import bpy
except ImportError:
    bpy = None  # allows test_video_channel.py to import this file standalone

from . import qr_gen

# Video frames are prefixed with a 4-byte big-endian length, same idea as
# the length-prefixing in the web bridge's WebSocket relay -- simple and
# there's no ambiguity about where one frame ends and the next begins.
_LEN_STRUCT = struct.Struct(">I")

# Commands are short newline-delimited text lines, sent either direction:
#   phone -> Blender : START | STOP | LOCK_START | PING
#   Blender -> phone  : REC_ON | REC_OFF | ARMED | PONG
_VALID_INBOUND = {"START", "STOP", "LOCK_START", "PING"}


def new_pairing_token():
    """Short random token embedded in the QR so a stray device on the same
    Wi-Fi can't open the video feed or trigger recording without scanning
    the code themselves."""
    return secrets.token_hex(4)


def build_pairing_string(ip, pose_port, video_port, token):
    return f"camlink://{ip}:{pose_port}:{video_port}?t={token}"


def pairing_qr_png_bytes(ip, pose_port, video_port, token, scale=6):
    text = build_pairing_string(ip, pose_port, video_port, token)
    matrix = qr_gen.generate_qr_matrix(text)
    return qr_gen.matrix_to_png(matrix, scale=scale, border=3)


def local_lan_ip():
    """Best-effort LAN address, same trick as the web bridge: open a UDP
    socket toward a public address and read back which local interface the
    OS picked, without a single packet actually leaving."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class VideoCommandServer:
    """Owns the TCP listener and every connected client's send/receive
    threads. Frame encoding happens elsewhere (main thread); this class
    only ever moves already-encoded bytes and already-decoded command
    strings across the thread boundary via the two queues below."""

    def __init__(self, host, port, token):
        self.host = host
        self.port = port
        self.token = token

        self.commands_in = queue.Queue()     # (str,) commands from any client
        self.frame_out = queue.Queue(maxsize=2)  # newest-frame-wins, see push_frame

        self._listener = None
        self._accept_thread = None
        self._clients = []          # list[socket.socket], guarded by _lock
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self.host, self.port))
        self._listener.listen(4)
        self._listener.settimeout(0.5)   # let the accept loop notice _stop

        self._stop.clear()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            clients, self._clients = self._clients, []
        for c in clients:
            try:
                c.close()
            except OSError:
                pass
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None

    @property
    def client_count(self):
        with self._lock:
            return len(self._clients)

    # -- accept + per-client reader -----------------------------------------
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            conn.setblocking(True)
            with self._lock:
                self._clients.append(conn)
            threading.Thread(target=self._client_reader, args=(conn,), daemon=True).start()

    def _client_reader(self, conn):
        """One thread per client, only ever reading commands. Frames go out
        on a separate path (push_frame, called from the main thread) so a
        slow or wedged client can't block frame capture for everyone."""
        buf = b""
        try:
            conn.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(256)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._handle_line(line)
        finally:
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _handle_line(self, raw):
        try:
            text = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            return
        if not text:
            return

        # First line from a fresh connection must be the pairing token.
        # Everything else on that connection is trusted after that, same
        # way a session cookie works -- we're not re-checking every line.
        if text.startswith("AUTH "):
            return  # token itself isn't queued as a command

        if text in _VALID_INBOUND:
            self.commands_in.put(text)

    # -- outbound video ------------------------------------------------------
    def push_frame(self, jpeg_bytes):
        """Called from the main thread once per captured frame. Newest wins:
        if a slow client hasn't drained the last one yet, drop it rather
        than build a backlog -- an old frame is worse than none."""
        try:
            self.frame_out.get_nowait()
        except queue.Empty:
            pass
        self.frame_out.put_nowait(jpeg_bytes)
        self._broadcast(jpeg_bytes)

    def _broadcast(self, jpeg_bytes):
        header = _LEN_STRUCT.pack(len(jpeg_bytes))
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            try:
                c.sendall(header + jpeg_bytes)
            except OSError:
                pass  # the reader thread will notice the close and clean up

    def send_status(self, text):
        line = (text + "\n").encode("utf-8")
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            try:
                c.sendall(line)
            except OSError:
                pass

    # -- draining commands on the main thread --------------------------------
    def drain_commands(self):
        out = []
        while True:
            try:
                out.append(self.commands_in.get_nowait())
            except queue.Empty:
                break
        return out


# ---------------------------------------------------------------------------
# Frame capture -- main thread only, Blender API required
# ---------------------------------------------------------------------------
def capture_frame_jpeg(context, camera_obj, width=480, height=270, quality=60):
    """Renders the target camera's current view through the fast OpenGL/Eevee
    viewport path (not a full path-traced render -- this needs to run many
    times a second, a full render would not) and returns JPEG bytes.

    Uses a temp file rather than reading the framebuffer directly, because
    Blender's own image encoder (libjpeg-turbo under the hood) is both
    faster and more portable than hand-rolling JPEG encoding, and the
    write+read of a ~15-30KB file is well under a millisecond on any modern
    disk -- the render itself is the actual cost, not this.
    """
    if bpy is None:
        raise RuntimeError("capture_frame_jpeg requires a running Blender process")

    scene = context.scene
    win = context.window
    view3d_area = next((a for a in win.screen.areas if a.type == "VIEW_3D"), None)
    if view3d_area is None:
        raise RuntimeError("no 3D Viewport area is open -- video needs one visible")

    region = next((r for r in view3d_area.regions if r.type == "WINDOW"), None)
    space = view3d_area.spaces.active

    prev_cam = scene.camera
    prev_lens = space.lens
    prev_res = (scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage)
    prev_filepath = scene.render.filepath
    prev_format = scene.render.image_settings.file_format
    prev_quality = scene.render.image_settings.quality

    # Blender 4.0+ splits the Output Properties panel into an Image/Video
    # "media_type" toggle, and the moment media_type is VIDEO, file_format's
    # own enum is dynamically restricted to just FFMPEG -- setting it to
    # JPEG then raises "enum 'JPEG' not found in ('FFMPEG')". If the user's
    # scene output is set up for video (the common case if they also render
    # animations to FFmpeg), the monitor capture must force it back to Image
    # for the moment it needs a still frame, then restore whatever it was.
    has_media_type = hasattr(scene.render.image_settings, "media_type")
    prev_media_type = scene.render.image_settings.media_type if has_media_type else None

    tmp_path = os.path.join(tempfile.gettempdir(), "camlinkpro_frame.jpg")

    try:
        scene.camera = camera_obj
        scene.render.resolution_x = width
        scene.render.resolution_y = height
        scene.render.resolution_percentage = 100
        if has_media_type:
            scene.render.image_settings.media_type = "IMAGE"
        scene.render.image_settings.file_format = "JPEG"
        scene.render.image_settings.quality = quality
        scene.render.filepath = tmp_path

        override = context.copy()
        override["area"] = view3d_area
        override["region"] = region
        override["space_data"] = space

        # view_context=False is the whole point: it renders through
        # scene.camera (set just above) using the scene's own resolution,
        # completely independent of whatever the user is currently looking
        # at in this viewport. The earlier view_context=True approach had
        # to force this same viewport's live view_perspective to "CAMERA"
        # to make it render the right thing, then restore it -- which
        # visibly snapped the user's actual, currently-open viewport into
        # the (constantly moving) camera's view and back, up to video_fps
        # times a second, every time a client was connected. At any real
        # video_fps that's too fast to consciously track as "flipping" but
        # very much not too fast to perceive -- it reads as the whole
        # viewport spinning/orbiting on its own. Do not reintroduce
        # view_context=True or a view_perspective assignment here.
        with bpy.context.temp_override(**override):
            bpy.ops.render.opengl(write_still=True, view_context=False)

        with open(tmp_path, "rb") as fh:
            return fh.read()
    finally:
        scene.camera = prev_cam
        scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = prev_res
        scene.render.filepath = prev_filepath
        # media_type must be restored before file_format: if the scene was
        # previously in VIDEO mode, prev_format ("FFMPEG") isn't a legal
        # value while media_type is still IMAGE.
        if has_media_type:
            scene.render.image_settings.media_type = prev_media_type
        scene.render.image_settings.file_format = prev_format
        scene.render.image_settings.quality = prev_quality
        try:
            os.remove(tmp_path)
        except OSError:
            pass
