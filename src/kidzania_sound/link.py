"""2台のSurfaceを連携させるためのHTTP JSON APIサービス。

- 1台=スケジュール自動再生担当、1台=ステージショー手動操作担当、という運用を想定し、
  (a) スケジュール/設定の同期、(b) 再生状態連動(ダッキング)、(c) 一括停止・営業モード切替の
  伝播、の3種類のイベントを2台の間でやり取りする。
- 施設内の閉域LAN・端末2台限定という前提で、認証機構は入れず、送信元IPの簡易フィルタのみ行う。
- config.link.enabled=False の場合は全メソッドがno-opになり、単一PC運用には一切影響しない。
- 受信したイベントはinbound_queueに積むだけで、実際の適用(GUI操作・スケジューラー操作)は
  呼び出し側(MainWindow)がTkメインスレッドのroot.afterループでドレインして行う
  (http.serverのハンドラスレッドから直接Tkinter/APSchedulerを触ると安全でないため)。
- 送信(notify_async/push_config_async)は必ずバックグラウンドスレッドで行う。Tkメインスレッドから
  同期的にHTTPリクエストを送ると、ピアがオフラインの間GUIごとフリーズしてしまうため。
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .config import AppConfig

_KNOWN_EVENT_PATHS = {
    "/event/playback-started",
    "/event/playback-ended",
    "/event/stop-all",
    "/event/mode-changed",
    "/config/push",
}


class LinkService:
    def __init__(self, config: AppConfig, logger: logging.Logger):
        self._config = config
        self._logger = logger
        # 受信イベント(path, payload)。MainWindow側がroot.afterでドレインする。
        self.inbound_queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._running = False
        # 直近で連携先と通信できた時刻(送信成功/受信/ポーリング成功のいずれか)。
        # GUIの接続状態表示(「連携: 接続中/未接続」)に使う。
        self.last_contact_ts: Optional[float] = None
        # 直近で確認できた連携先の端末名(将来3台以上に拡張した場合の識別用)。
        self.peer_device_name: Optional[str] = None

    def mark_contact(self, peer_name: Optional[str] = None) -> None:
        self.last_contact_ts = time.time()
        if peer_name:
            self.peer_device_name = peer_name

    @property
    def enabled(self) -> bool:
        return self._config.link.enabled

    # ------------------------------------------------------------------
    # 起動・終了
    # ------------------------------------------------------------------
    def start(self) -> None:
        if not self.enabled:
            return
        self._running = True

        self._httpd = _Server(("0.0.0.0", self._config.link.listen_port), _Handler, self)
        self._server_thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._server_thread.start()

        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        self._logger.info(
            "リンク機能を開始しました(listen_port=%d, peer=%s:%d)",
            self._config.link.listen_port,
            self._config.link.peer_host,
            self._config.link.peer_port,
        )

    def shutdown(self) -> None:
        self._running = False
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

    # ------------------------------------------------------------------
    # 送信(非同期、Tkメインスレッドをブロックしない)
    # ------------------------------------------------------------------
    def notify_async(self, path: str, payload: dict) -> None:
        if not self.enabled:
            return
        threading.Thread(target=self._send, args=(path, payload), daemon=True).start()

    def push_config_async(self) -> None:
        if not self.enabled:
            return
        self.notify_async("/config/push", self.read_config_payload())

    def _send(self, path: str, payload: dict) -> None:
        url = f"http://{self._config.link.peer_host}:{self._config.link.peer_port}{path}"
        try:
            outgoing = {**payload, "from": self._config.device_name}
            body = json.dumps(outgoing).encode("utf-8")
            req = urllib.request.Request(
                url, data=body, method="POST", headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=self._config.link.http_timeout_seconds).read()
            self.mark_contact()
        except Exception:
            self._logger.warning("連携先への送信に失敗しました(%s): 接続できません", path)

    # ------------------------------------------------------------------
    # 設定同期(config push/pull)
    # ------------------------------------------------------------------
    def read_config_payload(self) -> dict:
        return {
            "schedule": self._config.read_schedule_raw(),
            "stage_shows": self._config.read_stage_shows_raw(),
        }

    def local_config_hash(self) -> str:
        return self._hash_payload(self.read_config_payload())

    @staticmethod
    def _hash_payload(payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # ポーリング(push取りこぼし時のフォールバック同期)
    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        # 監視間隔(poll_interval_seconds)は設定画面から変更されうるため、
        # ループの都度読み直して次回の待機時間に反映する(再起動不要で効かせるため)。
        while self._running:
            interval = max(5, self._config.link.poll_interval_seconds)
            time.sleep(interval)
            if not self._running:
                return
            self._poll_once()

    def _poll_once(self) -> None:
        peer = self._config.link
        version_url = f"http://{peer.peer_host}:{peer.peer_port}/config/version"
        try:
            with urllib.request.urlopen(version_url, timeout=peer.http_timeout_seconds) as resp:
                remote = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return  # ピア未接続。次回ポーリングで再試行するのみでログには残さない。

        self.mark_contact(remote.get("from"))
        if remote.get("hash") == self.local_config_hash():
            return

        pull_url = f"http://{peer.peer_host}:{peer.peer_port}/config/pull"
        try:
            with urllib.request.urlopen(pull_url, timeout=peer.http_timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            self._logger.warning("連携先との設定差分を検知しましたが、取得に失敗しました")
            return

        self._logger.info("連携先との設定差分をポーリングで検知し、取得しました")
        self.inbound_queue.put(("/config/push", payload))


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler_cls, link_service: LinkService):
        super().__init__(address, handler_cls)
        self.link_service = link_service


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - BaseHTTPRequestHandler API
        pass  # アプリのロガーに一本化するため標準エラー出力は抑止する

    @property
    def _service(self) -> LinkService:
        return self.server.link_service  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_peer(self) -> bool:
        peer_host = self._service._config.link.peer_host
        if peer_host and self.client_address[0] != peer_host:
            self._send_json(403, {"error": "forbidden"})
            return False
        return True

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._check_peer():
            return
        if self.path == "/config/version":
            self._service.mark_contact()
            self._send_json(
                200,
                {"hash": self._service.local_config_hash(), "from": self._service._config.device_name},
            )
        elif self.path == "/config/pull":
            self._service.mark_contact()
            self._send_json(200, self._service.read_config_payload())
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._check_peer():
            return
        if self.path in _KNOWN_EVENT_PATHS:
            body = self._read_json_body()
            self._service.mark_contact(body.get("from"))
            self._service.inbound_queue.put((self.path, body))
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "not found"})
