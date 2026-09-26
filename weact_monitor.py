"""Live PC monitor on WeAct Studio Display FS V1 0.96".

The panel is driven in its native portrait mode (80x160 buffer). Landscape
frames (160x80) are rendered with Pillow and rotated 90 degrees in software.
Each update sends the full frame as a single bitmap command - the same
transfer pattern as the stock WeAct demo, proven to render reliably at
115200 baud (the firmware drops bulk data at higher speeds).

If the on-screen text appears upside down, switch ROT between
ROTATE_270 and ROTATE_90.
"""
import subprocess
import sys
import time
from pathlib import Path

import psutil
import serial
from serial.tools import list_ports
from PIL import Image, ImageDraw, ImageFont

BAUD = 115200
VID, PID = 0x1A86, 0xFE0C   # WCH CH343 USB-UART bridge on the WeAct display
W, H = 160, 80              # rendered landscape frame
PH_W, PH_H = 80, 160        # native portrait buffer sent to the panel
UPDATE_S = 1.0
ROT = Image.Transpose.ROTATE_270   # ROTATE_90 renders the frame upside down

BASE = Path(__file__).resolve().parent   # pid/log live next to the script
PID_FILE = BASE / "weact_monitor.pid"
LOG_FILE = BASE / "weact_monitor.log"

CMD_SYSTEM_VERSION = 0x42
CMD_READ = 0x80
CMD_SET_ORIENTATION = 0x02
CMD_SET_BRIGHTNESS = 0x03
CMD_FULL = 0x04
CMD_SET_BITMAP = 0x05
CMD_END = 0x0A
NO_WINDOW_FLAG = getattr(subprocess, "CREATE_NO_WINDOW", 0)

F_LABEL = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 14)
F_VALUE = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 16)
F_INFO = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 14)

COL_LABEL = (110, 170, 255)
COL_TRACK = (45, 45, 45)


def rgb565(r, g, b):
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def le(color):
    c = rgb565(*color)
    return bytes((c & 0xFF, (c >> 8) & 0xFF))


def bitmap_cmd(x0, y0, x1, y1):
    return bytes((CMD_SET_BITMAP, x0 & 0xFF, (x0 >> 8) & 0xFF, y0 & 0xFF, (y0 >> 8) & 0xFF,
                  x1 & 0xFF, (x1 >> 8) & 0xFF, y1 & 0xFF, (y1 >> 8) & 0xFF, CMD_END))


def frame_bytes(img):
    """Whole image as RGB565 little-endian, row-major from top-left."""
    im = img.convert("RGB")
    raw = im.tobytes()
    out = bytearray(len(raw) // 3 * 2)
    j = 0
    for i in range(0, len(raw), 3):
        c = rgb565(raw[i], raw[i + 1], raw[i + 2])
        out[j] = c & 0xFF
        out[j + 1] = (c >> 8) & 0xFF
        j += 2
    return bytes(out)


def load_color(v):
    if v >= 85:
        return (255, 60, 40)
    if v >= 60:
        return (255, 200, 0)
    return (40, 220, 80)


def temp_color(t):
    if t >= 80:
        return (255, 60, 40)
    if t >= 65:
        return (255, 200, 0)
    return (40, 220, 80)


def gpu_stats():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=NO_WINDOW_FLAG).stdout
        util, temp = [int(x.strip()) for x in out.splitlines()[0].split(",")]
        return util, temp
    except Exception:
        return None, None


def read_cpu_temp():
    """ACPI thermal zone (max instance), degC. Requires elevated process."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature | "
             "Measure-Object -Property CurrentTemperature -Maximum).Maximum/10-273.15"],
            capture_output=True, text=True, errors="ignore", timeout=8,
            creationflags=NO_WINDOW_FLAG)
        # some locales print decimals with a comma
        t = float(out.stdout.strip().splitlines()[-1].replace(",", "."))
        t -= 4  # ACPI zone reads ~4C above core DTS (calibrated vs reference app)
        return round(t) if -10 < t < 110 else None
    except Exception as exc:
        detail = (out.stdout[-200:] + " | " + out.stderr[-200:]) if 'out' in dir() else repr(exc)
        _log(f"cpu_temp fail: {detail}")
        return None


def _log(msg):
    prev = LOG_FILE.read_text(encoding="utf-8", errors="ignore") if LOG_FILE.exists() else ""
    LOG_FILE.write_text(prev + f"[{time.strftime('%H:%M:%S')}] {msg}\n", encoding="utf-8")


def render(cpu, gpu_util, gpu_temp, ram, cpu_t):
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    for label, value, y in (("CPU", cpu, 0), ("GPU", gpu_util, 20), ("RAM", ram, 40)):
        color = load_color(value) if value is not None else (120, 120, 120)
        d.text((2, y), label, font=F_LABEL, fill=COL_LABEL)
        txt = f"{value}%" if value is not None else "n/a"
        bb = d.textbbox((0, 0), txt, font=F_VALUE)
        d.text((W - 2 - (bb[2] - bb[0]) - bb[0], y - 1), txt, font=F_VALUE, fill=color)
        d.rectangle((2, y + 15, W - 3, y + 19), fill=COL_TRACK)
        if value is not None:
            d.rectangle((2, y + 15, 2 + max(1, int(value / 100 * (W - 6))), y + 19), fill=color)

    # Info line: CPU temp left, GPU temp right
    if cpu_t is not None:
        d.text((2, 62), f"CPU {cpu_t}\u00b0", font=F_INFO, fill=temp_color(cpu_t))
    else:
        d.text((2, 62), "CPU --", font=F_INFO, fill=(120, 120, 120))
    if gpu_temp is not None:
        d.text((W - 2 - d.textlength(f"GPU {gpu_temp}\u00b0", font=F_INFO), 62),
               f"GPU {gpu_temp}\u00b0", font=F_INFO, fill=temp_color(gpu_temp))
    return img


def find_display():
    """COM port of the WeAct display (same match as the official driver)."""
    for p in list_ports.comports():
        if p.vid == VID and p.pid == PID:
            return p.device
        if isinstance(p.serial_number, str) and p.serial_number.startswith("AD"):
            return p.device
    return None


def init_display(ser):
    time.sleep(0.1)
    ser.reset_input_buffer()
    ser.write(bytes((CMD_SYSTEM_VERSION | CMD_READ, CMD_END)))   # handshake
    ser.read(19)
    ser.reset_input_buffer()
    ser.write(bytes((CMD_SET_ORIENTATION, 0, CMD_END)))          # native portrait
    ser.write(bytes((CMD_SET_BRIGHTNESS, 178, 0xE8, 0x03, CMD_END)))  # ~70%
    ser.write(bytes((CMD_FULL, 0, 0, 0, 0, (PH_W - 1) & 0xFF, 0, (PH_H - 1) & 0xFF, 0)) +
              le((0, 0, 0)) + bytes((CMD_END,)))


def main():
    iters = 0
    if "--test" in sys.argv:
        iters = int(sys.argv[sys.argv.index("--test") + 1])

    psutil.cpu_percent(interval=None)  # warm-up
    ser = None
    n = 0
    while True:
        if ser is None:                      # not connected: find and open
            port = find_display()
            if port is None:
                time.sleep(3)
                continue
            try:
                ser = serial.Serial(port, BAUD, timeout=1)
                init_display(ser)
            except serial.SerialException:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
                ser = None
                time.sleep(3)
                continue

        try:
            cpu = round(psutil.cpu_percent())
            ram = round(psutil.virtual_memory().percent)
            cpu_t = read_cpu_temp()
            gpu_util, gpu_temp = gpu_stats()

            frame = render(cpu, gpu_util, gpu_temp, ram, cpu_t).transpose(ROT)
            ser.write(bitmap_cmd(0, 0, PH_W - 1, PH_H - 1))
            ser.write(frame_bytes(frame))
        except serial.SerialException:       # unplugged / port vanished
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(1)
            continue

        n += 1
        if iters and n >= iters:
            break
        time.sleep(UPDATE_S)


if __name__ == "__main__":
    PID_FILE.write_text(str(__import__("os").getpid()), encoding="ascii")
    try:
        main()
    except Exception:
        import traceback
        prev = LOG_FILE.read_text(encoding="utf-8", errors="ignore") if LOG_FILE.exists() else ""
        LOG_FILE.write_text(prev + f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n" +
                            traceback.format_exc(), encoding="utf-8")
        raise
