#!/usr/bin/env python3
"""
Quick MJPEG-over-HTTP viewer for a vc_mipi camera on Raspberry Pi (RP1/PiSP).

The RP1 CSI-2 receiver only delivers 10/12-bit raw Bayer in its own
MIPI-packed pixel formats (e.g. 'pRAA' - 5 bytes per 4 pixels), not the
16-bit-container formats GStreamer's bayer2rgb understands, and the
16-bit formats it also advertises (e.g. 'RG16') fail at VIDIOC_STREAMON
(confirmed: format is accepted, streaming is not). So this bypasses
GStreamer entirely: it shells out to v4l2-ctl for raw capture, unpacks
the MIPI packing itself with numpy, debayers with OpenCV, and serves
each frame as JPEG over a standard multipart/x-mixed-replace stream any
browser can open directly.

Usage:
    python3 mjpeg_test_stream.py --device /dev/video0
    python3 mjpeg_test_stream.py --device /dev/video0 --subdev /dev/v4l-subdev2
    python3 mjpeg_test_stream.py --device /dev/video0 --bayer-pattern bggr
    python3 mjpeg_test_stream.py --device /dev/video0 --mono

Then open http://<pi-hostname-or-ip>:8080/ in a browser.

Requires only what's already on this Pi: numpy, OpenCV (cv2), v4l2-ctl,
media-ctl.

Bayer pattern caveat: OpenCV's BayerXX2BGR codes and V4L2's SXGGB mbus
codes don't map 1:1 in an obviously-documented way (this is a
long-standing source of R/B-swapped or off-by-one-pixel debayering in
the V4L2 + OpenCV community). The mapping below is the one most
commonly reported correct, but it has NOT been visually verified against
a real image by this script's author - if colors look swapped or the
mosaic looks off, pass --bayer-pattern with a different value.
"""
import argparse
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

BOUNDARY = "vcmipiframe"

# V4L2 mbus pattern name -> (packed-fourcc prefix, OpenCV debayer code).
# See the module docstring: the OpenCV code side of this is unverified.
BAYER_INFO = {
    "SBGGR": ("B", cv2.COLOR_BayerRG2BGR),
    "SGBRG": ("G", cv2.COLOR_BayerGR2BGR),
    "SGRBG": ("g", cv2.COLOR_BayerGB2BGR),
    "SRGGB": ("R", cv2.COLOR_BayerBG2BGR),
}
BAYER_PATTERN_OVERRIDE = {
    "bggr": cv2.COLOR_BayerRG2BGR,
    "gbrg": cv2.COLOR_BayerGR2BGR,
    "grbg": cv2.COLOR_BayerGB2BGR,
    "rggb": cv2.COLOR_BayerBG2BGR,
}

# Packed fourcc, by (bayer-prefix-or-None-for-mono, bits)
PACKED_FOURCC = {
    ("B", 8): "BA81", ("G", 8): "GBRG", ("g", 8): "GRBG", ("R", 8): "RGGB",
    ("B", 10): "pBAA", ("G", 10): "pGAA", ("g", 10): "pgAA", ("R", 10): "pRAA",
    ("B", 12): "pBCC", ("G", 12): "pGCC", ("g", 12): "pgCC", ("R", 12): "pRCC",
    (None, 8): "GREY",
    (None, 10): "Y10P",
    (None, 12): "Y12P",
}


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=5, **kw)


def extract_mbus_code(out: str):
    """v4l2-ctl reports e.g. '0x300f (MEDIA_BUS_FMT_SRGGB10_1X10)' - strip the prefix."""
    m = re.search(r"Mediabus Code\s*:\s*0x[0-9a-f]+ \((?:MEDIA_BUS_FMT_)?(\w+)\)", out)
    return m.group(1) if m else None


def find_sensor_subdev():
    """Scan /dev/v4l-subdev* for the one reporting a raw Bayer/mono mbus code."""
    import glob

    for path in sorted(glob.glob("/dev/v4l-subdev*")):
        out = run(["v4l2-ctl", f"--device={path}", "--get-subdev-fmt", "pad=0"]).stdout
        code = extract_mbus_code(out)
        if code and re.match(r"^(SBGGR|SGBRG|SGRBG|SRGGB|Y)\d*_1X\d+$", code):
            return path, code, out
    return None, None, None


def parse_subdev_fmt(out: str):
    w = int(re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", out).group(1))
    h = int(re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", out).group(2))
    return w, h


def parse_mbus_code(code: str):
    """'SRGGB10_1X10' -> ('R', 10); 'Y10_1X10' -> (None, 10)."""
    m = re.match(r"^(SBGGR|SGBRG|SGRBG|SRGGB)(\d+)_1X\d+$", code)
    if m:
        prefix, _ = BAYER_INFO[m.group(1)]
        return prefix, int(m.group(2))
    m = re.match(r"^Y(\d+)_1X\d+$", code)
    if m:
        return None, int(m.group(1))
    raise ValueError(f"Don't know how to handle mbus code {code!r}")


def unpack_raw10(data: bytes, width: int, height: int) -> np.ndarray:
    raw = np.frombuffer(data, dtype=np.uint8).reshape(height, width * 10 // 8)
    groups = raw.reshape(height, width // 4, 5)
    b0, b1, b2, b3, b4 = (groups[:, :, i].astype(np.uint16) for i in range(5))
    p0 = (b0 << 2) | ((b4 >> 0) & 0x3)
    p1 = (b1 << 2) | ((b4 >> 2) & 0x3)
    p2 = (b2 << 2) | ((b4 >> 4) & 0x3)
    p3 = (b3 << 2) | ((b4 >> 6) & 0x3)
    return np.stack([p0, p1, p2, p3], axis=-1).reshape(height, width)


def unpack_raw12(data: bytes, width: int, height: int) -> np.ndarray:
    raw = np.frombuffer(data, dtype=np.uint8).reshape(height, width * 12 // 8)
    groups = raw.reshape(height, width // 2, 3)
    b0, b1, b2 = (groups[:, :, i].astype(np.uint16) for i in range(3))
    p0 = (b0 << 4) | (b2 & 0x0F)
    p1 = (b1 << 4) | ((b2 >> 4) & 0x0F)
    return np.stack([p0, p1], axis=-1).reshape(height, width)


def unpack_frame(data: bytes, width: int, height: int, bits: int) -> np.ndarray:
    if bits == 8:
        return np.frombuffer(data, dtype=np.uint8).reshape(height, width)
    if bits == 10:
        return unpack_raw10(data, width, height)
    if bits == 12:
        return unpack_raw12(data, width, height)
    raise ValueError(f"Unsupported bit depth: {bits} (only 8/10/12 implemented)")


class FrameBus:
    """Holds the latest JPEG frame, shared between the capture thread and
    every HTTP client thread."""

    def __init__(self):
        self._cond = threading.Condition()
        self._frame = None
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
            pass  # keep stdout clean

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace;boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            seq = 0
            try:
                while True:
                    seq, frame = frame_bus.wait_next(seq)
                    if frame is None:
                        continue
                    self.wfile.write(f"--{BOUNDARY}\r\n".encode())
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

    return MjpegHandler


def capture_loop(device, width, height, bits, debayer_code, quality, frame_bus, stop_event, proc_holder):
    frame_size = height * (width * bits // 8)
    proc = subprocess.Popen(
        ["v4l2-ctl", f"--device={device}", "--stream-mmap", "--stream-count=0", "--stream-to=-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    proc_holder["proc"] = proc
    try:
        buf = b""
        while not stop_event.is_set():
            chunk = proc.stdout.read(frame_size - len(buf))
            if not chunk:
                print("v4l2-ctl stream ended unexpectedly", file=sys.stderr)
                break
            buf += chunk
            if len(buf) < frame_size:
                continue

            raw16 = unpack_frame(buf, width, height, bits)
            buf = b""

            img8 = (raw16 >> (bits - 8)).astype(np.uint8) if bits > 8 else raw16
            img = cv2.cvtColor(img8, debayer_code) if debayer_code is not None else img8

            ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok:
                frame_bus.publish(jpg.tobytes())
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default="/dev/video0", help="V4L2 video capture node (default: /dev/video0)")
    parser.add_argument("--subdev", default=None,
                         help="Sensor V4L2 subdev node, e.g. /dev/v4l-subdev2 (default: auto-detect)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--quality", type=int, default=85, help="JPEG quality 1-100 (default: 85)")
    parser.add_argument("--bayer-pattern", choices=sorted(BAYER_PATTERN_OVERRIDE), default=None,
                         help="Override auto-detected Bayer pattern if colors look wrong")
    parser.add_argument("--mono", action="store_true", help="Force monochrome (skip debayer)")
    args = parser.parse_args()

    subdev = args.subdev
    if subdev:
        out = run(["v4l2-ctl", f"--device={subdev}", "--get-subdev-fmt", "pad=0"]).stdout
        mbus_code = extract_mbus_code(out)
    else:
        subdev, mbus_code, out = find_sensor_subdev()

    if not subdev or not mbus_code:
        print("Could not find a sensor subdev reporting a raw Bayer/mono format.\n"
              "Pass --subdev explicitly, e.g. --subdev /dev/v4l-subdev2\n"
              "(check `media-ctl -d /dev/media0 -p` for the sensor entity's device node).",
              file=sys.stderr)
        sys.exit(1)

    width, height = parse_subdev_fmt(out)
    prefix, bits = parse_mbus_code(mbus_code)
    print(f"Sensor subdev {subdev}: {mbus_code} ({width}x{height})")

    if args.mono:
        prefix = None
    debayer_code = None
    if prefix is not None:
        debayer_code = (
            BAYER_PATTERN_OVERRIDE[args.bayer_pattern]
            if args.bayer_pattern
            else {v[0]: v[1] for v in BAYER_INFO.values()}[prefix]
        )

    fourcc = PACKED_FOURCC.get((prefix, bits))
    if fourcc is None:
        print(f"No known packed V4L2 format for pattern={prefix} bits={bits}", file=sys.stderr)
        sys.exit(1)

    set_out = run(["v4l2-ctl", f"--device={args.device}",
                   f"--set-fmt-video=width={width},height={height},pixelformat={fourcc}"]).stdout
    print(set_out.strip())

    get_out = run(["v4l2-ctl", f"--device={args.device}", "--get-fmt-video"]).stdout
    m = re.search(r"Bytes per Line\s*:\s*(\d+)", get_out)
    expected_bpl = width * bits // 8
    if m and int(m.group(1)) != expected_bpl:
        print(f"Warning: driver reports {m.group(1)} bytes/line, expected {expected_bpl} "
              f"- unpacking will likely be wrong", file=sys.stderr)

    frame_bus = FrameBus()
    stop_event = threading.Event()
    proc_holder = {}
    cap_thread = threading.Thread(
        target=capture_loop,
        args=(args.device, width, height, bits, debayer_code, args.quality, frame_bus, stop_event, proc_holder),
        daemon=True,
    )
    cap_thread.start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(frame_bus))
    print(f"Serving MJPEG stream on http://0.0.0.0:{args.port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping...")
        stop_event.set()
        # The capture thread blocks on proc.stdout.read(); terminating the
        # subprocess here (rather than only in the thread's own cleanup)
        # unblocks that read immediately instead of racing process exit
        # against a background thread still mid-teardown.
        proc = proc_holder.get("proc")
        if proc is not None:
            proc.terminate()
        cap_thread.join(timeout=5)
        server.shutdown()


if __name__ == "__main__":
    main()
