#!/usr/bin/env python3
"""
Quick MJPEG-over-HTTP viewer for a vc_mipi camera V4L2 node.

Captures from a /dev/videoX device via GStreamer's v4l2src, debayers if
the negotiated format is a raw Bayer pattern (skipped for monochrome
sensors), encodes to JPEG, and serves it as a standard
multipart/x-mixed-replace stream any browser can open directly.

Usage:
    python3 mjpeg_test_stream.py --device /dev/video0
    python3 mjpeg_test_stream.py --device /dev/video0 --mono
    python3 mjpeg_test_stream.py --device /dev/video0 --port 8081 --quality 90

Then open http://<pi-hostname-or-ip>:8080/ in a browser.

Requires only what's already on this Pi: PyGObject (gi), GStreamer with
gst-plugins-good (jpegenc) and gst-plugins-bad (bayer2rgb), v4l2-ctl.
"""
import argparse
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gst  # noqa: E402

BOUNDARY = "vcmipiframe"

# V4L2 fourccs that indicate a raw Bayer mosaic (color) sensor, as opposed
# to a plain monochrome one. Covers the packed/unpacked 8/10/12-bit
# variants the kernel media subsystem commonly reports.
BAYER_FOURCC_RE = re.compile(r"^(BA81|BG|GB|GR|RG)(8|10|12)?$")


def detect_is_bayer(device: str) -> bool | None:
    """Best-effort guess from `v4l2-ctl --list-formats`. Returns None if undetermined."""
    try:
        out = subprocess.run(
            ["v4l2-ctl", f"--device={device}", "--list-formats"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None

    fourccs = re.findall(r"'([A-Za-z0-9 ]{4})'", out)
    for code in fourccs:
        code = code.strip()
        if BAYER_FOURCC_RE.match(code):
            return True
        if code.upper().startswith("Y") or code == "GREY":
            return False
    return None


def build_pipeline(device: str, is_bayer: bool, quality: int) -> Gst.Element:
    debayer = "bayer2rgb ! " if is_bayer else ""
    desc = (
        f"v4l2src device={device} io-mode=4 ! "
        f"{debayer}"
        f"videoconvert ! "
        f"jpegenc quality={quality} ! "
        f"appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
    )
    print(f"GStreamer pipeline: {desc}")
    return Gst.parse_launch(desc)


class FrameBus:
    """Holds the latest JPEG frame, shared between the GStreamer callback
    thread and every HTTP client thread."""

    def __init__(self):
        self._cond = threading.Condition()
        self._frame: bytes | None = None
        self._seq = 0

    def publish(self, data: bytes):
        with self._cond:
            self._frame = data
            self._seq += 1
            self._cond.notify_all()

    def wait_next(self, last_seq: int, timeout=5.0):
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq != last_seq, timeout=timeout):
                return last_seq, None
            return self._seq, self._frame


def make_handler(frame_bus: FrameBus):
    class MjpegHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # keep stdout clean; errors still print via log_error

        def do_GET(self):
            self.send_response(200)
            self.send_header(
                "Content-Type", f"multipart/x-mixed-replace;boundary={BOUNDARY}"
            )
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            seq = 0
            try:
                while True:
                    seq, frame = frame_bus.wait_next(seq)
                    if frame is None:
                        continue  # timed out waiting for a frame, keep trying
                    self.wfile.write(f"--{BOUNDARY}\r\n".encode())
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass  # client disconnected, nothing to clean up

    return MjpegHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default="/dev/video0", help="V4L2 device node (default: /dev/video0)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--quality", type=int, default=85, help="JPEG quality 1-100 (default: 85)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bayer", action="store_true", help="Force Bayer debayering on")
    group.add_argument("--mono", action="store_true", help="Force Bayer debayering off (monochrome sensor)")
    args = parser.parse_args()

    if args.bayer:
        is_bayer = True
    elif args.mono:
        is_bayer = False
    else:
        is_bayer = detect_is_bayer(args.device)
        if is_bayer is None:
            print(f"Could not auto-detect format on {args.device}; assuming Bayer. "
                  f"Pass --mono if this is a monochrome sensor.")
            is_bayer = True
        else:
            print(f"Auto-detected {'Bayer color' if is_bayer else 'monochrome'} sensor on {args.device}")

    Gst.init(None)
    pipeline = build_pipeline(args.device, is_bayer, args.quality)
    sink = pipeline.get_by_name("sink")

    frame_bus = FrameBus()

    def on_new_sample(appsink):
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if ok:
            frame_bus.publish(bytes(mapinfo.data))
            buf.unmap(mapinfo)
        return Gst.FlowReturn.OK

    sink.connect("new-sample", on_new_sample)

    bus = pipeline.get_bus()

    def on_bus_message(_bus, message):
        # Runs on the GLib mainloop thread, not the main thread where
        # server.serve_forever() blocks - sys.exit() here would only kill
        # this thread and leave the HTTP server hanging, so force a hard
        # process exit instead.
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"GStreamer error: {err} ({debug})", file=sys.stderr)
            pipeline.set_state(Gst.State.NULL)
            os._exit(1)
        elif t == Gst.MessageType.EOS:
            print("GStreamer: end of stream", file=sys.stderr)
            pipeline.set_state(Gst.State.NULL)
            os._exit(1)
        return True

    bus.add_signal_watch()
    bus.connect("message", on_bus_message)

    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print(f"Failed to start pipeline on {args.device}", file=sys.stderr)
        sys.exit(1)

    loop = GLib.MainLoop()
    gst_thread = threading.Thread(target=loop.run, daemon=True)
    gst_thread.start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(frame_bus))
    print(f"Serving MJPEG stream on http://0.0.0.0:{args.port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping...")
        server.shutdown()
        pipeline.set_state(Gst.State.NULL)
        loop.quit()


if __name__ == "__main__":
    main()
