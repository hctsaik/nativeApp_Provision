#!/usr/bin/env python3
"""第八輪浸泡測試 —— 前七輪「沒有測」的那些,用真的東西測。

第七輪的浸泡測試(high_risk_soak.py)證明了狀態機在真磁碟上是對的,但它有一個
共通的漏洞:凡是需要「真的殼、真的視窗、真的 WebView2」的地方,它都用一棵
python 生 python 的替身程序樹代替。替身樹證明了 `taskkill /T` 這個「機制」會動,
卻一次都沒有執行過使用者每天走一百次的那條路:

    start.bat -> bootstrap -> launcher -> cim-light.exe -> WebView2 開窗
              -> 使用者按 Start -> engine_shim -> /control/start -> Streamlit 算繪
              -> 使用者關窗 -> 全部收乾淨

這一輪就是去走那條路,外加六個沒人測過的高風險情境。

涵蓋:
  [R8-1] 真的 cim-light.exe、真的 WebView2 視窗、真的動態 port、真的關窗
         (含 CDP 進到 WebView2 裡面按下 Start,證明 iframe 真的載入了那個 port)
  [R8-2] Streamlit 健康檢查逾時的時候,系統會怪誰?(怪版本 = 好版本被判死)
  [R8-3] 磁碟空間不足:建置到一半 ENOSPC
  [R8-4] 長路徑:交付樹的固定成本吃掉多少 MAX_PATH 預算
  [R8-5] current 指向的版本槽被使用者手工刪掉
  [R8-6] 兩個「真程序」同時搶同一個 runtime fingerprint
  [R8-7] 真的 exFAT 卷:硬連結 fallback 與匯出(需要真的 USB,見下)

不涵蓋,而且不假裝涵蓋:
  * 全新 Windows VM(沒有 Python / 沒有 WebView2)雙擊 start.bat。
    這台機器有 WebView2、有 Python,任何模擬都是自欺欺人。必須在真的 VM 上做。
  * [R8-7] 需要一個真的 exFAT/FAT 磁碟區。這台機器只有 C:(NTFS),而且沒有管理員
    權限可以掛 VHD,所以預設會「明確跳過並印出 SKIP」——不是 PASS。
    插上 USB 之後:  set CIM_SOAK_USB=E:\   然後重跑
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import errno
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from high_risk_soak import (  # noqa: E402  (共用同一組 check/FAILURES,不要各寫一份)
    FAILURES, bootstrap, check, make_app, request_for, state_of, step,
)
from provision_builder.streamlit_desktop import (  # noqa: E402
    build_into_store, export_full_tree,
)

WORK = ROOT / "dist" / "soak8"
APP_ID = "app-r8"                     # app- 前綴:portal 會用整頁 iframe 算繪它
CDP_PORT = 9333
NOT_TESTED: list[str] = []


def not_tested(name: str, why: str) -> None:
    print(f"    [SKIP] {name} —— 沒有測,也不假裝測了:{why}", flush=True)
    NOT_TESTED.append(f"{name}({why})")


def run_bootstrap(tree: Path, *args: str, timeout: float = 120):
    """不帶參數的 bootstrap 就是「啟動 App」—— start.bat 走的正是這條。

    它會一路開到真的視窗,然後「等使用者關窗」才回來。所以任何一支測試如果
    預期它應該失敗、結果它其實成功了,就會把整個測試卡到天荒地老。逾時就收屍,
    而且把「它竟然開起來了」如實回報,不要假裝沒發生。
    """
    proc = subprocess.Popen(
        [sys.executable, str(tree / "bootstrap" / "bootstrap.py"), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONUTF8="1"))
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, (out or "") + (err or "")
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        proc.communicate()
        return None, f"(逾時 {timeout:.0f}s:它沒有結束 —— 很可能真的把 App 開起來了)"


# ══ Windows 視窗 API:證明「真的有一個視窗」 ═════════════════════════════════
#
# 「殼還活著」不等於「視窗開起來了」。一個 WebView2 起不來的殼可能還在跑,只是
# 永遠不會有視窗。要證明使用者真的看得到東西,只能去問作業系統:這個 PID 底下
# 有沒有一個「可見的、而且有大小的」最上層視窗。

user32 = ctypes.WinDLL("user32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
WM_CLOSE = 0x0010


def _windows_of(pid: int) -> list[tuple[int, str]]:
    """這個 PID 擁有的、可見的、非零尺寸的最上層視窗。"""
    found: list[tuple[int, str]] = []

    def _cb(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
            return True            # 有 HWND 不代表使用者看得到東西
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found


def _pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True,
                         encoding="cp950", errors="replace")
    return str(pid) in (out.stdout or "")


def _shell_pids() -> set[int]:
    """現在還活著的 cim-light.exe。(wmic 在 Win11 已經拿掉了,不能用。)"""
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq cim-light.exe", "/NH", "/FO", "CSV"],
        capture_output=True, text=True, encoding="cp950", errors="replace")
    pids = set()
    for line in (out.stdout or "").splitlines():
        cols = [c.strip('" ') for c in line.split('","')]
        if len(cols) >= 2 and cols[1].isdigit():
            pids.add(int(cols[1]))
    return pids


def _runtime_pids(python_exe: Path) -> set[int]:
    """還活著的、屬於「這個 store 的 runtime」的 python 程序。

    用執行檔的完整路徑比對,不是用名字掃 python.exe —— 開發機上到處都是 python,
    按名字掃會把別人的程序算進來(而產品程式碼裡也明文禁止 name-scan kill)。
    """
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or "
         "Name='pythonw.exe'\" | Where-Object { $_.ExecutablePath -eq "
         f"'{python_exe}'" + " } | Select-Object -ExpandProperty ProcessId"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return {int(x) for x in (out.stdout or "").split() if x.strip().isdigit()}


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


# ══ CDP:進到真的 WebView2 裡面,證明 iframe 真的載入了那個動態 port ═════════
#
# WebView2 認得 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS,所以可以在不改一行 Rust 的
# 情況下把 remote-debugging-port 打開。stdlib 零第三方:CDP 的 target 清單走 HTTP,
# 但 Runtime.evaluate 要走 WebSocket —— 所以這裡有一個夠用就好的 WebSocket client。


class WS:
    """RFC 6455 的一小塊:CDP 只需要 text frame。"""

    def __init__(self, url: str, timeout: float = 15.0):
        rest = url.split("://", 1)[1]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("CDP WebSocket 握手失敗(連線被關閉)")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            raise OSError(f"CDP WebSocket 握手失敗:{buf.split(b'?')[0][:60]!r}")
        self._rest = buf.split(b"\r\n\r\n", 1)[1]
        self._id = 0

    def _recv_exact(self, n: int) -> bytes:
        while len(self._rest) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("CDP 連線中斷")
            self._rest += chunk
        out, self._rest = self._rest[:n], self._rest[n:]
        return out

    def _send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        self.sock.sendall(bytes(header) +
                          bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _recv_text(self) -> str:
        while True:
            b0, b1 = self._recv_exact(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            data = self._recv_exact(n)
            if opcode == 0x1:
                return data.decode("utf-8", "replace")
            if opcode == 0x8:
                raise OSError("CDP 連線被對方關閉")

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        self._id += 1
        want = self._id
        self._send_text(json.dumps({"id": want, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(self._recv_text())
            if msg.get("id") == want:            # 中間夾雜一堆 event,略過
                return msg
        raise TimeoutError(f"CDP {method} 逾時")

    def evaluate(self, expression: str):
        msg = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True})
        return msg.get("result", {}).get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _cdp_page_target(timeout: float = 25.0) -> dict | None:
    """WebView2 真的載入了一個頁面嗎?(載不出來就不會有 page target)"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list",
                                        timeout=2) as resp:
                for target in json.loads(resp.read().decode("utf-8")):
                    if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                        return target
        except OSError:
            pass
        time.sleep(0.4)
    return None


# ══ 一次完整的啟動:跑真的 bootstrap.py(= start.bat 走的那一條) ══════════════
#
# 第一版的這支測試想「在程序內把 launcher 接起來」,好拿到 /control/start 的 token。
# 那是錯的,而且錯得很有教育意義:store 佈局的包「沒有」自帶殼和 python —— 共用的
# 殼由 bootstrap 用 CIM_SHELL_EXE 注入,而 launcher 必須「被 store 的 runtime python
# 執行」(manifest 的 _python 就是 sys.executable)。在開發機的 python 裡接線,跑的
# 就不是使用者那一份 runtime,整支測試會變成一個精緻的自我欺騙。
#
# 所以:直接跑真的 bootstrap.py。token 在殼 → shim → launcher 之間自然流動,我們
# 一個字都不必知道 —— 而「按下 Start」則從 WebView2 裡面用 CDP 真的按下去。


class Cycle:
    """一次完整的「雙擊 start.bat → 開窗 →(可選)按 Start → 使用者關窗」。"""

    def __init__(self, tree: Path, *, cdp: bool):
        self.tree = tree
        self.cdp = cdp
        version_dir = tree / "apps" / APP_ID / "versions" / "v1.0.0"
        manifest = json.loads((version_dir / "app-package.json").read_text("utf-8"))
        self.python_exe = (tree / "deps" / "runtimes"
                           / manifest["runtime_fingerprint"] / "python.exe")
        self.window_titles: list[str] = []
        self.shell_pid: int | None = None
        self.port: int | None = None
        self.url: str | None = None
        self.iframe_src: str | None = None
        self.portal_text: str = ""
        self.exit_code: int | None = None
        self.output: str = ""

    def run(self, *, press_start: bool) -> None:
        env = dict(os.environ, PYTHONUTF8="1")
        if self.cdp:
            env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = \
                f"--remote-debugging-port={CDP_PORT}"

        before_shells = _shell_pids()
        lines: list[str] = []
        proc = subprocess.Popen(
            [sys.executable, str(self.tree / "bootstrap" / "bootstrap.py")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=env)

        def _pump() -> None:
            for line in proc.stdout:            # launcher 的 stdout 是繼承下來的
                lines.append(line)
        pump = threading.Thread(target=_pump, daemon=True)
        pump.start()

        try:
            # 首次啟動 bootstrap 會逐檔驗 runtime(600MB),慢是正常的。
            self.shell_pid = self._await_shell_pid(before_shells, timeout=240)
            if self.shell_pid:
                self._await_window(timeout=40)
            if self.cdp and press_start:
                self._drive_webview()
            elif self.shell_pid:
                # 視窗剛冒出來的頭一兩秒,WebView2 還在初始化,訊息迴圈可能還沒
                # 準備好接 WM_CLOSE —— 就像使用者按了 X 但視窗還在轉圈。給它一點
                # 時間穩定,不然關窗訊息會被吃掉,程序就一直不結束。
                time.sleep(3)
            # 使用者關窗:按 X。沒關掉就再按一次(視窗還在忙),最多幾次 ——
            # 這是真實使用者會做的事,不是 taskkill。
            self.exit_code = None
            for _ in range(6):
                self._close_window()
                try:
                    self.exit_code = proc.wait(timeout=20)
                    break
                except subprocess.TimeoutExpired:
                    if proc.poll() is not None:
                        self.exit_code = proc.returncode
                        break
            if self.exit_code is None:            # 關窗六次都沒結束 —— 這本身是缺陷
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True)
        finally:
            if proc.poll() is None:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True)
            pump.join(timeout=5)
            self.output = "".join(lines)

        for line in lines:                       # [start] … ready at http://127.0.0.1:PORT
            if "ready at http" in line:
                self.url = line.split("ready at", 1)[1].strip()
                self.port = int(self.url.rsplit(":", 1)[1].rstrip("/"))

    def _await_shell_pid(self, before: set[int], timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            fresh = _shell_pids() - before
            if fresh:
                return sorted(fresh)[0]
            time.sleep(0.3)
        return None

    def _await_window(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            wins = _windows_of(self.shell_pid)
            if wins:
                self.window_titles = [t for _h, t in wins]
                return
            time.sleep(0.3)

    def _drive_webview(self) -> None:
        target = _cdp_page_target()
        if not target:
            return
        ws = WS(target["webSocketDebuggerUrl"])
        try:
            ws.call("Runtime.enable")
            self.portal_text = str(ws.evaluate(
                "(document.body && document.body.innerText || '').slice(0, 200)") or "")
            # portal 可能自己就把 app 起起來(app- 前綴 = 整頁 iframe),也可能要按
            # 一下 Start。兩種都接受,但一定要看到 iframe 真的指向那個動態 port。
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                src = ws.evaluate(
                    "(() => { const f = document.querySelector('iframe');"
                    " return f && f.src ? f.src : ''; })()")
                if src:
                    self.iframe_src = str(src)
                    return
                ws.evaluate(
                    "(() => { const hit = [...document.querySelectorAll("
                    "'button,a,[role=button]')].find(e => /start|啟動|開始|執行/i"
                    ".test(e.textContent || '')); if (hit) hit.click(); })()")
                time.sleep(1.5)
        finally:
            ws.close()

    def _close_window(self) -> None:
        """使用者按下右上角的 X —— 不是 taskkill。"""
        if not self.shell_pid:
            return
        for hwnd, _title in _windows_of(self.shell_pid):
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


# ══ [R8-1] 真的殼、真的視窗、真的動態 port、真的關窗 ═══════════════════════════

def test_real_shell_window(tree: Path) -> None:
    step("R8-1", "真的 cim-light.exe:真的 WebView2 視窗、真的動態 port、真的關窗")

    before_shells = _shell_pids()
    cycle = Cycle(tree, cdp=True)
    before_py = _runtime_pids(cycle.python_exe)
    cycle.run(press_start=True)

    # 這台機器的 WDAC 是 enforced、而 cim-light.exe 沒有簽章 —— 它到底跑不跑得起來,
    # 在這一行之前,沒有任何一支測試回答過。
    check("未簽章的 cim-light.exe 在這台 WDAC(enforced)機器上真的跑得起來",
          cycle.shell_pid is not None, f"pid={cycle.shell_pid}")
    check("而且真的開出一個「看得見、有大小」的視窗(WebView2 真的起來了)",
          bool(cycle.window_titles), f"標題={cycle.window_titles}")
    check("Streamlit 在動態 port 上(不是寫死的 8501)",
          bool(cycle.port) and cycle.port != 8501, f"port={cycle.port} url={cycle.url}")

    if cycle.portal_text:
        print(f"    portal 畫面上的字:{cycle.portal_text[:90]!r}", flush=True)
    if cycle.iframe_src:
        check("WebView2 裡的 iframe 真的載入了那個動態 port(不是空殼,也不是丟到外部瀏覽器)",
              bool(cycle.port) and str(cycle.port) in cycle.iframe_src, cycle.iframe_src[:60])
    else:
        not_tested("iframe 真的載入動態 port",
                   "CDP 進不到 WebView2 裡面(視窗有開,但沒能證明它載入了什麼)")

    check("使用者關窗之後,整條鏈乾淨結束(exit 0 —— 關窗不是錯誤)",
          cycle.exit_code == 0, f"exit={cycle.exit_code}")

    # 「App 真的被跑過」的證據不是我們自己說的:bootstrap 只有在 healthy marker 的
    # 內容是「真的開過 session」時,才會把這個版本提交成 last-known-good。
    st = state_of(tree, APP_ID)
    if cycle.iframe_src:
        check("bootstrap 把它提交成 last-known-good(= 殼→shim→launcher→Streamlit 整條鏈真的通了)",
              st.get("last_known_good") == "v1.0.0", f"last_known_good={st.get('last_known_good')}")

    check("殼的程序真的沒了",
          (not _pid_alive(cycle.shell_pid)) if cycle.shell_pid else False)
    check("Streamlit / Python 真的沒了(不是留在背景吃 CPU 和記憶體)",
          not (_runtime_pids(cycle.python_exe) - before_py),
          str(_runtime_pids(cycle.python_exe) - before_py))
    check("port 真的放掉了(下一次啟動 bind 得回來)",
          is_port_free(cycle.port) if cycle.port else False, f"port={cycle.port}")
    check("沒有留下多出來的 cim-light.exe",
          not (_shell_pids() - before_shells), str(_shell_pids() - before_shells))


def test_repeated_open_close(tree: Path, cycles: int = 3) -> None:
    step("R8-1b", f"連續開關 {cycles} 次:不可以累積殘留程序、不可以吃掉 port")
    print(f"    說清楚:這裡跑 {cycles} 次,不是 10 次 —— 每一次都是真的開窗、真的關窗。"
          f"沒有靜靜地砍掉覆蓋率,就是明講跑幾次。", flush=True)

    before_shells = _shell_pids()
    probe = Cycle(tree, cdp=False)
    before_py = _runtime_pids(probe.python_exe)

    ports: list[int | None] = []
    for i in range(cycles):
        cycle = Cycle(tree, cdp=False)
        cycle.run(press_start=False)
        ports.append(cycle.port)
        print(f"      第 {i + 1} 次:port={cycle.port} exit={cycle.exit_code} "
              f"視窗={'有' if cycle.window_titles else '沒有'}", flush=True)

    leaked_py = _runtime_pids(probe.python_exe) - before_py
    check(f"{cycles} 次全部都真的開出視窗、拿到 port", all(ports), str(ports))
    check("一個殘留的 Python 都沒有累積", not leaked_py, str(leaked_py))
    check("沒有累積 cim-light.exe", not (_shell_pids() - before_shells))
    check("每一次的 port 都是可用的動態 port", all(p and p != 8501 for p in ports), str(ports))
    check("用過的 port 全部還回去了", all(is_port_free(p) for p in ports if p), str(ports))


# ══ [R8-2] Streamlit 起不來的時候,系統會怪誰? ═══════════════════════════════

def test_slow_start_blames_the_machine_not_the_version(tree: Path) -> None:
    step("R8-2", "Streamlit 健康檢查逾時 → 這是「機器慢」還是「版本壞」?")
    print("    真實情境:目標機第一次開機,Defender 正在逐位元組掃 600MB 的 runtime,\n"
          "    Streamlit 的 server 花了 65 秒才回應 /_stcore/health(預設上限 60 秒)。\n"
          "    這台機器磁碟太快、快取又熱,重現不了「慢」—— 所以改成把上限調到 1 秒,\n"
          "    讓「真的 Streamlit」真的逾時。程式碼走的是同一條路,結論也就是同一個。",
          flush=True)

    version_dir = tree / "apps" / APP_ID / "versions" / "v1.0.0"
    manifest_path = version_dir / "app-package.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    original = manifest.get("startup_timeout_seconds")
    manifest["startup_timeout_seconds"] = 1
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")

    before = state_of(tree, APP_ID)
    try:
        code, said = run_bootstrap(tree, timeout=120)
        after = state_of(tree, APP_ID)

        timed_out = any(k in said for k in ("健康", "起不來", "沒有正常啟動")) \
            or "not healthy" in said.lower()
        check("(前提)真的走到「Streamlit 沒有及時健康」這條路", timed_out,
              said.strip().splitlines()[-1][:70] if said.strip() else f"exit={code}")

        failed = [e.get("version") for e in after.get("failed_versions", [])]
        blamed = "v1.0.0" in failed
        check("Streamlit 起不來「不該」被當成這個版本的錯(機器慢 ≠ 版本壞)",
              not blamed,
              "v1.0.0 被標記為失敗了 —— 但這是第一次安裝,沒有版本可以退回去,"
              "使用者第一天就進死巷" if blamed else f"failed={failed}")
        check("而且不該把好版本從 current 上拔掉",
              after.get("current") == before.get("current"),
              f"{before.get('current')} → {after.get('current')}")
        check("訊息要指向「這台電腦」的可執行動作(防毒/重試),不是叫人換版本",
              any(k in said for k in ("防毒", "重試", "再試", "這台電腦", "掃描")),
              said.strip().splitlines()[-1][:70] if said.strip() else "(無輸出)")
    finally:
        if original is None:
            manifest.pop("startup_timeout_seconds", None)
        else:
            manifest["startup_timeout_seconds"] = original
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
        bootstrap(tree, "--clear-failed", "v1.0.0")


# ══ [R8-3] 磁碟空間不足 ═══════════════════════════════════════════════════════

def test_disk_full(project: Path) -> None:
    step("R8-3", "建置到一半,磁碟就滿了(ENOSPC)")
    print("    這台機器有 515 GB 可用,又沒有管理員權限可以掛一個小磁碟區,所以\n"
          "    「把磁碟真的塞爆」做不到。改成在 syscall 邊界注入真的 ENOSPC:複製\n"
          "    runtime(那是 500MB、最可能遇到磁碟滿的一步)複製到第 40 個檔案時,\n"
          "    runtime._copy_file 丟 OSError(ENOSPC)。上面那一整層(staging 清理、\n"
          "    狀態、錯誤訊息)全部真的在跑 —— 這是故障注入,不是假的通過。\n"
          "    (第一版把注入打在 shutil.copy2,但 runtime 複製走的是自己的 _copy_file,\n"
          "     根本沒經過 copy2,注入落空、build 照樣成功 —— 一個沒測到東西的測試。)",
          flush=True)

    from provision_builder.streamlit_desktop import runtime as rt_mod

    store = WORK / "full-store"
    shutil.rmtree(store, ignore_errors=True)       # 全新 store:runtime 一定會被複製
    real_copy = rt_mod._copy_file
    hits = {"n": 0}

    def exploding_copy(source, target, should_cancel=None):
        hits["n"] += 1
        if hits["n"] == 40:                        # 複製到一半才炸,不是第一個檔案
            raise OSError(errno.ENOSPC, "There is not enough space on the disk")
        return real_copy(source, target, should_cancel)

    rt_mod._copy_file = exploding_copy
    try:
        result = build_into_store(request_for(project, store), store, version="v1.0.0",
                                  progress=lambda _l: None)
    except Exception as exc:                       # noqa: BLE001
        check("磁碟滿了,不可以噴一個裸例外給使用者", False, f"{type(exc).__name__}: {exc}")
        return
    finally:
        rt_mod._copy_file = real_copy

    check("(前提)ENOSPC 真的被注入到 runtime 複製途中了",
          hits["n"] >= 40, f"_copy_file 被呼叫 {hits['n']} 次")
    message = " ".join(result.errors)
    check("磁碟滿了 → 不會謊報建置成功", not result.ok, "竟然回報 ok=True" if result.ok else "")
    check("而且訊息是人話,講得出「磁碟空間」這件事",
          any(k in message for k in ("空間", "磁碟", "space", "disk")), message[:80])
    check("沒有把半套的版本留在 store 裡冒充成品",
          not (store / "apps" / APP_ID / "versions" / "v1.0.0" / ".complete").is_file())
    staging = [p for p in store.rglob("*.staging-*")] if store.exists() else []
    check("沒有留下吃磁碟的 .staging-*(磁碟都滿了,還留垃圾是雪上加霜)",
          not staging, f"{len(staging)} 個殘留")


# ══ [R8-4] 長路徑 ═════════════════════════════════════════════════════════════

def test_long_path_budget(tree: Path) -> None:
    step("R8-4", "長路徑:交付樹的固定成本,吃掉多少 260 字元的預算?")
    print("    注意:這台機器的 LongPathsEnabled=1,所以「在這裡跑得過」完全不能證明\n"
          "    「在預設的 Windows 上跑得過」—— 而且 start.bat 是 cmd.exe 跑的,cmd.exe\n"
          "    不管登錄檔怎麼設都不支援 >260。所以這裡測的是「機器無關」的那一題:\n"
          "    交付樹自己有多深?使用者還剩多少預算可以用?", flush=True)

    root_len = len(str(tree))
    deepest, worst = 0, ""
    for path in tree.rglob("*"):
        rel = len(str(path)) - root_len
        if rel > deepest:
            deepest, worst = rel, str(path)[root_len:]
    check("量得出交付樹自己的相對路徑深度", deepest > 0,
          f"最深 {deepest} 字元:…{worst[-55:]}")

    # 一個非常真實的使用者根目錄(中文使用者名 + OneDrive 公司名 + 桌面)
    realistic = r"C:\Users\陳彥廷\OneDrive - 某某科技股份有限公司\桌面\CIM 交付"
    total = len(realistic) + deepest
    check("放進一個「很真實的」深根目錄之後,整條路徑仍然在 260 以內",
          total < 260,
          f"{len(realistic)}(根)+ {deepest}(樹)= {total} 字元"
          + ("" if total < 260 else " → 預設的 Windows 上會炸,cmd.exe 一定會炸"))
    check("最深的那個檔案來自 runtime(site-packages),不是我們自己疊出來的層數",
          ("runtimes" in worst or "site-packages" in worst), worst[:50])


# ══ [R8-5] current 指向的版本槽被手工刪掉 ═════════════════════════════════════

def test_current_version_deleted_by_hand(tree: Path) -> None:
    step("R8-5", "使用者在檔案總管裡,把 current 指向的版本資料夾直接刪掉了")
    st = state_of(tree, APP_ID)
    current = str(st["current"])
    victim = tree / "apps" / APP_ID / "versions" / current
    if not check("(前提)current 的版本槽現在真的在", victim.is_dir(), current):
        return

    shutil.rmtree(victim)                      # 使用者按了 Delete
    out = bootstrap(tree, "--status")
    said = (out.stdout or "") + (out.stderr or "")

    check("--status 不會因為版本槽不見了就噴裸 traceback",
          "Traceback" not in said,
          said.strip()[-80:] if "Traceback" in said else "")
    check("而且它說得出「這個版本不見了 / 不完整」",
          any(k in said for k in ("不見", "找不到", "遺失", "缺", "不完整", "損毀")),
          said.strip().splitlines()[-1][:70] if said.strip() else "(無輸出)")

    code2, said2 = run_bootstrap(tree, timeout=90)
    check("雙擊 start.bat 也不會噴裸 traceback(使用者只會做這個動作)",
          "Traceback" not in said2,
          said2.strip()[-80:] if "Traceback" in said2 else "")
    check("而且不是無聲卡住:要嘛自己救回來,要嘛講清楚要怎麼修",
          bool(said2.strip()) and code2 is not None,
          said2.strip().splitlines()[-1][:70] if said2.strip() else "(無輸出)")


# ══ [R8-6] 兩個真程序搶同一個 runtime fingerprint ═════════════════════════════

_RACER = r"""
import sys, pathlib
sys.path.insert(0, r"{src}")
from provision_builder.streamlit_desktop import (
    build_into_store, BuildRequest, find_entrypoint, find_runtime, find_shell)
project = pathlib.Path(r"{project}")
store = pathlib.Path(r"{store}")
req = BuildRequest(
    project_dir=project, entrypoint=find_entrypoint(project).value,
    display_name="race", output_dir=store, shell_exe=find_shell().value,
    runtime_template=find_runtime().value, preferred_port=0,
    requirements=project / "requirements.lock.txt", app_id_override="app-race",
)
r = build_into_store(req, store, version="{version}", progress=lambda _l: None)
print("OK" if r.ok else "FAIL:" + "; ".join(r.errors)[:200])
"""


def test_two_processes_race_one_runtime(project: Path) -> None:
    step("R8-6", "兩個「真程序」同時建置,搶同一個 runtime fingerprint")
    print("    現有的並行測試不是單程序內的執行緒,就是別的子系統(PackageService)。\n"
          "    per-fingerprint lock 宣稱的是「跨程序」—— 那就得用兩個真的程序去撞。",
          flush=True)
    store = WORK / "race-store"
    shutil.rmtree(store, ignore_errors=True)

    procs = [
        subprocess.Popen(
            [sys.executable, "-c",
             _RACER.format(src=ROOT / "src", project=project, store=store, version=v)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONUTF8="1"))
        for v in ("v1.0.0", "v2.0.0")
    ]
    outs = [p.communicate(timeout=1800) for p in procs]

    for i, (out, err) in enumerate(outs, start=1):
        check(f"第 {i} 個程序建置成功(沒有被鎖擋死、也沒有半途崩潰)",
              "OK" in (out or ""), ((out or "") + (err or "")).strip()[-90:])

    rt_dir = store / "deps" / "runtimes"
    entries = [p for p in rt_dir.iterdir() if p.is_dir()] if rt_dir.is_dir() else []
    real = [p for p in entries if ".staging-" not in p.name]
    staging = [p for p in entries if ".staging-" in p.name]
    check("兩個程序搶完之後,只有「一份」runtime(不是各裝一份)",
          len(real) == 1, str([p.name for p in real]))
    check("沒有留下半套的 staging", not staging, str([p.name for p in staging]))
    check("那一份 runtime 是完整的(有 .complete)",
          bool(real) and all((p / ".complete").is_file() for p in real))


# ══ [R8-7] 真的 exFAT 卷 ══════════════════════════════════════════════════════

def test_exfat_volume(store: Path) -> None:
    step("R8-7", "真的 exFAT/FAT 磁碟區:硬連結 fallback 與匯出")
    usb = os.environ.get("CIM_SOAK_USB")
    if not usb:
        not_tested("exFAT 上的硬連結 fallback 與匯出",
                   "這台機器只有 C:(NTFS),又沒有管理員權限可以掛 VHD。"
                   "插上 USB 之後 set CIM_SOAK_USB=E:\\ 重跑")
        return

    target = Path(usb) / "cim-r8"
    shutil.rmtree(target, ignore_errors=True)
    vol = subprocess.run(["cmd", "/c", "vol", Path(usb).drive],
                         capture_output=True, text=True, encoding="cp950", errors="replace")
    print(f"    目標:{usb}  {(vol.stdout or '').strip().splitlines()[0] if vol.stdout else ''}",
          flush=True)

    tree = Path(export_full_tree(store, target, app_id=APP_ID, version="v1.0.0").out_dir)
    check("匯出到 exFAT 沒有崩潰(os.link 在這種檔案系統上會丟 OSError)", tree.is_dir())
    files = [p for p in tree.rglob("*") if p.is_file()][:300]
    check("USB 上寫的是「真檔案」不是連結(換一台電腦要能自足)",
          all(p.stat().st_nlink == 1 for p in files), f"抽驗 {len(files)} 個檔案")
    out = bootstrap(tree, "--status")
    check("在 exFAT 上跑得起來(鎖也要能在沒有硬連結的檔案系統上運作)",
          out.returncode == 0, ((out.stdout or "") + (out.stderr or "")).strip()[-70:])


# ══ main ══════════════════════════════════════════════════════════════════════

def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)

    project = WORK / "app"
    make_app(project, "r8")
    store = WORK / "store"

    print("\n[準備] 建一個真的 store(真的可攜 Python + 真的 pip install)…", flush=True)
    req = request_for(project, store)
    req.app_id_override = APP_ID
    result = build_into_store(req, store, version="v1.0.0", progress=lambda _l: None)
    if not check("準備:建置 v1.0.0", result.ok, "; ".join(result.errors)[:400]):
        return 1
    tree = Path(export_full_tree(store, WORK / "deliver", app_id=APP_ID,
                                 version="v1.0.0").out_dir)
    if not check("準備:交付樹匯出", (tree / "start.bat").is_file()):
        return 1

    test_real_shell_window(tree)
    test_repeated_open_close(tree)
    test_slow_start_blames_the_machine_not_the_version(tree)
    test_disk_full(project)
    test_long_path_budget(tree)
    test_two_processes_race_one_runtime(project)
    test_exfat_volume(store)
    test_current_version_deleted_by_hand(tree)      # 會破壞 tree,放最後

    print("\n" + "=" * 70)
    if NOT_TESTED:
        print("沒有測到(明講,不算通過):")
        for n in NOT_TESTED:
            print("  ·", n)
    if FAILURES:
        print(f"\n{len(FAILURES)} 項未通過:")
        for f in FAILURES:
            print("  ·", f)
        return 1
    print("\n全部通過。")
    print("提醒:全新 Windows VM(沒有 Python / 沒有 WebView2)雙擊 start.bat 這一項,")
    print("      仍然「不在」任何自動測試的涵蓋範圍內 —— 它必須在真的 VM 上做。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
