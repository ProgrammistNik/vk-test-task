import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# ---------- Steam path detection (Windows registry + fallbacks) ----------

def _win_registry_get_steam_path() -> Optional[str]:
    if sys.platform != "win32":
        return None
    try:
        import winreg
        candidates = [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ]
        for root, subkey, value in candidates:
            try:
                with winreg.OpenKey(root, subkey) as k:
                    path, _ = winreg.QueryValueEx(k, value)
                    if path and os.path.isdir(path):
                        return path
            except OSError:
                continue
    except Exception:
        return None
    return None


def detect_steam_root() -> str:
    reg = _win_registry_get_steam_path()
    if reg:
        return reg

    fallbacks: List[str] = []
    if sys.platform == "win32":
        fallbacks = [
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Steam"),
        ]
    else:
        home = os.path.expanduser("~")
        fallbacks = [
            os.path.join(home, ".steam", "steam"),
            os.path.join(home, ".local", "share", "Steam"),
        ]

    for p in fallbacks:
        if os.path.isdir(p):
            return p

    raise FileNotFoundError("Не удалось найти Steam. Укажи путь вручную через STEAM_PATH.")


# ---------- Parsing helpers ----------

RATE_RE = re.compile(r"Current download rate:\s*([0-9]*\.?[0-9]+)\s*Mbps", re.IGNORECASE)
APP_UPDATE_CHANGED_RE = re.compile(r"AppID\s+(\d+)\s+update changed\s*:\s*(.*)$", re.IGNORECASE)
APP_STATE_CHANGED_RE = re.compile(r"AppID\s+(\d+)\s+state changed\s*:\s*(.*)$", re.IGNORECASE)

def mbps_to_mib_s(mbps: float) -> float:
    return (mbps * 1_000_000 / 8) / (1024 * 1024)

def read_text_tail(path: str, max_bytes: int = 512_000) -> str:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - max_bytes)
        f.seek(start, os.SEEK_SET)
        data = f.read()
    return data.decode("utf-8", errors="replace")

def parse_acf_name(acf_path: str) -> Optional[str]:
    if not os.path.isfile(acf_path):
        return None
    try:
        with open(acf_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith('"name"'):
                    parts = re.findall(r'"([^"]*)"', line)
                    if len(parts) >= 2 and parts[0].lower() == "name":
                        return parts[1]
    except Exception:
        return None
    return None

def appid_to_name(steam_root: str, appid: str) -> Optional[str]:
    steamapps = os.path.join(steam_root, "steamapps")
    acf = os.path.join(steamapps, f"appmanifest_{appid}.acf")
    return parse_acf_name(acf)

def normalize_status(raw_status: Optional[str], mbps: Optional[float]) -> str:
    s = (raw_status or "").lower()

    if "suspend" in s or "(suspended)" in s or "paused" in s or "stopping" in s:
        return "PAUSED"

    if mbps is not None and mbps > 0 and "update running" in s:
        return "DOWNLOADING"

    if "update queued" in s or "update required" in s or "update started" in s:
        return "QUEUED"

    return "IDLE"


def get_active_appids(steam_root: str) -> List[str]:
    """
    Most reliable: steamapps/downloading contains folders named by AppID currently downloading/updating.
    """
    downloading_dir = os.path.join(steam_root, "steamapps", "downloading")
    if not os.path.isdir(downloading_dir):
        return []
    appids: List[str] = []
    for name in os.listdir(downloading_dir):
        if name.isdigit() and os.path.isdir(os.path.join(downloading_dir, name)):
            appids.append(name)
    appids.sort(key=int)
    return appids


def parse_latest_status_by_appid(content_log_tail: str) -> Dict[str, str]:
    """
    Scan tail from bottom to top, remember first seen status per appid (i.e. most recent).
    """
    statuses: Dict[str, str] = {}
    for line in reversed(content_log_tail.splitlines()):
        m = APP_UPDATE_CHANGED_RE.search(line)
        if not m:
            m = APP_STATE_CHANGED_RE.search(line)
        if m:
            appid = m.group(1)
            if appid not in statuses:
                statuses[appid] = m.group(2).strip()
    return statuses


@dataclass
class Snapshot:
    mbps: Optional[float]
    mib_s: Optional[float]
    apps: List[Tuple[str, str, str]]  # (name, appid, human_status)


def build_snapshot(steam_root: str) -> Snapshot:
    log_path = os.path.join(steam_root, "logs", "content_log.txt")
    if not os.path.isfile(log_path):
        raise FileNotFoundError(f"Не найден лог Steam: {log_path}")

    tail = read_text_tail(log_path)

    # latest global rate
    mbps: Optional[float] = None
    for line in reversed(tail.splitlines()):
        m = RATE_RE.search(line)
        if m:
            mbps = float(m.group(1))
            break

    mib_s = mbps_to_mib_s(mbps) if mbps is not None else None

    # statuses by appid from logs
    status_by_appid = parse_latest_status_by_appid(tail)

    # active appids
    active_appids = get_active_appids(steam_root)

    apps_out: List[Tuple[str, str, str]] = []
    for appid in active_appids:
        raw_status = status_by_appid.get(appid)
        human = normalize_status(raw_status, mbps)
        name = appid_to_name(steam_root, appid) or f"AppID {appid}"
        apps_out.append((name, appid, human))

    return Snapshot(mbps=mbps, mib_s=mib_s, apps=apps_out)


# ---------- Main loop: 1 minute, 5 times ----------

NAME_W = 40
STATUS_W = 14
SPEED_W = 26

def main():
    steam_root = os.environ.get("STEAM_PATH") or detect_steam_root()
    print(f"Steam найден: {steam_root}")
    print("Мониторинг загрузки: 5 измерений, раз в 1 минуту.\n")

    for i in range(1, 6):
        try:
            snap = build_snapshot(steam_root)

            if snap.mbps is None:
                speed_str = "скорость: нет данных"
            else:
                speed_str = f"скорость: {snap.mbps:.3f} Mbps ({snap.mib_s:.2f} MiB/s)"

            if not snap.apps:
                print(f"[{i}/5] NO ACTIVE DOWNLOADS | {speed_str}")
            else:
                print(f"[{i}/5] Загрузки: {len(snap.apps)}")

                print(
                    f"      {'Название':<{NAME_W}} | "
                    f"{'Статус':<{STATUS_W}} | "
                    f"{'Скорость':<{SPEED_W}}"
                )
                print(
                    f"      {'-' * NAME_W} | "
                    f"{'-' * STATUS_W} | "
                    f"{'-' * SPEED_W}"
                )

                for name, appid, status in snap.apps:
                    if status == "DOWNLOADING" and snap.mbps is not None:
                        speed = f"{snap.mbps:.2f} Mbps ({snap.mib_s:.2f} MiB/s)"
                    else:
                        speed = "—"

                    print(
                        f"      {name:<{NAME_W}} | "
                        f"{status:<{STATUS_W}} | "
                        f"{speed:<{SPEED_W}}"
                    )

        except Exception as e:
            print(f"[{i}/5] Ошибка: {e}")

        if i != 5:
            time.sleep(20)


if __name__ == "__main__":
    main()
