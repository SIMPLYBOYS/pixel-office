#!/usr/bin/env python3
"""員工的無頭瀏覽器（MCP 伺服器 `browser`）：@playwright/mcp，所有流量經過只放行白名單網域的本機代理。

為什麼要包一層（issue #1）：@playwright/mcp 的 --allowed-origins 文件明寫「不是安全邊界、不管轉址」，而且瀏覽器跑在
員工的 Bash 沙箱外，網路不受 WebFetch 白名單限制——直接給等於開一條繞過白名單的路。所以：
- 起一個只做 CONNECT 的本機代理，只放行白名單網域（含子網域）的 443/80；其餘一律 403。純 HTTP 不轉，員工用 https。
- 瀏覽器所有請求（含轉址、子資源）都走這個代理；`<-loopback>` 讓 localhost 也走代理＝被擋，碰不到本機的橋與 cogito。
- WebRTC 的 UDP 不走代理會繞過它：Chromium 參數關掉。
- --isolated：瀏覽器狀態只在記憶體，關掉就清空，員工之間不共用 cookie 與登入。
白名單＝員工 profile（CLAUDE_CONFIG_DIR/settings.json）放行的 WebFetch 網域，每次啟動重讀；外加 Cloudflare 驗證頁要的網域。
只用標準函式庫：跑在 Claude Code 的行程樹裡，不依賴橋的 venv。
"""
import json
import os
import re
import select
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

PLAYWRIGHT_MCP = os.environ.get("OFFICE_BROWSER_MCP_PKG", "@playwright/mcp@0.0.78")   # 釘版本（稽核 #12 的同一個道理）
INFRA_HOSTS = ["challenges.cloudflare.com"]   # Cloudflare 的驗證頁本身從這裡載入；不放行就永遠過不了驗證
DOMAIN_RE = re.compile(r"WebFetch\(domain:([A-Za-z0-9.-]+)\)")


def allowlist(settings: Path) -> list[str]:
    """員工 profile 放行的 WebFetch 網域（www.X 視為 X）＋基礎設施網域＋OFFICE_BROWSER_EXTRA_HOSTS（逗號分隔）。讀不到就只剩後兩者。"""
    try:
        allow = json.loads(settings.read_text(encoding="utf-8")).get("permissions", {}).get("allow", [])
    except (OSError, ValueError):
        allow = []
    hosts = [m.group(1).lower() for a in allow if isinstance(a, str) and (m := DOMAIN_RE.fullmatch(a))]
    # 放行 www.X 等於放行整個 X：網頁的圖、腳本常放在 assets.X、static.X（Yourator 就是），只給 www 頁面會是空殼
    hosts = [h.removeprefix("www.") for h in hosts]
    extra = [h.strip().lower() for h in os.environ.get("OFFICE_BROWSER_EXTRA_HOSTS", "").split(",") if h.strip()]
    return sorted(set(hosts + INFRA_HOSTS + extra))


def host_allowed(host: str, domains: list[str]) -> bool:
    """網域本身或它的子網域才放行（cake.me 放行 www.cake.me；evilcake.me 不算）。"""
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


class Proxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, domains: list[str], ports: set[int]):
        self.domains, self.ports = domains, ports
        super().__init__(("127.0.0.1", 0), ProxyHandler)


class ProxyHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(4096).decode("latin-1").strip()
        while (h := self.rfile.readline(4096)) not in (b"\r\n", b"\n", b""):
            pass   # 丟掉其餘標頭
        parts = line.split()
        target = parts[1] if len(parts) == 3 and parts[0] == "CONNECT" else ""
        host, _, port = target.rpartition(":")
        if not target or not port.isdigit() or int(port) not in self.server.ports or not host_allowed(host.strip("[]"), self.server.domains):
            print(f"[office-browser] 擋下：{line[:120]}", file=sys.stderr, flush=True)
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        try:
            up = socket.create_connection((host.strip("[]"), int(port)), timeout=15)
        except OSError:
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.wfile.flush()
        socks = [self.connection, up]
        try:
            while True:
                r, _, _ = select.select(socks, [], [], 120)
                if not r:
                    break
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (up if s is self.connection else self.connection).sendall(data)
        except OSError:
            pass
        finally:
            up.close()


def start_proxy(domains: list[str], ports: set[int] = frozenset({80, 443})) -> Proxy:
    srv = Proxy(domains, set(ports))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    cfg_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude-office").expanduser()
    domains = allowlist(cfg_dir / "settings.json")
    proxy = start_proxy(domains)
    port = proxy.server_address[1]
    work = Path(tempfile.mkdtemp(prefix="office-browser-"))
    conf = work / "config.json"
    conf.write_text(json.dumps({"browser": {"launchOptions": {
        "proxy": {"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"},
        "args": ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--webrtc-ip-handling-policy=disable_non_proxied_udp"]}}}),
        encoding="utf-8")
    print(f"[office-browser] 代理 127.0.0.1:{port}，放行 {len(domains)} 個網域", file=sys.stderr, flush=True)
    # --headed：開一個看得到的視窗。無頭模式過不了 Cloudflare 的驗證頁（Cake 實測 403、卡在「正在執行安全驗證」），
    # 有視窗的才過得了；代價是班表跑的時候桌面會跳出瀏覽器視窗。預設無頭，要過 Cloudflare 的 profile 才在 args 帶這個。
    headless = [] if "--headed" in sys.argv[1:] else ["--headless"]
    argv = ["npx", "-y", PLAYWRIGHT_MCP, *headless, "--isolated", "--config", str(conf), "--block-service-workers",
            "--image-responses", "omit", "--output-dir", str(work / "out")]
    return subprocess.call(argv)   # stdio 直接交給它：Claude Code 跟它講 MCP，代理在這個行程裡陪跑


if __name__ == "__main__":
    sys.exit(main())
