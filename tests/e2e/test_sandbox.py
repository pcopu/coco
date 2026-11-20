"""Safety prerequisites are real child-process negative controls."""


def test_sandbox_denies_external_network_state_and_unowned_signals(controller_factory, tmp_path):
    controller = controller_factory(start=False)
    outside = tmp_path / "outside.txt"
    outside.write_text("private sentinel")
    probe = controller.root / "probe.py"
    probe.write_text(
        "import errno, os, socket\n"
        f"outside = {str(outside)!r}\n"
        "def denied(action):\n"
        "    try: action()\n"
        "    except OSError as exc:\n"
        "        assert isinstance(exc, socket.gaierror) or exc.errno == errno.EACCES, exc\n"
        "        return\n"
        "    raise AssertionError('sandbox allowed forbidden action')\n"
        "denied(lambda: open(outside).read())\n"
        "denied(lambda: open(outside, 'w'))\n"
        f"denied(lambda: socket.socket().connect(('198.51.100.1', {controller.peer.port})))\n"
        "denied(lambda: socket.create_connection(('127.0.0.1', 9), timeout=.1))\n"
        "denied(lambda: socket.getaddrinfo('api.telegram.org', 443))\n"
        "denied(lambda: os.kill(os.getppid(), 0))\n"
        f"with socket.create_connection(('127.0.0.1', {controller.peer.port}), timeout=1) as s:\n"
        "    s.sendall(b'GET /invalid HTTP/1.0\\r\\n\\r\\n')\n"
        "    assert b'404' in s.recv(1000)\n"
        "print('ISOLATION VERIFIED')\n"
    )
    controller.env["PYTHONPATH"] += ":" + str(controller.root)
    proc = controller.launch(module="probe")
    assert proc.wait(timeout=5) == 0, controller.diagnostics()
    assert "ISOLATION VERIFIED" in controller.diagnostics()
    assert outside.read_text() == "private sentinel"
    assert {"connect", "dns", "kill"} <= set((controller.root / "denials.log").read_text().splitlines())
