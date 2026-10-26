"""printer_online() must probe the address the CUPS queue really sends to (lpstat -v device-uri),
falling back to STATION_PRINTER_IP only when the queue can't be read."""
import socket

import pytest

import station_support as sup


@pytest.fixture()
def lpstat_v(m, monkeypatch):
    box = {"out": "", "raise": None, "calls": []}

    def hook(cmd, real_run, *a, **kw):
        box["calls"].append(cmd)
        assert cmd == ["lpstat", "-v", m.QUEUE], cmd
        if box["raise"]:
            raise box["raise"]
        return sup.completed(box["out"])
    monkeypatch.setattr(m, "subprocess", sup.SubprocessProxy(hook))
    return box


@pytest.mark.parametrize("uri,ip", [
    ("socket://1.2.3.4:9100", "1.2.3.4"),
    ("socket://1.2.3.4", "1.2.3.4"),
    ("ipp://host:631/ipp/print", "host"),
    ("ipp://printer.example/ipp/print", "printer.example"),
    ("socket://admin@192.0.2.50:9100", "192.0.2.50"),
    ("lpd://m437-office.lan/queue", "m437-office.lan"),
    ("socket://[fe80::1]:9100", "fe80::1"),
])
def test_queue_ip_from_device_uri(m, lpstat_v, uri, ip):
    lpstat_v["out"] = "device for %s: %s\n" % (m.QUEUE, uri)
    assert m.queue_printer_ip() == ip


@pytest.mark.parametrize("out,exc", [
    ("", None),
    ("lpstat: Invalid destination name in list \"M437\".\n", None),
    ("device for M437: dnssd://HP%20LaserJet._ipp._tcp.example/\n", None),
    (None, OSError("lpstat missing")),
])
def test_queue_ip_falls_back_to_env(m, lpstat_v, out, exc):
    lpstat_v["out"], lpstat_v["raise"] = out or "", exc
    assert m.queue_printer_ip() == m.PRINTER_IP == "192.0.2.1"


@pytest.fixture()
def tcp(monkeypatch):
    box = {"open": set(), "calls": []}

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_connect(addr, timeout=None):
        box["calls"].append((addr, timeout))
        if addr in box["open"]:
            return Conn()
        raise ConnectionRefusedError(61, "Connection refused")
    monkeypatch.setattr(socket, "create_connection", fake_connect)
    return box


def test_online_probes_the_queue_address(m, lpstat_v, tcp):
    lpstat_v["out"] = "device for M437: socket://1.2.3.4:9100\n"
    tcp["open"] = {("1.2.3.4", 9100)}
    assert m.printer_online() == (True, "1.2.3.4")
    assert [c[0] for c in tcp["calls"]] == [("1.2.3.4", 9100)]
    assert all(t and t <= 5 for _, t in tcp["calls"])        # bounded probe


def test_online_via_ipp_port(m, lpstat_v, tcp):
    lpstat_v["out"] = "device for M437: ipp://host:631/ipp/print\n"
    tcp["open"] = {("host", 631)}
    assert m.printer_online() == (True, "host")
    assert [c[0] for c in tcp["calls"]] == [("host", 9100), ("host", 631)]


def test_offline_when_nothing_answers(m, lpstat_v, tcp):
    lpstat_v["out"] = "device for M437: socket://1.2.3.4:9100\n"
    assert m.printer_online() == (False, "1.2.3.4")
    assert [c[0] for c in tcp["calls"]] == [("1.2.3.4", 9100), ("1.2.3.4", 631)]


def test_fallback_address_is_probed_when_queue_unreadable(m, lpstat_v, tcp):
    lpstat_v["raise"] = OSError("no lpstat")
    tcp["open"] = {("192.0.2.1", 9100)}
    assert m.printer_online() == (True, "192.0.2.1")


def test_job_ids_are_whole_words(m):
    text = "M437-290  office 1 x\n\n   \nM437-2 office 2 y\n"
    assert m._job_ids(text) == {"M437-290", "M437-2"}
    assert "M437-29" not in m._job_ids(text)
    assert m._job_ids("") == set() and m._job_ids(None) == set()
