"""watch_print_job against scripted lpstat/ipptool output (fake clock: no real sleeping), and /cancel.

CUPS facts the scripts model: `lpstat -l -o Q` lists pending jobs — one NON-indented line per job
(first field = job id) followed by that job's indented Status:/Alerts:/queued-for lines;
`lpstat -W completed -o Q` lists finished jobs — CUPS's "completed" set is every job in state
>= canceled, i.e. completed, CANCELED and ABORTED alike (scheduler/ipp.c get_jobs), so it can't tell
success from cancel/abort. The watcher therefore asks the job itself via
`ipptool -tv ipp://localhost/jobs/<n> get-job-attributes.test` ("job-state (enum) = X") and only falls
back to the lpstat list when ipptool gives no answer."""
import pytest

import station_support as sup

LINE = "%s  office  1024  Tue Sep 23 10:00:00 2026"
BLOCK = "\tStatus: Connecting to printer.\n\tAlerts: job-printing\n\tqueued for M437"
FILTER_MSG = "Error: The print filter could not process this file. Export a new PDF from its original application and retry"
UNKNOWN_MSG = "Cannot determine the result of job M437-29. Check for printed pages before retrying"
IDLE = "printer M437 is idle.  enabled since Tue Sep 22 02:59:49 2026"
DESC = "1 copies / A4 / Single-sided / All 3 pages"
IPP_TEST = "/usr/share/cups/ipptool/get-job-attributes.test"

DONE_MSG = "The printer received all data (%s). Check the printer and collect the pages" % DESC
ABORTED_MSG = "Error: Print job M437-29 was aborted. Check the printer panel, then retry"
CANCELED_MSG = "Job M437-29 was cancelled at the printer or elsewhere. Remaining pages will not print"

MISSING = object()          # ipptool not installed → FileNotFoundError from subprocess


def ipptool_output(state):
    """What `ipptool -tv` prints. state=None → the job is unknown to CUPS (no job-state line)."""
    head = ('"%s":\n    Get-Job-Attributes:\n        attributes-charset (charset) = utf-8\n'
            '        attributes-natural-language (naturalLanguage) = en\n' % IPP_TEST)
    if state is None:
        return head + "        status-code = client-error-not-found (client-error-not-found)\n" \
                      "    Get job attributes                                                   [FAIL]\n"
    return head + ("        job-id (integer) = 29\n        job-state (enum) = %s\n"
                   "        job-state-reasons (keyword) = job-%s\n"
                   "    Get job attributes                                                   [PASS]\n"
                   % (state, state))


class Cups:
    """Scripted CUPS. One step per watcher poll (a poll starts with `lpstat -l -o`):
    queue (ids still queued), blocks ({id: indented lines}, default BLOCK), rc (non-zero exit of
    `lpstat -l -o`: empty output, like a busy/restarting cupsd), ipp ('completed'|'canceled'|'aborted'|
    'processing-stopped'|None=job unknown|MISSING=no ipptool), completed (ids in `lpstat -W completed`),
    p (`lpstat -l -p` text), raise (exception for this poll), on_ipp (callable run when ipptool is asked —
    to simulate a /cancel racing the watcher). `cancel <id>` calls are recorded."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.i = 0
        self.step = None
        self.calls = []

    def hook(self, cmd, real_run, *a, **kw):
        self.calls.append(list(cmd))
        if cmd == ["lpstat", "-l", "-o", "M437"]:
            self.step = self.steps[min(self.i, len(self.steps) - 1)]
            self.i += 1
            if "raise" in self.step:
                raise self.step["raise"]
            if self.step.get("rc"):
                return sup.completed("", "lpstat: Bad file descriptor", self.step["rc"])
            blocks = self.step.get("blocks", {})
            return sup.completed("".join("%s\n%s\n" % (LINE % j, blocks.get(j, BLOCK))
                                         for j in self.step.get("queue", [])))
        step = self.step
        if cmd[0] == "ipptool":
            assert cmd[1] == "-tv" and cmd[2].startswith("ipp://localhost/jobs/") and cmd[3] == IPP_TEST, cmd
            if step.get("on_ipp"):
                step["on_ipp"]()
            state = step.get("ipp", MISSING)
            if state is MISSING:
                raise FileNotFoundError(2, "No such file or directory", "ipptool")
            return sup.completed(ipptool_output(state))
        if cmd == ["lpstat", "-W", "completed", "-o", "M437"]:
            return sup.completed("\n".join(LINE % j for j in step.get("completed", [])) + "\n")
        if cmd == ["lpstat", "-l", "-p", "M437"]:
            return sup.completed(step.get("p", IDLE) + "\n")
        if cmd[0] == "cancel":
            return sup.completed()
        raise AssertionError("unexpected command %r" % (cmd,))

    def ran(self, name):
        return [c for c in self.calls if c[0] == name]

    def cancels(self):
        return [c[1:] for c in self.calls if c[0] == "cancel"]

    def ran_completed_list(self):
        return [c for c in self.calls if c[:3] == ["lpstat", "-W", "completed"]]


@pytest.fixture()
def watch(m, monkeypatch, job_history):
    monkeypatch.setattr(m, "QUEUE", "M437")
    clock = sup.FakeClock()
    monkeypatch.setattr(m, "time", clock)

    def run(steps, cups_id="M437-29", sheets=3, pj=None):
        cups = Cups(steps)
        monkeypatch.setattr(m, "subprocess", sup.SubprocessProxy(cups.hook))
        if pj is None:
            pj = m.new_job("print", cups=cups_id)
            m.job_update(pj, state="running", msg="Sent to the printer (%s); printing..." % DESC)
        start = clock.t
        m.watch_print_job(pj, cups_id, DESC, sheets)
        states = [kw.get("state") for j, kw in job_history if j == pj and "state" in kw]
        return m.job_view(pj, "print"), states, clock.t - start, cups
    return run


# ---------------------------------------------------------------- outcome via ipptool (primary)

def test_ipptool_completed_is_done(m, watch):
    v, states, took, cups = watch([{"queue": ["M437-290"], "ipp": "completed"}])
    assert v == {"state": "done", "msg": DONE_MSG, "file": ""}
    assert "打印完成" not in v["msg"]                      # CUPS accepted ≠ paper out
    assert [c[2] for c in cups.ran("ipptool")] == ["ipp://localhost/jobs/29"]
    assert cups.ran_completed_list() == []                 # no need for the ambiguous list
    assert took == 4


def test_ipptool_canceled_is_cancelled_not_success(m, watch):
    v, states, took, cups = watch([{"queue": [], "ipp": "canceled", "completed": ["M437-29"]}])
    assert v["state"] == "cancelled" and v["msg"] == CANCELED_MSG
    assert "Printer received the job" not in v["msg"]
    assert cups.ran_completed_list() == []
    with open(m.EVENTS, encoding="utf-8") as f:
        assert "\tprint-canceled\tM437-29" in f.read()


def test_ipptool_aborted_is_error(m, watch):
    v, states, took, cups = watch([{"queue": [], "ipp": "aborted", "completed": ["M437-29"]}])
    assert v["state"] == "error" and v["msg"] == ABORTED_MSG
    with open(m.EVENTS, encoding="utf-8") as f:
        assert "\tprint-lost\tM437-29 aborted" in f.read()


@pytest.mark.parametrize("ipp", ["processing", "pending-held"])
def test_ipptool_unexpected_state_is_not_success(m, watch, ipp):
    v, states, took, cups = watch([{"queue": [], "ipp": ipp, "completed": ["M437-29"]}])
    assert v["state"] == "error" and v["msg"] == ABORTED_MSG


# ---------------------------------------------------------------- lpstat fallback (ipptool gives nothing)

@pytest.mark.parametrize("ipp", [MISSING, None], ids=["ipptool-missing", "job-unknown-to-ipptool"])
def test_fallback_done_needs_exact_id_in_completed_list(m, watch, ipp):
    # our job 29 left the queue; 290 is still queued (substring match would wait forever)
    v, states, took, cups = watch([{"queue": ["M437-290"], "ipp": ipp, "completed": ["M437-2", "M437-29"]}])
    assert v == {"state": "done", "msg": DONE_MSG, "file": ""}
    assert len(cups.ran_completed_list()) == 1
    assert took == 4


@pytest.mark.parametrize("ipp", [MISSING, None], ids=["ipptool-missing", "job-unknown-to-ipptool"])
def test_fallback_substring_in_completed_is_not_our_job(m, watch, ipp):
    # outcome can't be established → say so honestly; don't claim "aborted, reprint" (could double-print)
    v, states, took, cups = watch([{"queue": [], "ipp": ipp, "completed": ["M437-290", "M437-2"]}])
    assert v["state"] == "unknown" and v["msg"] == UNKNOWN_MSG
    assert "重新打印" not in v["msg"] and cups.cancels() == []
    with open(m.EVENTS, encoding="utf-8") as f:
        assert "\tprint-unknown\tM437-29" in f.read()


def test_still_queued_keeps_waiting_then_done(m, watch):
    v, states, took, cups = watch([{"queue": ["M437-29"]}, {"queue": ["M437-29"]},
                                   {"queue": [], "ipp": "completed"}])
    assert v["state"] == "done" and took == 12
    assert len(cups.ran("ipptool")) == 1                 # only asked once the job left the queue


# ---------------------------------------------------------------- blocked / errors / deadline

def test_blocked_message_set_then_cleared(m, watch):
    v, states, took, cups = watch([
        {"queue": ["M437-29"], "p": "printer M437 now printing M437-29.  enabled since X\n\tMedia empty: out of paper"},
        {"queue": ["M437-29"], "p": "printer M437 now printing M437-29.  enabled since X\n\tout of paper"},
        {"queue": ["M437-29"], "p": "printer M437 now printing M437-29.  enabled since X"},
        {"queue": [], "ipp": "completed"},
    ])
    assert states == ["running", "blocked", "running", "done"]     # blocked set once, then cleared
    assert v["state"] == "done"


@pytest.mark.parametrize("ptext,want", [
    ("printer M437 disabled since X -\n\tPaused", "The print queue is stopped"),
    ("printer M437 is idle. enabled since X\n\tmedia-jam-error", "The printer has a paper jam"),
    ("printer M437 now printing. enabled since X\n\tThe printer's cover is open.", "The printer cover is open"),
    ("printer M437 now printing. enabled since X\n\tmarker-supply-empty toner-empty", "The printer is out of toner"),
])
def test_blocked_reasons(m, watch, job_history, ptext, want):
    watch([{"queue": ["M437-29"], "p": ptext}, {"queue": [], "ipp": "completed"}])
    blocked = [kw["msg"] for j, kw in job_history if kw.get("state") == "blocked"]
    assert blocked == ["Error: %s. Your job remains queued and will resume after the problem is resolved" % want]


def test_three_consecutive_errors_give_unknown(m, watch):
    v, states, took, cups = watch([{"raise": OSError("lpstat: cups not running")}])
    assert v["state"] == "unknown"
    assert v["msg"] == "Cannot track job M437-29. Check the printer for output"
    assert took == 12 and len(cups.calls) == 3


def test_errors_reset_after_a_good_poll(m, watch):
    err = {"raise": OSError("transient")}
    v, states, took, cups = watch([err, err, {"queue": ["M437-29"]}, err, err,
                                   {"queue": [], "ipp": "completed"}])
    assert v["state"] == "done"


@pytest.mark.parametrize("sheets,limit", [(0, 124), (1, 124), (3, 132), (100, 520)])
def test_deadline_scales_with_sheets(m, watch, sheets, limit):
    v, states, took, cups = watch([{"queue": ["M437-29"]}], sheets=sheets)
    assert v["state"] == "slow" and "waiting too long" in v["msg"]
    assert took == 120 + 4 * max(1, sheets) == limit


@pytest.mark.parametrize("block", [
    "\tStatus: Filter failed\n\tqueued for M437",
    "\tStatus: Filter errors\n\tqueued for M437",
    "\tAlerts: job-completed-with-errors\n\tqueued for M437",
    "\tAlerts: cups-filter-crashed job-printing\n\tqueued for M437",
])
def test_own_filter_failure_cancels_and_explains(m, watch, block):
    v, states, took, cups = watch([{"queue": ["M437-29"], "blocks": {"M437-29": block}},
                                   {"queue": [], "ipp": "completed"}])
    assert v == {"state": "error", "msg": FILTER_MSG, "file": ""}
    assert cups.cancels() == [["M437-29"]]                # the stuck job is removed from the queue
    assert took == 4                                        # acted on the first poll that saw it
    assert "blocked" not in states                          # not "out of paper / jam" — it's the file
    with open(m.EVENTS, encoding="utf-8") as f:
        assert "\tprint-filter-failed\tM437-29" in f.read()


def test_processing_stopped_via_ipptool_every_5th_poll(m, watch):
    v, states, took, cups = watch([{"queue": ["M437-29"], "ipp": "processing-stopped"}])
    assert v == {"state": "error", "msg": FILTER_MSG, "file": ""}
    assert cups.cancels() == [["M437-29"]]
    assert took == 20 and len(cups.ran("ipptool")) == 1     # asked once, on the 5th poll


@pytest.mark.parametrize("ipp", ["processing", "pending", MISSING])
def test_in_queue_job_not_stopped_is_left_alone(m, watch, ipp):
    v, states, took, cups = watch([{"queue": ["M437-29"], "ipp": ipp}] * 12 + [{"queue": [], "ipp": "completed"}])
    assert v["state"] == "done" and cups.cancels() == []
    assert len(cups.ran("ipptool")) == 2 + 1                # polls 5 and 10, then the final outcome


def test_other_jobs_filter_failure_does_not_touch_ours(m, watch):
    other = ("\tStatus: Filter failed\n\tAlerts: job-completed-with-errors cups-filter-crashed "
             "media-jam-error cover-open\n\tqueued for M437")
    v, states, took, cups = watch([{"queue": ["M437-28", "M437-29"], "blocks": {"M437-28": other}},
                                   {"queue": ["M437-28", "M437-29"], "blocks": {"M437-28": other}},
                                   {"queue": [], "ipp": "completed"}])
    assert v["state"] == "done" and cups.cancels() == []    # neither ours nor theirs is cancelled
    assert "error" not in states and "blocked" not in states


def test_printer_level_filter_message_is_not_our_failure(m, watch):
    v, states, took, cups = watch([{"queue": ["M437-29"], "p": "printer M437 is idle.  enabled since X\n\tFilter failed"},
                                   {"queue": [], "ipp": "completed"}])
    assert v["state"] == "done" and cups.cancels() == [] and "blocked" not in states


def test_door_open_alert_blocks(m, watch, job_history):
    watch([{"queue": ["M437-29"], "p": "printer M437 now printing M437-29.  enabled since X\n\tAlerts: door-open"},
           {"queue": [], "ipp": "completed"}])
    blocked = [kw["msg"] for j, kw in job_history if kw.get("state") == "blocked"]
    assert blocked == ["Error: The printer cover is open. Your job remains queued and will resume after the problem is resolved"]


def test_lpstat_failure_is_transient_not_left_the_queue(m, watch):
    # an empty listing from a FAILED lpstat must not be read as "job left the queue"
    v, states, took, cups = watch([{"rc": 1}, {"rc": 1}, {"queue": ["M437-29"]}, {"queue": [], "ipp": "completed"}])
    assert v["state"] == "done" and took == 16
    assert len(cups.ran("ipptool")) == 1                    # outcome only asked once it really left


def test_lpstat_failing_three_times_is_unknown(m, watch):
    v, states, took, cups = watch([{"rc": 1}])
    assert v["state"] == "unknown" and v["msg"] == "Cannot track job M437-29. Check the printer for output"
    assert took == 12 and cups.ran("ipptool") == [] and cups.ran_completed_list() == []


def test_waiting_behind_other_jobs_is_not_slow(m, watch):
    waiting = {"queue": ["M437-28", "M437-29"]}
    v, states, took, cups = watch([waiting] * 40 + [{"queue": [], "ipp": "completed"}], sheets=1)
    assert v["state"] == "done" and took == 164             # longer than the 124 s budget, never "slow"


def test_first_in_queue_still_times_out(m, watch):
    v, states, took, cups = watch([{"queue": ["M437-29", "M437-30"]}], sheets=1)
    assert v["state"] == "slow" and took == 124


def test_indented_lines_never_count_as_our_job(m, watch):
    # our job left the queue; another job's indented status line happens to start with our id
    v, states, took, cups = watch([{"queue": ["M437-30"], "blocks": {"M437-30": "\tM437-29 finished"},
                                    "ipp": "completed"}])
    assert v["state"] == "done" and took == 4


def test_job_ids_and_job_block_parsing(m):
    text = ("M437-28  office  1024  Tue\n\tStatus: Filter failed\n\tqueued for M437\n"
            "M437-29  office  2048  Tue\n\tStatus: Sending data to printer.\n\tAlerts: job-printing\n"
            "M437-290 office  10    Tue\n    M437-2 is not a job line\n")
    assert m._job_ids(text) == {"M437-28", "M437-29", "M437-290"}
    assert m._job_block(text, "M437-29") == "\tStatus: Sending data to printer.\n\tAlerts: job-printing"
    assert m._job_block(text, "M437-28") == "\tStatus: Filter failed\n\tqueued for M437"
    assert m._job_block(text, "M437-2") == "" and m._job_block("", "M437-29") == ""
    assert m._job_block(None, "M437-29") == ""


# ---------------------------------------------------------------- _cups_job_state

@pytest.mark.parametrize("state", ["completed", "canceled", "aborted"])
def test_cups_job_state_parses_ipptool(m, monkeypatch, state):
    seen = []

    def hook(cmd, real_run, *a, **kw):
        seen.append(cmd)
        return sup.completed(ipptool_output(state))
    monkeypatch.setattr(m, "subprocess", sup.SubprocessProxy(hook))
    assert m._cups_job_state("M437-1234") == state
    assert seen == [["ipptool", "-tv", "ipp://localhost/jobs/1234", IPP_TEST]]


@pytest.mark.parametrize("cups_id", ["", None, "M437-", "M437-abc", "garbage"])
def test_cups_job_state_rejects_bad_ids_without_calling(m, monkeypatch, cups_id):
    def hook(cmd, real_run, *a, **kw):
        raise AssertionError("ipptool must not be called for %r" % (cups_id,))
    monkeypatch.setattr(m, "subprocess", sup.SubprocessProxy(hook))
    assert m._cups_job_state(cups_id) == ""


@pytest.mark.parametrize("result", [FileNotFoundError(2, "no ipptool"), sup.completed(ipptool_output(None)),
                                    sup.completed("", "ipptool: Unable to connect", 1)])
def test_cups_job_state_unknown_is_empty(m, monkeypatch, result):
    def hook(cmd, real_run, *a, **kw):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(m, "subprocess", sup.SubprocessProxy(hook))
    assert m._cups_job_state("M437-29") == ""


def test_ipptool_is_guarded_in_the_sandbox(m):
    # the real ipptool must never be reached from the suite: the PATH stub exits 99 → '' (fallback)
    assert m._cups_job_state("M437-29") == ""


# ---------------------------------------------------------------- /cancel

@pytest.fixture()
def print_job(m):
    pj = m.new_job("print", cups="M437-999")
    m.job_update(pj, state="running", msg="Sent to printer（x），正在打印…")
    return pj


def test_cancel_running_job(m, client, fake_cups, print_job):
    r = client.post("/cancel", data={"job": print_job})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is True and j["state"] == "cancelled"
    assert fake_cups.cancel_calls() == [["M437-999"]]
    v = m.job_view(print_job, "print")
    assert v["state"] == "cancelled" and v["msg"].startswith("Cancelled") and j["msg"] == v["msg"]
    assert "may still emerge" in v["msg"]                       # raw queue: honest about pages in flight


@pytest.mark.parametrize("job", ["", "nope", "0123456789abcdef"])
def test_cancel_unknown_job_404(m, client, fake_cups, job):
    r = client.post("/cancel", data={"job": job})
    assert r.status_code == 404 and r.is_json and fake_cups.cancel_calls() == []


def test_cancel_scan_job_or_untracked_print_404(m, client, fake_cups):
    sj = m.new_job("scan")
    untracked = m.new_job("print", cups="")
    for j in (sj, untracked):
        assert client.post("/cancel", data={"job": j}).status_code == 404
    assert fake_cups.cancel_calls() == []


@pytest.mark.parametrize("state", ["done", "error", "cancelled"])
def test_cancel_finished_job_409(m, client, fake_cups, print_job, state):
    m.job_update(print_job, state=state)
    r = client.post("/cancel", data={"job": print_job})
    assert r.status_code == 409 and r.is_json and fake_cups.cancel_calls() == []
    assert m.job_view(print_job, "print")["state"] == state


def test_cancel_failure_502(m, client, fake_cups, print_job):
    fake_cups.env(FAKE_CANCEL_FAIL="1")
    r = client.post("/cancel", data={"job": print_job})
    assert r.status_code == 502 and r.get_json()["error"] == "Cancellation failed: This job has already been sent and cannot be cancelled"
    assert m.job_view(print_job, "print")["state"] == "running"


@pytest.mark.parametrize("ipp,completed", [
    ("canceled", ["M437-29"]),                 # what CUPS really reports after a cancel
    ("completed", ["M437-29"]),                # even a (racy) "completed" must not overwrite
    (MISSING, ["M437-29"]),                    # lpstat fallback lists canceled jobs as completed
    (MISSING, []),                             # … or not at all
])
def test_cancel_then_watcher_poll_stays_cancelled(m, client, fake_cups, watch, ipp, completed):
    pj = m.new_job("print", cups="M437-29")
    m.job_update(pj, state="running", msg="Sent to printer（x），正在打印…")
    assert client.post("/cancel", data={"job": pj}).status_code == 200
    msg = m.job_view(pj, "print")["msg"]
    v, states, took, cups = watch([{"queue": [], "ipp": ipp, "completed": completed}], pj=pj)
    assert v["state"] == "cancelled" and v["msg"] == msg     # the user's cancel is final
    assert cups.calls == []                                  # watcher stops before polling CUPS
    assert took == 4


@pytest.mark.parametrize("ipp", ["completed", "aborted", MISSING])
def test_cancel_racing_the_outcome_check_stays_cancelled(m, watch, ipp):
    pj = m.new_job("print", cups="M437-29")
    m.job_update(pj, state="running", msg="Sent to printer（x），正在打印…")

    def user_cancels():                                      # /cancel lands while the watcher asks CUPS
        m.job_update(pj, state="cancelled", msg="Cancelled：…")
    v, states, took, cups = watch([{"queue": [], "ipp": ipp, "completed": ["M437-29"], "on_ipp": user_cancels}],
                                  pj=pj)
    assert v == {"state": "cancelled", "msg": "Cancelled：…", "file": ""}
    assert states[-1] == "cancelled"
