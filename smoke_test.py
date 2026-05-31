"""Standalone integration smoke test for the Caucus hub.

Starts the hub in-process, then drives the HTTP API the way two agents would:
register, broadcast, direct-message, verify delivery, exercise pause + stop.
Run with: ``python smoke_test.py``.
"""

from __future__ import annotations

import threading
import time

import httpx
import uvicorn

from caucus.hub import app

BASE = "http://127.0.0.1:8799"


def _run_server() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8799, log_level="warning")


def main() -> None:
    t = threading.Thread(target=_run_server, daemon=True)
    t.start()
    time.sleep(1.2)  # let uvicorn bind

    with httpx.Client(base_url=BASE, timeout=30.0) as c:
        alpha = c.post("/register", json={"project": "project-a"}).json()
        beta = c.post("/register", json={"project": "project-b"}).json()
        assert alpha["token"] and beta["token"], "registration failed"
        print("registered:", c.get("/peers").json()["peers"])

        # Direct message project-a -> project-b
        r = c.post("/send", json={"token": alpha["token"], "to": "project-b",
                                  "content": "renaming the `name` field to `full_name`"})
        assert r.status_code == 200, r.text
        print("sent direct:", r.json())

        got = c.get("/receive", params={"token": beta["token"], "timeout": 3}).json()
        assert any("full_name" in m["content"] for m in got["messages"]), got
        print("project-b received:", [m["content"] for m in got["messages"]])

        # Broadcast project-b -> all (project-a should get it, sender should not)
        c.post("/send", json={"token": beta["token"], "to": "all",
                              "content": "deploying v2.3.0 to staging"})
        got = c.get("/receive", params={"token": alpha["token"], "timeout": 3}).json()
        assert any("v2.3.0" in m["content"] for m in got["messages"]), got
        print("project-a received broadcast:", [m["content"] for m in got["messages"]])

        # Pause: a send is logged but not delivered until resume
        c.post("/control", json={"action": "pause"})
        c.post("/send", json={"token": alpha["token"], "to": "project-b",
                              "content": "held while paused"})
        held = c.get("/receive", params={"token": beta["token"], "timeout": 2}).json()
        assert held["messages"] == [], f"expected no delivery while paused, got {held}"
        print("pause OK: delivery held")

        c.post("/control", json={"action": "resume"})
        released = c.get("/receive", params={"token": beta["token"], "timeout": 3}).json()
        assert any("held while paused" in m["content"] for m in released["messages"]), released
        print("resume OK: held message delivered")

        # Rate limit: burst beyond the bucket should eventually 429
        codes = [c.post("/send", json={"token": beta["token"], "to": "all",
                                       "content": f"spam {i}"}).status_code
                 for i in range(12)]
        assert 429 in codes, f"expected a 429 in burst, got {codes}"
        print("rate limit OK: burst produced", codes.count(429), "x 429")

        # Stop: every agent's receive returns a stop control, sends are rejected
        c.post("/control", json={"action": "stop"})
        stop = c.get("/receive", params={"token": alpha["token"], "timeout": 3}).json()
        assert any(m["kind"] == "control" and m["content"] == "stop"
                   for m in stop["messages"]), stop
        blocked = c.post("/send", json={"token": beta["token"], "to": "all",
                                        "content": "should be rejected"})
        assert blocked.status_code == 409, blocked.status_code
        print("stop OK: stop signal delivered, sends rejected (409)")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
