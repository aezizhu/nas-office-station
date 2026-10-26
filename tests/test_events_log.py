"""events.log: concurrent writers lose nothing, truncation keeps the newest 3000 lines, SGT stamps,
download endpoint."""
import datetime
import threading

N_THREADS, N_LINES = 8, 200


def test_concurrent_writes_lose_nothing(m):
    start = threading.Barrier(N_THREADS)

    def writer(t):
        start.wait()
        for i in range(N_LINES):
            m.log_event("conc", "t%d-i%d %s" % (t, i, "x" * 40))
    ts = [threading.Thread(target=writer, args=(t,)) for t in range(N_THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    with open(m.EVENTS, encoding="utf-8") as f:
        lines = f.read().split("\n")
    assert lines[-1] == ""                               # file ends with a newline
    lines = lines[:-1]
    assert len(lines) == N_THREADS * N_LINES             # below the 2 MB truncation size: nothing dropped
    msgs = []
    for line in lines:
        parts = line.split("\t")
        assert len(parts) == 3 and parts[1] == "conc", repr(line)   # no interleaved/torn lines
        msgs.append(parts[2].split(" ")[0])
    assert sorted(msgs) == sorted("t%d-i%d" % (t, i) for t in range(N_THREADS) for i in range(N_LINES))


def test_truncation_keeps_last_3000_lines(m):
    filler = "20260923-000000\told\t" + "y" * 700 + "\n"
    with open(m.EVENTS, "w") as f:
        f.write(filler * 3100)                            # ~2.2 MB > 2 MB
    m.log_event("newest", "after-truncation")
    with open(m.EVENTS, encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert len(lines) == 3000
    assert lines[-1].split("\t")[1:] == ["newest", "after-truncation"]
    assert not __import__("os").path.exists(m.EVENTS + ".tmp")


def test_print_tokens_are_masked(m):
    tok = "0123456789abcdef0123456789abcdef"
    m.log_event("upload-fail", "x.pdf /root/printjobs/%s/src.pdf %s" % (tok, tok))
    m.log_event("keep", "job 0123456789abcdef sha1 " + "a" * 40 + " upper " + tok.upper())
    with open(m.EVENTS, encoding="utf-8") as f:
        first, second = [line.split("\t")[2] for line in f.read().splitlines()]
    assert tok not in first and first == "x.pdf /root/printjobs/01234567…/src.pdf 01234567…"
    assert second == "job 0123456789abcdef sha1 " + "a" * 40 + " upper " + tok.upper()   # only 32-hex tokens


def test_upload_failure_log_does_not_leak_the_token(m, client, poppler):
    import io
    import re
    r = client.post("/upload", data={"file": (io.BytesIO(b"%PDF-1.4 broken"), "x.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 422
    body = client.get("/events.log").get_data(as_text=True)
    assert "\tupload-fail\t" in body
    assert not re.search(r"[0-9a-f]{32}", body), "a full print token is in the public log"


def test_message_capped_at_500_chars(m):
    m.log_event("long", "z" * 2000)
    with open(m.EVENTS, encoding="utf-8") as f:
        assert len(f.read().splitlines()[0].split("\t")[2]) == 500


def test_timestamps_are_singapore_time(m):
    before = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    got = datetime.datetime.strptime(m.sgt(), "%Y%m%d-%H%M%S")
    assert abs((got - before.replace(tzinfo=None)).total_seconds()) < 5


def test_events_download(m, client):
    r = client.get("/events.log")
    assert r.status_code == 200 and "No events recorded yet" in r.get_data(as_text=True)
    m.log_event("dl", "hello")
    r = client.get("/events.log")
    assert r.status_code == 200 and r.mimetype == "text/plain"
    assert "\tdl\thello" in r.get_data(as_text=True)
