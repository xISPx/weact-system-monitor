"""Live PC monitor on WeAct Studio Display FS V1 0.96".

The panel is driven in its native portrait mode (80x160 buffer). Landscape
frames (160x80) are rendered with Pillow and rotated 90 degrees in software.
Each update sends the full frame as a single bitmap command - the same
transfer pattern as the stock WeAct demo, proven to render reliably at
115200 baud (the firmware drops bulk data at higher speeds).

If the on-screen text appears upside down, switch ROT between
ROTATE_270 and ROTATE_90.
"""
import ctypes
import os
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


def lhm_gpu_stats():
    """GPU utilization/temp via LHM (NVIDIA/AMD/Intel). None = not available."""
    if _init_lhm() is None or _LHM_GPU is None:
        return None, None
    try:
        _LHM_GPU.Update()
        temps, loads = {}, {}
        for s in _LHM_GPU.Sensors:
            if s.Value is None:
                continue
            name = str(s.Name).lower()
            if str(s.SensorType) == "Temperature":
                temps[name] = float(s.Value)
            elif str(s.SensorType) == "Load":
                loads[name] = float(s.Value)
        core_temps = [v for k, v in temps.items() if "hot spot" not in k and "memory" not in k]
        temp = max(core_temps) if core_temps else (max(temps.values()) if temps else None)
        load = loads.get("gpu core")
        if load is None and loads:
            load = max(loads.values())
        return (round(load) if load is not None else None,
                round(temp) if temp is not None else None)
    except Exception as exc:
        _log(f"LHM gpu read failed: {exc!r}")
        return None, None


def gpu_stats():
    util, temp = lhm_gpu_stats()
    if util is not None or temp is not None:
        return util, temp
    try:  # fallback: NVIDIA driver CLI (when LHM has no GPU hardware)
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=NO_WINDOW_FLAG).stdout
        util, temp = [int(x.strip()) for x in out.splitlines()[0].split(",")]
        return util, temp
    except Exception:
        return None, None


_LHM_COMP = None
_LHM_CPU = None
_LHM_GPU = None
_LHM_TRIED = False


def _init_lhm():
    """LibreHardwareMonitorLib reader (lib/ next to this script).

    Reads the real CPU package temperature (core DTS) and GPU sensors
    (temperature/load). The library loads inside this process (WinRing0
    kernel driver, needs elevation) - no third-party app is started.
    Use LibreHardwareMonitorLib 0.9.4: newer releases ship no embedded
    kernel driver and read nothing.
    """
    global _LHM_COMP, _LHM_CPU, _LHM_GPU, _LHM_TRIED
    if _LHM_TRIED:
        return _LHM_CPU
    _LHM_TRIED = True
    try:
        import clr
        clr.AddReference(str(BASE / "lib" / "LibreHardwareMonitorLib.dll"))
        from LibreHardwareMonitor import Hardware
        comp = Hardware.Computer()
        comp.IsCpuEnabled = True
        comp.IsGpuEnabled = True
        comp.Open()
        cpu_hw = next((hw for hw in comp.Hardware if str(hw.HardwareType) == "Cpu"), None)
        gpus = [hw for hw in comp.Hardware if str(hw.HardwareType).startswith("Gpu")]
        # prefer the discrete card on hybrid laptops (iGPU + dGPU)
        gpu_hw = next((hw for hw in gpus
                       if any(k in hw.Name.lower() for k in ("nvidia", "geforce", "radeon", "arc"))),
                      gpus[0] if gpus else None)
        if cpu_hw is None and gpu_hw is None:
            _log("LHM init: no CPU/GPU hardware")
            return None
        _LHM_COMP = comp
        _LHM_CPU = cpu_hw
        _LHM_GPU = gpu_hw
        _log(f"LHM init OK: {cpu_hw.Name if cpu_hw else 'no cpu'} + {gpu_hw.Name if gpu_hw else 'no gpu'}")
        return cpu_hw
    except Exception as exc:
        _log(f"LHM init failed: {exc!r}")
        return None


def read_cpu_temp():
    """CPU temperature, degC. Real DTS via LHM lib; ACPI zone as fallback."""
    cpu_hw = _init_lhm()
    if cpu_hw is not None:
        try:
            cpu_hw.Update()
            package, core_max = None, None
            for s in cpu_hw.Sensors:
                if s.Value is None or str(s.SensorType) != "Temperature":
                    continue
                if "Package" in str(s.Name):
                    package = float(s.Value)
                elif str(s.Name).startswith("Core"):
                    core_max = float(s.Value) if core_max is None else max(core_max, float(s.Value))
            t = package if package is not None else core_max
            return round(t) if t is not None and -10 < t < 110 else None
        except Exception as exc:
            _log(f"LHM read failed: {exc!r}")
    # Fallback: ACPI thermal zone (elevated only). Some boards never update it.
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


LOG_MAX_BYTES = 1_000_000  # rotate: keep the tail, drop the head


def _log(msg):
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            data = LOG_FILE.read_text(encoding="utf-8", errors="ignore")
            LOG_FILE.write_text(data[-LOG_MAX_BYTES // 2:], encoding="utf-8")
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass  # logging must never take the monitor down


def _font(path, size):
    """Truetype font with a logged fallback - a missing/broken font file
    must not kill the process before the log is even available."""
    try:
        return ImageFont.truetype(path, size)
    except Exception as exc:
        _log(f"font fallback for {path}: {exc!r}")
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10.1 has no sized default font
            return ImageFont.load_default()


F_LABEL = _font("C:/Windows/Fonts/arialbd.ttf", 14)
F_VALUE = _font("C:/Windows/Fonts/arialbd.ttf", 16)
F_INFO = _font("C:/Windows/Fonts/arialbd.ttf", 14)


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


def probe_display(ser):
    """Verify the device speaks the WeAct protocol: version handshake must
    return exactly 19 bytes echoing CMD_SYSTEM_VERSION|CMD_READ."""
    try:
        ser.reset_input_buffer()
        ser.write(bytes((CMD_SYSTEM_VERSION | CMD_READ, CMD_END)))
        resp = ser.read(19)
        return len(resp) == 19 and resp[0] == (CMD_SYSTEM_VERSION | CMD_READ)
    except Exception:
        return False


def open_display():
    """Find the display by VID/PID (serial-number fallback) and verify it
    with a handshake; returns an initialized serial connection or None."""
    for p in list_ports.comports():
        by_vidpid = p.vid == VID and p.pid == PID
        by_serial = isinstance(p.serial_number, str) and p.serial_number.startswith("AD")
        if not (by_vidpid or by_serial):
            continue
        try:
            ser = serial.Serial(p.device, BAUD, timeout=1)
        except serial.SerialException:
            continue
        if probe_display(ser):
            init_display(ser)
            return ser
        ser.close()  # wrong device (or busy) - try the next candidate
    return None


def init_display(ser):
    time.sleep(0.1)  # protocol already verified by probe_display()
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
        if ser is None:                      # not connected: find, verify, init
            ser = open_display()
            if ser is None:
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
        except Exception:                    # unattended daemon: log and keep
            import traceback                 # going, the scheduler restart is
            _log("frame failed:\n" + traceback.format_exc())  # the last resort
            time.sleep(5)
            continue

        n += 1
        if iters and n >= iters:
            break
        time.sleep(UPDATE_S)


_MUTEX = None  # keep referenced: the OS releases the mutex when the process dies


def acquire_single_instance():
    """Named-mutex singleton. The kernel releases the mutex on any process
    death (even taskkill /F or power loss), so a reused PID cannot lock a
    fresh instance out - unlike a pid-file liveness check."""
    global _MUTEX
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\WeActSystemMonitor")
    if handle and ctypes.get_last_error() != 183:  # 183 = ERROR_ALREADY_EXISTS
        _MUTEX = handle
        return True
    return False


if __name__ == "__main__":
    if not acquire_single_instance():
        _log("another instance is running - exiting")
        sys.exit(0)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")  # for manual taskkill
    try:
        main()
    except Exception:
        import traceback
        _log(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} crash\n" + traceback.format_exc())
        raise
