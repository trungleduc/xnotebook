"""Network policy enforced by the host through CDP Fetch interception.

Every target (pages, dedicated/shared/service workers) is auto-attached while
paused, gets `Fetch.enable` for all URLs at the request stage, and only then is
resumed. Policy per request:

  SETUP  proxy-origin URLs carrying the run token   -> continue
         allowlisted upstream URLs                   -> rewritten to the proxy
         anything else                               -> BlockedByClient
  RUN    everything                                  -> BlockedByClient
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional

from .cdp import PipeConnection
from .proxy import Proxy

FETCH_TARGET_TYPES = {"page", "iframe", "worker", "shared_worker", "service_worker"}


class Firewall:
    def __init__(self, conn: PipeConnection, proxy: Proxy, log: Callable[[str], None] = lambda m: None) -> None:
        self.conn = conn
        self.proxy = proxy
        self.log = log
        self.sealed = False
        self.lock = threading.Lock()
        self.blocked: List[str] = []
        self.rewritten = 0
        self.requests_after_seal = 0
        self.sessions: Dict[str, dict] = {}
        self._attach_listeners: List[Callable[[str, dict], None]] = []
        conn.on_event(self._on_event)

    def on_attach(self, cb: Callable[[str, dict], None]) -> None:
        self._attach_listeners.append(cb)

    def install(self) -> None:
        self.conn.call(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
        )

    def seal(self) -> None:
        with self.lock:
            self.sealed = True
        self.proxy.seal()

    # -- events -----------------------------------------------------------
    def _on_event(self, method: str, params: dict, session_id: Optional[str]) -> None:
        if method == "Fetch.requestPaused":
            self._on_request(params, session_id)
        elif method == "Target.attachedToTarget":
            self._on_attached(params)
        elif method == "Target.detachedFromTarget":
            self.sessions.pop(params.get("sessionId", ""), None)

    def _on_attached(self, params: dict) -> None:
        sid = params["sessionId"]
        info = params.get("targetInfo", {})
        self.sessions[sid] = info
        ttype = info.get("type")
        send = self.conn.send
        if ttype in FETCH_TARGET_TYPES:
            send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}, sid)
        send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}, sid)
        for cb in list(self._attach_listeners):
            cb(sid, info)
        if params.get("waitingForDebugger"):
            send("Runtime.runIfWaitingForDebugger", {}, sid)

    def _on_request(self, params: dict, sid: Optional[str]) -> None:
        req_id = params["requestId"]
        url = params.get("request", {}).get("url", "")
        proxy = self.proxy
        with self.lock:
            sealed = self.sealed
            if sealed:
                self.requests_after_seal += 1
        if not sealed:
            if url.startswith(proxy.base):
                self.conn.send("Fetch.continueRequest", {"requestId": req_id}, sid)
                return
            if proxy.is_allowed(url):
                with self.lock:
                    self.rewritten += 1
                self.conn.send("Fetch.continueRequest", {"requestId": req_id, "url": proxy.upstream_url(url)}, sid)
                return
        with self.lock:
            self.blocked.append(url)
        self.log(f"firewall: blocked {'(sealed) ' if sealed else ''}{url[:200]}")
        self.conn.send("Fetch.failRequest", {"requestId": req_id, "errorReason": "BlockedByClient"}, sid)
