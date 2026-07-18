from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from importlib.util import find_spec
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser

from .local_auth import create_session_secret
from .metadata import APP_VERSION, BUILD_COMMIT, PRODUCT_NAME, PRODUCT_SUBTITLE, SOURCE_URL
from .paths import APP_DATA_ROOT, IS_FROZEN, LOG_DIR, ensure_runtime_directories, resource_path


MUTEX_NAME = "Local\\SightlineDesktopApplication"
SHUTDOWN_EVENT_NAME = "Local\\SightlineDesktopApplicationShutdown"
RUNTIME_STATE_PATH = APP_DATA_ROOT / "runtime.json"
ERROR_ALREADY_EXISTS = 183
ERROR_FILE_NOT_FOUND = 2
EVENT_MODIFY_STATE = 0x0002
MUTEX_MODIFY_STATE = 0x0001
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102


def enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class WindowsMutex:
    def __init__(self, name: str) -> None:
        self.handle: int | None = None
        self.already_exists = False
        if os.name != "nt":
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        self.handle = kernel32.CreateMutexW(None, True, name)
        self.already_exists = kernel32.GetLastError() == ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self.handle and os.name == "nt":
            if not self.already_exists:
                ctypes.windll.kernel32.ReleaseMutex(self.handle)
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


class ShutdownEvent:
    def __init__(self, name: str) -> None:
        self._thread_event = threading.Event()
        self.handle: int | None = None
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateEventW.restype = wintypes.HANDLE
            self.handle = kernel32.CreateEventW(None, True, False, name)

    def is_set(self) -> bool:
        if self._thread_event.is_set():
            return True
        if self.handle and os.name == "nt":
            return ctypes.windll.kernel32.WaitForSingleObject(self.handle, 0) == 0
        return False

    def set(self) -> None:
        self._thread_event.set()
        if self.handle and os.name == "nt":
            ctypes.windll.kernel32.SetEvent(self.handle)

    def close(self) -> None:
        if self.handle and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def signal_running_instance_shutdown() -> bool:
    if os.name != "nt":
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenEventW.restype = wintypes.HANDLE
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, SHUTDOWN_EVENT_NAME)
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def _running_instance_mutex_is_held() -> bool:
    """Return whether the primary instance still owns its singleton mutex."""
    if os.name != "nt":
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    handle = kernel32.OpenMutexW(SYNCHRONIZE | MUTEX_MODIFY_STATE, False, MUTEX_NAME)
    if not handle:
        # Only a missing object proves the app has stopped. An access or
        # system failure must be treated as live so setup cannot replace an
        # executable that may still be running.
        return kernel32.GetLastError() != ERROR_FILE_NOT_FOUND
    try:
        wait_result = kernel32.WaitForSingleObject(handle, 0)
        if wait_result == WAIT_TIMEOUT:
            return True
        if wait_result in {WAIT_OBJECT_0, WAIT_ABANDONED}:
            kernel32.ReleaseMutex(handle)
            return False
        # An unexpected wait failure must not be mistaken for a stopped app
        # while an installer is about to replace its files.
        return True
    finally:
        kernel32.CloseHandle(handle)


def shutdown_running_instance(*, timeout_s: float = 15.0) -> int:
    """Request shutdown and return a process-style exit code.

    Exit code 2 means no live instance owned the named shutdown event, which
    lets callers distinguish stale runtime state from an instance that failed
    to stop within the timeout.
    """
    if not signal_running_instance_shutdown():
        return 1 if _running_instance_mutex_is_held() else 2
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        # runtime.json is written only after the server becomes healthy, so it
        # cannot prove a just-starting instance has stopped. The singleton
        # mutex remains held for the full process lifetime.
        if not _running_instance_mutex_is_held():
            return 0
        time.sleep(0.1)
    return 0 if not _running_instance_mutex_is_held() else 1


def _configure_logging() -> None:
    ensure_runtime_directories()
    handler = RotatingFileHandler(
        LOG_DIR / "sightline.log",
        maxBytes=1_000_000,
        backupCount=4,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def _available_port(preferred: int = 8765) -> int:
    for port in (preferred, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
                candidate.bind(("127.0.0.1", port))
                selected = int(candidate.getsockname()[1])
        except OSError:
            continue
        if selected:
            return selected
    raise RuntimeError("Sightline could not reserve a loopback port")


def _read_runtime_state() -> dict[str, Any] | None:
    try:
        value = json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _write_runtime_state(*, port: int, secret: str) -> None:
    ensure_runtime_directories()
    temporary = RUNTIME_STATE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"pid": os.getpid(), "port": port, "token": secret, "version": APP_VERSION}),
        encoding="utf-8",
    )
    temporary.replace(RUNTIME_STATE_PATH)


def _launch_url(state: dict[str, Any]) -> str | None:
    try:
        return f"http://127.0.0.1:{int(state['port'])}/launch?token={state['token']}"
    except (KeyError, TypeError, ValueError):
        return None


def _open_existing_instance(*, timeout_s: float = 15.0) -> bool:
    """Open an existing instance, including one that is still starting."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = _read_runtime_state()
        url = _launch_url(state or {})
        if url:
            try:
                return bool(webbrowser.open(url))
            except Exception:
                logging.getLogger(__name__).exception("Could not open the existing Sightline instance")
                return False
        time.sleep(0.1)
    return False


def _wait_for_health(port: int, *, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/api/health"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except (OSError, URLError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"Sightline server did not become healthy: {last_error}")


def _show_error(message: str) -> None:
    logging.getLogger(__name__).error(message)
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, f"{PRODUCT_NAME} startup error", 0x10)


def _show_about_dialog(message: str) -> None:
    """Show the native About dialog outside the tray callback's message loop."""
    ctypes.windll.user32.MessageBoxW(None, message, f"About {PRODUCT_NAME}", 0x40)


def _show_about() -> None:
    if os.name != "nt":
        return
    message = (
        f"{PRODUCT_NAME} {APP_VERSION}\n{PRODUCT_SUBTITLE}\n\n"
        f"Build: {BUILD_COMMIT}\nLicense: AGPL-3.0-only\nSource: {SOURCE_URL}\n\n"
        "Capture, OCR, and encounter processing run locally on this computer."
    )
    threading.Thread(
        target=_show_about_dialog,
        args=(message,),
        name="Sightline About",
        daemon=True,
    ).start()


def _open_data_folder() -> None:
    ensure_runtime_directories()
    if os.name == "nt":
        os.startfile(APP_DATA_ROOT)  # type: ignore[attr-defined]


def _smoke_test() -> int:
    from .db import init_db
    from .tesseract_backend import get_backend_stats, image_to_data

    init_db()
    required = [
        resource_path("app", "static", "index.html"),
        resource_path("brand", "favicons", "favicon.svg"),
        resource_path("brand", "favicons", "favicon.ico"),
    ]
    if IS_FROZEN:
        required.append(resource_path("tesseract", "tessdata", "eng.traineddata"))
    missing = [str(path) for path in required if not path.is_file()]
    if IS_FROZEN and find_spec("pytesseract") is not None:
        missing.append("development-only pytesseract module is present in the packaged runtime")
    if IS_FROZEN and resource_path("tesseract", "tesseract.exe").exists():
        missing.append("development-only Tesseract CLI executable is present in the packaged runtime")
    if IS_FROZEN:
        try:
            import tkinter

            tk_root = tkinter.Tk()
            tk_root.withdraw()
            try:
                tk_root.update_idletasks()
                if not tk_root.tk.eval("info patchlevel"):
                    raise RuntimeError("Tcl did not report a runtime version")
                for tk_resource in (
                    resource_path("_tcl_data", "init.tcl"),
                    resource_path("_tk_data", "tk.tcl"),
                ):
                    if not tk_resource.is_file():
                        missing.append(str(tk_resource))
            finally:
                tk_root.destroy()
        except Exception as exc:
            missing.append(f"bundled Tk runtime: {exc}")
    if IS_FROZEN:
        try:
            from PIL import Image, ImageDraw, ImageFont

            image = Image.new("RGB", (600, 100), "white")
            font = ImageFont.truetype("arial.ttf", 44)
            ImageDraw.Draw(image).text((16, 18), "SIGHTLINE OCR TEST", fill="black", font=font)
            ocr_data = image_to_data(image, config="--psm 7")
            recognized = " ".join(str(text) for text in ocr_data.get("text", [])).upper()
            if "SIGHTLINE" not in recognized:
                missing.append("bundled in-process OCR recognition self-test")
            if get_backend_stats().get("last_backend") != "tesserocr":
                missing.append("packaged OCR did not use the in-process backend")
        except Exception as exc:
            missing.append(f"bundled in-process OCR self-test: {exc}")
    result = {"ok": not missing, "version": APP_VERSION, "commit": BUILD_COMMIT, "missing": missing}
    log = logging.getLogger(__name__)
    if missing:
        log.error("Packaged smoke test failed: %s", json.dumps(result))
    else:
        log.info("Packaged smoke test passed: %s", json.dumps(result))
    if sys.stdout is not None:
        print(json.dumps(result))
    return 0 if not missing else 1


def _run_tray(open_ui, shutdown_event: ShutdownEvent) -> None:
    try:
        import pystray
        from PIL import Image

        icon_path = resource_path("Sightline.ico")
        if not icon_path.is_file():
            icon_path = resource_path("brand", "favicons", "android-chrome-512x512.png")
        image = Image.open(icon_path).convert("RGBA")
        menu = pystray.Menu(
            pystray.MenuItem("Open Sightline", lambda _icon, _item: open_ui(), default=True),
            pystray.MenuItem("Open Data Folder", lambda _icon, _item: _open_data_folder()),
            pystray.MenuItem("About", lambda _icon, _item: _show_about()),
            pystray.MenuItem("Exit", lambda icon, _item: (shutdown_event.set(), icon.stop())),
        )
        icon = pystray.Icon("Sightline", image, PRODUCT_NAME, menu)

        def stop_tray_on_external_shutdown() -> None:
            while not shutdown_event.is_set():
                time.sleep(0.2)
            icon.stop()

        def notify_runtime_errors() -> None:
            from .service import service

            last_notified_error: str | None = None
            while not shutdown_event.is_set():
                error = service.status().get("last_error")
                if error and error != last_notified_error:
                    last_notified_error = str(error)
                    try:
                        icon.notify(
                            f"Capture, OCR, or database processing failed: {last_notified_error[:240]}",
                            PRODUCT_NAME,
                        )
                    except Exception:
                        logging.getLogger(__name__).exception("Could not show a Sightline tray notification")
                time.sleep(1.0)

        threading.Thread(target=stop_tray_on_external_shutdown, daemon=True).start()
        threading.Thread(target=notify_runtime_errors, daemon=True).start()
        icon.run()
    except Exception:
        logging.getLogger(__name__).exception("Tray icon failed; Sightline will keep running without it")
        while not shutdown_event.is_set():
            time.sleep(0.25)


def run(*, no_browser: bool = False) -> int:
    _configure_logging()
    enable_dpi_awareness()
    mutex = WindowsMutex(MUTEX_NAME)
    if mutex.already_exists:
        if not no_browser and not _open_existing_instance():
            _show_error(
                "Sightline is already running, but Windows could not open its browser UI. "
                "Use Open Sightline from the tray icon."
            )
        mutex.close()
        return 0

    shutdown_event = ShutdownEvent(SHUTDOWN_EVENT_NAME)
    server = None
    server_thread: threading.Thread | None = None
    try:
        port = _available_port()
        secret = create_session_secret()
        os.environ["SIGHTLINE_SESSION_SECRET"] = secret
        from .main import app
        import uvicorn

        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
        server = uvicorn.Server(config)
        server_thread = threading.Thread(target=server.run, name="SightlineServer", daemon=False)
        server_thread.start()
        _wait_for_health(port)
        _write_runtime_state(port=port, secret=secret)
        state = {"port": port, "token": secret}

        def open_ui() -> bool:
            url = _launch_url(state)
            try:
                return bool(url and webbrowser.open(url))
            except Exception:
                logging.getLogger(__name__).exception("Could not open the default browser")
                return False

        if not no_browser and not open_ui():
            _show_error(f"Sightline is running at http://127.0.0.1:{port}, but Windows could not open your browser.")

        watcher = threading.Thread(
            target=lambda: _watch_shutdown(shutdown_event, server),
            name="SightlineShutdownWatcher",
            daemon=True,
        )
        watcher.start()
        _run_tray(open_ui, shutdown_event)
        shutdown_event.set()
        return 0
    except Exception as exc:
        _show_error(str(exc))
        return 1
    finally:
        if server is not None:
            server.should_exit = True
        if server_thread and server_thread.is_alive():
            server_thread.join(timeout=15)
        try:
            RUNTIME_STATE_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        shutdown_event.close()
        mutex.close()


def _watch_shutdown(shutdown_event: ShutdownEvent, server) -> None:
    while not shutdown_event.is_set():
        time.sleep(0.2)
    server.should_exit = True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="Sightline")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--shutdown", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.shutdown:
        return shutdown_running_instance()
    if args.smoke_test:
        _configure_logging()
        return _smoke_test()
    signal.signal(signal.SIGINT, lambda _signum, _frame: signal_running_instance_shutdown())
    return run(no_browser=args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
